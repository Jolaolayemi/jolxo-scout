# Jolxo Scout 🔎

**Telegram bot ([@jolxoscoutbot](https://t.me/jolxoscoutbot)) that surfaces "survivor" memecoins on Solana and Base, with entry zones and risk scores.**

> ⚠️ **NFA, memecoins can go to zero.** This is a research tool, not financial advice.

## What it does

- **Finds survivors.** Every 4 hours it scans hundreds of coins from Dexscreener and GeckoTerminal and keeps the ones that have taken several -70% dumps and bounced back, spent a long time under a $10M market cap, have a real community and a healthy holder spread.
- **Checks safety.** RugCheck (Solana) and GoPlus / honeypot.is (Base) flag mint or freeze authority, honeypots, taxes, insider wallet clusters and unlocked liquidity. Dangerous coins are dropped outright.
- **Gives a plan per coin.** Each pick gets an entry zone, an invalidation level, a 1–5 risk score, plus chart and BubbleMaps links.
- **Watches the shortlist.** Every 30 minutes it pings you when a coin moves into its entry zone or breaks invalidation (at most once per coin per 24h).
- **Answers commands:** `/scan`, `/top`, `/coin <CA or ticker>`, `/watchlist`, `/help`.

See [`sample_report.txt`](sample_report.txt) for what a scan report looks like.

## Setup: environment variables

All keys and settings come from environment variables; nothing secret lives in the code.

You need Python 3.10+ (3.12 recommended) and two values:

| Env var | What it is | Where to get it |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot's token (`123456789:AA...`) | [@BotFather](https://t.me/BotFather) → `/newbot` or `/token` |
| `ALLOWED_CHAT_IDS` | Chat id(s) the bot answers and posts to, comma-separated. Everyone else is ignored. | DM [@userinfobot](https://t.me/userinfobot) for yours. Group ids are negative. |

```bash
git clone https://github.com/Jolaolayemi/jolxo-scout.git && cd jolxo-scout
pip install -r requirements.txt
cp .env.example .env            # fill in TELEGRAM_BOT_TOKEN and ALLOWED_CHAT_IDS
export $(grep -v '^#' .env | xargs)
python bot.py
```

Never commit `.env` or your token (`.gitignore` already keeps it out). Every other setting is optional and listed in [`.env.example`](.env.example).

**Try it with no Telegram traffic at all:** `DRY_RUN=1 python bot.py --cmd /scan --cmd /top` prints every message instead of sending it.

## Deploy 24/7

The bot is one small always-on process, so it needs a host. Railway costs about $5/month and step-by-step instructions are in **[DEPLOY.md](DEPLOY.md)**.

## Repo layout

| File | What it is |
|---|---|
| `bot.py` | The Telegram bot: commands, scheduler, alerts, sqlite state |
| `scanner.py` | The scan engine. Also runs alone: `python scanner.py` |
| `test_alerts_dryrun.py` | Forces an entry and an invalidation alert in dry-run mode to test dedupe |
| `Dockerfile`, `railway.json`, `.dockerignore` | Container + Railway config |
| `.env.example` | Every env var, with placeholders |
| `DEPLOY.md` | Railway deploy guide |
| `sample_report.txt` | An example scan report |
| `docs/HOW_IT_WORKS.md` | The full method: data sources, how each signal is measured, scoring, bot internals |

## How it works, in short

`scanner.py` pulls candidates from Dexscreener and GeckoTerminal, runs safety checks (RugCheck, GoPlus, honeypot.is), measures drawdown-and-recovery history, distribution and community signals, then ranks the survivors and gives each one a rule-based entry zone, invalidation level and risk score. `bot.py` runs it on a schedule, keeps a watchlist in sqlite and alerts on zone entries and invalidations. Every source used works on its free tier. The full method is in [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## Credits

Designed and built by **Olayemi** ([@Jolaolayemi](https://github.com/Jolaolayemi)): product design, scoring rules and bot experience.
Data: Dexscreener, GeckoTerminal / CoinGecko, RugCheck, GoPlus, honeypot.is; BubbleMaps links for manual checks.

## Not financial advice

Jolxo Scout is a personal research tool. Its picks, entry zones and risk scores are rule-based outputs, not recommendations to buy or sell anything. Do your own research and never put in more than you can afford to lose.

**NFA, memecoins can go to zero.**
