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
for good in sorted(W.hc_odds(CH, CA).keys(), key=float):
    ok_g, _ = W.place([], "Erol", FUTURE, "HOME", 5, 100, CH, CA, now=NOW, market="hc", line=float(good))
    ck("offered line %s is accepted" % good, ok_g, None)
# a line the ladder filtered at these strengths is VALID input but prices to a clean error, not a crash
filtered = [L for L in W.HC_LINES if W._line_key(L) not in W.hc_odds(CH, CA)]
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

print("\n== singles only: accas reject a handicap leg ==")
sel_hc = {"match": FUTURE, "selection": "HOME", "market": "hc", "line": -1.5, "comp_home": CH, "comp_away": CA}
sel_res = {"match": dict(FUTURE, home="France", away="Ghana"), "selection": "HOME", "comp_home": 70, "comp_away": 50}
ok_acc, msg_acc = W.place_acca([], "Erol", [sel_hc, sel_res], 5, 100, now=NOW)
ck("acca with an hc leg is rejected with a clear message", not ok_acc and "single" in str(msg_acc).lower(), msg_acc)
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
