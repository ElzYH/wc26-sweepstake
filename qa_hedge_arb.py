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

# ---------------------------------------------------------------- (C) O/U lines can't be farmed
print("\n=== (C) Over/Under lines are only offered when NEITHER side beats the price ladder ===")
grid = [(50, 50), (60, 50), (90, 50), (95, 55), (120, 40), (200, 30), (300, 30), (1000, 1), (30, 300)]
farm_free = True
for ch, ca in grid:
    lam = W.expected_goals(ch, ca)
    offered = W.goals_odds(ch, ca)
    ck("some O/U lines still offered (%s,%s)" % (ch, ca), len(offered) >= 2, sorted(offered))
    for key in offered:
        n = int(float(key))
        p_under = W._poisson_cdf(n, lam)
        p_over = 1.0 - p_under
        if p_under > W.OU_MAX_PROB + 1e-9 or p_over > W.OU_MAX_PROB + 1e-9:
            farm_free = False
            ck("offered line %s on (%s,%s) has no capped-value side" % (key, ch, ca), False, (p_under, p_over))
        b = 1.0 / offered[key]["OVER"]["decimal"] + 1.0 / offered[key]["UNDER"]["decimal"]
        if not b > 1.0:
            farm_free = False
            ck("offered line %s on (%s,%s) overrounds" % (key, ch, ca), False, b)
ck("NO offered O/U selection anywhere has fair probability above its deep-ladder cap (nothing to farm)", farm_free)
o = W.goals_odds(95, 55)
ck("a near-certain Under (4.5) pays WORSE than the favourites' 1/6 floor — likelier can't share the price",
   "4.5" in o and o["4.5"]["UNDER"]["decimal"] < 1.0 + 1.0/6 - 1e-6, o.get("4.5", {}).get("UNDER"))
_off = [L for L in W.OU_LINES if W._line_key(L) not in W.goals_odds(300, 30)]
ck("any rule-filtered line is rejected at placement (dynamic)",
   (not _off) or (not W.place([], "Erol", M("Argentina", "Switzerland", "q1", stage="QUARTER_FINALS"),
   "UNDER", 5, 1000, 300, 30, now=NOW, market="ou", line=_off[0])[0]), _off)

# ---------------------------------------------------------------- (D) exact-score market: balanced, no exploits
print("\n=== (D) exact-score book is margin-heavy, partitioned, and has no punter-positive cell ===")
cs_ok = True
for ch, ca in [(50, 50), (95, 55), (200, 30), (30, 200), (1000, 1)]:
    o = W.cs_odds(ch, ca)
    lam = W.expected_goals(ch, ca); ph, pd, pa = W._fair_probs(ch, ca)
    share = min(0.85, max(0.15, ph + pd / 2.0)); lh, la = lam * share, lam * (1 - share)
    book = sum(1.0 / v["decimal"] for v in o.values())
    if not (1.12 <= book <= 1.45):
        cs_ok = False; ck("cs book (%s,%s) in the margin band" % (ch, ca), False, book)
    fair_sum = 0.0
    for k, v in o.items():
        if k == "OTHER":
            fair = max(0.0, 1.0 - fair_sum)
        else:
            h, a = k.split("-"); fair = W._poisson_pmf(int(h), lh) * W._poisson_pmf(int(a), la); fair_sum += fair
        if (1.0 / v["decimal"]) < fair * 0.995:                       # punter edge on a cell = exploit
            cs_ok = False; ck("cs cell %s (%s,%s) is never punter-positive" % (k, ch, ca), False, (fair, v["decimal"]))
    if "OTHER" not in o or len(o) != (W.CS_GRID_MAX + 1) ** 2 + 1:
        cs_ok = False; ck("cs grid partitions all outcomes (%s,%s)" % (ch, ca), False, len(o))
ck("every cs cell across the grid carries house margin (dutching all cells guarantees a loss)", cs_ok)
g1cs = M("France", "Morocco", "cs1", stage="QUARTER_FINALS")
wl = []
ok, w = W.place(wl, "Erol", g1cs, "2-1", 5, 1000, 95, 55, now=NOW, market="cs")
ck("a scoreline places as a single with locked odds", ok and w.get("market") == "cs" and w.get("frac"), w if not ok else None)
ck("garbage scorelines are rejected", not W.place([], "Erol", g1cs, "12-0", 5, 1000, 95, 55, now=NOW, market="cs")[0]
   and not W.place([], "Erol", g1cs, "2:1", 5, 1000, 95, 55, now=NOW, market="cs")[0], None)
ck("cs can't ride in an accumulator", not W.place_acca([], "Erol",
   [{"match": g1cs, "selection": "2-1", "market": "cs", "comp_home": 95, "comp_away": 55},
    {"match": g2, "selection": "HOME", "comp_home": 95, "comp_away": 55}], 5, 1000, now=NOW)[0], None)
ck("cs alongside a result bet is allowed (mutually-exclusive cells, margin-protected — not a hedge)",
   W.place(wl, "Erol", g1cs, "HOME", 5, 1000, 95, 55, now=NOW)[0], None)
import copy as _cp
mfin = dict(g1cs, status="FINISHED", homeScore=2, awayScore=1, winner="HOME")
wl2 = [_cp.deepcopy(wl[0])]; W.settle(wl2, mfin, now=NOW + 9000)
ck("cs settles won on the exact score", wl2[0]["status"] == "won" and wl2[0]["return"] > 5, wl2[0].get("status"))
wl3 = [_cp.deepcopy(wl[0])]; W.settle(wl3, dict(mfin, homeScore=1, awayScore=2, winner="AWAY"), now=NOW + 9000)
ck("cs loses on any other score", wl3[0]["status"] == "lost", wl3[0].get("status"))
wl4 = []
W.place(wl4, "Erol", g1cs, "OTHER", 5, 1000, 95, 55, now=NOW, market="cs")
W.settle(wl4, dict(mfin, homeScore=6, awayScore=0), now=NOW + 9000)
ck("OTHER wins outside the grid", wl4[0]["status"] == "won", wl4[0].get("status"))

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
