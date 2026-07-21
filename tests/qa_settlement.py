#!/usr/bin/env python3
"""Settlement QA against real football outcomes: full time, extra time, penalty shootouts,
abandoned/postponed, and glitchy knockout data. Covers singles and accumulators.
Mirrors how the live feed labels games (winner already reflects the shootout; homeScore/awayScore
are on-field goals only)."""
import time, sys
import wager as W

FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

NOW = 1_700_000_000
FUT = NOW + 86_400
C = (90, 50)

def M(home, away, mid, stage="GROUP_STAGE"):
    return {"id": mid, "home": home, "away": away, "stage": stage, "status": "TIMED",
            "utcDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FUT))}

def result(home, away, mid, stage, hs, as_, winner=None, status="FINISHED", penH=None, penA=None):
    return {"id": mid, "home": home, "away": away, "stage": stage, "status": status,
            "homeScore": hs, "awayScore": as_, "winner": winner, "penHome": penH, "penAway": penA,
            "utcDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FUT))}

def bet(player, home, away, mid, sel, stage="GROUP_STAGE"):
    wl = []
    ok, w = W.place(wl, player, M(home, away, mid, stage), sel, 10, 1000, *C, now=NOW)
    assert ok, w
    return wl

print("=== FULL TIME ===")
wl = bet("E", "A", "B", 1, "HOME"); W.settle(wl, result("A","B",1,"GROUP_STAGE",2,0,"HOME"), now=NOW)
ck("FT home win -> HOME bet wins", wl[0]["status"] == "won", wl[0]["status"])
wl = bet("E", "A", "B", 1, "AWAY"); W.settle(wl, result("A","B",1,"GROUP_STAGE",2,0,"HOME"), now=NOW)
ck("FT home win -> AWAY bet loses", wl[0]["status"] == "lost", wl[0]["status"])
wl = bet("E", "A", "B", 1, "DRAW"); W.settle(wl, result("A","B",1,"GROUP_STAGE",1,1,"DRAW"), now=NOW)
ck("FT draw (group) -> DRAW bet wins", wl[0]["status"] == "won", wl[0]["status"])
wl = bet("E", "A", "B", 1, "HOME"); W.settle(wl, result("A","B",1,"GROUP_STAGE",1,1,"DRAW"), now=NOW)
ck("FT draw (group) -> HOME bet loses", wl[0]["status"] == "lost", wl[0]["status"])

print("\n=== EXTRA TIME (knockout, decided in ET) ===")
wl = bet("E", "A", "B", 2, "HOME", "SEMI_FINAL"); W.settle(wl, result("A","B",2,"SEMI_FINAL",2,1,"HOME"), now=NOW)
ck("ET 2-1 home -> HOME bet wins", wl[0]["status"] == "won", wl[0]["status"])
wl = bet("E", "A", "B", 2, "AWAY", "SEMI_FINAL"); W.settle(wl, result("A","B",2,"SEMI_FINAL",2,1,"HOME"), now=NOW)
ck("ET 2-1 home -> AWAY bet loses", wl[0]["status"] == "lost", wl[0]["status"])

print("\n=== PENALTY SHOOTOUT (level on field, winner via pens) ===")
# feed gives the shootout winner in `winner`; on-field score stays level
wl = bet("E", "A", "B", 3, "AWAY", "FINAL"); W.settle(wl, result("A","B",3,"FINAL",1,1,"AWAY",penH=4,penA=5), now=NOW)
ck("shootout: bet on the side that advances (AWAY) WINS", wl[0]["status"] == "won", wl[0]["status"])
wl = bet("E", "A", "B", 3, "HOME", "FINAL"); W.settle(wl, result("A","B",3,"FINAL",1,1,"AWAY",penH=4,penA=5), now=NOW)
ck("shootout: bet on the side that loses pens LOSES (not void)", wl[0]["status"] == "lost", wl[0]["status"])
# winner field missing but pens present -> resolve from pens
wl = bet("E", "A", "B", 3, "HOME", "FINAL"); W.settle(wl, result("A","B",3,"FINAL",0,0,None,penH=5,penA=3), now=NOW)
ck("shootout w/o winner field: pens resolve it (HOME 5-3) -> HOME wins", wl[0]["status"] == "won", wl[0]["status"])

print("\n=== ABANDONED / POSTPONED / CANCELLED -> void (refund) ===")
for st in ("ABANDONED", "POSTPONED", "CANCELLED"):
    wl = bet("E", "A", "B", 4, "HOME"); W.settle(wl, result("A","B",4,"GROUP_STAGE",0,0,None,status=st), now=NOW)
    ck("%s -> bet voided (stake refunded)" % st, wl[0]["status"] == "void", wl[0]["status"])

print("\n=== GLITCH GUARD: a knockout that resolves 'level' with no winner/pens must NOT settle ===")
wl = bet("E", "A", "B", 5, "HOME", "QUARTER_FINAL")
W.settle(wl, result("A","B",5,"QUARTER_FINAL",1,1,None), now=NOW)   # no winner, no pens -> bad data
ck("KO level w/ no winner/pens -> stays pending (not wrongly lost)", wl[0]["status"] == "pending", wl[0]["status"])
# once the real data lands (winner present), it settles correctly
W.settle(wl, result("A","B",5,"QUARTER_FINAL",1,1,"HOME",penH=4,penA=2), now=NOW)
ck("…then settles correctly when winner/pens arrive", wl[0]["status"] == "won", wl[0]["status"])

print("\n=== ACCUMULATOR across mixed outcomes ===")
def leg(h,a,mid,sel,stage="GROUP_STAGE"):
    return {"match": M(h,a,mid,stage), "selection": sel, "comp_home": 90, "comp_away": 50}
wl = []
ok, acca = W.place_acca(wl, "E", [leg("A","B",1,"HOME"), leg("C","D",2,"AWAY","SEMI_FINAL")], 5, 1000, now=NOW)
assert ok, acca
W.settle(wl, result("A","B",1,"GROUP_STAGE",3,0,"HOME"), now=NOW)              # leg1 win (FT)
W.settle(wl, result("C","D",2,"SEMI_FINAL",1,1,"AWAY",penH=2,penA=4), now=NOW) # leg2 win (pens)
ck("acca: FT-win + shootout-win legs -> acca WON", wl[0]["status"] == "won" and wl[0]["return"] > 5, (wl[0]["status"], wl[0].get("return")))
# acca where a knockout leg is abandoned -> that leg voids, rest carry
wl = []
ok, acca = W.place_acca(wl, "E", [leg("A","B",1,"HOME"), leg("C","D",2,"HOME","FINAL")], 5, 1000, now=NOW)
W.settle(wl, result("A","B",1,"GROUP_STAGE",2,0,"HOME"), now=NOW)
W.settle(wl, result("C","D",2,"FINAL",0,0,None,status="ABANDONED"), now=NOW)
ck("acca: an abandoned leg drops out, the rest still settle the acca", wl[0]["status"] == "won", (wl[0]["status"], wl[0].get("return")))

if FAILS:
    print("\nSETTLEMENT QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll settlement QA passed — FT, ET, penalties, abandonment and glitchy data all handled.")
