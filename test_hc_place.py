#!/usr/bin/env python3
"""Handicap placement QA — wager.place(market='hc'). Mirrors the O/U placement suite: valid placement
with locked server-side odds, selection + line validation (only offered HC_LINES; a ladder-filtered
line prices to a clean error), the pre-kickoff lock, stake floor/caps/budget, singles-only (acca
rejection), the O/U-style exemption from the opposing-bet block, knockout placement — and proof that
result, O/U and exact-score placement are completely unchanged by the new branch."""
import wager as W

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

NOW = 1_700_000_000
FUTURE = {"home": "Brazil", "away": "Serbia", "stage": "GROUP_STAGE", "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"}
KO = {"home": "Brazil", "away": "Serbia", "stage": "QUARTER_FINALS", "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"}
KICKED = {"home": "Brazil", "away": "Serbia", "stage": "GROUP_STAGE", "utcDate": "2000-01-01T00:00:00Z", "status": "IN_PLAY"}
CH, CA = 80, 60

print("== a valid handicap bet places cleanly ==")
ws = []
ok, w = W.place(ws, "Erol", FUTURE, "HOME", 5, 100, CH, CA, now=NOW, market="hc", line=-1.5)
ck("HOME -1.5 placed", ok and isinstance(w, dict), w)
ck("stored as the hc market with the line", ok and w.get("market") == "hc" and w.get("line") == -1.5, w if ok else None)
ck("selection + odds + return stored", ok and w["selection"] == "HOME" and w["num"] and w["return"] > 0, w if ok else None)
ck("it was appended to the list", len(ws) == 1, len(ws))
_live = W.hc_odds(CH, CA)["-1.5"]["HOME"]
ck("struck odds == server price for that line/side", ok and w["num"] == _live["num"] and w["den"] == _live["den"],
   (w.get("frac") if ok else None, _live["frac"]))
ok2, w2 = W.place(ws, "Erol", FUTURE, "AWAY", 4, 100, CH, CA, now=NOW, market="hc", line=-1.5)
ck("AWAY on the -1.5 key (underdog GETTING 1.5) places too", ok2 and w2.get("selection") == "AWAY" and w2.get("line") == -1.5, w2 if ok2 else None)

print("\n== selection + line validation ==")
for bad_sel in ("DRAW", "OVER", "BRAZIL", "", None, "home"):
    ok_b, msg = W.place([], "Erol", FUTURE, bad_sel, 5, 100, CH, CA, now=NOW, market="hc", line=-1.5)
    ck("selection %r is rejected" % (bad_sel,), not ok_b, msg if ok_b else None)
for bad in (0.5, -0.5, 1.0, 3.0, 9.5, 0, "x", None, float("nan"), float("inf")):
    ok_b, msg = W.place([], "Erol", FUTURE, "HOME", 5, 100, CH, CA, now=NOW, market="hc", line=bad)
    ck("line %r is rejected" % (bad,), not ok_b, msg if ok_b else None)
_book = W.hc_odds(CH, CA)
for good in sorted(_book.keys(), key=float):
    for sel in ("HOME", "AWAY"):
        ok_g, msg = W.place([], "Erol", FUTURE, sel, 5, 100, CH, CA, now=NOW, market="hc", line=float(good))
        if sel in _book[good]:
            ck("offered side %s @ %s is accepted" % (sel, good), ok_g, msg if not ok_g else None)
        else:
            # one-sided line: the capped near-certainty is OFF the board -> clean price error, never a bet
            ck("unoffered side %s @ %s -> clean 'couldn't price' error" % (sel, good), (not ok_g) and isinstance(msg, str), msg)
# a line the ladder filtered ENTIRELY at these strengths is VALID input but prices to a clean error, not a crash
filtered = [L for L in W.HC_LINES if W._line_key(L) not in _book]
for L in filtered:
    ok_f, msg = W.place([], "Erol", FUTURE, "HOME", 5, 100, CH, CA, now=NOW, market="hc", line=L)
    ck("ladder-filtered line %+g -> clean 'couldn't price' error" % L, (not ok_f) and isinstance(msg, str), msg)

print("\n== the usual guardrails all still apply ==")
ok_k, msg_k = W.place([], "Erol", KICKED, "HOME", 5, 100, CH, CA, now=NOW, market="hc", line=-1.5)
ck("kicked-off game is locked", not ok_k, msg_k if ok_k else None)
ok_s, msg_s = W.place([], "Erol", FUTURE, "HOME", 0, 100, CH, CA, now=NOW, market="hc", line=-1.5)
ck("zero stake rejected", not ok_s)
ok_c, msg_c = W.place([], "Erol", FUTURE, "HOME", 10 ** 6, 100, CH, CA, now=NOW, market="hc", line=-1.5)
ck("over-cap stake rejected", not ok_c)
ok_a, msg_a = W.place([], "Erol", FUTURE, "HOME", 50, 10, CH, CA, now=NOW, market="hc", line=-1.5)
ck("stake beyond available points rejected", not ok_a)

print("\n== knockout: handicap has no draw problem ==")
ok_ko, w_ko = W.place([], "Erol", KO, "AWAY", 5, 100, CH, CA, now=NOW, market="hc", line=1.5)
ck("hc places on a knockout (settles on the 90+ET margin, not the shootout)", ok_ko and w_ko.get("market") == "hc", w_ko)

print("\n== accumulators: hc and cs legs are IN (distinct games), same-game combos stay blocked ==")
sel_hc = {"match": FUTURE, "selection": "HOME", "market": "hc", "line": -1.5, "comp_home": CH, "comp_away": CA}
sel_res = {"match": dict(FUTURE, home="France", away="Ghana"), "selection": "HOME", "comp_home": 70, "comp_away": 50}
ok_acc, w_acc = W.place_acca([], "Erol", [sel_hc, sel_res], 5, 100, now=NOW)
ck("an hc leg + a result leg on DIFFERENT games places", ok_acc and isinstance(w_acc, dict), w_acc)
_hleg = next((l for l in (w_acc.get("legs") or []) if l.get("market") == "hc"), None) if ok_acc else None
ck("the hc leg is struck at the live hc_odds price with its line stored",
   ok_acc and _hleg and _hleg.get("line") == -1.5 and _hleg["num"] == W.hc_odds(CH, CA)["-1.5"]["HOME"]["num"], _hleg)
sel_cs = {"match": dict(FUTURE, home="Japan", away="Chile"), "selection": "2-1", "market": "cs", "comp_home": 60, "comp_away": 60}
ok_cs, w_cs = W.place_acca([], "Erol", [sel_cs, sel_res], 5, 100, now=NOW)
ck("a cs leg on a different game places too", ok_cs and any(l.get("market") == "cs" for l in (w_cs.get("legs") or [])), w_cs)
ok_sg, w_sg = W.place_acca([], "Erol",
                           [sel_hc, {"match": FUTURE, "selection": "OVER", "market": "ou", "line": 2.5,
                                     "comp_home": CH, "comp_away": CA}], 5, 100, now=NOW)
ck("two legs on the SAME game now place as a JOINT-priced group", ok_sg and w_sg.get("groups")
   and any(g.get("sgm") for g in w_sg["groups"]), w_sg)
if ok_sg:
    _naive = 1.0
    for _l in w_sg["legs"]:
        _naive *= 1 + _l["num"] / _l["den"]
    ck("the correlated pair pays LESS than the naive product (hc -1.5 and Over 2.5 overlap)",
       w_sg["decimal"] < _naive - 1e-9, (w_sg["decimal"], _naive))
ok_bl, msg_bl = W.place_acca([], "Erol", [dict(sel_hc, line=0.5), sel_res], 5, 100, now=NOW)
ck("an off-ladder hc line in a leg is rejected", not ok_bl, msg_bl if ok_bl else None)
ok_dr, msg_dr = W.place_acca([], "Erol", [dict(sel_hc, selection="DRAW"), sel_res], 5, 100, now=NOW)
ck("a DRAW handicap leg is rejected", not ok_dr, msg_dr if ok_dr else None)
ok_bc, msg_bc = W.place_acca([], "Erol", [dict(sel_cs, selection="12-1"), sel_res], 5, 100, now=NOW)
ck("an off-grid exact-score leg is rejected", not ok_bc, msg_bc if ok_bc else None)
ok_acc2, w_acc2 = W.place_acca([], "Erol", [sel_res, {"match": dict(FUTURE, home="Japan", away="Chile"),
                                                      "selection": "OVER", "market": "ou", "line": 2.5,
                                                      "comp_home": 60, "comp_away": 60}], 5, 100, now=NOW)
ck("a normal result+OU acca still places (regression)", ok_acc2, w_acc2)

print("\n== hedge policy: hc carries its own margin, so it's exempt like O/U ==")
ws3 = []
ok_h1, _ = W.place(ws3, "Erol", FUTURE, "HOME", 3, 100, CH, CA, now=NOW, market="hc", line=-1.5)
ok_h2, w_h2 = W.place(ws3, "Erol", FUTURE, "AWAY", 3, 100, CH, CA, now=NOW, market="hc", line=-1.5)
ck("both sides of one hc line allowed (guaranteed margin LOSS, not an arb)", ok_h1 and ok_h2, w_h2)
ok_h3, _ = W.place(ws3, "Erol", FUTURE, "HOME", 3, 100, CH, CA, now=NOW)
ok_h4, msg_h4 = W.place(ws3, "Erol", FUTURE, "AWAY", 3, 100, CH, CA, now=NOW)
ck("the result opposing-bet block is untouched (regression)", ok_h3 and not ok_h4, msg_h4)

print("\n== other markets are completely unchanged ==")
ws4 = []
ok_r, w_r = W.place(ws4, "Erol", FUTURE, "DRAW", 5, 100, CH, CA, now=NOW)
ck("a result bet still places with no market/line fields", ok_r and "market" not in w_r and "line" not in w_r, w_r)
ok_o, w_o = W.place(ws4, "Erol", FUTURE, "UNDER", 5, 100, CH, CA, now=NOW, market="ou", line=2.5)
ck("an O/U bet still places exactly as before", ok_o and w_o.get("market") == "ou" and w_o.get("line") == 2.5, w_o)
ok_cs, w_cs = W.place(ws4, "Erol", FUTURE, "2-1", 5, 100, CH, CA, now=NOW, market="cs")
ck("an exact-score bet still places exactly as before", ok_cs and w_cs.get("market") == "cs", w_cs)

print()
if fails:
    print("FAILED: %d -> %s" % (len(fails), fails))
    raise SystemExit(1)
print("ALL HANDICAP PLACEMENT CHECKS PASSED")
