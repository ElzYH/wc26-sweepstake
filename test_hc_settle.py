#!/usr/bin/env python3
"""Handicap settlement QA — settle() for market='hc'. Golden margin vectors on both signs of line,
the 90'+ET basis (a level knockout score after ET loses HOME -1.5 even when a shootout sends home
through — while the RESULT bet on the same match settles off the shootout), voids, unfinished games,
hostile scores, won-return arithmetic, the defensive acca-leg branch (an hc leg can never be settled
as a match-winner pick) — and proof result + O/U settlement are untouched."""
import wager as W

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

NOW = 1_700_000_000
BASE = {"home": "Brazil", "away": "Serbia", "stage": "GROUP_STAGE", "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"}
CH, CA = 80, 60

def bet(sel, line, stake=5):
    # settlement is driven by the STORED record (line, selection, locked odds) — build one directly so a
    # ladder-filtered line at these comps can't block a golden vector; placement pricing has its own suite
    w = {"id": "t-%s-%s" % (sel, line), "player": "Erol", "matchId": W.match_id(BASE),
         "home": BASE["home"], "away": BASE["away"], "stage": BASE["stage"], "utcDate": BASE["utcDate"],
         "selection": sel, "stake": stake, "epoch": 0, "num": 3, "den": 1, "frac": "3/1",
         "return": W.potential_return(stake, 3, 1), "status": "pending", "placed_at": NOW,
         "market": "hc", "line": line}
    return [w], w

def fin(hs, as_, status="FINISHED", **kw):
    m = dict(BASE, status=status, homeScore=hs, awayScore=as_)
    m.update(kw)
    return m

print("== golden margin vectors (line applies to the HOME side; half-lines never push) ==")
GOLDEN = [
    ("HOME", -1.5, 3, 1, "won"), ("HOME", -1.5, 2, 1, "lost"), ("AWAY", -1.5, 2, 1, "won"),
    ("HOME", -1.5, 2, 0, "won"), ("AWAY", -1.5, 0, 0, "won"),  ("HOME", -2.5, 3, 0, "won"),
    ("HOME", -2.5, 2, 0, "lost"), ("AWAY", 1.5, 0, 1, "lost"), ("HOME", 1.5, 0, 1, "won"),  ("AWAY", -1.5, 0, 1, "won"),
    ("HOME", 1.5, 0, 2, "lost"), ("AWAY", 1.5, 0, 2, "won"),   ("HOME", 2.5, 0, 2, "won"),
    ("AWAY", 2.5, 0, 3, "won"),  ("HOME", 2.5, 0, 3, "lost"),  ("AWAY", -2.5, 5, 3, "won"),
]
for sel, line, hs, as_, want in GOLDEN:
    ws, w = bet(sel, line)
    W.settle(ws, fin(hs, as_), now=NOW)
    ck("%s %+g on %d-%d -> %s" % (sel, line, hs, as_, want), w.get("status") == want, w.get("status"))
    if want == "won":
        ck("  won return == stake x odds", abs(w["return"] - W.potential_return(w["stake"], w["num"], w["den"])) < 1e-9, w["return"])
    else:
        ck("  lost return == 0", w.get("return") == 0, w.get("return"))

print("\n== knockout basis: 90'+ET margin, shootout excluded ==")
KOM = dict(BASE, stage="QUARTER_FINALS")
ws = []
okh, wh = W.place(ws, "Erol", KOM, "HOME", 5, 100, CH, CA, now=NOW, market="hc", line=-1.5)
oka, wa = W.place(ws, "Erol", KOM, "AWAY", 5, 100, CH, CA, now=NOW, market="hc", line=-1.5)   # away GETTING 1.5 (the key is the HOME line)
okr, wr = W.place(ws, "Erol", KOM, "HOME", 5, 100, CH, CA, now=NOW)          # result: to advance
level_pens = dict(KOM, status="FINISHED", homeScore=1, awayScore=1, penHome=4, penAway=2, shootout=True)
W.settle(ws, level_pens, now=NOW)
ck("KO level after ET: HOME -1.5 LOSES even though home advance on pens", wh.get("status") == "lost", wh.get("status"))
ck("KO level after ET: AWAY +1.5 start WINS", wa.get("status") == "won", wa.get("status"))
ck("the result bet on the same game settles off the shootout (won)", wr.get("status") == "won", wr.get("status"))

print("\n== voids, unfinished games, hostile scores ==")
ws, w = bet("HOME", -1.5)
W.settle(ws, fin(None, None, status="CANCELLED"), now=NOW)
ck("cancelled game -> void, stake back", w.get("status") == "void" and w.get("return") == w.get("stake"), w)
ws, w = bet("HOME", -1.5)
W.settle(ws, fin(2, 0, status="IN_PLAY"), now=NOW)
ck("in-play game doesn't settle", w.get("status") == "pending", w.get("status"))
for hs, as_ in ((None, 1), (float("nan"), 0), (float("inf"), 0), (-1, 0), ("x", 0)):
    ws, w = bet("AWAY", 1.5)
    W.settle(ws, fin(hs, as_), now=NOW)
    ck("hostile score %r-%r stays pending" % (hs, as_), w.get("status") == "pending", w.get("status"))

print("\n== defensive: an hc acca LEG settles on the margin, never as a match-winner pick ==")
leg = {"matchId": W.match_id(BASE), "selection": "HOME", "market": "hc", "line": -1.5,
       "home": "Brazil", "away": "Serbia", "stage": "GROUP_STAGE", "num": 3, "den": 1, "frac": "3/1"}
acca = {"id": "x1", "player": "Erol", "stake": 5, "status": "pending", "legs": [leg], "return": 20}
ws = [acca]
W.settle(ws, fin(2, 1), now=NOW)   # home WON the match, but only by 1 -> the -1.5 leg must LOSE
ck("hand-crafted hc leg on 2-1: leg result is 'lost' (a fallthrough would say won)", leg.get("result") == "lost", leg)
ck("the acca settles lost accordingly", acca.get("status") == "lost", acca.get("status"))

print("\n== other markets settle exactly as before (regression) ==")
ws = []
ok1, w1 = W.place(ws, "Erol", BASE, "HOME", 5, 100, CH, CA, now=NOW)
ok2, w2 = W.place(ws, "Erol", BASE, "OVER", 5, 100, CH, CA, now=NOW, market="ou", line=2.5)
ok3, w3 = W.place(ws, "Erol", BASE, "2-1", 5, 100, CH, CA, now=NOW, market="cs")
W.settle(ws, fin(2, 1), now=NOW)
ck("result HOME on 2-1 still wins", w1.get("status") == "won", w1.get("status"))
ck("OVER 2.5 on 2-1 still wins", w2.get("status") == "won", w2.get("status"))
ck("exact score 2-1 still wins", w3.get("status") == "won", w3.get("status"))

print()
if fails:
    print("FAILED: %d -> %s" % (len(fails), fails))
    raise SystemExit(1)
print("ALL HANDICAP SETTLEMENT CHECKS PASSED")
