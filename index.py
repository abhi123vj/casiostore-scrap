import json
import logging
import os
import random
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
from invisible_playwright import InvisiblePlaywright
from pymongo import MongoClient, ReplaceOne

env_file = Path(__file__).with_name(".env")  # absent on GitHub Actions, where secrets arrive as env vars
for line in env_file.read_text().splitlines() if env_file.exists() else []:
    key, sep, value = line.partition("=")
    if sep and not key.strip().startswith("#"):
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("casio")
log.setLevel(logging.INFO)  # our logs at INFO; libraries stay at WARNING

COLLECTIONS = ["g-shock", "edifice-watches"]
ALERT_DISCOUNT = 50
MAX_DELAY = 3  # seconds; no random pause anywhere goes above this
COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")

# Reads each card on the current page. MRP and price come back as numbers.
CARDS_JS = """() => [...document.querySelectorAll('.product-card-wrapper')].map(c => {
  const num = sel => Number((c.querySelector(sel)?.innerText || '').replace(/[^0-9.]/g, '')) || 0;
  const img = c.querySelector('.card__media img');
  return {
    handle: (c.querySelector('.card__heading a, .card__media-link')?.getAttribute('href') || '').split('/products/')[1],
    model: c.querySelector('.card__media-link')?.getAttribute('aria-label') || c.querySelector('.card__heading')?.innerText.trim(),
    name: img?.getAttribute('alt')?.trim(),
    image: img?.getAttribute('src'),
    mrp: num('.price__sale s.price-item--regular') || num('.price__regular .price-item--regular'),
    price: num('.price-item--sale') || num('.price__regular .price-item--regular'),
  };
})"""


def pause(page, low=1.0, high=3.0):
    page.wait_for_timeout(random.uniform(low, min(high, MAX_DELAY)) * 1000)


def browse(page):
    # scroll a little like a person reading the grid; call only after reading cards (scrolling loads later pages)
    for _ in range(random.randint(1, 3)):
        page.mouse.wheel(0, random.randint(300, 900))
        pause(page, 0.5, 2)


def offer_blocks(offers):
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"⌚ {len(offers)} new Casio offer{'s' * (len(offers) > 1)}"}}]
    for o in offers[:20]:  # ponytail: Slack caps a message at 50 blocks; split into several messages if 20+ offers ever shows up
        hot = "  :fire:" if o["discount_percentage"] >= ALERT_DISCOUNT else ""
        name = f"\n{o['name']}" if o["name"] and o["name"] != o["model"] else ""
        blocks += [
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*<{o['url']}|{o['model']}>*{hot}\n*₹{o['price']:,.0f}*   ~₹{o['mrp']:,.0f}~   `{o['discount_percentage']:.0f}% OFF`{name}"},
                "accessory": {"type": "image", "image_url": o["image_url"], "alt_text": o["model"]},
            },
        ]
    if len(offers) > 20:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_+{len(offers) - 20} more offers_"}})
    blocks += [{"type": "divider"}, {"type": "context", "elements": [{"type": "mrkdwn", "text": ":shopping_trolley: <https://casiostore.bhawar.com|casiostore.bhawar.com>"}]}]
    return blocks


def slack(text, blocks=None):
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        # text is the notification preview; unfurls off so links don't add big store previews under the message
        data=json.dumps({"channel": os.environ["SLACK_CHANNEL"], "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False}).encode(),
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}", "Content-Type": "application/json; charset=utf-8"},
    )
    result = json.load(urllib.request.urlopen(req))
    if result["ok"]:
        log.info("Slack message sent to %s", os.environ["SLACK_CHANNEL"])
    else:
        log.error("Slack error: %s", result["error"])


def alert_failure(error):
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🚨 Casio price check failed"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{type(error).__name__}: {error}```"[:2900]}},
    ]
    if "GITHUB_RUN_ID" in os.environ:
        run_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f":scroll: <{run_url}|View run logs>"}]})
    try:
        slack(f"🚨 Casio price check failed: {type(error).__name__}", blocks)
    except Exception:  # the alert must never hide the original error
        log.exception("Could not send the failure alert to Slack")


def scrape(page, collection):
    watches, n = [], 1
    while True:
        url = f"https://casiostore.bhawar.com/collections/{collection}?page={n}"
        started = time.monotonic()
        response = page.goto(url)
        page.wait_for_load_state()  # wait for this page before moving to the next
        status = response.status if response else "?"
        if response and response.status >= 400:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        pause(page, 0.5, 2)
        cards = page.evaluate(CARDS_JS)  # read before scrolling: the theme's infinite scroll appends later pages
        log.info("%s page %d: HTTP %s, %d watches, loaded in %.1fs", collection, n, status, len(cards), time.monotonic() - started)
        if not cards:
            if n == 1:  # a real collection always has watches on page 1; an empty one means blocked or the layout changed
                raise RuntimeError(f"No watches on {url} (title: {page.title()!r}) - blocked or page layout changed?")
            log.info("%s: done, %d watches over %d pages", collection, len(watches), n - 1)
            return watches
        for c in cards:
            c["collection"] = collection
        watches += cards
        browse(page)
        pause(page)
        n += 1


def login(page, email, password):
    log.info("Opening login page")
    page.goto("https://casiostore.bhawar.com/account/login")
    pause(page)
    log.info("Typing email and password")
    page.locator("#CustomerEmail").press_sequentially(email, delay=random.randint(60, 180))
    pause(page)
    page.locator("#CustomerPassword").press_sequentially(password, delay=random.randint(60, 180))
    pause(page)
    log.info("Submitting login form")
    page.click("#customer_login button")  # plain <button>, no type attr, so [type=submit] never matched
    page.wait_for_load_state()
    pause(page)
    if "/account/login" in page.url:
        raise RuntimeError(f"Login failed, still on {page.url} (captcha or wrong password?)")
    log.info("Logged in, now on %s", page.url)


def collect(mongo, account_no, email, password, use_saved_session, passwords):
    session_id = f"casiostore:{email}"  # one saved session per account; emails stay in Mongo, never in the (public) logs
    log.info("Starting hidden browser")
    with InvisiblePlaywright(headless=True) as browser:
        page = browser.new_page()
        session = mongo.sessions.find_one({"_id": session_id}) if use_saved_session else None
        if session:
            page.context.add_cookies(session["cookies"])
            log.info("Loaded saved session for account %d: %d cookies from %s", account_no, len(session["cookies"]), session["saved_at"])
        else:
            log.info("No saved session for account %d" if use_saved_session else "Not using a saved session for account %d", account_no)

        page.goto("https://casiostore.bhawar.com/account")  # Shopify redirects to /account/login when the session is missing or expired
        page.wait_for_load_state()
        pause(page)
        if "/account/login" in page.url:
            log.info("Not logged in, logging in with account %d", account_no)
            login(page, email, password)
        else:
            log.info("Saved session still valid, skipping login")

        watches = []
        for collection in COLLECTIONS:
            pause(page)
            watches += scrape(page, collection)

        # Save refreshed cookies for the next run. Skip any holding a password (Shopify's login_form_backup cookie does).
        cookies = [
            {k: v for k, v in c.items() if k in COOKIE_KEYS}
            for c in page.context.cookies()
            if "login_form_backup" not in c.get("name", "") and not any(p in unquote(c.get("value", "")) for p in passwords)
        ]
        mongo.sessions.replace_one({"_id": session_id}, {"cookies": cookies, "saved_at": datetime.now(timezone.utc)}, upsert=True)
        log.info("Saved session for account %d: %d cookies", account_no, len(cookies))
        return watches


def main():
    run_started = time.monotonic()
    log.info("Run started")
    accounts = [(os.environ["CASIO_EMAIL"], os.environ["CASIO_PASSWORD"])]
    if os.environ.get("CASIO_EMAIL_2") and os.environ.get("CASIO_PASSWORD_2"):
        accounts.append((os.environ["CASIO_EMAIL_2"], os.environ["CASIO_PASSWORD_2"]))
    else:
        log.warning("CASIO_EMAIL_2 / CASIO_PASSWORD_2 not set, the retry will use account 1 again")
    passwords = [p for _, p in accounts]
    mongo = MongoClient(os.environ["MONGODB_URI"]).casio_price_info

    watches, errors = [], []
    for attempt in (1, 2):
        account_no = min(attempt, len(accounts))  # attempt 1 -> account 1, retry -> account 2 (or account 1 if there is no second)
        email, password = accounts[account_no - 1]
        # a different account can use its own saved session; the same account retries with a fresh login
        use_saved_session = attempt == 1 or len(accounts) == 2
        try:
            log.info("Attempt %d of 2 with account %d", attempt, account_no)
            watches = collect(mongo, account_no, email, password, use_saved_session, passwords)
            break
        except Exception as e:
            log.exception("Attempt %d with account %d failed", attempt, account_no)
            errors.append(f"Attempt {attempt} (account {account_no}): {type(e).__name__}: {e}")
            if attempt == 2:
                raise RuntimeError("Both attempts failed\n" + "\n".join(errors)) from e
            log.warning("Retrying once with account %d and a fresh browser", min(2, len(accounts)))
            time.sleep(random.uniform(1, MAX_DELAY))

    db = mongo.watches
    previous = {d["_id"]: d for d in db.find({}, {"price": 1, "discount_percentage": 1})}
    log.info("Loaded %d watches from the last run", len(previous))
    now = datetime.now(timezone.utc)
    writes, drops, alerts, new_offers = [], 0, 0, []

    for w in watches:
        discount = round((w["mrp"] - w["price"]) / w["mrp"] * 100, 1) if w["mrp"] > w["price"] else 0
        doc = {
            "name": w["name"],
            "model": w["model"],
            "price": w["price"],
            "mrp": w["mrp"],
            "is_offer_price": discount > 0,
            "discount_percentage": discount,
            "image_url": "https:" + w["image"] if w["image"].startswith("//") else w["image"],
            "url": f"https://casiostore.bhawar.com/products/{w['handle']}",
            "collection": w["collection"],
            "updated_at": now,
        }
        old = previous.get(w["handle"])
        if old and w["price"] < old["price"]:
            drops += 1
            log.warning("PRICE DROP %s: Rs %s -> Rs %s %s", w["model"], f"{old['price']:,.0f}", f"{w['price']:,.0f}", doc["url"])
        if discount >= ALERT_DISCOUNT:
            alerts += 1
            log.warning("ALERT %.0f%% OFF %s: Rs %s (MRP Rs %s) %s", discount, w["model"], f"{w['price']:,.0f}", f"{w['mrp']:,.0f}", doc["url"])
        if discount > (old or {}).get("discount_percentage", 0):  # newly on offer, or a bigger discount than last run
            new_offers.append(doc)
        writes.append(ReplaceOne({"_id": w["handle"]}, doc, upsert=True))

    if writes:
        result = db.bulk_write(writes)
        log.info("Saved to MongoDB: %d updated, %d new", result.modified_count, result.upserted_count)
    offers = sum(w["mrp"] > w["price"] for w in watches)
    log.info("%d watches, %d on offer, %d price drops, %d alerts (>= %d%% off), %d new offers", len(watches), offers, drops, alerts, ALERT_DISCOUNT, len(new_offers))

    if not previous:  # first run only records the baseline, so existing offers don't all fire at once
        log.info("First run: prices recorded as the baseline, no Slack message")
    elif new_offers:
        summary = ", ".join(f"{o['model']} {o['discount_percentage']:.0f}% off" for o in new_offers)
        slack(f"⌚ {len(new_offers)} new Casio offer(s): {summary}", offer_blocks(new_offers))
    else:
        log.info("No new offers, no Slack message")

    log.info("Run finished in %.0fs", time.monotonic() - run_started)


try:
    main()
except Exception as error:
    log.error("Run failed: %s", error)
    alert_failure(error)
    raise
