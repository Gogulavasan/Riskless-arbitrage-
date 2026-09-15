# Arbitrage Insights

Live site: **https://gogulavasan.github.io/Riskless-arbitrage-/**

A small FX arbitrage toolkit. It compares the *fair* forward rate of a currency pair (from spot and the two interest rates) with the *market* forward rate, finds the gap, and shows what is left after trading costs and tax.

## Pages

| Page | What it does |
|---|---|
| `index.html` — **Live Monitor** | Pulls live spot rates every minute, computes the fair forward for each watched pair, compares it with your forward quote, takes off costs, and alerts (banner, sound, browser notification) when the gap is worth acting on. |
| `calculator.html` — Arbitrage Calculator | Check any single trade step by step. |
| `invest.html` — Investment Calculator | Capital, pair and country in → gross, after-cost and after-tax profit out, with a country comparison. |

Shared files: `engine.js` (the maths), `theme.css`, `monitor.js`, `monitor_config.json`.

## The live monitor

* **Live spot** comes from a public feed (`api.fxratesapi.com`, fallback `open.er-api.com`).
* **Interest rates** and **forward points** come from `monitor_config.json`. No free feed provides forward quotes, so you enter your broker's forward points there (market forward minus spot, in price units). Until you do, the site runs in *sample* mode and does not send notifications.
* **Threshold**: an alert fires when the after-cost gap exceeds `alert_threshold_bp` (basis points per year on the amount borrowed) and the inputs pass the sanity checks.
* Settings can also be changed per browser on the page; the repo file is what the server-side checker uses.

### Alerts when nobody has the page open (Telegram)

`monitor_check.py` runs on GitHub Actions every 15 minutes (`.github/workflows/monitor.yml`). It uses the same config and engine, writes `signals.json` (which the page reads), and sends a Telegram message when an opportunity appears and `quotes_are_real` is true.

1. In Telegram, talk to **@BotFather** → `/newbot` → copy the token. Send your new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
2. GitHub → repo **Settings → Secrets and variables → Actions → New repository secret**: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Edit `monitor_config.json`: real `forward_points`, `quotes_as_of`, and `"quotes_are_real": true`.
4. Actions tab → "Arbitrage monitor" → **Run workflow** to test.

## Python engine

`fx_arbitrage.py` is the reference implementation (the website's `engine.js` is a port of it, cross-checked). `python fx_arbitrage.py` prints four worked examples; `python monitor_check.py` runs one monitor pass locally.

Illustrative interest rates and placeholder tax rates — verify before use. Not investment advice.
