#!/usr/bin/env python3
"""Concurrency QA: hammer the free-points claim from many threads at once and prove the
atomic in-lock re-check lets exactly ONE claim through per player per drop. Runs in a
throwaway temp dir with copies of the code + minimal data, so it never touches the repo."""
import os, sys, json, shutil, tempfile, threading

SRC = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="wc26_conc_")
for fn in os.listdir(SRC):
    if fn.endswith(".py"):
        try: shutil.copy(os.path.join(SRC, fn), TMP)
        except Exception: pass
json.dump({"configured": True, "wagering_enabled": True,
           "players": ["Erol", "James", "Louis", "Ismail", "Reuben"],
           "free_bet_claims": {}}, open(os.path.join(TMP, "config.json"), "w"))
json.dump([], open(os.path.join(TMP, "wagers.json"), "w"))
os.environ["WC26_CONFIG"] = os.path.join(TMP, "config.json")
os.chdir(TMP)
sys.path.insert(0, TMP)

import server as S   # import-safe (serving is under __main__)

FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

def hammer(player, drop, n_threads=24):
    results = []
    barrier = threading.Barrier(n_threads)
    lock = threading.Lock()
    def worker():
        barrier.wait()                       # release all at once for maximum contention
        status, _ = S._claim_free_drop(player, drop)
        with lock: results.append(status)
    ts = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in ts: t.start()
    for t in ts: t.join()
    return results

print("=== 24 threads claim the SAME drop for the SAME player at once ===")
res = hammer("Erol", "drop-1")
oks = res.count("ok")
already = res.count("already")
ck("exactly ONE claim succeeds", oks == 1, {"ok": oks, "already": already})
ck("every other thread is told 'already'", already == len(res) - 1, res)
credits = [w for w in json.load(open("wagers.json")) if w.get("credit") and w.get("player") == "Erol" and w.get("drop") == "drop-1"]
ck("wagers.json holds exactly ONE credit (no double points)", len(credits) == 1, len(credits))

print("\n=== a SECOND drop is independently claimable once ===")
res2 = hammer("Erol", "drop-2")
ck("one success on the new drop", res2.count("ok") == 1, res2.count("ok"))
allcred = [w for w in json.load(open("wagers.json")) if w.get("credit") and w.get("player") == "Erol"]
ck("Erol now has exactly 2 credits total (one per drop)", len(allcred) == 2, len(allcred))

print("\n=== different players each get their own single claim, concurrently ===")
def multi():
    out = {}
    lock = threading.Lock()
    barrier = threading.Barrier(15)
    def worker(pl):
        barrier.wait()
        st, _ = S._claim_free_drop(pl, "drop-3")
        with lock: out.setdefault(pl, []).append(st)
    ts = []
    for pl in ["James", "Louis", "Ismail"]:
        for _ in range(5):
            ts.append(threading.Thread(target=worker, args=(pl,)))
    for t in ts: t.start()
    for t in ts: t.join()
    return out
out = multi()
ck("each of 3 players gets exactly one success on drop-3",
   all(v.count("ok") == 1 for v in out.values()) and len(out) == 3, out)

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("\nCONCURRENCY QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll concurrency QA passed — free claims are strictly one-per-player-per-drop under load.")
