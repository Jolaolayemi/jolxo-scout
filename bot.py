#!/usr/bin/env python3
"""Jolxo Scout (@jolxoscoutbot): always-on Telegram front end for scanner.py.

One long-running process, plain HTTPS long-polling (no framework), three threads:
  main       Telegram getUpdates loop -> commands
  scheduler  full scan every FULL_SCAN_HOURS (posts top picks), light check every LIGHT_CHECK_MINUTES
             (Dexscreener only, watchlist coins: alerts on entering the entry zone / breaking invalidation,
             deduped in sqlite for ALERT_DEDUPE_HOURS)
  workers    /scan and /coin run in their own threads so the bot keeps answering

Env: TELEGRAM_BOT_TOKEN (required unless DRY_RUN=1), ALLOWED_CHAT_IDS (required: your chat id(s), comma-separated),
     COINGECKO_DEMO_KEY (optional), DATA_DIR (default ./data; /data on Railway), DRY_RUN=1 (print, never send).
Dry run:  DRY_RUN=1 python bot.py --cmd /scan --cmd /top --light-check      (scripted, then exits)
          DRY_RUN=1 python bot.py                                            (scheduler + commands typed on stdin)
"""
import argparse, json, os, re, sqlite3, sys, threading, time, traceback
from datetime import datetime, timezone, timedelta

DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR") or "./data")
os.makedirs(DATA_DIR, exist_ok=True)
os.environ["DATA_DIR"] = DATA_DIR          # scanner.py reads it at import for cache/out/snapshots
import requests
import scanner as sc

TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
ALLOWED = [int(x) for x in re.findall(r"-?\d+", os.environ.get("ALLOWED_CHAT_IDS") or "")]
DRY_RUN = (os.environ.get("DRY_RUN") or "").lower() in ("1", "true", "yes", "on")
FULL_SCAN_HOURS = float(os.environ.get("FULL_SCAN_HOURS", 4))
LIGHT_CHECK_MINUTES = float(os.environ.get("LIGHT_CHECK_MINUTES", 30))
ALERT_DEDUPE_HOURS = float(os.environ.get("ALERT_DEDUPE_HOURS", 24))
CACHE_KEEP_DAYS = float(os.environ.get("CACHE_KEEP_DAYS", 3))
TG_LIMIT = 4000                             # Telegram's hard limit is 4096; keep headroom
NFA = "NFA, memecoins can go to zero."
WAT = timezone(timedelta(hours=1), "WAT")   # Lagos, no DST
DB_PATH = os.path.join(DATA_DIR, "bot.sqlite")

# ------------------------------------------------------------------ logging (stdout, token never printed)
_print_lock = threading.Lock()
def redact(x):
    t = str(x)
    return t.replace(TOKEN, "***") if TOKEN else t

def log(*a):
    with _print_lock:
        print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), redact(" ".join(str(x) for x in a)), flush=True)

sc.log = lambda *a: log("[scan]", *a)       # scanner logs to stderr standalone; under the bot, stdout

# ------------------------------------------------------------------ sqlite
_db_lock = threading.RLock()
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with _db_lock, db() as con:
        con.executescript("""
        create table if not exists kv(k text primary key, v text);
        create table if not exists reports(id integer primary key autoincrement, ts real, reason text,
            stats text, messages text, coins text);
        create table if not exists watchlist(chain text, addr text, symbol text, pair_address text, url text,
            lo real, hi real, inval real, kind text, supply real, risk integer, risk_flags text,
            ref_price real, last_price real, state text, added_ts real, updated_ts real, primary key(chain, addr));
        create table if not exists alerts(chain text, addr text, event text, ts real, price real);
        create index if not exists alerts_idx on alerts(chain, addr, event, ts);
        """)

def kv_get(k, default=None):
    with _db_lock, db() as con:
        r = con.execute("select v from kv where k=?", (k,)).fetchone()
    return r["v"] if r else default

def kv_set(k, v):
    with _db_lock, db() as con:
        con.execute("insert into kv(k, v) values(?, ?) on conflict(k) do update set v=excluded.v", (k, str(v)))

# ------------------------------------------------------------------ Telegram
API = "https://api.telegram.org/bot{}/{}"
HTTP = requests.Session()

class TelegramError(Exception):
    def __init__(self, code, desc, retry_after=None):
        super().__init__(f"{code} {desc}"); self.code, self.desc, self.retry_after = code, desc, retry_after

def tg(method, http_timeout=30, **params):
    if DRY_RUN: raise RuntimeError("tg() called in DRY_RUN")   # belt and braces: dry run never touches Telegram
    try:
        r = HTTP.post(API.format(TOKEN, method), json=params, timeout=http_timeout)
    except requests.RequestException as e:
        raise TelegramError(0, redact(e))
    try: d = r.json()
    except ValueError: raise TelegramError(r.status_code, r.text[:200])
    if not d.get("ok"):
        raise TelegramError(d.get("error_code", r.status_code), d.get("description", ""),
                            (d.get("parameters") or {}).get("retry_after"))
    return d["result"]

def split_text(text, limit=TG_LIMIT):
    """Split on blank lines, then lines, then hard cuts, so every chunk is <= limit chars."""
    text = text.strip()
    if len(text) <= limit: return [text]
    chunks, cur = [], ""
    def push(piece, sep):
        nonlocal cur
        if not cur: cur = piece
        elif len(cur) + len(sep) + len(piece) <= limit: cur += sep + piece
        else: chunks.append(cur); cur = piece
    for block in text.split("\n\n"):
        if len(block) <= limit: push(block, "\n\n"); continue
        for line in block.split("\n"):
            while len(line) > limit:
                push(line[:limit], "\n"); line = line[limit:]
            push(line, "\n")
    if cur: chunks.append(cur)
    return [c for c in chunks if c.strip()]

def send(chat_id, text):
    """Send text to one chat, split under the limit, web previews off. Never raises."""
    parts = split_text(text)
    for i, part in enumerate(parts, 1):
        if DRY_RUN:
            with _print_lock:
                print(f"\n──── DRY_RUN → chat {chat_id} · message {i}/{len(parts)} · {len(part)} chars ────\n{part}\n"
                      f"──── end ────", flush=True)
            continue
        for attempt in range(6):
            try:
                tg("sendMessage", chat_id=chat_id, text=part, link_preview_options={"is_disabled": True})
                break
            except TelegramError as e:
                if e.code == 429:
                    wait = min(int(e.retry_after or 5) + 1, 120); log("telegram 429, waiting", wait); time.sleep(wait)
                elif e.code in (0, 500, 502, 503, 504):
                    time.sleep(min(2 ** attempt, 30)); log("telegram send retry:", e)
                else:
                    log("telegram send failed:", e); break
        time.sleep(0.5)

def broadcast(text):
    for cid in ALLOWED: send(cid, text)

# ------------------------------------------------------------------ helpers
def ago(ts):
    m = int((time.time() - float(ts)) / 60)
    return f"{m} min ago" if m < 90 else f"{m // 60}h {m % 60}m ago"

def wat(ts):
    return datetime.fromtimestamp(float(ts), WAT).strftime("%d %b %H:%M WAT")

def last_report():
    with _db_lock, db() as con:
        r = con.execute("select * from reports order by id desc limit 1").fetchone()
    if not r: return None
    return dict(ts=r["ts"], reason=r["reason"], stats=json.loads(r["stats"]), messages=json.loads(r["messages"]),
                coins=json.loads(r["coins"]))

def card(c, header=""):
    """Full one-coin card from scanner.telegram_block, without the list number."""
    block = re.sub(r"^\d+\.\s*", "", sc.telegram_block(1, c))
    if c.get("hard_fail"):
        why = [k for k in ("honeypot", "rugged", "mint_auth", "freeze_auth", "owner_can_change_balance") if (c.get("dist") or {}).get(k)]
        block = f"⚠️ FAILS a hard safety check ({', '.join(why)}), the scanner would never pick this.\n" + block
    return (header + "\n" if header else "") + block

def classify(price, lo, hi, inval):
    if price is None: return None
    if price < inval: return "invalid"
    if lo <= price <= hi: return "zone"
    return "above" if price > hi else "between"   # between = under the zone but above invalidation

STATE_WORDS = {"zone": "IN entry zone", "above": "above zone", "between": "below zone, above invalidation",
               "invalid": "BELOW invalidation", None: "price unavailable"}

def live_prices(rows):
    """Dexscreener only, batched: {(chain, addr): (price, pair)}. Missing coins are left out."""
    out, by_chain = {}, {}
    for r in rows: by_chain.setdefault(r["chain"], []).append(r)
    for chain, rs in by_chain.items():
        for i in range(0, len(rs), 30):
            batch = rs[i:i + 30]
            pairs = sc.get(f"https://api.dexscreener.com/tokens/v1/{chain}/" + ",".join(r["addr"] for r in batch)) or []
            if not isinstance(pairs, list): continue
            for r in batch:
                mine = [p for p in pairs if isinstance(p, dict) and p["baseToken"]["address"].lower() == r["addr"].lower()]
                if not mine: continue
                same = [p for p in mine if p.get("pairAddress") == r["pair_address"]]
                p = same[0] if same else max(mine, key=lambda p: sc.f((p.get("liquidity") or {}).get("usd")))
                price = sc.f(p.get("priceUsd"), None)
                if price: out[(chain, r["addr"])] = (price, p)
    return out

# ------------------------------------------------------------------ full scan
scan_lock = threading.Lock()

def run_full_scan(chats, reason):
    if not scan_lock.acquire(blocking=False):
        log("scan already running; skip", reason); return False
    t0 = time.time()
    try:
        log("full scan start:", reason)
        picks, msgs, stats = sc.run()
        latest = json.load(open(os.path.join(sc.OUT, "latest.json")))
        coins = latest.get("all_checked") or []
        with _db_lock, db() as con:
            con.execute("insert into reports(ts, reason, stats, messages, coins) values(?,?,?,?,?)",
                        (time.time(), reason, json.dumps(stats), json.dumps(msgs), json.dumps(coins, default=str)))
            con.execute("delete from reports where id not in (select id from reports order by id desc limit 30)")
        update_watchlist(latest.get("picks") or [])
        kv_set("last_full_scan_ts", time.time())
        if picks:
            msgs = list(msgs); msgs[0] = "🪳 Jolxo Scout top picks\n" + msgs[0]
        else:
            msgs = [f"🪳 Jolxo Scout scan done: no coin passed every check this time "
                    f"({stats['candidates']} seen, {stats['passed']} passed filters, {stats['deep']} fully checked). "
                    f"Next scan in {FULL_SCAN_HOURS:g}h."]
        for cid in chats:
            for m in msgs: send(cid, m)
        log(f"full scan done in {time.time() - t0:.0f}s: {len(picks)} picks", stats)
        return True
    except Exception:
        log("full scan FAILED:\n" + traceback.format_exc())
        # retry in ~30 min instead of waiting a whole cycle
        kv_set("last_full_scan_ts", time.time() - FULL_SCAN_HOURS * 3600 + 1800)
        if reason == "manual":
            for cid in chats: send(cid, "Scan hit a data-source error. I'll retry automatically within 30 min.")
        return False
    finally:
        scan_lock.release()
        prune_cache()

def update_watchlist(picks):
    now = time.time()
    with _db_lock, db() as con:
        old = {(r["chain"], r["addr"]): dict(r) for r in con.execute("select * from watchlist")}
        con.execute("delete from watchlist")
        for p in picks:
            e = p.get("entry") or {}
            if not e.get("lo"): continue
            price = sc.f(p.get("price"), None)
            supply = sc.f(p.get("mcap")) / price if price else None
            prev = old.get((p["chain"], p["addr"])) or {}
            con.execute("insert into watchlist values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                p["chain"], p["addr"], p.get("symbol"), p.get("pair_address"), p.get("dexscreener"),
                e["lo"], e["hi"], e["inval"], e.get("kind"), supply, p.get("risk"), json.dumps(p.get("risk_flags") or []),
                price, price, classify(price, e["lo"], e["hi"], e["inval"]), prev.get("added_ts") or now, now))
    log("watchlist updated:", len(picks), "coins")

def prune_cache():
    try:
        cut = time.time() - CACHE_KEEP_DAYS * 86400; n = 0
        for fn in os.listdir(sc.CACHE):
            fp = os.path.join(sc.CACHE, fn)
            if os.path.getmtime(fp) < cut: os.remove(fp); n += 1
        if n: log("pruned", n, "old cache files")
        with _db_lock, db() as con:
            con.execute("delete from alerts where ts < ?", (time.time() - 30 * 86400,))
    except Exception as e:
        log("prune error:", e)

# ------------------------------------------------------------------ light check (Dexscreener only)
def recently_alerted(con, chain, addr, event):
    return con.execute("select 1 from alerts where chain=? and addr=? and event=? and ts>?",
                       (chain, addr, event, time.time() - ALERT_DEDUPE_HOURS * 3600)).fetchone() is not None

def alert_text(r, event, price):
    sym, chain = r["symbol"], "SOL" if r["chain"] == "solana" else "BASE"
    mc = f" (MC {sc.fmt_usd(price * r['supply'])})" if r["supply"] else ""
    flags = "; ".join(json.loads(r["risk_flags"] or "[]")) or "no major flags found"
    if event == "entry":
        head = f"🎯 ${sym} ({chain}) just entered its entry zone"
        body = (f"Now {sc.fmt_p(price)}{mc}. Zone {sc.fmt_p(r['lo'])}–{sc.fmt_p(r['hi'])} ({r['kind']}). "
                f"Invalid on a daily close below {sc.fmt_p(r['inval'])}.")
    else:
        head = f"⚠️ ${sym} ({chain}) broke below its invalidation level"
        body = (f"Now {sc.fmt_p(price)}{mc}, under {sc.fmt_p(r['inval'])}. The setup is void on a daily close "
                f"below that level; the zone was {sc.fmt_p(r['lo'])}–{sc.fmt_p(r['hi'])}.")
    return "\n".join([head, body, f"Risk {r['risk']}/5 at last scan: {flags}", f"Chart: {r['url']}",
                      f"Bubblemap: {sc.bubblemaps_link(r['chain'], r['addr'])}", f"CA: {r['addr']}", NFA])

def light_check():
    """Returns the list of alerts sent (for the dry-run test)."""
    with _db_lock, db() as con:
        rows = [dict(r) for r in con.execute("select * from watchlist")]
    if not rows:
        log("light check: watchlist empty"); return []
    prices = live_prices(rows)
    sent = []
    for r in rows:
        key = (r["chain"], r["addr"])
        if key not in prices:
            log("light check: no price for", r["symbol"]); continue
        price = prices[key][0]
        new, prev = classify(price, r["lo"], r["hi"], r["inval"]), r["state"]
        event = "entry" if new == "zone" and prev != "zone" else "invalidation" if new == "invalid" and prev != "invalid" else None
        log(f"light check: ${r['symbol']} {sc.fmt_p(price)} {prev} -> {new}" + (f" EVENT {event}" if event else ""))
        with _db_lock, db() as con:
            if event and recently_alerted(con, r["chain"], r["addr"], event):
                log(f"light check: ${r['symbol']} {event} already alerted within {ALERT_DEDUPE_HOURS:g}h, not re-sending")
                event = None
            if event:
                con.execute("insert into alerts values(?,?,?,?,?)", (r["chain"], r["addr"], event, time.time(), price))
            con.execute("update watchlist set last_price=?, state=?, updated_ts=? where chain=? and addr=?",
                        (price, new, time.time(), r["chain"], r["addr"]))
        if event:
            broadcast(alert_text(r, event, price)); sent.append((r["symbol"], event))
    log(f"light check done: {len(rows)} coins, {len(sent)} alerts")
    return sent

# ------------------------------------------------------------------ commands
WELCOME = (f"🪳 Jolxo Scout\n"
           f"I hunt memecoins that refuse to die: several -70% crashes survived, long stretches under $10M, "
           f"an organic community, clean holder spread. Every pick gets an entry zone and a 1–5 risk score.\n\n"
           f"I scan every {FULL_SCAN_HOURS:g}h and ping you when a shortlisted coin hits its entry zone or breaks its "
           f"invalidation. /help for commands.\n\n{NFA}")

HELP = (f"Commands\n"
        f"/scan  run a full scan now (5–25 min) and post the picks\n"
        f"/top  the last scan's report\n"
        f"/coin <CA or ticker>  full card for one coin\n"
        f"/watchlist  shortlisted coins, live price vs entry zone\n"
        f"/help  this list\n\n"
        f"Automatic: full scan every {FULL_SCAN_HOURS:g}h; entry-zone / invalidation check on the shortlist every "
        f"{LIGHT_CHECK_MINUTES:g} min (no repeat of the same alert within {ALERT_DEDUPE_HOURS:g}h).\n"
        f"Data: Dexscreener, GeckoTerminal/CoinGecko, RugCheck, GoPlus, honeypot.is. Bubblemap links are for you to eyeball.\n\n{NFA}")

def cmd_scan(cid, arg):
    if scan_lock.locked():
        send(cid, "A scan is already running. The picks will post here when it finishes."); return None
    send(cid, "Scanning… pulling Solana/Base coins from Dexscreener, then price history, holders and rug checks. "
              "Takes about 5–25 min (the free price-history API is slow). I'll post the picks here.")
    t = threading.Thread(target=run_full_scan, args=([cid], "manual"), daemon=True, name="scan"); t.start()
    return t

def cmd_top(cid, arg):
    rep = last_report()
    if not rep:
        send(cid, "No scan on record yet. Send /scan to run one now."); return
    send(cid, f"Last scan: {wat(rep['ts'])} ({ago(rep['ts'])}). Prices below are as of that scan.")
    for m in rep["messages"]: send(cid, m)

def find_in_report(rep, q):
    q = q.strip().lstrip("$").lower()
    for c in (rep or {}).get("coins") or []:
        if c.get("addr", "").lower() == q or (c.get("symbol") or "").lower() == q: return c
    return None

def _coin_fresh(cid, q):
    try:
        c, why = sc.analyze(q)
        if not c: send(cid, f"Couldn't build a card for {q}: {why}."); return
        send(cid, card(c, "Fresh check (not from the last scan's shortlist):"))
    except Exception:
        log("coin FAILED:\n" + traceback.format_exc()); send(cid, f"Couldn't check {q} right now, data-source error. Try again in a few min.")

def cmd_coin(cid, arg):
    q = (arg or "").strip()
    if not q:
        send(cid, "Usage: /coin <contract address or ticker>, e.g. /coin RETARDIO"); return None
    rep = last_report()
    c = find_in_report(rep, q)
    if c:
        live = ""
        try:
            lp = live_prices([dict(chain=c["chain"], addr=c["addr"], pair_address=c.get("pair_address"))])
            if lp:
                price = list(lp.values())[0][0]
                e = c.get("entry") or {}
                live = f"\nLive now: {sc.fmt_p(price)}" + (f" ({STATE_WORDS[classify(price, e['lo'], e['hi'], e['inval'])]})" if e.get("lo") else "")
        except Exception as ex:
            log("coin live price error:", ex)
        send(cid, card(c, f"From the scan at {wat(rep['ts'])}:") + live); return None
    send(cid, f"{q} isn't in the last scan. Checking it now, about a minute…")
    t = threading.Thread(target=_coin_fresh, args=(cid, q), daemon=True, name="coin"); t.start()
    return t

def cmd_watchlist(cid, arg):
    with _db_lock, db() as con:
        rows = [dict(r) for r in con.execute("select * from watchlist order by risk, symbol")]
    if not rows:
        send(cid, "Watchlist is empty until the first scan finishes. Send /scan."); return
    try: prices = live_prices(rows)
    except Exception as ex: log("watchlist price error:", ex); prices = {}
    lines = [f"Watchlist: {len(rows)} coins from the last scan (live prices, Dexscreener)", ""]
    for r in rows:
        p = prices.get((r["chain"], r["addr"]), (None,))[0]
        st = STATE_WORDS[classify(p, r["lo"], r["hi"], r["inval"]) if p else None]
        ch = "SOL" if r["chain"] == "solana" else "BASE"
        lines.append(f"${r['symbol']} ({ch}) · risk {r['risk']}/5 · now {sc.fmt_p(p)} · {st}\n"
                     f"Entry {sc.fmt_p(r['lo'])}–{sc.fmt_p(r['hi'])} ({r['kind']}) · invalid < {sc.fmt_p(r['inval'])}\n"
                     f"{r['url']}")
        lines.append("")
    lines.append(f"I alert when one enters its zone or breaks invalidation. {NFA}")
    send(cid, "\n".join(lines))

COMMANDS = {"/start": lambda c, a: send(c, WELCOME), "/help": lambda c, a: send(c, HELP), "/scan": cmd_scan,
            "/top": cmd_top, "/coin": cmd_coin, "/watchlist": cmd_watchlist}

def handle_text(chat_id, text):
    """Returns a worker thread when the command spawned one (dry-run scripts join it)."""
    if chat_id not in ALLOWED:
        log("ignored message from chat", chat_id, "(not in ALLOWED_CHAT_IDS)"); return None
    text = (text or "").strip()
    if not text.startswith("/"): return None
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    fn = COMMANDS.get(cmd)
    if not fn:
        send(chat_id, "Unknown command. /help for the list."); return None
    log("command", cmd, arg[:60], "from", chat_id)
    try:
        return fn(chat_id, arg)
    except Exception:
        log("command FAILED:\n" + traceback.format_exc()); send(chat_id, "Something broke on my side handling that. Logged.")
        return None

# ------------------------------------------------------------------ scheduler
def scheduler(stop):
    log(f"scheduler on: full scan every {FULL_SCAN_HOURS:g}h, light check every {LIGHT_CHECK_MINUTES:g} min")
    while not stop.is_set():
        try:
            now = time.time()
            if now - float(kv_get("last_full_scan_ts", 0)) >= FULL_SCAN_HOURS * 3600 and not scan_lock.locked():
                threading.Thread(target=run_full_scan, args=(ALLOWED, "scheduled"), daemon=True, name="scan").start()
            if now - float(kv_get("last_light_ts", 0)) >= LIGHT_CHECK_MINUTES * 60:
                kv_set("last_light_ts", now)
                light_check()
        except Exception:
            log("scheduler error (continuing):\n" + traceback.format_exc())
        stop.wait(20)

# ------------------------------------------------------------------ polling
def poll_forever():
    me = None
    while me is None:
        try:
            me = tg("getMe"); log("connected as @" + me.get("username", "?"))
        except TelegramError as e:
            log("getMe failed:", e, "(bad TELEGRAM_BOT_TOKEN?)" if e.code == 401 else ""); time.sleep(60 if e.code == 401 else 10)
    try:
        if (tg("getWebhookInfo") or {}).get("url"):
            tg("deleteWebhook"); log("removed a webhook so long-polling works")
        tg("setMyCommands", commands=[{"command": "scan", "description": "run a full scan now"},
                                      {"command": "top", "description": "last report"},
                                      {"command": "coin", "description": "card for one coin: /coin <CA or ticker>"},
                                      {"command": "watchlist", "description": "shortlist vs entry zones"},
                                      {"command": "help", "description": "commands"}])
    except TelegramError as e:
        log("startup setup warning:", e)
    offset, backoff = int(kv_get("tg_offset", 0)), 1
    while True:
        try:
            ups = tg("getUpdates", http_timeout=65, offset=offset, timeout=50, allowed_updates=["message"])
            backoff = 1
        except TelegramError as e:
            if e.code == 409: log("409 conflict: another copy of the bot is polling; waiting"); time.sleep(15)
            elif e.code == 401: log("401: token rejected; waiting 5 min"); time.sleep(300)
            else:
                log("getUpdates error:", e, f"(retry in {backoff}s)"); time.sleep(backoff); backoff = min(backoff * 2, 60)
            continue
        for u in ups:
            offset = u["update_id"] + 1; kv_set("tg_offset", offset)
            msg = u.get("message") or {}
            try:
                handle_text((msg.get("chat") or {}).get("id"), msg.get("text"))
            except Exception:
                log("update handling error:\n" + traceback.format_exc())

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", action="append", default=[], help="DRY_RUN: run this command as the first allowed chat")
    ap.add_argument("--light-check", action="store_true", help="DRY_RUN: run one light check cycle")
    a = ap.parse_args()
    init_db()
    log(f"Jolxo Scout starting · DATA_DIR={DATA_DIR} · DRY_RUN={DRY_RUN} · allowed chats={ALLOWED} · "
        f"price history via {'CoinGecko demo key' if sc.CG_KEY else 'GeckoTerminal (no key)'}")
    if not ALLOWED:
        if not DRY_RUN: sys.exit("ALLOWED_CHAT_IDS is not set (your Telegram chat id, e.g. from @userinfobot)")
        ALLOWED.append(0)                   # DRY_RUN only: a stand-in chat id, nothing is sent
    if a.cmd or a.light_check:
        if not DRY_RUN: sys.exit("--cmd/--light-check are for DRY_RUN=1 only")
        for c in a.cmd:
            t = handle_text(ALLOWED[0], c)
            if t: t.join()
        if a.light_check: light_check()
        return
    if not DRY_RUN and not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(stop,), daemon=True, name="scheduler").start()
    if DRY_RUN:
        log("DRY_RUN: type commands (e.g. /top) on stdin; nothing is sent to Telegram")
        for line in sys.stdin:
            t = handle_text(ALLOWED[0], line)
        stop.wait()
    else:
        poll_forever()

if __name__ == "__main__":
    main()
