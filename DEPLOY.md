# Deploying Jolxo Scout (@jolxoscoutbot) to Railway

One always-on service, one 1 GB volume at `/data`, no public port. The bot long-polls Telegram, so it needs no domain or webhook.

## What it costs
- **Hobby plan: $5/month**, which includes $5 of usage credit each month.
- This bot is one small Python process (~60–100 MB RAM, near-zero CPU between scans, a few MB on the volume). Railway's rates are $10/GB RAM/month, $20/vCPU/month and $0.15/GB/month for volume storage, so expected usage is roughly $1–3/month. That sits inside the included $5, so **expect $5/month flat**.
- Builds are free. Set a usage limit in Railway (Workspace → Usage) if you want a hard cap.

## You need
- A Railway account on the Hobby plan, and an **account token** (Account Settings → Tokens). Use it as `RAILWAY_API_TOKEN`; that's the account-scoped kind `railway init` needs. A project token (`RAILWAY_TOKEN`) can't create projects, and the CLI errors if both are set.
- The Telegram bot token (`TELEGRAM_BOT_TOKEN`) from @BotFather.
- Your Telegram chat id (`ALLOWED_CHAT_IDS`); DM @userinfobot to get it.
- Optional: a free CoinGecko Demo key (`COINGECKO_DEMO_KEY`).

## Exact steps (Railway CLI)
Run from the repo root. (Or skip the CLI: Railway → New Project → Deploy from GitHub repo → pick this repo, add the volume and the variables in the dashboard.)

```bash
# 0. CLI + auth (account token, non-interactive)
npm i -g @railway/cli                      # or: bash <(curl -fsSL cli.new)
unset RAILWAY_TOKEN
export RAILWAY_API_TOKEN=<account token>
railway whoami                             # should print the account email

# 1. project + empty service, linked to this folder
railway init --name jolxo-scout
railway add --service jolxo-scout
railway service jolxo-scout                # link the service

# 2. persistent volume for cache + sqlite (watchlist, alert dedupe, last report)
railway volume add --mount-path /data

# 3. env vars (--stdin keeps the bot token out of shell history)
printf '%s' "$TELEGRAM_BOT_TOKEN" | railway variable set TELEGRAM_BOT_TOKEN --stdin --skip-deploys
railway variable set ALLOWED_CHAT_IDS=<your_chat_id> DATA_DIR=/data --skip-deploys
# optional:
# printf '%s' "$COINGECKO_DEMO_KEY" | railway variable set COINGECKO_DEMO_KEY --stdin --skip-deploys

# 4. deploy this folder (Dockerfile + railway.json are picked up; .gitignore keeps local data out)
railway up . --path-as-root --ci

# 5. watch it come up
railway service logs
```

## A healthy start looks like this in the logs
```
Jolxo Scout starting · DATA_DIR=/data · DRY_RUN=False · allowed chats=[123456789] · price history via GeckoTerminal (no key)
scheduler on: full scan every 4h, light check every 30 min
connected as @jolxoscoutbot
full scan start: scheduled
[scan] seeds ...
```
On the very first boot it runs a full scan right away (the volume has no scan yet) and posts the picks to every chat in `ALLOWED_CHAT_IDS`. The first scan without a CoinGecko key takes ~20–30 min. Later ones are faster because daily candles are cached on the volume.

## After deploy
- In Telegram: `/start`, `/help`, `/watchlist` should answer within seconds. `/top` answers once the first scan is done.
- Change settings: `railway variable set FULL_SCAN_HOURS=6` (redeploys automatically).
- Restart: `railway service restart`. Redeploy new code: `railway up . --path-as-root --ci`.
- Add another chat (e.g. a group): put its id in `ALLOWED_CHAT_IDS` (comma-separated). Group ids are negative.

## Gotchas
- **Only one copy may poll.** If the same token runs anywhere else (a laptop, a second service), Telegram returns 409 Conflict and the bot logs "another copy of the bot is polling" and waits. Stop the other copy. The bot removes a webhook on startup if one was set.
- Keep `numReplicas` at 1 (railway.json). A volume can only attach to one replica anyway.
- A redeploy can show a short 409 window while the old container stops. It clears by itself.
- No healthcheck is configured on purpose: the bot serves no HTTP. Railway restarts it if the process exits (`restartPolicyType: ALWAYS`), but the loops catch errors and don't exit on their own.

## Local dry run (no Telegram traffic at all)
```bash
pip install -r requirements.txt
DRY_RUN=1 DATA_DIR=./data_dryrun python bot.py --cmd /scan --cmd /top --cmd /watchlist --cmd "/coin RETARDIO" --light-check
DRY_RUN=1 python bot.py        # interactive: scheduler runs, type /commands on stdin
```
`scanner.py` still runs alone too: `python scanner.py` writes `out/latest.json` and `out/telegram.txt` next to it.
