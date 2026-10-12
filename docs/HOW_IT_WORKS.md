# Jolxo Scout: how it works

> **NFA, memecoins can go to zero.** Nothing here is financial advice.

## The scanner engine (scanner.py)

Run: `pip install requests && python scanner.py`
Outputs: `out/latest.json` (every number per coin) and `out/telegram.txt` (ready-to-send messages, split under Telegram's 4096-char cap).
Knobs (env vars): CHAINS=solana,base  MIN_AGE_DAYS=30  MIN_MCAP=150000  MAX_MCAP=30000000  MIN_LIQ=25000  MIN_VOL24=10000  MAX_DEEP=30  TOP_N=8  COINGECKO_DEMO_KEY=(optional free CoinGecko key; CG_DEMO_KEY also accepted)  DATA_DIR=(where cache/out/snapshots go; defaults to this folder)


## APIs, verified by calling them on 6 Oct 2026

| Source | What we use | Works free, no key? | Limits / cost |
|---|---|---|---|
| Dexscreener | token profiles, community takeovers (CTO), boosts, ads, trending metas (+ their pairs), /tokens/v1 batch (30 per call), /token-pairs/v1 | YES | 60 req/min (profiles, CTO, boosts, ads, metas); 300 req/min (pairs, tokens, search). No OHLCV/candle endpoint in the public API. |
| GeckoTerminal | daily OHLCV per pool (price history), token info (holder count, top-10 %, mint/freeze, dev holding %), top/trending pools | YES | Docs say ~10 calls/min keyless; measured ~6/min before 429s. ~180 daily candles per call, paginate with before_timestamp. A free CoinGecko Demo key (signup) gives 30/min via api.coingecko.com/api/v3/onchain (keyless there = 401). |
| RugCheck (Solana) | full report: mint/freeze authority, top holders (pools/lockers removed), insider flags, insider networks (wallet clusters), LP locked %, risks, creator balance | YES | Header says 15 (X-Rate-Limit-Limit); a key (sign in with a Solana wallet) raises it. |
| GoPlus (Base/EVM + Solana) | honeypot, buy/sell tax, mintable, hidden owner, owner-can-change-balance, blacklist, top holders, LP holders + locked | YES | Free plan: 150 CU/min, 30k CU/day; $199/mo for more. |
| honeypot.is (Base) | buy/sell simulation, real taxes | YES | 50 per window (x-ratelimit header). |
| BubbleMaps | map link per coin: https://v2.bubblemaps.io/map?address=ADDR&chain=solana|base | Link: YES (free to open). Data API: NO | Data API (clusters, top holders, scores) needs X-ApiKey from pro.bubblemaps.io: Starter $170/mo, Standard $850/mo. iFrame embed needs a partnerId (demo id only works on localhost). |
| HolderScan | holder breakdowns, holder trends | NO (403 without key) | API needs HolderScan Premium (Standard tier, 200k request units/mo, 300 req/min) or $149 / $349 / $899 per month. Solana + Ethereum only; Base returns "Only Solana/Ethereum chains are supported". |
| Birdeye | price/OHLCV | NO (401 without key) | Free "Standard" tier exists with a key (signup). Not needed: GeckoTerminal covers history. |
| Solscan | holders/transfers | Pro API is paid | Not used. |
| X (Twitter) official API | mentions / posts | NO | Pay-per-use, no free tier. Used instead: api.fxtwitter.com/<handle> (unofficial, free) for follower count only. |
| Telegram public page | member count | YES | Scraped from https://t.me/<channel> ("N members/subscribers"). |

## How each criterion is measured

1. "Multiple -70%": on daily closes from GeckoTerminal, a state machine counts each fall of 70%+ from the running peak that was later followed by a 100%+ bounce off its low (then the peak resets, so events are distinct). Closes, not wicks, because thin pools print single-trade spikes. Also reports whether the coin is in an unrecovered 70% drawdown right now. Limitation: history is the coin's main pool only; if liquidity migrated, early history may be missing.
2. "Long time under $10M": market cap per day = daily close × current circulating supply (current MC / current price). Reports total days under $10M, the longest unbroken run and the share of its life. Assumes supply hasn't changed (true for mint-revoked memecoins).
3. "Culting for free": a cult-like community promoting organically, no paid influencers. Approximated, not measured directly: Dexscreener community-takeover flag (community runs/paid for the listing after the dev left), socials present (X/TG/site), X follower count (unofficial), Telegram member count, holders per $1M of market cap, holder growth per day (needs 2+ scans; stored in snapshots.sqlite), average trade size (small = retail), MINUS paid promotion currently running (Dexscreener boosts, ads). We cannot see who is posting, whether posts are paid, or mention velocity on X without the paid X API. Weak signal; treat as a tiebreaker.
4. Distribution (BubbleMaps & HolderScan): both APIs are paid, so the scanner uses RugCheck on Solana (top-10 % excluding pools/lockers, largest wallet, insider-flagged wallets, insider wallet clusters and % they still hold — the closest free thing to a bubblemap) and GoPlus on Base (top-10 % excluding LP/pool contracts, creator %, LP locked). Every pick carries its BubbleMaps link to eyeball clusters by hand.
5. Dexscreener: candidate source (CTO, profiles, boosts, ads, trending metas) plus liquidity, 24h volume, txns, age, boosts and the chart link.
Early entry: rule-based off the last 45 days of daily candles. Support = 15th-percentile low, range high = 85th-percentile high, prior high = top of the previous ~2 months. Near support: entry support×0.95–1.15. Just broke the prior high (≤35% above): entry = retest of that level ×0.92–1.03. More than 35% above: "extended, wait" for a retest. Invalidation = a daily close 20% below the level the entry depends on.
Risk 1–5: points for mint/freeze authority, honeypot/tax, owner powers, top-10 >30%/45%, largest wallet >8%, insider clusters >3%/10%, dev wallet >3%, LP <50% locked (classic AMM only), liquidity <$75k or <4% of MC, fading volume, thin volume, age <60d, open drawdown. Mint/freeze authority, honeypot, owner-can-change-balance or rugged = excluded outright.
Ranking: recovered drawdowns (capped at 4), days under $10M (capped 240), currently under $10M bonus, 7-day vs prior-4-week volume trend, culting score, top-10 concentration, penalty for open drawdown.

## Hosting 24/7 (checked 6 Oct 2026)

The bot needs an always-on home. It's a tiny Python process (~60 MB RAM), so the smallest plan anywhere is enough.

1. Recommended: Railway Hobby (see [DEPLOY.md](DEPLOY.md)), $5/month (includes $5 of usage, which this bot fits in). Fork this repo, then "New Project → Deploy from GitHub", set env vars (TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS, optional COINGECKO_DEMO_KEY), and add a volume for cache/ and snapshots.sqlite so holder growth survives restarts. The trial is 30 days with $5 credit and no card; after that the free plan is only $1/month of credit, which is not enough for always-on.
2. Cheapest always-on: Fly.io shared-cpu-1x 256 MB, about $2/month pay-as-you-go. You need the flyctl CLI and a card. There's no free allowance for new accounts.
3. A $4–6 VPS (Hetzner from about €5, DigitalOcean/Vultr about $4–6). Most control and the best value, but you manage a Linux server yourself: systemd service plus cron.
Not recommended: Render's free web services sleep when idle (a background worker is $7/month). GitHub Actions cron is free, but scheduled runs have been dropping or stalling (GitHub community threads Aug–Sep 2026), and it can't answer /commands.

What you need: an account on the chosen host and a card that can pay in USD (some Nigerian naira cards get declined by foreign processors, so a dollar/virtual card helps), your bot token, and the chat id where alerts go (your DM, a private channel or a group). Optional: a free CoinGecko Demo API key (signup at coingecko.com/en/api) triples the price-history rate limit.

Cadence: full scan every 4 hours (6 a day). With the cache, a warm scan takes about 3–4 min. A cold one takes about 12 min, because the free price-history API allows only about 6 calls a minute. Also run a light 30-minute check on coins already shortlisted (Dexscreener only: price vs entry zone and invalidation, liquidity pulls), and alert only when a coin enters its entry zone or breaks invalidation. Holder-growth signals need at least two scans 6+ hours apart before they appear.


## The bot (bot.py)

`bot.py` is one long-running process with plain HTTPS long-polling and no framework (only `requests`). It imports `scanner.py`, which still runs on its own.

- **Commands** (only chats in `ALLOWED_CHAT_IDS`, which is required; anyone else is ignored and logged):
  - `/start`, `/help`
  - `/scan`: "Scanning…" notice, then a full scan in a worker thread, then the picks. A second /scan during a scan gets "already running".
  - `/top`: the last report, with its time.
  - `/coin <CA or ticker>`: the card from the last scan plus the live price and zone status. A coin not in the scan gets a fresh full check (about 1 min).
  - `/watchlist`: the shortlist with live price vs entry zone and invalidation.
- **Scheduler:**
  - Full scan every `FULL_SCAN_HOURS` (4), posted to every allowed chat. A failed scan retries in 30 min.
  - Light check every `LIGHT_CHECK_MINUTES` (30) on the watchlist, Dexscreener only, batched. It alerts only on a transition into the entry zone ("entry") or below invalidation ("invalidation"). The same coin and event isn't re-sent within `ALERT_DEDUPE_HOURS` (24); the `alerts` table in sqlite does the dedupe. Coins already in their zone at scan time don't alert, since the report already said so.
- **State on the volume** (`DATA_DIR`, `/data` on Railway): `bot.sqlite` (kv: last scan, last check, Telegram offset; reports, the last 30; watchlist; alerts), plus the scanner's `cache/`, `out/` and `snapshots.sqlite`. Cache files older than 3 days are pruned.
- **Messages:** split under 4000 chars at blank lines or newlines, with web previews off (`link_preview_options.is_disabled`). Every pick, card and alert ends with "NFA, memecoins can go to zero."
- **Robustness:**
  - Every loop and handler is wrapped, so nothing crashes the process.
  - Telegram 429 waits `retry_after`; 5xx and network errors back off. 409 (another poller) waits. A bad token (401) waits 5 min instead of crash-looping.
  - Data APIs: per-host spacing, shared across threads, plus 429 backoff of at least 20 s. GeckoTerminal sends `retry-after: 0`, so that header isn't trusted alone.
  - Logs go to stdout. The bot token is redacted from every log line.
- **Price history:** with `COINGECKO_DEMO_KEY` set it uses `api.coingecko.com/api/v3/onchain` (30/min). The key is sent only to CoinGecko, and any miss falls back to GeckoTerminal.
- **Dry run:** `DRY_RUN=1` prints every message instead of sending it, and `tg()` refuses to run at all in that mode.
  - `DRY_RUN=1 DATA_DIR=./data_dryrun python bot.py --cmd /scan --cmd /top --cmd /watchlist --cmd "/coin RETARDIO" --light-check`
  - `DRY_RUN=1 python test_alerts_dryrun.py ./data_dryrun` forces one entry and one invalidation on a copy of the data, then shows the 24h dedupe.

---

**NFA, memecoins can go to zero.**
