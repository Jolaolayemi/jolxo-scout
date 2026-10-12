
"""Memecoin early-entry scanner (phase 1 engine, no Telegram delivery).

Free data only: Dexscreener (candidates, liquidity, volume, boosts, CTO), GeckoTerminal
(daily OHLCV, holder count + top-10 share), RugCheck (Solana: authorities, top holders,
insider clusters, LP lock), GoPlus + honeypot.is (Base: honeypot/tax/owner/LP lock/holders),
fxtwitter + t.me public pages (follower/member counts, unofficial).
Run:  python scanner.py            -> writes out/latest.json and out/telegram.txt
"""
import json, math, os, re, sqlite3, statistics, sys, time
from datetime import datetime, timezone
import requests

CHAINS = [c.strip() for c in os.environ.get("CHAINS", "solana,base").split(",")]
MIN_AGE_DAYS = float(os.environ.get("MIN_AGE_DAYS", 30))
MIN_MCAP = float(os.environ.get("MIN_MCAP", 150_000))
MAX_MCAP = float(os.environ.get("MAX_MCAP", 30_000_000))
MIN_LIQ = float(os.environ.get("MIN_LIQ", 25_000))
MIN_VOL24 = float(os.environ.get("MIN_VOL24", 10_000))
MAX_DEEP = int(os.environ.get("MAX_DEEP", 30))      # tokens that get the slow, rate-limited checks
TOP_N = int(os.environ.get("TOP_N", 8))
CAP_LINE = 10_000_000
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("DATA_DIR") or HERE   # standalone: next to this file; bot: the Railway volume
os.makedirs(BASE, exist_ok=True)
OUT = os.path.join(BASE, "out"); os.makedirs(OUT, exist_ok=True)
DB = os.path.join(BASE, "snapshots.sqlite")
NFA = "Not financial advice. Memecoins can go to zero."

S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0 (memebot scanner)"
_last = {}
import threading
_slot_lock = threading.Lock()   # the bot may run a scan and a /coin lookup at once; spacing stays per host
SPACING = {"api.geckoterminal.com": 7.0, "api.coingecko.com": 2.2, "api.rugcheck.xyz": 1.2, "api.gopluslabs.io": 2.1,
           "api.dexscreener.com": 0.25, "api.honeypot.is": 1.3, "api.fxtwitter.com": 1.0, "t.me": 0.6}

def log(*a): print(*a, file=sys.stderr, flush=True)

def get(url, tries=4, headers=None):
    host = url.split("/")[2]
    for i in range(tries):
        with _slot_lock:
            now = time.time(); slot = max(now, _last.get(host, 0) + SPACING.get(host, 0.5)); _last[host] = slot
        if slot > now: time.sleep(slot - now)
        try:
            r = S.get(url, timeout=25, headers=headers)
        except requests.RequestException as e:
            log("net", host, e); time.sleep(3 * (i + 1)); continue
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            wait = min(max(float(ra) if ra and ra.isdigit() else 0, 20 * (i + 1)), 120)   # GeckoTerminal sends retry-after: 0
            log("429", host, "sleep", wait); time.sleep(wait); continue
        if r.status_code >= 500: time.sleep(3 * (i + 1)); continue
        if r.status_code != 200: return None
        try: return r.json() if "json" in r.headers.get("content-type", "") or r.text[:1] in "[{" else r.text
        except ValueError: return r.text
    return None

# optional free CoinGecko Demo key: 30 calls/min instead of GeckoTerminal's ~6-10 (same on-chain data)
CG_KEY = os.environ.get("COINGECKO_DEMO_KEY") or os.environ.get("CG_DEMO_KEY")
CACHE = os.path.join(BASE, "cache"); os.makedirs(CACHE, exist_ok=True)

def gt(path, ttl):
    """GeckoTerminal call with a disk cache (daily candles barely change within hours)."""
    key = os.path.join(CACHE, re.sub(r"[^A-Za-z0-9]+", "_", path)[:180] + ".json")
    if os.path.exists(key) and time.time() - os.path.getmtime(key) < ttl:
        try: return json.load(open(key))
        except ValueError: pass
    d = None
    if CG_KEY:   # key goes only to CoinGecko, never on the shared session
        d = get("https://api.coingecko.com/api/v3/onchain/" + path, headers={"x-cg-demo-api-key": CG_KEY})
        if not (isinstance(d, dict) and d.get("data") is not None): log("coingecko miss, falling back to geckoterminal")
    if not (isinstance(d, dict) and d.get("data") is not None):
        d = get("https://api.geckoterminal.com/api/v2/" + path)
    if isinstance(d, dict) and d.get("data") is not None: json.dump(d, open(key, "w"))
    return d

def f(x, d=0.0):
    try: return float(x)
    except (TypeError, ValueError): return d

# ---------------------------------------------------------------- candidates
def collect_candidates():
    seeds = {}   # (chain, addr) -> set(source tags)
    def add(chain, addr, tag):
        if chain in CHAINS and addr: seeds.setdefault((chain, addr), set()).add(tag)
    for path, tag in [("community-takeovers/latest/v1", "cto"), ("token-profiles/latest/v1", "profile"),
                      ("token-profiles/recent-updates/v1", "profile"), ("token-boosts/latest/v1", "boost"),
                      ("token-boosts/top/v1", "boost"), ("ads/latest/v1", "ad")]:
        for it in get("https://api.dexscreener.com/" + path) or []:
            add(it.get("chainId"), it.get("tokenAddress"), tag)
    metas = get("https://api.dexscreener.com/metas/trending/v1") or []
    for m in metas[:25]:
        d = get("https://api.dexscreener.com/metas/meta/v1/" + m["slug"]) or {}
        for p in d.get("pairs", []):
            add(p.get("chainId"), p["baseToken"]["address"], "meta:" + m["slug"])
    for chain in CHAINS:
        for sort in ("h24_volume_usd_desc", "h24_tx_count_desc"):
            for page in (1, 2):
                d = gt(f"networks/{chain}/pools?sort={sort}&page={page}", 1800) or {}
                for p in d.get("data", []):
                    base = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                    add(chain, base, "gt_top")
        d = gt(f"networks/{chain}/trending_pools?duration=24h", 1800) or {}
        for p in d.get("data", []):
            add(chain, p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1], "gt_trending")
    return seeds

STABLE_OR_MAJOR = {"SOL", "WSOL", "USDC", "USDT", "WETH", "ETH", "CBBTC", "WBTC", "JUP", "JTO", "RAY", "PYTH",
                   "USDS", "EURC", "AERO", "VIRTUAL", "CBETH", "WSTETH", "BONK", "WIF", "POPCAT"}

def best_pairs(seeds):
    by_chain = {}
    for (c, a) in seeds: by_chain.setdefault(c, []).append(a)
    out = {}
    for chain, addrs in by_chain.items():
        for i in range(0, len(addrs), 30):
            for p in get(f"https://api.dexscreener.com/tokens/v1/{chain}/" + ",".join(addrs[i:i + 30])) or []:
                a = p["baseToken"]["address"]
                key = (chain, a) if (chain, a) in seeds else (chain, a.lower()) if (chain, a.lower()) in seeds else None
                if not key: continue
                if key not in out or f(p.get("liquidity", {}).get("usd")) > f(out[key].get("liquidity", {}).get("usd")):
                    out[key] = p
    return out

def prefilter(pairs, seeds):
    now = time.time() * 1000; keep = []
    for key, p in pairs.items():
        sym = (p["baseToken"].get("symbol") or "").upper()
        mcap = f(p.get("marketCap") or p.get("fdv")); liq = f(p.get("liquidity", {}).get("usd"))
        vol = f(p.get("volume", {}).get("h24")); age = (now - f(p.get("pairCreatedAt"), now)) / 86400000
        if sym in STABLE_OR_MAJOR or "USD" in sym or "EUR" in sym: continue
        if not (MIN_MCAP <= mcap <= MAX_MCAP and liq >= MIN_LIQ and vol >= MIN_VOL24 and age >= MIN_AGE_DAYS): continue
        keep.append(dict(chain=key[0], addr=p["baseToken"]["address"], pair=p, sources=sorted(seeds[key]),
                         mcap=mcap, liq=liq, vol24=vol, age_days=age))
    # cheap pre-rank so the slow checks go to the most promising: organic tags, sane liq/mcap, activity
    def pre(c):
        s = 2 if "cto" in c["sources"] else 0
        s += 1 if any(t.startswith("meta:") for t in c["sources"]) else 0
        s += min(c["liq"] / max(c["mcap"], 1) * 10, 1.5)
        s += min(math.log10(max(c["vol24"], 1)) - 4, 2)
        s += min(c["age_days"] / 90, 1.5)
        s += 1 if c["mcap"] < CAP_LINE else 0
        return s
    keep.sort(key=pre, reverse=True)
    return keep

# ---------------------------------------------------------------- history metrics
def ohlcv(chain, pool, created_ts=0, max_pages=3):
    rows, before = [], None
    for _ in range(max_pages):
        u = f"networks/{chain}/pools/{pool}/ohlcv/day?limit=1000&currency=usd&token=base"
        if before: u += f"&before_timestamp={before}"
        d = gt(u, 12 * 3600)
        lst = (((d or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not lst: break
        rows += lst; before = lst[-1][0]
        if len(lst) < 150 or lst[-1][0] <= created_ts + 86400: break
    rows = sorted({r[0]: r for r in rows}.values())
    return [dict(t=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]) for r in rows if r[4] and r[4] > 0]

def drawdowns(c, dd=0.70, rec=1.0):
    """Count distinct >=70% drawdowns that were followed by a recovery of >=100% off the trough.
    Uses daily CLOSES (single-trade wicks on thin pools would otherwise fake drawdowns).
    After a recovery the peak resets, so each event is counted separately."""
    peak = None; trough = None; events = []; cur = None
    for k in c:
        px = k["c"]
        if peak is None: peak = px; continue
        if trough is None:
            peak = max(peak, px)
            if px <= peak * (1 - dd): trough = px; cur = dict(peak=peak, start=k["t"])
        else:
            if px < trough: trough = px
            if px >= trough * (1 + rec):
                cur.update(trough=trough, depth=1 - trough / cur["peak"], recovered_at=k["t"]); events.append(cur)
                peak, trough, cur = px, None, None
    open_dd = None
    if trough is not None: open_dd = dict(peak=cur["peak"], trough=trough, depth=1 - c[-1]["c"] / cur["peak"])
    max_dd_from_ath = 1 - c[-1]["c"] / max(x["c"] for x in c) if c else 0
    return events, open_dd, max_dd_from_ath

def history_metrics(c, mcap_now, price_now):
    if len(c) < 5: return None
    supply = mcap_now / price_now if price_now else 0
    mc = [k["c"] * supply for k in c]
    under = [m < CAP_LINE for m in mc]
    longest = run = 0
    for u in under: run = run + 1 if u else 0; longest = max(longest, run)
    ev, open_dd, from_ath = drawdowns(c)
    vols = [k["v"] for k in c]
    v7 = statistics.mean(vols[-7:]); v_prev = statistics.mean(vols[-35:-7]) if len(vols) >= 14 else None
    win = c[-45:] if len(c) >= 20 else c
    lows = sorted(k["l"] for k in win); highs = sorted(k["h"] for k in win)
    support = lows[int(len(lows) * 0.15)]; resist = highs[int(len(highs) * 0.85)]
    prior = c[-60:-7] if len(c) >= 30 else c[:-3]
    prior_high = max(k["h"] for k in prior) if prior else resist
    mid20 = statistics.median(k["c"] for k in c[-20:]); low14 = min(k["c"] for k in c[-14:])
    return dict(mid20=mid20, low14=low14, days=len(c), days_under_10m=sum(under), longest_streak_under_10m=longest,
                pct_life_under_10m=sum(under) / len(c), ath_mcap=max(k["c"] for k in c) * supply,
                drawdowns_recovered=len(ev), drawdown_events=ev, open_drawdown=open_dd, off_ath=from_ath,
                vol7=v7, vol_prev28=v_prev, vol_trend=(v7 / v_prev) if v_prev else None,
                support=support, resist=resist, prior_high=prior_high, last=c[-1]["c"],
                ret30=(c[-1]["c"] / c[-31]["c"] - 1) if len(c) > 31 else None)

def entry_zone(h, price):
    """Rule-based zones from the last 45 daily candles. support = 15th-pct low, resist = 85th-pct high,
    prior_high = top of the ~2 months before the last week, mid20 = median close of the last 20 days."""
    s, r, ph, m20, low14 = h["support"], h["resist"], h["prior_high"], h["mid20"], h["low14"]
    if price > ph * 1.35:
        return dict(kind="extended, wait", lo=ph * 0.95, hi=ph * 1.10, inval=ph * 0.80,
                    note="already far above its range; do not chase, wait for a pullback")
    if price > ph:
        return dict(kind="breakout retest", lo=ph * 0.92, hi=ph * 1.03, inval=ph * 0.80,
                    note="broke above its prior range high; look for a retest of that level")
    if price < s * 0.97:
        return dict(kind="lost support, wait for reclaim", lo=s, hi=s * 1.10, inval=low14 * 0.85,
                    note="trading below its range floor; only on a daily close back above it")
    if price <= s * 1.20:
        return dict(kind="range support", lo=s * 0.95, hi=s * 1.15, inval=s * 0.80,
                    note="sitting near the bottom of its 45-day range")
    if price > s * 1.6:
        return dict(kind="pullback to 20-day median", lo=m20 * 0.92, hi=m20 * 1.02, inval=min(s, m20 * 0.75),
                    note="well off its floor; wait for a dip toward the 20-day median")
    return dict(kind="mid-range", lo=s * 0.98, hi=s * 1.15, inval=s * 0.80,
                note="mid-range; better fills nearer support, or on a break and retest of " + fmt_p(r))

# ---------------------------------------------------------------- safety / distribution
def rugcheck(addr):
    r = get(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report")
    if not isinstance(r, dict): return None
    known = r.get("knownAccounts") or {}
    pools = {m.get("liquidityA") for m in r.get("markets") or []} | {m.get("liquidityB") for m in r.get("markets") or []}
    pool_owners = {m.get("pubkey") for m in r.get("markets") or []}
    holders = []
    for h in r.get("topHolders") or []:
        own = h.get("owner")
        if own in known or own in pool_owners or h.get("address") in pools or own in (r.get("lockers") or {}): continue
        holders.append(h)
    supply = f((r.get("token") or {}).get("supply")) or 1
    nets = r.get("insiderNetworks") or []
    # RugCheck's "currentHolding" per network can exceed what the top holders even hold (it seems to count
    # exchange/pool wallets and flows), so treat it as an upper bound and cap it by the top-20 holders' share.
    raw_cluster = sum(f(n.get("currentHolding")) for n in nets) / supply * 100 if nets else 0.0
    top20_all = sum(f(h.get("pct")) for h in (r.get("topHolders") or [])[:20])
    cluster_pct = min(raw_cluster, top20_all, 100.0)
    lp_locked = [f((m.get("lp") or {}).get("lpLockedPct")) for m in r.get("markets") or [] if m.get("lp")]
    top_market = max(r.get("markets") or [{}], key=lambda m: f((m.get("lp") or {}).get("quoteUSD")) + f((m.get("lp") or {}).get("baseUSD")))
    return dict(mint_auth=bool(r.get("mintAuthority")), freeze_auth=bool(r.get("freezeAuthority")),
                top1=f(holders[0]["pct"]) if holders else None, top10=sum(f(h["pct"]) for h in holders[:10]),
                insiders_in_top=sum(1 for h in holders[:20] if h.get("insider")),
                cluster_pct=cluster_pct, cluster_pct_raw=raw_cluster, clusters=len(nets),
                biggest_cluster_wallets=max([n.get("size", 0) for n in nets] or [0]), cluster_wallets=sum(n.get("size", 0) for n in nets),
                lp_locked_pct=f((top_market.get("lp") or {}).get("lpLockedPct")) if top_market else (max(lp_locked) if lp_locked else None),
                market_type=top_market.get("marketType"), holders=r.get("totalHolders"), rugged=r.get("rugged"),
                risks=[x.get("name") for x in r.get("risks") or [] if x.get("level") in ("danger", "warn")],
                creator_pct=f(r.get("creatorBalance")) / supply * 100 if r.get("creatorBalance") else 0.0,
                transfer_fee=f((r.get("transferFee") or {}).get("pct")))

def goplus_base(addr):
    d = get(f"https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={addr}")
    t = list(((d or {}).get("result") or {}).values())
    if not t: return None
    t = t[0]
    hs = [h for h in t.get("holders") or [] if not (h.get("is_contract") == 1 and h.get("is_locked") != 1 and "pool" in (h.get("tag") or "").lower())]
    hs = [h for h in hs if (h.get("tag") or "").lower().find("uniswap") < 0 and (h.get("tag") or "").lower().find("aerodrome") < 0]
    lp = t.get("lp_holders") or []
    lp_locked = sum(f(x.get("percent")) for x in lp if x.get("is_locked") == 1 or (x.get("address") or "").lower().startswith("0x000000000000000000000000000000000000dead")) * 100
    return dict(honeypot=t.get("is_honeypot") == "1", buy_tax=f(t.get("buy_tax")) * 100, sell_tax=f(t.get("sell_tax")) * 100,
                mint_auth=t.get("is_mintable") == "1", hidden_owner=t.get("hidden_owner") == "1",
                owner_can_change_balance=t.get("owner_change_balance") == "1", blacklist=t.get("is_blacklisted") == "1",
                open_source=t.get("is_open_source") == "1", owner=t.get("owner_address"),
                top1=f(hs[0].get("percent")) * 100 if hs else None, top10=sum(f(h.get("percent")) for h in hs[:10]) * 100,
                creator_pct=f(t.get("creator_percent")) * 100, lp_locked_pct=lp_locked if lp else None,
                holders=int(f(t.get("holder_count"))), freeze_auth=False, cluster_pct=None, clusters=None)

def honeypot_is(addr):
    d = get(f"https://api.honeypot.is/v2/IsHoneypot?address={addr}&chainID=8453")
    if not isinstance(d, dict): return None
    return dict(honeypot=(d.get("honeypotResult") or {}).get("isHoneypot"), sim=(d.get("simulationResult") or {}),
                sim_ok=bool(d.get("simulationSuccess")), flags=d.get("flags"),
                risk=(d.get("summary") or {}).get("risk"))

def gt_info(chain, addr):
    d = gt(f"networks/{chain}/tokens/{addr}/info", 6 * 3600)
    return (((d or {}).get("data") or {}).get("attributes")) or {}

# ---------------------------------------------------------------- organic / "culting" signals
def socials(pair, info):
    links = {"x": None, "tg": None, "web": None}
    for s in (pair.get("info") or {}).get("socials") or []:
        u = s.get("url") or ""
        if s.get("type") in ("twitter", "x") and not links["x"]: links["x"] = u
        if s.get("type") == "telegram" and not links["tg"]: links["tg"] = u
    if (pair.get("info") or {}).get("websites"): links["web"] = pair["info"]["websites"][0].get("url")
    if not links["x"] and info.get("twitter_handle"): links["x"] = "https://x.com/" + info["twitter_handle"]
    if not links["tg"] and info.get("telegram_handle"): links["tg"] = "https://t.me/" + info["telegram_handle"]
    return links

def x_followers(url):
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?]|$)", url or "")
    if not m or m.group(1).lower() in ("i", "home", "search", "intent"): return None   # community links have no count
    d = get("https://api.fxtwitter.com/" + m.group(1))
    return ((d or {}).get("user") or {}).get("followers") if isinstance(d, dict) else None

def tg_members(url):
    m = re.search(r"t\.me/([A-Za-z0-9_]{4,})", url or "")
    if not m or m.group(1).lower() in ("joinchat", "addlist"): return None
    t = get("https://t.me/" + m.group(1))
    if not isinstance(t, str): return None
    m2 = re.search(r'tgme_page_extra">([\d\s,]+)\s*(members|subscribers)', t)
    return int(re.sub(r"\D", "", m2.group(1))) if m2 else None

def snapshot_growth(chain, addr, holders):
    con = sqlite3.connect(DB); con.execute("create table if not exists snap(chain,addr,ts,holders)")
    prev = con.execute("select ts,holders from snap where chain=? and addr=? and ts<? order by ts desc limit 1",
                       (chain, addr, time.time() - 6 * 3600)).fetchone()
    if holders: con.execute("insert into snap values(?,?,?,?)", (chain, addr, time.time(), holders)); con.commit()
    con.close()
    if prev and prev[1] and holders:
        days = (time.time() - prev[0]) / 86400
        return (holders / prev[1] - 1) / max(days, 0.25)   # growth per day
    return None

# ---------------------------------------------------------------- scoring
def score(c):
    h, d, p = c["hist"], c["dist"] or {}, c["pair"]
    o = 0.0; why = []
    n = h["drawdowns_recovered"]
    o += min(n, 4) * 6; why.append(f"{n} recovered -70% drawdowns")
    o += min(h["days_under_10m"], 240) / 240 * 20
    if c["mcap"] < CAP_LINE: o += 6
    elif c["mcap"] < CAP_LINE * 2: o += 3
    vt = h["vol_trend"]
    if vt: o += max(min((vt - 1) * 8, 8), -8)
    cult = c["cult"]["score"]; o += cult * 3
    t10 = d.get("top10")
    if t10 is not None: o += 6 if t10 < 20 else 3 if t10 < 30 else 0 if t10 < 45 else -6
    if h["open_drawdown"]: o -= 4
    return round(o, 1)

def risk(c):
    h, d, p = c["hist"], c["dist"] or {}, c["pair"]
    pts = 0; flags = []
    def flag(w, s): nonlocal pts; pts += w; flags.append(s)
    if d.get("mint_auth"): flag(3, "mint authority still enabled (supply can be inflated)")
    if d.get("freeze_auth"): flag(3, "freeze authority enabled (wallets can be frozen)")
    if d.get("honeypot"): flag(5, "honeypot flag")
    if f(d.get("sell_tax")) > 5 or f(d.get("buy_tax")) > 5: flag(2, f"tax {d.get('buy_tax',0):.0f}/{d.get('sell_tax',0):.0f}%")
    if d.get("hidden_owner") or d.get("owner_can_change_balance"): flag(3, "owner can alter balances / hidden owner")
    t10, t1 = d.get("top10"), d.get("top1")
    if t10 is None: flag(1, "holder distribution not measured")
    elif t10 > 45: flag(2, f"top-10 wallets hold {t10:.0f}%")
    elif t10 > 30: flag(1, f"top-10 wallets hold {t10:.0f}%")
    if t1 and t1 > 8: flag(1, f"largest wallet {t1:.1f}%")
    if f(d.get("cluster_pct")) > 25: flag(3, f"linked-wallet clusters hold up to {d['cluster_pct']:.0f}%")
    elif f(d.get("cluster_pct")) > 10: flag(2, f"linked-wallet clusters hold up to {d['cluster_pct']:.0f}%")
    elif f(d.get("cluster_pct")) > 3: flag(1, f"linked-wallet clusters hold up to {d['cluster_pct']:.1f}%")
    if d.get("hp_fail_rate"): flag(2, "honeypot.is saw many failed sells (test-sell a tiny amount first)")
    if f(d.get("creator_pct")) > 3: flag(1, f"dev wallet holds {d['creator_pct']:.1f}%")
    lp = d.get("lp_locked_pct")
    if lp is not None and lp < 50 and (d.get("market_type") or "") not in ("pump_amm", "meteora", "meteoraDamm", "meteoraDammV2", "orca", "raydium_clmm"):
        flag(1, f"LP only {lp:.0f}% locked/burned")
    if c["liq"] < 75_000: flag(1, f"thin liquidity ${c['liq']/1e3:.0f}k")
    if c["liq"] / max(c["mcap"], 1) < 0.04: flag(1, "liquidity under 4% of market cap")
    if h["vol_trend"] is not None and h["vol_trend"] < 0.6: flag(1, "volume fading")
    if c["vol24"] < 50_000: flag(1, "thin daily volume")
    if c["age_days"] < 60: flag(1, f"young ({c['age_days']:.0f} days)")
    if h["open_drawdown"]: flag(1, f"still in a {h['open_drawdown']['depth']*100:.0f}% drawdown")
    level = 1 if pts <= 1 else 2 if pts <= 3 else 3 if pts <= 5 else 4 if pts <= 7 else 5
    return level, flags

def cult_signals(c, info):
    p = c["pair"]; links = socials(p, info); sig = {}; s = 0.0
    sig["cto"] = "cto" in c["sources"]
    boosts = f((p.get("boosts") or {}).get("active"))
    sig["paid_boosts"] = boosts; sig["ad"] = "ad" in c["sources"]
    sig["links"] = links
    sig["x_followers"] = x_followers(links["x"]) if links["x"] else None
    sig["tg_members"] = tg_members(links["tg"]) if links["tg"] else None
    holders = (info.get("holders") or {}).get("count") or (c["dist"] or {}).get("holders")
    sig["holders"] = holders
    sig["holders_per_1m"] = holders / (c["mcap"] / 1e6) if holders else None
    sig["holder_growth_day"] = snapshot_growth(c["chain"], c["addr"], holders)
    tx = (p.get("txns") or {}).get("h24") or {}
    ntx = f(tx.get("buys")) + f(tx.get("sells"))
    sig["avg_trade_usd"] = c["vol24"] / ntx if ntx else None
    sig["buy_ratio"] = f(tx.get("buys")) / ntx if ntx else None
    if sig["cto"]: s += 1.5
    s += 0.5 * sum(1 for v in links.values() if v)
    if sig["x_followers"]: s += min(math.log10(sig["x_followers"]) - 3, 1.5) if sig["x_followers"] > 1000 else 0
    if sig["tg_members"]: s += min(math.log10(sig["tg_members"]) - 2.5, 1) if sig["tg_members"] > 300 else 0
    if sig["holders_per_1m"]: s += 1.5 if sig["holders_per_1m"] > 2000 else 1 if sig["holders_per_1m"] > 800 else 0.3 if sig["holders_per_1m"] > 300 else 0
    if sig["holder_growth_day"] and sig["holder_growth_day"] > 0.01: s += 1
    if sig["avg_trade_usd"] and sig["avg_trade_usd"] < 400: s += 0.5   # many small retail-sized trades
    if boosts >= 100 or sig["ad"]: s -= 1   # paid promotion running now
    sig["score"] = round(max(s, 0), 2)
    return sig

# ---------------------------------------------------------------- formatting
def fmt_usd(x):
    if x is None: return "?"
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= d: return f"${x/d:.1f}{s}" if x / d < 100 else f"${x/d:.0f}{s}"
    return f"${x:.0f}"

def fmt_p(x):
    if x is None: return "?"
    if x >= 1: return f"${x:.3f}"
    s = f"{x:.12f}"; z = len(s.split(".")[1]) - len(s.split(".")[1].lstrip("0"))
    return f"${x:.{z+3}f}".rstrip("0")

def bubblemaps_link(chain, addr):
    return f"https://v2.bubblemaps.io/map?address={addr}&chain={chain}"

def telegram_block(i, c):
    h, d, cu, e = c["hist"], c["dist"] or {}, c["cult"], c["entry"]
    sym = c["pair"]["baseToken"].get("symbol"); chain = "SOL" if c["chain"] == "solana" else "BASE"
    sup = c["mcap"] / f(c["pair"].get("priceUsd"), 1)
    lines = [f"{i}. ${sym} ({chain}) · MC {fmt_usd(c['mcap'])} · Liq {fmt_usd(c['liq'])} · {c['age_days']:.0f}d old"]
    dd = f"Survived {h['drawdowns_recovered']}× -70% dumps in {h['days']}d of data"
    if h["open_drawdown"]: dd += f" (now {h['open_drawdown']['depth']*100:.0f}% off its last peak)"
    lines.append(dd)
    lines.append(f"Under $10M MC on {h['days_under_10m']} of {h['days']} days (longest run {h['longest_streak_under_10m']}d)")
    vt = f"{h['vol_trend']:.1f}x" if h["vol_trend"] else "n/a"
    lines.append(f"Vol 24h {fmt_usd(c['vol24'])} · 7d vs prior 4w: {vt}")
    dist = []
    if d.get("top10") is not None: dist.append(f"top10 {d['top10']:.0f}%")
    if d.get("cluster_pct") is not None:
        dist.append(f"linked-wallet clusters ≤{d['cluster_pct']:.0f}%" if d["cluster_pct"] >= 0.5 else "no linked-wallet clusters flagged")
    if cu.get("holders"): dist.append(f"{cu['holders']:,} holders")
    lines.append("Holders: " + (" · ".join(dist) if dist else "not measured"))
    org = []
    if cu["cto"]: org.append("community takeover (CTO)")
    if cu.get("x_followers"): org.append(f"X {cu['x_followers']:,}")
    if cu.get("tg_members"): org.append(f"TG {cu['tg_members']:,}")
    if cu.get("holder_growth_day") is not None: org.append(f"holders {cu['holder_growth_day']*100:+.1f}%/day")
    if cu.get("paid_boosts"): org.append(f"paid boosts {cu['paid_boosts']:.0f}")
    lines.append("Community: " + (" · ".join(org) if org else "no strong organic signal"))
    lo_mc, hi_mc, inv_mc = e["lo"] * sup, e["hi"] * sup, e["inval"] * sup
    lines.append(f"Entry ({e['kind']}): {fmt_p(e['lo'])}–{fmt_p(e['hi'])} (MC {fmt_usd(lo_mc)}–{fmt_usd(hi_mc)}); "
                 f"invalid on a daily close below {fmt_p(e['inval'])} (MC {fmt_usd(inv_mc)}). Now {fmt_p(f(c['pair'].get('priceUsd')))}.")
    lines.append(f"Risk {c['risk']}/5: " + ("; ".join(c["risk_flags"]) if c["risk_flags"] else "no major flags found"))
    lines.append(f"Chart: {c['pair']['url']}")
    lines.append(f"Bubblemap: {bubblemaps_link(c['chain'], c['addr'])}")
    lines.append(f"CA: {c['addr']}")
    lines.append("NFA, memecoins can go to zero.")
    return "\n".join(lines)

def telegram_messages(picks, stats):
    ts = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    head = (f"Memecoin scan · {ts}\n{stats['candidates']} coins seen, {stats['passed']} passed filters, {stats['deep']} fully checked.\n"
            f"Ranked by: recovered -70% dumps, time under $10M, community, holder spread, volume trend.\n")
    blocks = [telegram_block(i + 1, c) for i, c in enumerate(picks)]
    foot = (f"\n{NFA} Entry zones are rule-based levels, not predictions. Price history covers at most the last 12 months. "
            f"Check the bubblemap yourself before buying.")
    msgs, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + 2 > 3900: msgs.append(cur); cur = ""
        cur += "\n" + b + "\n"
    if len(cur) + len(foot) > 3900: msgs.append(cur); cur = ""
    msgs.append(cur + foot)
    return msgs

# ---------------------------------------------------------------- one coin, all slow checks
def deep_check(c):
    """Price history, holders, rug checks, community, entry, risk and score for one prefiltered candidate.
    Returns c (filled in) or None when there is not enough price history. May raise on bad data."""
    sym = c["pair"]["baseToken"].get("symbol")
    hist = ohlcv(c["chain"], c["pair"]["pairAddress"], f(c["pair"].get("pairCreatedAt")) / 1000)
    c["hist"] = history_metrics(hist, c["mcap"], f(c["pair"].get("priceUsd")))
    if not c["hist"]: log("skip no history", sym); return None
    info = {}
    if c["chain"] == "solana":
        c["dist"] = rugcheck(c["addr"]) or {}
        if not c["dist"]: info = gt_info(c["chain"], c["addr"])   # GeckoTerminal is the rate-limited one; only as fallback
    else:
        info = gt_info(c["chain"], c["addr"])
        c["dist"] = goplus_base(c["addr"]) or {}
        hp = honeypot_is(c["addr"])
        if hp and hp.get("honeypot"):
            # honeypot.is also says "honeypot" when its buy/sell simulation PASSES but past holders' sells
            # often failed (flag medium_fail_rate). Only a failed simulation or a huge sell tax excludes.
            if not hp.get("sim_ok") or f((hp.get("sim") or {}).get("sellTax")) > 50: c["dist"]["honeypot"] = True
            else: c["dist"]["hp_fail_rate"] = True
        if hp: c["dist"]["sim"] = hp
    if info.get("mint_authority") == "yes": c["dist"]["mint_auth"] = True
    if info.get("freeze_authority") == "yes": c["dist"]["freeze_auth"] = True
    if c["dist"].get("top10") is None and (info.get("holders") or {}).get("distribution_percentage"):
        c["dist"]["top10"] = f(info["holders"]["distribution_percentage"].get("top_10")); c["dist"]["top10_src"] = "geckoterminal (may include pools)"
    c["cult"] = cult_signals(c, info)
    c["entry"] = entry_zone(c["hist"], f(c["pair"].get("priceUsd")))
    c["risk"], c["risk_flags"] = risk(c)
    c["score"] = score(c)
    c["hard_fail"] = bool(c["dist"].get("honeypot") or c["dist"].get("rugged") or c["dist"].get("mint_auth")
                          or c["dist"].get("freeze_auth") or c["dist"].get("owner_can_change_balance"))
    log(f"ok {sym} dd={c['hist']['drawdowns_recovered']} u10={c['hist']['days_under_10m']} score={c['score']} risk={c['risk']}")
    return c

def candidate_from_pair(p, sources=()):
    now = time.time() * 1000
    return dict(chain=p["chainId"], addr=p["baseToken"]["address"], pair=p, sources=sorted(sources),
                mcap=f(p.get("marketCap") or p.get("fdv")), liq=f((p.get("liquidity") or {}).get("usd")),
                vol24=f((p.get("volume") or {}).get("h24")), age_days=(now - f(p.get("pairCreatedAt"), now)) / 86400000)

def guess_chain(addr):
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", addr or ""): return "base"
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr or ""): return "solana"
    return None

def find_pair(query):
    """A contract address or a ticker -> the deepest-liquidity Dexscreener pair on CHAINS, or None."""
    q = (query or "").strip().lstrip("$")
    chain = guess_chain(q)
    if chain:
        pairs = get(f"https://api.dexscreener.com/tokens/v1/{chain}/{q}") or []
        pairs = [p for p in pairs if isinstance(p, dict) and p["baseToken"]["address"].lower() == q.lower()]
    else:
        d = get("https://api.dexscreener.com/latest/dex/search?q=" + requests.utils.quote(q)) or {}
        pairs = [p for p in (d.get("pairs") or []) if p.get("chainId") in CHAINS
                 and (p["baseToken"].get("symbol") or "").upper() == q.upper()]
    if not pairs: return None
    return max(pairs, key=lambda p: f((p.get("liquidity") or {}).get("usd")))

def analyze(query):
    """Full card data for one coin, outside the scan (used by the bot's /coin). Returns (c or None, reason)."""
    p = find_pair(query)
    if not p: return None, "not found on Dexscreener (Solana/Base)"
    c = candidate_from_pair(p)
    try:
        c = deep_check(c)
    except Exception as ex:
        log("analyze error", query, repr(ex)); return None, "data error while checking it, try again in a few min"
    if not c: return None, "not enough price history yet (needs a few weeks of daily candles)"
    return c, ""

def slim(c):
    x = dict(c)
    x["symbol"] = c["pair"]["baseToken"].get("symbol"); x["dexscreener"] = c["pair"]["url"]
    x["bubblemaps"] = bubblemaps_link(c["chain"], c["addr"]); x["price"] = f(c["pair"].get("priceUsd"))
    x["pair_address"] = c["pair"]["pairAddress"]; x["dex"] = c["pair"].get("dexId")
    return x

# ---------------------------------------------------------------- main
def run():
    t0 = time.time()
    seeds = collect_candidates(); log("seeds", len(seeds))
    pairs = best_pairs(seeds); log("pairs", len(pairs))
    cands = prefilter(pairs, seeds); log("passed prefilter", len(cands))
    deep = []
    for c in cands[:MAX_DEEP]:
        sym = c["pair"]["baseToken"].get("symbol")
        try:
            if deep_check(c): deep.append(c)
        except Exception as ex:
            log("error", sym, repr(ex))
    good = [c for c in deep if not c["hard_fail"]]
    good.sort(key=lambda c: c["score"], reverse=True)
    picks = good[:TOP_N]
    stats = dict(candidates=len(seeds), passed=len(cands), deep=len(deep), seconds=round(time.time() - t0))
    msgs = telegram_messages(picks, stats)
    json.dump(dict(stats=stats, picks=[slim(c) for c in picks], all_checked=[slim(c) for c in deep],
                   excluded_hard_fail=[c["pair"]["baseToken"].get("symbol") for c in deep if c["hard_fail"]]),
              open(os.path.join(OUT, "latest.json"), "w"), indent=1, default=str)
    open(os.path.join(OUT, "telegram.txt"), "w").write("\n\n----- next message -----\n\n".join(msgs))
    log("done", stats)
    return picks, msgs, stats

if __name__ == "__main__":
    run()
