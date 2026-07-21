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
    # LEGACY half-line bets (placed before the European switch) settle two-way forever:
    ("HOME", -1.5, 3, 1, "won"), ("HOME", -1.5, 2, 1, "lost"), ("AWAY", -1.5, 2, 1, "won"),
    ("HOME", -1.5, 2, 0, "won"), ("AWAY", -1.5, 0, 0, "won"),  ("HOME", -2.5, 3, 0, "won"),
    ("HOME", -2.5, 2, 0, "lost"), ("AWAY", 1.5, 0, 1, "lost"), ("HOME", 1.5, 0, 1, "won"),  ("AWAY", -1.5, 0, 1, "won"),
    ("HOME", 1.5, 0, 2, "lost"), ("AWAY", 1.5, 0, 2, "won"),   ("HOME", 2.5, 0, 2, "won"),
    # EUROPEAN whole lines: three outcomes, the margin landing ON the line is the handicap draw
    ("HOME", -1, 2, 0, "won"),  ("DRAW", -1, 2, 1, "won"),  ("AWAY", -1, 1, 1, "won"),
    ("HOME", -1, 2, 1, "lost"), ("DRAW", -1, 3, 1, "lost"), ("AWAY", -1, 2, 0, "lost"),
    ("HOME", 1, 1, 1, "won"),   ("DRAW", 1, 0, 1, "won"),   ("AWAY", 1, 0, 2, "won"),
    ("DRAW", 2, 0, 2, "won"),   ("HOME", -3, 4, 0, "won"),  ("DRAW", -2, 2, 0, "won"),
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
okh, wh = W.place(ws, "Erol", KOM, "HOME", 5, 100, CH, CA, now=NOW, market="hc", line=-1)
oka, wa = W.place(ws, "Erol", KOM, "AWAY", 5, 100, CH, CA, now=NOW, market="hc", line=-1)   # away GETTING 1.5 (the key is the HOME line)
okr, wr = W.place(ws, "Erol", KOM, "HOME", 5, 100, CH, CA, now=NOW)          # result: to advance
level_pens = dict(KOM, status="FINISHED", homeScore=1, awayScore=1, penHome=4, penAway=2, shootout=True)
W.settle(ws, level_pens, now=NOW)
ck("KO level after ET: HOME -1 LOSES even though home advances on pens", wh.get("status") == "lost", wh.get("status"))
ck("KO level after ET: AWAY covers the -1 line (level = margin below it) and WINS", wa.get("status") == "won", wa.get("status"))
ck("the result bet on the same game settles off the shootout (won)", wr.get("status") == "won", wr.get("status"))

print("\n== voids, unfinished games, hostile scores ==")
ws, w = bet("HOME", -1)
W.settle(ws, fin(None, None, status="CANCELLED"), now=NOW)
ck("cancelled game -> void, stake back", w.get("status") == "void" and w.get("return") == w.get("stake"), w)
ws, w = bet("HOME", -1)
W.settle(ws, fin(2, 0, status="IN_PLAY"), now=NOW)
ck("in-play game doesn't settle", w.get("status") == "pending", w.get("status"))
for hs, as_ in ((None, 1), (float("nan"), 0), (float("inf"), 0), (-1, 0), ("x", 0)):
    ws, w = bet("AWAY", 1)
    W.settle(ws, fin(hs, as_), now=NOW)
    ck("hostile score %r-%r stays pending" % (hs, as_), w.get("status") == "pending", w.get("status"))

print("\n== defensive: an hc acca LEG settles on the margin, never as a match-winner pick ==")
leg = {"matchId": W.match_id(BASE), "selection": "HOME", "market": "hc", "line": -1.5,   # a LEGACY half-line leg
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


print("\n== premature-FT guard: a level knockout ticked FINISHED with no decider settles NOTHING, ==")
print("==   wrong settlements auto-reopen, and the real extra-time end settles correctly ==")
PKO = dict(BASE, stage="QUARTER_FINALS", status="FINISHED", homeScore=1, awayScore=1,
           duration="REGULAR", durationKnown=True)
for mkt, sel, ln in (("ou", "UNDER", 2.5), ("hc", "DRAW", -1), ("cs", "1-1", None), ("btts", "YES", None)):
    wl = [{"id": "p" + mkt, "player": "E", "matchId": W.match_id(BASE), "market": mkt, "selection": sel,
           **({"line": ln} if ln is not None else {}), "stake": 5, "num": 1, "den": 1, "frac": "1/1",
           "return": 10, "status": "pending"}]
    W.settle(wl, PKO, now=NOW)
    ck("%s stays PENDING on the premature 90' tick" % mkt, wl[0]["status"] == "pending", wl[0])
wl = [{"id": "wr", "player": "E", "matchId": W.match_id(BASE), "market": "ou", "selection": "UNDER", "line": 2.5,
       "stake": 5, "num": 1, "den": 1, "frac": "1/1", "return": 0, "status": "lost", "result": "lost", "settled_at": NOW}]
ch = W.settle(wl, dict(PKO, status="IN_PLAY"), now=NOW + 60)
ck("a bet wrongly settled at 90' auto-REOPENS when the game runs on (and the pass reports a change to save)",
   wl[0]["status"] == "pending" and wl[0]["return"] == 10.0 and "result" not in wl[0] and ch >= 1, (wl[0], ch))
W.settle(wl, dict(BASE, stage="QUARTER_FINALS", status="FINISHED", homeScore=2, awayScore=1,
                  winner="HOME", aet=True, duration="EXTRA_TIME", durationKnown=True), now=NOW + 7200)
ck("...and the REAL after-extra-time end settles it properly", wl[0]["status"] == "lost", wl[0])
pens_m = dict(BASE, stage="QUARTER_FINALS", status="FINISHED", homeScore=1, awayScore=1,
              penHome=4, penAway=3, shootout=True)
wl2 = [{"id": "ps", "player": "E", "matchId": W.match_id(BASE), "market": "cs", "selection": "1-1",
        "stake": 5, "num": 8, "den": 1, "frac": "8/1", "return": 45, "status": "pending"}]
W.settle(wl2, pens_m, now=NOW)
ck("a GENUINE pens finish (level + shootout evidence) still settles instantly", wl2[0]["status"] == "won", wl2[0])
grp_m = dict(BASE, status="FINISHED", homeScore=1, awayScore=1, winner="DRAW")
wl3 = [{"id": "gr", "player": "E", "matchId": W.match_id(BASE), "market": "ou", "selection": "UNDER", "line": 2.5,
        "stake": 5, "num": 1, "den": 1, "frac": "1/1", "return": 10, "status": "pending"}]
W.settle(wl3, grp_m, now=NOW)
ck("group games still finish level and settle normally (guard is knockout-only)", wl3[0]["status"] == "won", wl3[0])

print()
print("FAILED (%d): %s" % (len(fails), ", ".join(fails)) if fails else "All handicap settlement tests passed.")
import sys as _s
_s.exit(1 if fails else 0)
