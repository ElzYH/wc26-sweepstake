#!/usr/bin/env python3
"""Stage 2 QA — Over/Under placement via wager.place(market='ou'). Mirrors the existing single-bet
validation: valid placement, line validation, pre-kickoff lock, stake floor/caps, return cap, hostile
inputs — and proves result (1X2) bets are completely unchanged by the new branch."""
import time
import wager as W

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

NOW = 1_700_000_000
FUTURE = {"home": "Brazil", "away": "Serbia", "stage": "GROUP_STAGE", "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"}
KICKED = {"home": "Brazil", "away": "Serbia", "stage": "GROUP_STAGE", "utcDate": "2000-01-01T00:00:00Z", "status": "IN_PLAY"}
CH, CA = 80, 60

print("== a valid Over/Under bet places cleanly ==")
ws = []
ok, w = W.place(ws, "Erol", FUTURE, "OVER", 5, 100, CH, CA, now=NOW, market="ou", line=2.5)
ck("OVER 2.5 placed", ok and isinstance(w, dict), w)
ck("stored as the ou market with the line", ok and w.get("market") == "ou" and w.get("line") == 2.5, w if ok else None)
ck("selection + odds + return stored", ok and w["selection"] == "OVER" and w["num"] and w["return"] > 0, w if ok else None)
ck("it was appended to the list", len(ws) == 1, len(ws))
# the odds it was struck at must equal the live price for that line/selection (no spoofing)
_live = W.goals_odds(CH, CA)["2.5"]["OVER"]
ck("struck odds == server price for that line", ok and w["num"] == _live["num"] and w["den"] == _live["den"], (w.get("frac") if ok else None, _live["frac"]))

ok2, w2 = W.place(ws, "Erol", FUTURE, "UNDER", 4, 100, CH, CA, now=NOW, market="ou", line=1.5)
ck("UNDER 1.5 places too", ok2 and w2.get("selection") == "UNDER" and w2.get("line") == 1.5, w2 if ok2 else None)

print("\n== line validation ==")
for bad in (3.0, 9.5, 0, -1, "x", None, 2.0):
    ok_b, msg = W.place([], "Erol", FUTURE, "OVER", 5, 100, CH, CA, now=NOW, market="ou", line=bad)
    ck("line %r is rejected" % (bad,), not ok_b, msg if ok_b else None)
offered = sorted(W.goals_odds(CH, CA).keys(), key=float)
for good in offered:
    ok_g, _ = W.place([], "Erol", FUTURE, "OVER", 5, 100, CH, CA, now=NOW, market="ou", line=float(good))
    ck("offered line %s is accepted" % good, ok_g, None)
# a line on the ladder but NOT offered for this game (one side near-certain -> would underround) is rejected, not mispriced
not_offered = [L for L in W.OU_LINES if W._line_key(L) not in W.goals_odds(CH, CA)]
ck("some outer lines are filtered out for a typical game", len(not_offered) > 0, not_offered)
if not_offered:
    ok_n, msg_n = W.place([], "Erol", FUTURE, "OVER", 5, 100, CH, CA, now=NOW, market="ou", line=not_offered[0])
    ck("a ladder line not offered for this game is rejected (can't be mispriced)", not ok_n, msg_n if ok_n else None)

print("\n== selection validation (O/U only takes OVER/UNDER) ==")
for bad in ("HOME", "DRAW", "AWAY", "over ", "", "YES"):
    ok_s, msg = W.place([], "Erol", FUTURE, bad, 5, 100, CH, CA, now=NOW, market="ou", line=2.5)
    ck("selection %r rejected for O/U" % bad, not ok_s, msg if ok_s else None)

print("\n== pre-kickoff lock (same as result bets) ==")
ok_k, msg = W.place([], "Erol", KICKED, "OVER", 5, 100, CH, CA, now=NOW, market="ou", line=2.5)
ck("no O/U bet once the game has kicked off", not ok_k, msg if ok_k else None)

print("\n== stake floor, per-bet cap, available + budget all apply to O/U ==")
ok_z, msg = W.place([], "Erol", FUTURE, "OVER", 0, 100, CH, CA, now=NOW, market="ou", line=2.5)
ck("zero stake rejected", not ok_z, None)
ok_n, msg = W.place([], "Erol", FUTURE, "OVER", float("nan"), 100, CH, CA, now=NOW, market="ou", line=2.5)
ck("NaN stake rejected", not ok_n, None)
_cap = W.stage_max_stake("GROUP_STAGE")
ok_c, msg = W.place([], "Erol", FUTURE, "OVER", _cap + 50, 100000, CH, CA, now=NOW, market="ou", line=2.5)
ck("stake above the per-bet cap rejected", not ok_c, msg if ok_c else None)
ok_a, msg = W.place([], "Erol", FUTURE, "OVER", 8, 0, CH, CA, now=NOW, market="ou", line=2.5)   # only 5 free pts available
ck("stake beyond available points rejected", not ok_a, msg if ok_a else None)

print("\n== per-bet RETURN cap (if configured) applies to O/U too ==")
_saved = W.MAX_RETURN
try:
    W.MAX_RETURN = 10
    ok_r, msg = W.place([], "Erol", FUTURE, "OVER", 9, 100, 95, 5, now=NOW, market="ou", line=0.5)  # short price, big stake
    # 0.5 over is short, return on 9 ~ small; pick a long line to breach the cap instead:
    ok_r2, msg2 = W.place([], "Erol", FUTURE, "OVER", 9, 100, 80, 80, now=NOW, market="ou", line=4.5)  # ~6/1
    ck("a return over the cap is rejected", (not ok_r2), msg2 if ok_r2 else None)
finally:
    W.MAX_RETURN = _saved

print("\n== result (1X2) bets are UNCHANGED by the new branch ==")
wr = []
okr, rw = W.place(wr, "Erol", FUTURE, "HOME", 5, 100, CH, CA, now=NOW)   # no market kwarg -> default result
ck("a plain HOME bet still places", okr and rw["selection"] == "HOME", rw if okr else None)
ck("result bets carry NO market/line field (back-compatible)", okr and "market" not in rw and "line" not in rw, rw if okr else None)
ck("result odds still come from match_odds", okr and rw["num"] == W.match_odds(CH, CA)["HOME"]["num"], None)
# the no-draw-on-knockout rule still holds
okd, _ = W.place([], "Erol", {"home": "A", "away": "B", "stage": "QUARTER_FINALS", "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"},
                 "DRAW", 5, 100, CH, CA, now=NOW)
ck("draw on a knockout still rejected (result path intact)", not okd, None)

print("\n== hostile composites don't break placement ==")
for a, b in [(float("nan"), 50), (float("inf"), float("-inf")), (-9, -9), ("x", "y"), (None, None)]:
    ok_h, res = W.place([], "Erol", FUTURE, "OVER", 5, 100, a, b, now=NOW, market="ou", line=2.5)
    ck("O/U places sanely with composites (%r,%r)" % (a, b), ok_h and res["return"] > 0, res if ok_h else None)

print("\n" + ("All O/U placement QA passed." if not fails else "O/U PLACEMENT QA FAILED (%d): %s" % (len(fails), ", ".join(fails))))
import sys
sys.exit(1 if fails else 0)
