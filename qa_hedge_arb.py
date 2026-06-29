#!/usr/bin/env python3
"""QA for the anti-hedge work:
 (A) knockout result markets are a 2-way 'to advance' book with a real house edge (Home+Away sum > 100%),
     so backing both sides can never be a risk-free arb — even for extreme favourites that hit the price cap;
 (B) a player can't hold result bets on two different outcomes of the SAME match (singles AND acca legs),
     while same-side re-backs, the O/U market, other players and other matches are all unaffected.
Pure-logic; no network."""
import time, sys
import wager as W

FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        FAILS.append(name)

NOW = 1_700_000_000
FUT = NOW + 86_400
def M(home, away, mid, stage="GROUP_STAGE"):
    return {"id": mid, "home": home, "away": away, "stage": stage, "status": "TIMED",
            "utcDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FUT)),
            "homeScore": None, "awayScore": None, "winner": None}

def book(decimals):
    return sum(1.0 / d for d in decimals)

# ---------------------------------------------------------------- (A) knockout 2-way overround
print("=== (A) knockout result market is a margined 2-way book (no risk-free both-sides) ===")
mismatches = [(50, 50), (60, 50), (90, 50), (120, 40), (200, 30), (1000, 1), (1, 1000)]
all_ko_edge = True
for ch, ca in mismatches:
    ko = W.match_odds(ch, ca, knockout=True)
    ck("KO book(%s,%s) has HOME+AWAY, no DRAW" % (ch, ca), ("HOME" in ko and "AWAY" in ko and "DRAW" not in ko), sorted(ko))
    b = book([ko["HOME"]["decimal"], ko["AWAY"]["decimal"]])
    if not (b > 1.0 + 1e-9):
        all_ko_edge = False
    ck("KO book(%s,%s) overround > 100%% (=%.1f%%)" % (ch, ca, b * 100), b > 1.0 + 1e-9, round(b, 4))
ck("EVERY knockout 2-way book carries a house edge (incl. extreme favourites)", all_ko_edge)

# the concrete leak from the live site: heavy favourite + longshot must NOT sum under 100%
ko = W.match_odds(300, 30, knockout=True)
ck("heavy-favourite KO (Germany-style) book > 100%", book([ko["HOME"]["decimal"], ko["AWAY"]["decimal"]]) > 1.0)

# group games are UNCHANGED: still a 3-way book with a draw and an edge
print("=== group (3-way) book is unchanged: keeps the draw + its edge ===")
for ch, ca in [(50, 50), (90, 50), (120, 40)]:
    g = W.match_odds(ch, ca, knockout=False)
    ck("group book(%s,%s) keeps HOME/DRAW/AWAY" % (ch, ca), all(k in g for k in ("HOME", "DRAW", "AWAY")), sorted(g))
    ck("group book(%s,%s) 3-way overround > 100%%" % (ch, ca),
       book([g["HOME"]["decimal"], g["DRAW"]["decimal"], g["AWAY"]["decimal"]]) > 1.0)
ck("default (no knockout arg) == group 3-way", "DRAW" in W.match_odds(90, 50))

# ---------------------------------------------------------------- (B) no backing both sides of one match
print("\n=== (B) a player can't back two different outcomes of the SAME match ===")
C = (90, 50)
g1 = M("Germany", "Paraguay", "g1", stage="LAST_16")
g2 = M("Brazil", "Japan", "g2", stage="LAST_16")

wl = []
ok, _ = W.place(wl, "Ismail", g1, "HOME", 5, 1000, C[0], C[1], now=NOW)
ck("first single (Germany) places", ok, _)
ok, e = W.place(wl, "Ismail", g1, "AWAY", 5, 1000, C[0], C[1], now=NOW)
ck("opposite single (Paraguay) on same match is REJECTED", not ok, e)
ok, _ = W.place(wl, "Ismail", g1, "HOME", 3, 1000, C[0], C[1], now=NOW)
ck("same-side re-back (Germany again) is allowed", ok)
ok, _ = W.place(wl, "Ismail", g1, "OVER", 5, 1000, C[0], C[1], now=NOW, market="ou", line=2.5)
ck("an Over/Under bet on that match is allowed (different market, margin-protected)", ok)
ok, _ = W.place(wl, "Louis", g1, "AWAY", 5, 1000, C[0], C[1], now=NOW)
ck("a DIFFERENT player can back the other side", ok)
ok, _ = W.place(wl, "Ismail", g2, "AWAY", 5, 1000, C[0], C[1], now=NOW)
ck("a different match is unaffected", ok)

# opposing via an ACCA leg
wl2 = []
W.place(wl2, "Reuben", g1, "HOME", 5, 1000, C[0], C[1], now=NOW)   # single on Germany
legs = [{"match": g1, "selection": "AWAY", "comp_home": C[0], "comp_away": C[1]},
        {"match": g2, "selection": "HOME", "comp_home": C[0], "comp_away": C[1]}]
ok, e = W.place_acca(wl2, "Reuben", legs, 5, 1000, now=NOW)
ck("an acca leg on the opposite side of an open single is REJECTED", not ok, e)
legs_ok = [{"match": g1, "selection": "HOME", "comp_home": C[0], "comp_away": C[1]},
           {"match": g2, "selection": "HOME", "comp_home": C[0], "comp_away": C[1]}]
ok, _ = W.place_acca(wl2, "Reuben", legs_ok, 5, 1000, now=NOW)
ck("an acca that re-backs the SAME sides is allowed", ok)

# single opposing an existing acca leg
wl3 = []
W.place_acca(wl3, "James", [{"match": g1, "selection": "HOME", "comp_home": C[0], "comp_away": C[1]},
                            {"match": g2, "selection": "HOME", "comp_home": C[0], "comp_away": C[1]}], 5, 1000, now=NOW)
ok, e = W.place(wl3, "James", g1, "AWAY", 5, 1000, C[0], C[1], now=NOW)
ck("a single on the opposite side of an open acca leg is REJECTED", not ok, e)

# free bet opposing an existing real bet
wl4 = []
W.place(wl4, "Erol", g1, "HOME", 5, 1000, C[0], C[1], now=NOW)
ok, e = W.place_free(wl4, "Erol", g1, "AWAY", C[0], C[1], now=NOW)
ck("a FREE bet on the opposite side is REJECTED", not ok, e)
ok, _ = W.place_free(wl4, "Erol", g2, "HOME", C[0], C[1], now=NOW)
ck("a free bet on a different match is allowed", ok)

# settled bets don't block — only OPEN ones
wl5 = []
W.place(wl5, "Nat", g1, "HOME", 5, 1000, C[0], C[1], now=NOW)
wl5[0]["status"] = "lost"   # simulate it settled
ok, _ = W.place(wl5, "Nat", g1, "AWAY", 5, 1000, C[0], C[1], now=NOW)
ck("a SETTLED bet on one side doesn't block the other later", ok)

# ---------------------------------------------------------------- (B) admin on/off toggle
print("\n=== (B) is switchable via BLOCK_OPPOSING_BETS (admin toggle) ===")
_saved = W.BLOCK_OPPOSING_BETS
try:
    W.BLOCK_OPPOSING_BETS = False
    wlt = []
    W.place(wlt, "Ismail", g1, "HOME", 5, 1000, C[0], C[1], now=NOW)
    ok, _ = W.place(wlt, "Ismail", g1, "AWAY", 5, 1000, C[0], C[1], now=NOW)
    ck("toggle OFF -> opposite single is allowed again", ok)
    ok, _ = W.place_free(wlt, "Ismail", g1, "AWAY", C[0], C[1], now=NOW)
    ck("toggle OFF -> opposite free bet is allowed again", ok)
    ok, _ = W.place_acca(wlt, "Ismail", [{"match": g1, "selection": "AWAY", "comp_home": C[0], "comp_away": C[1]},
                                         {"match": g2, "selection": "HOME", "comp_home": C[0], "comp_away": C[1]}], 5, 1000, now=NOW)
    ck("toggle OFF -> opposite acca leg is allowed again", ok)
    W.BLOCK_OPPOSING_BETS = True
    wlt2 = []
    W.place(wlt2, "Ismail", g1, "HOME", 5, 1000, C[0], C[1], now=NOW)
    ok, _ = W.place(wlt2, "Ismail", g1, "AWAY", 5, 1000, C[0], C[1], now=NOW)
    ck("toggle ON again -> opposite single is blocked", not ok)
finally:
    W.BLOCK_OPPOSING_BETS = _saved

print("\n" + ("FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)) if FAILS else "All anti-hedge / anti-arb checks passed."))
sys.exit(1 if FAILS else 0)
