"""Dry-run test of the 30-min alert cycle with two forced transitions (never touches Telegram).
Runs on a COPY of the dry-run data dir. Real Dexscreener prices; zones for two coins are moved so that
one coin 'enters' its entry zone and another 'breaks' invalidation. Then the cycle runs again to show the
24h dedupe. Usage: DRY_RUN=1 python test_alerts_dryrun.py ./data_dryrun"""
import os, shutil, sys
src = sys.argv[1] if len(sys.argv) > 1 else "./data_dryrun"
dst = src.rstrip("/") + "_alerttest"
shutil.rmtree(dst, ignore_errors=True); shutil.copytree(src, dst)
os.environ["DATA_DIR"] = dst; os.environ["DRY_RUN"] = "1"
import bot
assert bot.DRY_RUN
bot.init_db()
with bot.db() as con:
    rows = [dict(r) for r in con.execute("select * from watchlist order by symbol")]
prices = bot.live_prices(rows)
have = [r for r in rows if (r["chain"], r["addr"]) in prices]
a, b = have[0], have[1]
pa, pb = prices[(a["chain"], a["addr"])][0], prices[(b["chain"], b["addr"])][0]
print(f"\n### FORCED SETUP: ${a['symbol']} zone moved around live price {pa} with prior state 'above'; "
      f"${b['symbol']} invalidation moved above live price {pb} with prior state 'zone'\n", flush=True)
with bot.db() as con:
    con.execute("update watchlist set lo=?, hi=?, inval=?, state='above' where addr=?", (pa * 0.95, pa * 1.05, pa * 0.8, a["addr"]))
    con.execute("update watchlist set lo=?, hi=?, inval=?, state='zone' where addr=?", (pb * 1.2, pb * 1.3, pb * 1.1, b["addr"]))
print("### CYCLE 1 (expect 2 alerts)", flush=True)
s1 = bot.light_check()
with bot.db() as con:
    con.execute("update watchlist set state='above' where addr=?", (a["addr"],))   # pretend it left and came back
    con.execute("update watchlist set state='zone' where addr=?", (b["addr"],))
print("\n### CYCLE 2, same transitions again within 24h (expect 0 alerts: deduped)", flush=True)
s2 = bot.light_check()
print(f"\nRESULT: cycle1 alerts={s1} cycle2 alerts={s2}", "PASS" if len(s1) == 2 and not s2 else "FAIL")
