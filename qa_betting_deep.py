#!/usr/bin/env python3
"""Deep betting QA — ~100 independent checks hammering the wagering engine for correctness and safety:
odds integrity, single + accumulator placement limits, settlement (win/lose/void/penalties/abandoned),
void refunds, budgets/epochs, free bets + free points, money conservation, idempotency, and adversarial data.
Pure-function level (no server/network needed)."""
import os, sys

SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC)
import wager as W

FAILS = []
def ck(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
        print("  FAIL " + name + ("" if extra == "" else "  -> %r" % (extra,)))
    else:
        print("  PASS " + name)

NOW = 1_700_000_000
FUT = "2099-06-15T18:00:00Z"
PAST = "2000-06-15T18:00:00Z"

def fx(mid="m1", h="Brazil", a="Serbia", stage="GROUP_STAGE", status="TIMED", utc=FUT):
    return {"id": mid, "home": h, "away": a, "stage": stage, "status": status, "utcDate": utc}

def fin(mid="m1", h="Brazil", a="Serbia", hs=2, as_=1, winner="HOME", stage="GROUP_STAGE", **kw):
    m = {"id": mid, "home": h, "away": a, "stage": stage, "status": "FINISHED",
         "homeScore": hs, "awayScore": as_, "winner": winner, "utcDate": PAST}
    m.update(kw); return m

# composites (FIFA-ish strength)
STRONG, WEAK, EVEN = 85, 50, 70

# ============================================================== ODDS
print("\n== ODDS INTEGRITY ==")
o = W.match_odds(STRONG, WEAK)
ck("odds have all three outcomes", all(k in o for k in ("HOME", "DRAW", "AWAY")))
ck("every outcome has num/den/frac/decimal", all(all(k in o[s] for k in ("num", "den", "frac", "decimal")) for s in o))
ck("no zero/negative denominators", all(o[s]["den"] > 0 and o[s]["num"] > 0 for s in o))
ck("decimals are > 1 (you always get stake back + winnings)", all(o[s]["decimal"] > 1.0 for s in o))
ck("favourite is shorter than the underdog", o["HOME"]["decimal"] < o["AWAY"]["decimal"])
imp = sum(1.0 / o[s]["decimal"] for s in o)
ck("implied probabilities sum > 1 (bookmaker margin present)", imp > 1.0, imp)
ck("implied book is sane (<1.5, i.e. margin not absurd)", imp < 1.5, imp)
oe = W.match_odds(EVEN, EVEN)
ck("equal teams price home==away symmetric", abs(oe["HOME"]["decimal"] - oe["AWAY"]["decimal"]) < 1e-6, (oe["HOME"], oe["AWAY"]))
o2 = W.match_odds(STRONG + 5, WEAK)
ck("a stronger favourite is priced no longer than before (monotonic)", o2["HOME"]["decimal"] <= o["HOME"]["decimal"] + 1e-9, (o2["HOME"], o["HOME"]))
ohuge = W.match_odds(1000, 1)
ck("extreme favourite capped (decimal >= ~1.05, never 1.0)", ohuge["HOME"]["decimal"] >= 1.05, ohuge["HOME"])
ck("extreme underdog still finite/sane", ohuge["AWAY"]["decimal"] < 1000, ohuge["AWAY"])
ck("degenerate (0,0) gives sane odds", all(W.match_odds(0, 0)[s]["decimal"] > 1.0 for s in ("HOME", "DRAW", "AWAY")))
ck("None composites give sane odds", all(W.match_odds(None, None)[s]["decimal"] > 1.0 for s in ("HOME", "DRAW", "AWAY")))
ck("inf composite no longer breaks odds", all(W.match_odds(float("inf"), 5)[s]["decimal"] > 1.0 for s in ("HOME", "DRAW", "AWAY")))
ck("nan composite no longer breaks odds", all(W.match_odds(float("nan"), 5)[s]["decimal"] > 1.0 for s in ("HOME", "DRAW", "AWAY")))

print("\n== potential_return MATH ==")
ck("5 @ 9/2 -> 27.5", W.potential_return(5, 9, 2) == 27.5, W.potential_return(5, 9, 2))
ck("2 @ evens -> 4", W.potential_return(2, 1, 1) == 4.0, W.potential_return(2, 1, 1))
ck("10 @ 1/2 (odds-on) -> 15", W.potential_return(10, 1, 2) == 15.0, W.potential_return(10, 1, 2))
ck("1 @ 100/1 -> 101", W.potential_return(1, 100, 1) == 101.0, W.potential_return(1, 100, 1))
ck("return rounds to 2 dp", W.potential_return(3.33, 11, 4) == round(3.33 * (1 + 11 / 4), 2), W.potential_return(3.33, 11, 4))
ck("return always exceeds stake", W.potential_return(7, 1, 5) > 7)

# ============================================================== SINGLE PLACEMENT
print("\n== SINGLE BET PLACEMENT LIMITS ==")
def place(stake, sel="HOME", w=None, settled=999, m=None, ch=STRONG, ca=WEAK):
    w = [] if w is None else w
    return W.place(w, "Erol", m or fx(), sel, stake, settled_points=settled, comp_home=ch, comp_away=ca, now=NOW), w

(ok, res), w = place(5)
ck("valid bet accepted + pending", ok and res["status"] == "pending", res)
ck("bet stores its own locked odds (num/den/frac/return)", all(k in res for k in ("num", "den", "frac", "return")))
ck("bet return matches potential_return at locked odds", res["return"] == W.potential_return(5, res["num"], res["den"]), res)
(ok, res), w = place(0)
ck("zero stake rejected", not ok)
(ok, res), w = place(0.5)
ck("below-minimum (0.5) rejected", not ok)
(ok, res), w = place(1)
ck("exactly the minimum (1) accepted", ok, res)
(ok, res), w = place(31)
ck("above group cap (31) rejected", not ok and "Max stake" in res, res)
(ok, res), w = place(30)
ck("exactly the cap (30) accepted", ok, res)
(ok, res), w = place(1.23)
ck("2-dp stake stored exactly", ok and res["stake"] == 1.23, res)
(ok, res), w = place(1.999)
ck("3-dp stake clamped to 2dp", ok and res["stake"] == 2.0, res)
(ok, res), w = place(5, sel="DRAW")
ck("group-stage draw bet allowed", ok, res)
(ok, res), w = place(5, sel="DRAW", m=fx(stage="QUARTER_FINAL"))
ck("knockout draw bet rejected", not ok, res)
(ok, res), w = place(5, sel="WIN")
ck("invalid selection rejected", not ok)
(ok, res), w = place(5, m=fx(status="IN_PLAY"))
ck("bet on in-play game rejected", not ok, res)
(ok, res), w = place(5, m=fx(status="FINISHED", utc=PAST))
ck("bet on finished game rejected", not ok, res)
(ok, res), w = place(5, m=fx(utc=PAST))
ck("bet after kickoff time rejected", not ok, res)
(ok, res), w = place(5, settled=0)
ck("can still bet with 0 earned (starting free points cover it)", ok, res)
(ok, res), w = place(20, settled=0)
ck("stake beyond available (0 earned + 5 free = 5) rejected", not ok, res)

print("\n== EXPOSURE / PENDING-COUNT / BUDGET CAPS ==")
# fill pending exposure to the cap with several bets on distinct games
w = []
for i in range(6):
    W.place(w, "Erol", fx(mid="g%d" % i), "HOME", 5, settled_points=999, comp_home=STRONG, comp_away=WEAK, now=NOW)
ck("six 5pt bets place (30 exposure, at cap)", len([x for x in w if x["status"] == "pending"]) == 6, len(w))
ok, res = W.place(w, "Erol", fx(mid="g7"), "HOME", 1, settled_points=999, comp_home=STRONG, comp_away=WEAK, now=NOW)
ck("a 7th bet over the 30 open-exposure cap is rejected", not ok, res)
# pending-count cap (MAX_PENDING) with tiny stakes under a high exposure cap: use knockout cap to allow many
w = []
for i in range(W.MAX_PENDING):
    W.place(w, "Erol", fx(mid="k%d" % i, stage="FINAL"), "HOME", 1, settled_points=9999, comp_home=STRONG, comp_away=WEAK, now=NOW)
cnt = len([x for x in w if x["status"] == "pending"])
ck("can open up to MAX_PENDING bets", cnt == W.MAX_PENDING, cnt)
ok, res = W.place(w, "Erol", fx(mid="kx", stage="FINAL"), "HOME", 1, settled_points=9999, comp_home=STRONG, comp_away=WEAK, now=NOW)
ck("one more than MAX_PENDING is rejected", not ok and "open bets" in res, res)
# staking budget per epoch
e = W.epoch_of(fx())
ck("epoch_of returns a non-empty label", bool(e), e)
ck("budget_remaining starts at STAGE_BUDGET", W.budget_remaining([], "Erol", e) == W.STAGE_BUDGET, W.budget_remaining([], "Erol", e))
# a player who has staked PAST the budget this epoch (e.g. after the cap was lowered) clamps to 0 — never negative
_over = [{"player": "Erol", "epoch": e, "stake": W.STAGE_BUDGET + 25, "status": "lost"}]
ck("staking past the budget clamps to 0 (locked out, never negative)", W.budget_remaining(_over, "Erol", e) == 0.0, W.budget_remaining(_over, "Erol", e))
_w_over = list(_over)
_okb, _msgb = W.place(_w_over, "Erol", fx(mid="bx", stage="GROUP_STAGE"), "HOME", 1, settled_points=9999, comp_home=STRONG, comp_away=WEAK, now=NOW)
ck("with the budget used up, even a 1-pt bet is refused", not _okb and "budget" in (_msgb or "").lower(), _msgb)
# winning a bet climbs the budget back, never above the max
_back = [{"player": "Erol", "epoch": e, "stake": 20, "status": "lost"}, {"player": "Erol", "epoch": e, "stake": 5, "status": "won", "return": 80}]
ck("a win tops the budget back up but never above STAGE_BUDGET", W.budget_remaining(_back, "Erol", e) == W.STAGE_BUDGET, W.budget_remaining(_back, "Erol", e))

# ============================================================== SETTLEMENT (SINGLE)
print("\n== SETTLEMENT: SINGLES ==")
def one(sel, ch=STRONG, ca=WEAK, mid="m1", stage="GROUP_STAGE"):
    w = []
    W.place(w, "Erol", fx(mid=mid, stage=stage), sel, 10, settled_points=999, comp_home=ch, comp_away=ca, now=NOW)
    return w

w = one("HOME"); W.settle(w, fin(winner="HOME"))
ck("HOME bet wins when home wins", w[0]["status"] == "won", w[0])
ck("won return > stake", w[0]["return"] > w[0]["stake"], w[0])
w = one("HOME"); W.settle(w, fin(winner="AWAY", hs=0, as_=2))
ck("HOME bet loses when away wins", w[0]["status"] == "lost", w[0])
ck("lost return is 0", w[0]["return"] == 0, w[0])
w = one("DRAW"); W.settle(w, fin(winner=None, hs=1, as_=1))
ck("DRAW bet wins on a 1-1 draw", w[0]["status"] == "won", w[0])
w = one("AWAY"); W.settle(w, fin(winner="AWAY", hs=0, as_=1))
ck("AWAY bet wins when away wins", w[0]["status"] == "won", w[0])
for vs in ("CANCELLED", "POSTPONED", "ABANDONED"):
    w = one("HOME"); W.settle(w, fin(winner=None, status=vs))
    ck("%s -> bet voided" % vs, w[0]["status"] == "void", w[0])
# void refunds: available restored to full
w = []
W.place(w, "Erol", fx(), "HOME", 10, settled_points=20, comp_home=STRONG, comp_away=WEAK, now=NOW)
av_after_bet = W.available_points("Erol", 20, w)
W.settle(w, fin(status="CANCELLED", winner=None))
av_after_void = W.available_points("Erol", 20, w)
ck("placing holds stake (available drops)", av_after_bet < 25, av_after_bet)
ck("void refunds fully (available back to 20+5 bonus)", av_after_void == 25.0, av_after_void)
# idempotent settle
w = one("HOME"); W.settle(w, fin(winner="HOME")); r1 = w[0]["return"]
n2 = W.settle(w, fin(winner="HOME"))
ck("re-settling a won bet does nothing (idempotent)", n2 == 0 and w[0]["return"] == r1, (n2, w[0]))
# a voided bet is never re-settled as won/lost
w = one("HOME"); W.settle(w, fin(status="CANCELLED", winner=None)); W.settle(w, fin(winner="HOME"))
ck("a voided bet stays void even if the game later 'finishes'", w[0]["status"] == "void", w[0])
# not-yet-final stays pending
w = one("HOME"); W.settle(w, fin(status="IN_PLAY", winner=None))
ck("in-play match leaves bet pending", w[0]["status"] == "pending", w[0])
# knockout that shows a level 'draw' with no winner must NOT settle as lost (wait for pens)
w = one("HOME", stage="SEMI_FINAL"); W.settle(w, fin(winner=None, hs=1, as_=1, stage="SEMI_FINAL", status="FINISHED"))
ck("knockout shown level w/ no winner -> side bet still pending (awaits pens)", w[0]["status"] == "pending", w[0])
# penalties: winner reflects shootout even though on-field is level
w = one("HOME", stage="FINAL"); W.settle(w, fin(winner="HOME", hs=1, as_=1, stage="FINAL", status="FINISHED", shootout=True))
ck("penalty win settles the side that won the shootout", w[0]["status"] == "won", w[0])
w = one("AWAY", stage="FINAL"); W.settle(w, fin(winner="HOME", hs=1, as_=1, stage="FINAL", status="FINISHED", shootout=True))
ck("the shootout loser's backers lose", w[0]["status"] == "lost", w[0])
# AWARDED (walkover) settles like finished
w = one("HOME"); W.settle(w, fin(winner="HOME", status="AWARDED"))
ck("AWARDED result settles the bet", w[0]["status"] == "won", w[0])
# settling against an unrelated match does nothing
w = one("HOME"); n = W.settle(w, fin(mid="OTHER", winner="HOME"))
ck("settling an unrelated match leaves the bet pending", n == 0 and w[0]["status"] == "pending", w[0])

# ============================================================== ACCUMULATORS
print("\n== ACCUMULATORS ==")
def acca(legs_spec, stake=5, settled=999):
    """legs_spec: list of (mid, sel, ch, ca, stage)."""
    w = []
    sels = [{"match": fx(mid=m, stage=stg), "selection": s, "comp_home": ch, "comp_away": ca}
            for (m, s, ch, ca, stg) in legs_spec]
    return W.place_acca(w, "Erol", sels, stake, settled_points=settled, now=NOW), w

(ok, res), w = acca([("m1", "HOME", STRONG, WEAK, "GROUP_STAGE"), ("m2", "AWAY", WEAK, STRONG, "GROUP_STAGE")])
ck("valid 2-fold accepted", ok and res.get("legs") and len(res["legs"]) == 2, res)
if ok:
    prod = 1.0
    for lg in res["legs"]:
        prod *= (1 + lg["num"] / lg["den"])
    ck("acca return == stake x product of leg decimals (2dp)", res["return"] == round(5 * prod, 2), (res["return"], round(5 * prod, 2)))
    ck("acca odds beat any single leg (product > each)", res["decimal"] > max(1 + lg["num"] / lg["den"] for lg in res["legs"]) - 1e-9)
(ok, res), w = acca([("m1", "HOME", STRONG, WEAK, "GROUP_STAGE")])
ck("1-leg 'acca' falls back to a normal single", ok and not res.get("legs"), res)
(ok, res), w = acca([])
ck("empty acca rejected", not ok, res)
(ok, res), w = acca([("m1", "HOME", STRONG, WEAK, "GROUP_STAGE"), ("m1", "AWAY", WEAK, STRONG, "GROUP_STAGE")])
ck("same game twice in one acca rejected (one leg per game — result+goals are correlated)", not ok and "accumulator once" in res, res)
big = [("m%d" % i, "HOME", STRONG, WEAK, "GROUP_STAGE") for i in range(W.MAX_ACCA_LEGS + 1)]
(ok, res), w = acca(big)
ck("over max legs rejected", not ok, res)
(ok, res), w = acca([("m1", "HOME", STRONG, WEAK, "GROUP_STAGE"), ("m2", "DRAW", WEAK, STRONG, "QUARTER_FINAL")])
ck("knockout DRAW leg rejected", not ok, res)
(ok, res), w = acca([("m1", "WIN", STRONG, WEAK, "GROUP_STAGE"), ("m2", "AWAY", WEAK, STRONG, "GROUP_STAGE")])
ck("invalid selection in a leg rejected", not ok, res)
# acca settlement: all win -> won
(ok, res), w = acca([("m1", "HOME", STRONG, WEAK, "GROUP_STAGE"), ("m2", "AWAY", WEAK, STRONG, "GROUP_STAGE")])
W.settle(w, fin(mid="m1", winner="HOME")); W.settle(w, fin(mid="m2", winner="AWAY", hs=0, as_=1))
ck("acca with all legs winning -> won", w[0]["status"] == "won", w[0])
ck("won acca return > stake", w[0]["return"] > w[0]["stake"], w[0])
# one leg lost -> whole acca lost
(ok, res), w = acca([("m1", "HOME", STRONG, WEAK, "GROUP_STAGE"), ("m2", "AWAY", WEAK, STRONG, "GROUP_STAGE")])
W.settle(w, fin(mid="m1", winner="HOME")); W.settle(w, fin(mid="m2", winner="HOME", hs=2, as_=0))
ck("acca with one losing leg -> lost", w[0]["status"] == "lost", w[0])
ck("lost acca return is 0", w[0]["return"] == 0, w[0])
# a voided leg drops out, odds recompute on the rest
(ok, res), w = acca([("m1", "HOME", STRONG, WEAK, "GROUP_STAGE"), ("m2", "AWAY", WEAK, STRONG, "GROUP_STAGE")])
leg2_dec = 1 + res["legs"][1]["num"] / res["legs"][1]["den"]
W.settle(w, fin(mid="m1", status="CANCELLED", winner=None)); W.settle(w, fin(mid="m2", winner="AWAY", hs=0, as_=1))
ck("acca with a voided leg still wins on the rest", w[0]["status"] == "won", w[0])
ck("voided-leg acca pays on remaining leg only (stake x that leg)", abs(w[0]["return"] - round(5 * leg2_dec, 2)) < 0.02, (w[0]["return"], round(5 * leg2_dec, 2)))
# all legs void -> refund stake
(ok, res), w = acca([("m1", "HOME", STRONG, WEAK, "GROUP_STAGE"), ("m2", "AWAY", WEAK, STRONG, "GROUP_STAGE")])
W.settle(w, fin(mid="m1", status="CANCELLED", winner=None)); W.settle(w, fin(mid="m2", status="ABANDONED", winner=None))
ck("all-void acca refunds the stake (return == stake)", w[0]["return"] == w[0]["stake"], w[0])
ck("all-void acca marked void/won-refund (not lost)", w[0]["status"] in ("void", "won"), w[0])
# acca pending until every leg decided
(ok, res), w = acca([("m1", "HOME", STRONG, WEAK, "GROUP_STAGE"), ("m2", "AWAY", WEAK, STRONG, "GROUP_STAGE")])
W.settle(w, fin(mid="m1", winner="HOME"))
ck("acca stays pending while a leg is undecided", w[0]["status"] == "pending", w[0])
# idempotent acca settle
W.settle(w, fin(mid="m2", winner="AWAY", hs=0, as_=1)); rA = w[0]["return"]
n2 = W.settle(w, fin(mid="m2", winner="AWAY", hs=0, as_=1))
ck("re-settling a decided acca does nothing", n2 == 0 and w[0]["return"] == rA, (n2, w[0]))

# ============================================================== FREE BETS + FREE POINTS
print("\n== FREE BETS & FREE POINTS ==")
ck("everyone starts with STARTING_BONUS free points", W.free_bonus("Erol", []) == W.STARTING_BONUS, W.free_bonus("Erol", []))
w = []
ok, cr = W.grant_free_points(w, "Erol", "drop1", now=NOW)
ck("claiming a free-points drop succeeds", ok, cr)
ck("claimed drop raises free_bonus by the amount", W.free_bonus("Erol", w) == W.STARTING_BONUS + W.FREE_BET_STAKE, W.free_bonus("Erol", w))
ck("a free-points credit is NOT counted as a bet (player_deltas ignores it)", "Erol" not in W.player_deltas(w) or W.player_deltas(w).get("Erol", {}).get("pending_count", 0) == 0, W.player_deltas(w))
# free BET: only profit counts, stake never charged
w = []
ok, fb = W.place_free(w, "Erol", fx(), "HOME", STRONG, WEAK, now=NOW)
ck("free bet placed", ok and fb.get("free") is True, fb)
ck("free bet holds NO stake against available (free bets are free)", W.available_points("Erol", 0, w) == W.STARTING_BONUS, W.available_points("Erol", 0, w))
W.settle(w, fin(winner="HOME"))
ck("won free bet credits PROFIT only (return - stake)", round(W.player_deltas(w)["Erol"]["settled_net"], 2) == round(fb["return"] - fb["stake"], 2), W.player_deltas(w))
w = []
W.place_free(w, "Erol", fx(), "HOME", STRONG, WEAK, now=NOW)
W.settle(w, fin(winner="AWAY", hs=0, as_=2))
ck("lost free bet costs nothing (settled_net unchanged at 0)", W.player_deltas(w).get("Erol", {}).get("settled_net", 0.0) == 0.0, W.player_deltas(w))
# leaderboard cushion: first `free_bonus` of net losses absorbed
w = []
W.place(w, "Erol", fx(), "HOME", 5, settled_points=999, comp_home=STRONG, comp_away=WEAK, now=NOW)
W.settle(w, fin(winner="AWAY", hs=0, as_=2))   # a 5pt loss
ck("a 5pt loss within the 5 free-points cushion doesn't hit the leaderboard", W.leaderboard_net("Erol", w) == 0.0, W.leaderboard_net("Erol", w))

# ============================================================== MONEY CONSERVATION / ISOLATION
print("\n== MONEY CONSERVATION & PLAYER ISOLATION ==")
w = []
W.place(w, "Erol", fx(mid="a"), "HOME", 10, settled_points=50, comp_home=STRONG, comp_away=WEAK, now=NOW)
W.place(w, "James", fx(mid="b"), "AWAY", 8, settled_points=40, comp_home=WEAK, comp_away=STRONG, now=NOW)
ck("Erol's pending stake is his only (10)", W.player_deltas(w)["Erol"]["pending_stake"] == 10, W.player_deltas(w))
ck("James's pending stake is his only (8)", W.player_deltas(w)["James"]["pending_stake"] == 8, W.player_deltas(w))
ck("Erol available reflects his held stake", W.available_points("Erol", 50, w) == 50 + W.STARTING_BONUS - 10, W.available_points("Erol", 50, w))
ck("James available reflects his held stake", W.available_points("James", 40, w) == 40 + W.STARTING_BONUS - 8, W.available_points("James", 40, w))
# settle Erol's win, James's loss; verify nets isolated
W.settle(w, fin(mid="a", winner="HOME")); W.settle(w, fin(mid="b", winner="HOME", hs=2, as_=0))
de = W.player_deltas(w)
ck("Erol settled_net is his profit (>0), no pending", de["Erol"]["settled_net"] > 0 and de["Erol"]["pending_stake"] == 0, de["Erol"])
ck("James settled_net is -8 (his lost stake), no pending", de["James"]["settled_net"] == -8 and de["James"]["pending_stake"] == 0, de["James"])
ck("available floors at 0 (never negative) after big loss", W.available_points("Zed", 0, [{"player": "Zed", "status": "lost", "stake": 999}]) == 0.0, W.available_points("Zed", 0, [{"player": "Zed", "status": "lost", "stake": 999}]))
ck("applied_points holds only the beyond-bonus part of an open stake", W.applied_points(50, "Erol", [{"player": "Erol", "status": "pending", "stake": 10}]) == 45.0, W.applied_points(50, "Erol", [{"player": "Erol", "status": "pending", "stake": 10}]))  # 5 free bonus covers 5 of the 10 stake; 5 real points held -> 50-5

# ============================================================== SEQUENCING / LIFECYCLE
print("\n== SEQUENCING & LIFECYCLE ==")
w = []
W.place(w, "Erol", fx(mid="s1"), "HOME", 5, settled_points=999, comp_home=STRONG, comp_away=WEAK, now=NOW)
W.place(w, "Erol", fx(mid="s2"), "AWAY", 5, settled_points=999, comp_home=WEAK, comp_away=STRONG, now=NOW)
ck("two open bets -> pending_count 2", W.player_deltas(w)["Erol"]["pending_count"] == 2, W.player_deltas(w))
W.settle_all(w, [fin(mid="s1", winner="HOME"), fin(mid="s2", winner="AWAY", hs=0, as_=1)])
ck("settle_all resolves both", all(x["status"] == "won" for x in w), w)
ck("after settle no pending stake remains", W.player_deltas(w)["Erol"]["pending_count"] == 0, W.player_deltas(w))
# a won bet is never reverted by a later (stale) settle pass on a different match
W.settle_all(w, [fin(mid="zzz", winner="AWAY")])
ck("a won bet is untouched by unrelated later settlements", all(x["status"] == "won" for x in w), w)

# ============================================================== ADVERSARIAL DATA (recap, fast)
print("\n== ADVERSARIAL DATA ==")
junk = [{}, "x", None, 7, {"player": None}, {"player": "Erol", "status": "pending", "stake": "abc"},
        {"player": "Erol", "status": "pending", "stake": 5, "legs": "nope"},
        {"player": "Erol", "status": "won", "stake": 5, "return": "xyz"}]
crashed = None
try:
    W.player_deltas(junk); W.stats(junk); W.leaders(junk); W.free_bonus("Erol", junk)
    W.available_points("Erol", 10, junk); W.leaderboard_net("Erol", junk)
    W.settle([dict(x) if isinstance(x, dict) else x for x in junk], fin(winner="HOME"))
    W.settle_all([dict(x) if isinstance(x, dict) else x for x in junk], [fin(winner="HOME")])
except Exception as e:
    crashed = repr(e)
ck("no money/settlement function crashes on malformed records", crashed is None, crashed)
ck("settle tolerates a None match", (W.settle([], None) == 0) if True else False, "n/a")
ck("settle tolerates an empty match dict", isinstance(W.settle([{"player": "E", "status": "pending", "stake": 5, "matchId": "m1", "selection": "HOME"}], {}), int))
ck("settle_all tolerates an empty match list", W.settle_all([], []) == 0)
ck("match_odds handles string composites", all(W.match_odds("80", "40")[s]["decimal"] > 1.0 for s in ("HOME", "DRAW", "AWAY")))
ck("place rejects a None match cleanly", not W.place([], "Erol", None, "HOME", 5, 999, 80, 40, now=NOW)[0])
ck("place rejects a non-dict match cleanly", not W.place([], "Erol", "x", "HOME", 5, 999, 80, 40, now=NOW)[0])

# ============================================================== SUMMARY
if FAILS:
    print("\nDEEP BETTING QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll deep betting QA passed.")
