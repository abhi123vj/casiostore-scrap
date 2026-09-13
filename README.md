# Casio price tracker

Tracks watch prices on [casiostore.bhawar.com](https://casiostore.bhawar.com) and posts to Slack when a new offer appears.

Each run logs in, reads every watch in the **G-Shock** and **Edifice** collections, saves the prices to MongoDB and compares them with the previous run. On GitHub Actions it runs every 15 minutes.

## What it does

- **Scrapes** every page of each collection with [invisible_playwright](https://github.com/feder-cr/invisible_playwright), a patched Firefox that hides browser fingerprints. The browser runs hidden, pauses randomly (never more than 3 seconds) and scrolls like a person.
- **Saves** each watch to MongoDB: name, model, price, MRP, whether it's on offer, discount percentage, image URL and product URL.
- **Logs** price drops, and discounts of 50% or more.
- **Posts to Slack** when a watch is newly on offer or its discount went up. The first run only records starting prices, so it sends nothing.
- **Reuses the login session.** Cookies are saved in MongoDB, so the script only logs in when the saved session has expired. Cookies that contain a password are never saved.
- **Retries once** with the second account if anything fails, such as a blocked page, a captcha at login or an error page.
- **Alerts Slack on critical errors**, for example when both attempts fail or MongoDB is unreachable. The run then exits with an error.

## Configuration

Locally, the script reads a `.env` file next to `index.py`. On GitHub Actions, the same names come from repository secrets instead. Never commit `.env`; it's already in `.gitignore`.

| Variable | What it is |
|---|---|
| `CASIO_EMAIL`, `CASIO_PASSWORD` | Store account used on the first attempt |
| `CASIO_EMAIL_2`, `CASIO_PASSWORD_2` | Store account used on the retry. If these are missing, the retry uses the first account with a fresh login |
| `MONGODB_URI` | MongoDB connection string, e.g. `mongodb+srv://user:pass@cluster0.xxxx.mongodb.net` |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`). The bot needs the `chat:write` permission |
| `SLACK_CHANNEL` | Channel ID to post to, e.g. `C0123456789`. Invite the bot to that channel |

Example `.env`:

```
CASIO_EMAIL=you@example.com
CASIO_PASSWORD=...
CASIO_EMAIL_2=backup@example.com
CASIO_PASSWORD_2=...
MONGODB_URI=mongodb+srv://...
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL=C0123456789
```

## Run locally

Requires Python 3.11 or newer on Windows x86_64 or Linux.

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux: source .venv/bin/activate
pip install invisible-playwright==0.14.0 pymongo==4.18.1
python -m invisible_playwright fetch   # one-time browser download, about 250 MB
python index.py
```

On Linux, also install Xvfb, which the hidden browser needs: `sudo apt install xvfb`.

## Run on GitHub Actions

[.github/workflows/price-check.yml](.github/workflows/price-check.yml) is started every 15 minutes by [cron-job.org](https://cron-job.org), which calls GitHub's workflow dispatch API with a fine-grained token (Actions: read and write). GitHub's own schedule only delays or skips short intervals, so the built-in cron runs hourly at :07 (UTC) as a backup. You can also start it by hand from the **Actions** tab with **Run workflow**.

1. Push this project to a GitHub repository.
2. Add all 7 variables above under **Settings → Secrets and variables → Actions → New repository secret**. With the GitHub CLI you can upload them all from `.env` at once: `gh secret set -f .env`.
3. In MongoDB Atlas, open **Network Access** and allow `0.0.0.0/0`. GitHub runners use a different IP address on every run.
4. Start one run by hand from the **Actions** tab and check its logs.

**Free minutes:** each run takes about 4 minutes, which is about 11,500 minutes a month. That's free on a **public** repository, but far more than the 2,000 minutes included for a private repository on the Free plan. For a private repository, run it less often by changing the `cron` line.

A public repository's logs are public too, so the script never logs account emails. Secrets are always hidden.

## Data in MongoDB

Database: `casio_price_info`

**`watches`** has one document per watch, rewritten only when one of its fields changes. `_id` is the product's URL handle.

| Field | Example |
|---|---|
| `name` | `CASIO EDIFICE EFB-730D-2AVUDF BLUE ANALOG DIAL ...` |
| `model` | `GA-V01A-8A` |
| `price` | `8047` |
| `min_price` | `7499` (lowest price seen) |
| `max_price` | `11495` (highest price seen) |
| `mrp` | `11495` |
| `is_offer_price` | `true` |
| `discount_percentage` | `30.0` |
| `image_url` | `https://casiostore.bhawar.com/cdn/shop/files/...` |
| `url` | `https://casiostore.bhawar.com/products/...` |
| `collection` | `g-shock` or `edifice-watches` |
| `updated_at` | last time any field changed (UTC) |

Only the latest, lowest and highest prices are kept, not a full price history.

**`sessions`** has one document per store account, with `_id` `casiostore:<email>`. It holds the saved login cookies and `saved_at`. These cookies work like a login to the account, so limit who can access the database.

## Settings in the code

These are constants at the top of [index.py](index.py):

| Constant | Default | Meaning |
|---|---|---|
| `COLLECTIONS` | `["g-shock", "edifice-watches"]` | Store collections to scrape |
| `ALERT_DISCOUNT` | `50` | Discount percentage that gets logged as an alert and marked 🔥 in Slack |
| `MAX_DELAY` | `3` | Longest random pause, in seconds |

## Troubleshooting

- **The browser exits at startup on Windows** with exit code `3236495362`. Windows Smart App Control is blocking the unsigned patched Firefox. Turn it off under **Windows Security → App & browser control**, or run the script on Linux.
- **`Login failed, still on .../account/login`** means the store showed a captcha or rejected the password. This is more likely from GitHub's data-centre IPs than from a home connection.
- **`No watches on ... page 1`** means the page loaded without any products. Either the store blocked the request or its page layout changed and the selectors in `CARDS_JS` need updating.
- **`Slack error: not_in_channel`** means the bot isn't in the channel. Invite it there.
