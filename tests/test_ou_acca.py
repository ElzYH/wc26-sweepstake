#!/usr/bin/env python3
"""Stage 4 QA — accumulators with Over/Under legs (and mixing O/U with 1X2). Combined-odds maths,
leg schema, per-leg line/selection validation, partial settle, a losing O/U leg sinks the acca,
a void O/U leg drops out, one result + one O/U on the same game allowed (same-market duplicate blocked). Pure 1X2 accas must be unchanged."""
import wager as W

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

def fx(home, away, stage="GROUP_STAGE"):
    return {"home": home, "away": away, "stage": stage, "status": "TIMED", "utcDate": "2099-01-01T00:00:00Z"}

def fin(home, away, h, a, stage="GROUP_STAGE", status="FINISHED", **extra):
    m = {"home": home, "away": away, "stage": stage, "status": status, "homeScore": h, "awayScore": a,
         "utcDate": "2099-01-01T00:00:00Z"}
    m.update(extra)
    return m

CH, CA = 80, 60
NOW = 1_700_000_000

print("== a mixed acca (O/U leg + 1X2 leg) places, with correct combined odds ==")
mA, mB = fx("Brazil", "Serbia"), fx("Spain", "Japan")
sel = [
    {"match": mA, "selection": "OVER", "market": "ou", "line": 2.5, "comp_home": CH, "comp_away": CA},
    {"match": mB, "selection": "HOME", "comp_home": CH, "comp_away": CA},
]
ws = []
ok, w = W.place_acca(ws, "Erol", sel, 4, 100, now=NOW)
ck("mixed O/U + 1X2 acca placed", ok and isinstance(w, dict) and w.get("legs"), w)
if ok:
    # combined decimal should equal product of the two leg prices
    o_ou = W.goals_odds(CH, CA)["2.5"]["OVER"]["decimal"]
    o_1x2 = W.match_odds(CH, CA)["HOME"]["decimal"]
    exp = round(o_ou * o_1x2, 3)
    ck("combined odds = product of leg decimals", abs(w["decimal"] - exp) < 0.02, (w["decimal"], exp))
    ck("the O/U leg stored market+line", w["legs"][0].get("market") == "ou" and w["legs"][0].get("line") == 2.5, w["legs"][0])
    ck("the 1X2 leg carries NO market field", "market" not in w["legs"][1], w["legs"][1])
    ck("return = stake x combined decimal", abs(w["return"] - round(4 * w["decimal"], 2)) < 0.02, w["return"])

print("\n== an all-O/U acca places ==")
sel2 = [
    {"match": fx("A", "B"), "selection": "OVER", "market": "ou", "line": 1.5, "comp_home": 70, "comp_away": 70},
    {"match": fx("C", "D"), "selection": "UNDER", "market": "ou", "line": 3.5, "comp_home": 70, "comp_away": 70},
]
ok2, w2 = W.place_acca([], "Erol", sel2, 3, 100, now=NOW)
ck("two-leg all-O/U acca placed", ok2 and len(w2["legs"]) == 2, w2 if ok2 else None)

print("\n== per-leg line + selection validation ==")
badline = [{"match": fx("A", "B"), "selection": "OVER", "market": "ou", "line": 3.0, "comp_home": 70, "comp_away": 70},
           {"match": fx("C", "D"), "selection": "HOME", "comp_home": 70, "comp_away": 70}]
okb, msg = W.place_acca([], "Erol", badline, 3, 100, now=NOW)
ck("an off-grid O/U line (3.0) rejects the whole acca", not okb, msg if okb else None)
badsel = [{"match": fx("A", "B"), "selection": "HOME", "market": "ou", "line": 2.5, "comp_home": 70, "comp_away": 70},
          {"match": fx("C", "D"), "selection": "HOME", "comp_home": 70, "comp_away": 70}]
oks, msg = W.place_acca([], "Erol", badsel, 3, 100, now=NOW)
ck("a 1X2 selection on an O/U-market leg rejects", not oks, msg if oks else None)

print("\n== same game: a result + goals combo is PRICED JOINTLY (correlation captured, never given away) ==")
same_corr = [{"match": mA, "selection": "OVER", "market": "ou", "line": 2.5, "comp_home": CH, "comp_away": CA},
             {"match": mA, "selection": "HOME", "comp_home": CH, "comp_away": CA}]
ok_same, w_same = W.place_acca([], "Erol", same_corr, 3, 100, now=NOW)
ck("O/U + match-winner on the SAME game places as a joint-priced group (paying under the naive product)",
   ok_same and w_same.get("groups") and w_same["decimal"] < (1 + w_same["legs"][0]["num"]/w_same["legs"][0]["den"]) * (1 + w_same["legs"][1]["num"]/w_same["legs"][1]["den"]) - 1e-9, w_same)
# the OLD egregious case was Under 0.5 + Draw (Under 0.5 IS a draw — the naive product paid ~3x too
# much). Under 0.5 isn't even a sellable single at these strengths any more (ladder-filtered), so the
# ingredient itself is gone:
u05_draw = [{"match": mA, "selection": "UNDER", "market": "ou", "line": 0.5, "comp_home": CH, "comp_away": CA},
            {"match": mA, "selection": "DRAW", "comp_home": CH, "comp_away": CA}]
ok_ud, w_ud = W.place_acca([], "Erol", u05_draw, 3, 100, now=NOW)
ck("Under 0.5 + Draw can't be built — the unsellable line refuses cleanly", not ok_ud and isinstance(w_ud, str), w_ud)
# ...and its modern equivalent, Draw + Under 1.5 (jointly EXACTLY 0-0), prices at the 0-0 joint —
# a fraction of the naive product, so redundancy earns nothing:
d_u15 = [{"match": mA, "selection": "UNDER", "market": "ou", "line": 1.5, "comp_home": CH, "comp_away": CA},
         {"match": mA, "selection": "DRAW", "comp_home": CH, "comp_away": CA}]
ok_d15, w_d15 = W.place_acca([], "Erol", d_u15, 3, 100, now=NOW)
if ok_d15:
    # what matters is the JOINT: the sold implied must beat the true P(0-0) by the SGM margin.
    # (it may legitimately pay MORE than the naive product — two heavy marginal margins can overshoot a
    # correlation; the joint prices it honestly while staying firmly house-positive.)
    import math as _m
    _lh, _la = W._team_lambdas(CH, CA)
    _fair00 = _m.exp(-_lh) * _m.exp(-_la)
    _g = w_d15["groups"][0]
    ck("Draw + Under 1.5 sold above the true P(0-0) joint by the SGM margin",
       (_g["den"] / (_g["num"] + _g["den"])) >= _fair00 * (1 + W.SGM_MIN_MARGIN) - 1e-9, (w_d15["decimal"], _fair00))
else:
    ck("Draw + Under 1.5 refused only as unsellable, never mispriced", isinstance(w_d15, str) and "price" in w_d15.lower(), w_d15)
dup_result = [{"match": mA, "selection": "HOME", "comp_home": CH, "comp_away": CA},
              {"match": mA, "selection": "AWAY", "comp_home": CH, "comp_away": CA}]
ok_dup, msg_dup = W.place_acca([], "Erol", dup_result, 3, 100, now=NOW)
ck("two RESULT legs on the same game are blocked", not ok_dup, msg_dup if ok_dup else None)
dup_ou = [{"match": mA, "selection": "OVER", "market": "ou", "line": 2.5, "comp_home": CH, "comp_away": CA},
          {"match": mA, "selection": "UNDER", "market": "ou", "line": 3.5, "comp_home": CH, "comp_away": CA}]
ok_dou, msg_dou = W.place_acca([], "Erol", dup_ou, 3, 100, now=NOW)
ck("two compatible O/U legs on one game price jointly; the pair collapses to the tighter constraint",
   ok_dou and isinstance(msg_dou, dict) and msg_dou.get("groups"), msg_dou)

print("\n== settlement: partial, then a losing O/U leg sinks the acca ==")
# acca: OVER 2.5 on Brazil-Serbia, HOME on Spain-Japan
wlist = []
W.place_acca(wlist, "Erol", sel, 4, 100, now=NOW)
acca = wlist[0]
# settle only the 1X2 game first (Spain win) -> acca still pending (O/U leg undecided)
W.settle(wlist, fin("Spain", "Japan", 2, 0))
ck("acca stays pending until every leg is decided", acca["status"] == "pending", acca["status"])
ck("the decided 1X2 leg is marked won", any(l.get("matchId") == W.match_id(mB) and l.get("result") == "won" for l in acca["legs"]), acca["legs"])
# now settle the O/U game LOW (0-0) -> OVER 2.5 loses -> whole acca lost
W.settle(wlist, fin("Brazil", "Serbia", 0, 0))
ck("a losing O/U leg sinks the whole acca", acca["status"] == "lost" and acca["return"] == 0, acca)

print("\n== settlement: all legs win -> acca pays the combined return ==")
wlist2 = []
W.place_acca(wlist2, "Erol", sel, 4, 100, now=NOW)
a2 = wlist2[0]
W.settle(wlist2, fin("Brazil", "Serbia", 2, 1))     # total 3 -> OVER 2.5 wins
W.settle(wlist2, fin("Spain", "Japan", 1, 0))       # HOME wins
ck("all-win acca settles WON", a2["status"] == "won", a2["status"])
ck("won acca return = stake x combined decimal", abs(a2["return"] - round(4 * a2["decimal"], 2)) < 0.02, a2["return"])

print("\n== a VOID O/U leg drops out (its odds treated as 1.0), rest can still win ==")
wlist3 = []
W.place_acca(wlist3, "Erol", sel, 4, 100, now=NOW)
a3 = wlist3[0]
W.settle(wlist3, fin("Brazil", "Serbia", 0, 0, status="CANCELLED"))   # O/U leg void
W.settle(wlist3, fin("Spain", "Japan", 1, 0))                          # 1X2 leg wins
ck("acca with one void O/U leg settles on the surviving leg(s)", a3["status"] == "won", a3)
# return should reflect only the 1X2 leg's odds (void leg = 1.0)
exp3 = round(4 * W.match_odds(CH, CA)["HOME"]["decimal"], 2)
ck("void O/U leg removed from the combined odds", abs(a3["return"] - exp3) < 0.05, (a3["return"], exp3))

print("\n== a 1-leg 'acca' that is O/U routes to a normal single ==")
one = [{"match": fx("A", "B"), "selection": "OVER", "market": "ou", "line": 2.5, "comp_home": 70, "comp_away": 70}]
ok1, w1 = W.place_acca([], "Erol", one, 5, 100, now=NOW)
ck("1-leg O/U acca becomes a single O/U bet", ok1 and not w1.get("legs") and w1.get("market") == "ou", w1 if ok1 else None)

print("\n== pure 1X2 accas are completely unchanged ==")
pure = [{"match": fx("A", "B"), "selection": "HOME", "comp_home": CH, "comp_away": CA},
        {"match": fx("C", "D"), "selection": "AWAY", "comp_home": CH, "comp_away": CA}]
okp, wp = W.place_acca([], "Erol", pure, 4, 100, now=NOW)
ck("pure 1X2 acca still places", okp and len(wp["legs"]) == 2, wp if okp else None)
ck("its legs carry no market/line", okp and all("market" not in l and "line" not in l for l in wp["legs"]), wp["legs"] if okp else None)

print("\n" + ("All O/U accumulator QA passed." if not fails else "O/U ACCA QA FAILED (%d): %s" % (len(fails), ", ".join(fails))))
import sys
sys.exit(1 if fails else 0)
