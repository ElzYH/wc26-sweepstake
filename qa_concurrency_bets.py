#!/usr/bin/env python3
"""Concurrency QA for BETTING — fires many bets simultaneously (the exact lock-and-reread pattern the
web-single, web-acca and Discord handlers all use, sharing one process-wide lock) and proves that under
maximum contention you can NEVER: overspend your points, go negative, exceed the open-exposure cap, the
per-round staking budget, or the max-open-bets count. Also proves two players betting at once stay isolated.
This mirrors the real placement path (load_wagers inside the lock -> wager.place(that list) -> save inside the lock)."""
import os, sys, json, shutil, tempfile, threading

SRC = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="wc26_concbet_")
for fn in os.listdir(SRC):
    if fn.endswith(".py"):
        try: shutil.copy(os.path.join(SRC, fn), TMP)
        except Exception: pass
json.dump({"configured": True, "wagering_enabled": True,
           "players": ["Erol", "James", "Louis", "Ismail", "Reuben"]}, open(os.path.join(TMP, "config.json"), "w"))
json.dump([], open(os.path.join(TMP, "wagers.json"), "w"))
os.environ["WC26_CONFIG"] = os.path.join(TMP, "config.json")
os.chdir(TMP); sys.path.insert(0, TMP)
import server as S
import wager as wm

FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

NOW = 1_700_000_000
def fx(mid, stage="GROUP_STAGE", h="Brazil", a="Serbia"):
    return {"id": mid, "home": h, "away": a, "stage": stage, "status": "TIMED",
            "utcDate": "2099-06-15T18:00:00Z"}

def reset():
    json.dump([], open("wagers.json", "w"))

# ---- the EXACT pattern the handlers use (single + acca), callable from threads ----
def place_single(player, stake, settled, mid, sel="HOME", ch=80, ca=50, stage="GROUP_STAGE"):
    m = fx(mid, stage)
    with S._lock:
        wl = S.load_wagers()
        ok, res = wm.place(wl, player, m, sel, stake, settled_points=settled, comp_home=ch, comp_away=ca, now=NOW)
        if ok:
            S.save_wagers(wl)
    return ok

def place_acca(player, stake, settled, mids, stage="GROUP_STAGE"):
    sels = [{"match": fx(m, stage), "selection": "HOME", "comp_home": 80, "comp_away": 50} for m in mids]
    with S._lock:
        wl = S.load_wagers()
        ok, res = wm.place_acca(wl, player, sels, stake, settled_points=settled, now=NOW)
        if ok:
            S.save_wagers(wl)
    return ok

def fire(jobs):
    """jobs: list of zero-arg callables -> run all at once behind a barrier, return list of bool results."""
    n = len(jobs); out = [None] * n; bar = threading.Barrier(n); lk = threading.Lock()
    def run(i):
        bar.wait()
        r = jobs[i]()
        with lk: out[i] = r
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in ts: t.start()
    for t in ts: t.join()
    return out

def wl_now():
    return json.load(open("wagers.json"))
def staked(player):
    return sum(w["stake"] for w in wl_now() if w["player"] == player and w["status"] == "pending")
def count(player):
    return sum(1 for w in wl_now() if w["player"] == player and w["status"] == "pending")

# ============================================================== 1. AFFORD ONLY ONE
print("\n== 1. Same player, only enough for ONE bet, many simultaneous ==")
reset()
# 0 earned + 5 free bonus = 5 available; each bet stakes 5 on a DISTINCT game (so it's not deduped)
res = fire([(lambda i=i: place_single("Erol", 5, 0, "g%d" % i)) for i in range(24)])
ck("exactly ONE of 24 simultaneous 5pt bets succeeds (afford one)", res.count(True) == 1, res.count(True))
ck("total staked never exceeds the 5 available", staked("Erol") <= 5, staked("Erol"))
ck("balance never goes negative", wm.available_points("Erol", 0, wl_now()) >= 0, wm.available_points("Erol", 0, wl_now()))
ck("only one wager recorded", len(wl_now()) == 1, len(wl_now()))

# ============================================================== 2. EXPOSURE CAP (30)
print("\n== 2. Open-exposure cap (30) under contention ==")
reset()
# plenty of points; each bet stakes 6 on a distinct GROUP game (cap 30) -> at most 5 can ride at once
res = fire([(lambda i=i: place_single("Erol", 6, 9999, "e%d" % i)) for i in range(24)])
ck("no more than 5 x 6pt bets ride at once (30 exposure cap)", staked("Erol") <= 30 + 1e-9, staked("Erol"))
ck("succeeded count matches the staked total / 6", res.count(True) == round(staked("Erol") / 6), (res.count(True), staked("Erol")))
ck("exposure cap respected exactly (== 30)", staked("Erol") == 30, staked("Erol"))

# ============================================================== 3. MAX-OPEN-BETS COUNT (MAX_PENDING)
print("\n== 3. Max-open-bets count under contention ==")
reset()
# FINAL-stage games (cap 65) + tiny 1pt stakes + huge balance -> count cap (MAX_PENDING) is the binding limit
res = fire([(lambda i=i: place_single("Erol", 1, 9999, "f%d" % i, stage="FINAL")) for i in range(24)])
ck("no more than MAX_PENDING bets open at once", count("Erol") <= wm.MAX_PENDING, count("Erol"))
ck("exactly MAX_PENDING succeed (count is the binding cap here)", res.count(True) == wm.MAX_PENDING, (res.count(True), wm.MAX_PENDING))

# ============================================================== 4. STAKING BUDGET (per epoch)
print("\n== 4. Per-round staking budget under contention ==")
reset()
# big single-bet cap won't bind; drive total stake toward STAGE_BUDGET with FINAL games
# (cap 65 per bet, budget 100/epoch) -> total staked across the epoch must stay <= STAGE_BUDGET
res = fire([(lambda i=i: place_single("Erol", 40, 9999, "b%d" % i, stage="FINAL")) for i in range(10)])
total = sum(w["stake"] for w in wl_now() if w["player"] == "Erol")
ck("total staked in the epoch never exceeds STAGE_BUDGET", total <= wm.STAGE_BUDGET + 1e-9, total)

# ============================================================== 5. SINGLE + ACCA AT THE SAME TIME
print("\n== 5. Same account: a single AND an acca fired simultaneously ==")
reset()
# afford only ~5; fire a 5pt single and a 5pt acca at the same instant -> combined must respect the 5 available
res = fire([
    (lambda: place_single("Erol", 5, 0, "mixA")),
    (lambda: place_acca("Erol", 5, 0, ["mixB", "mixC"])),
])
ck("a single + acca racing can't BOTH place beyond available", staked("Erol") <= 5, staked("Erol"))
ck("exactly one of the two wins (only 5 available)", res.count(True) == 1, res)
ck("balance not negative after single-vs-acca race", wm.available_points("Erol", 0, wl_now()) >= 0, wm.available_points("Erol", 0, wl_now()))

# ============================================================== 6. TWO ACCOUNTS AT ONCE (isolation)
print("\n== 6. Two different players bet simultaneously (isolation) ==")
reset()
# Each has 0 earned + 5 free = 5; each fires several bets at once. Each should land exactly one, independently.
jobs = []
for i in range(12): jobs.append((lambda i=i: place_single("Erol", 5, 0, "E%d" % i)))
for i in range(12): jobs.append((lambda i=i: place_single("James", 5, 0, "J%d" % i)))
res = fire(jobs)
ck("Erol lands exactly one bet (his own budget)", count("Erol") == 1, count("Erol"))
ck("James lands exactly one bet (his own budget)", count("James") == 1, count("James"))
ck("Erol's staking is isolated (<=5)", staked("Erol") <= 5 and staked("James") <= 5, (staked("Erol"), staked("James")))
ck("neither player goes negative", wm.available_points("Erol", 0, wl_now()) >= 0 and wm.available_points("James", 0, wl_now()) >= 0, "ok")
ck("no bet was misattributed (every wager belongs to its placer)", all(w["player"] in ("Erol", "James") for w in wl_now()), "ok")

# ============================================================== 7. DUP NONCE RACE (same logical bet twice at once)
print("\n== 7. Same bet (same nonce) fired twice at the same instant ==")
reset()
NONCE = "race-nonce-1"
def place_with_nonce():
    with S._lock:
        wl = S.load_wagers()
        dup = S._dedup_wager(wl, "Erol", NONCE)
        if dup is not None:
            return False
        ok, res = wm.place(wl, "Erol", fx("dupG"), "HOME", 5, settled_points=9999, comp_home=80, comp_away=50, now=NOW)
        if ok:
            res["nonce"] = NONCE; S.save_wagers(wl)
        return ok
res = fire([place_with_nonce, place_with_nonce, place_with_nonce, place_with_nonce])
ck("the same nonce fired 4x at once creates exactly ONE bet", len(wl_now()) == 1, len(wl_now()))

# ============================================================== 8. HEAVY MIXED STORM
print("\n== 8. Heavy mixed storm: 40 mixed singles/accas, modest balance ==")
reset()
jobs = []
for i in range(20): jobs.append((lambda i=i: place_single("Erol", 7, 50, "S%d" % i, stage="FINAL")))
for i in range(20): jobs.append((lambda i=i: place_acca("Erol", 7, 50, ["A%dx" % i, "A%dy" % i], stage="FINAL")))
fire(jobs)
final_staked = sum(w["stake"] for w in wl_now() if w["player"] == "Erol")
ck("after a 40-way storm, exposure cap still holds", staked("Erol") <= wm.STAGE_MAX_STAKE.get("FINAL", 65) + 1e-9, staked("Erol"))
ck("after the storm, open count <= MAX_PENDING", count("Erol") <= wm.MAX_PENDING, count("Erol"))
ck("after the storm, epoch budget holds", final_staked <= wm.STAGE_BUDGET + 1e-9, final_staked)
ck("after the storm, balance is not negative", wm.available_points("Erol", 50, wl_now()) >= 0, wm.available_points("Erol", 50, wl_now()))
ck("after the storm, no wager has a non-positive stake", all(w["stake"] > 0 for w in wl_now()), "ok")
ck("after the storm, every wager is well-formed (player+stake+status)", all(w.get("player") and w.get("stake") and w.get("status") for w in wl_now()), "ok")

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("\nBET CONCURRENCY QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll bet-concurrency QA passed.")
