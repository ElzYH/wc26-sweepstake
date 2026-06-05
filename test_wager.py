"""Tests for the wagering engine: odds, payout maths, caps, pre-kickoff lock, settlement, balances."""
import sys
import time
import wager

FAILS = []


def ck(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -> " + str(detail)))
    if not cond:
        FAILS.append(name)


FUTURE = "2099-01-01T18:00:00Z"
PAST = "2000-01-01T18:00:00Z"


def fx(status="TIMED", utc=FUTURE, winner=None, hs=None, as_=None, mid="m1", stage="GROUP_STAGE"):
    return {"id": mid, "home": "Brazil", "away": "Japan", "stage": stage, "status": status,
            "utcDate": utc, "winner": winner, "homeScore": hs, "awayScore": as_}


def run():
    # --- payout maths: 5 @ 9/2 -> 27.5 (the brief's example) ---
    ck("5 points @ 9/2 returns 27.5", wager.potential_return(5, 9, 2) == 27.5, wager.potential_return(5, 9, 2))
    ck("2 points @ evens returns 4", wager.potential_return(2, 1, 1) == 4.0, wager.potential_return(2, 1, 1))
    ck("10 @ 1/2 returns 15", wager.potential_return(10, 1, 2) == 15.0, wager.potential_return(10, 1, 2))

    # --- odds: three selections, snapped to standard fractions, book overrounds (>100%) ---
    od = wager.match_odds(80, 40)
    ck("odds cover home/draw/away", set(od) == {"HOME", "DRAW", "AWAY"}, list(od))
    ck("favourite (home) is shorter than underdog (away)", od["HOME"]["decimal"] < od["AWAY"]["decimal"],
       (od["HOME"]["frac"], od["AWAY"]["frac"]))
    book = sum(1.0 / od[s]["decimal"] for s in od)
    ck("book overrounds (sums to >100%)", book > 1.0, round(book * 100, 1))
    ck("odds are standard fractions", all((od[s]["num"], od[s]["den"]) in wager._FRACTIONS for s in od), od)

    # --- placement: a valid bet is accepted, deducts available, locks odds ---
    w = []
    ok, res = wager.place(w, "Erol", fx(), "HOME", 5, settled_points=20, comp_home=80, comp_away=40)
    ck("valid bet accepted", ok and res["status"] == "pending", res)
    ck("bet locks odds + return", "num" in res and res["return"] == wager.potential_return(5, res["num"], res["den"]), res)
    ck("available drops by the stake", wager.available_points("Erol", 20, w) == 15.0, wager.available_points("Erol", 20, w))

    # --- can't stake more than you have (settled points only) ---
    ok, res = wager.place(w, "Erol", fx(mid="m2"), "HOME", 25, settled_points=20, comp_home=80, comp_away=40)
    ck("can't stake more than available", (not ok) and "available" in res, res)
    ck("nothing was appended on failure", len(w) == 1, len(w))

    # --- floor at zero: never negative available ---
    ck("available floored at 0", wager.available_points("Nobody", 0, w) == 0.0, wager.available_points("Nobody", 0, w))

    # --- stake bounds + caps ---
    ok, res = wager.place([], "Erol", fx(), "HOME", 0.5, settled_points=999, comp_home=80, comp_away=40)
    ck("below-minimum stake rejected", not ok, res)
    ok, res = wager.place([], "Erol", fx(), "HOME", 999, settled_points=99999, comp_home=80, comp_away=40)
    ck("over-max stake rejected", (not ok) and "Max stake" in res, res)
    # a big-odds underdog hitting the return cap
    ok, res = wager.place([], "Erol", fx(), "AWAY", 25, settled_points=9999, comp_home=99, comp_away=1)
    ck("return cap enforced (or within cap)", (not ok and "cap" in res) or (ok and res["return"] <= wager.MAX_RETURN), res)

    # --- pre-kickoff lock: can't bet once it's live / finished / past kickoff ---
    for st in ("IN_PLAY", "PAUSED", "FINISHED", "AWARDED"):
        ok, _ = wager.place([], "Erol", fx(status=st), "HOME", 5, settled_points=99, comp_home=80, comp_away=40)
        ck("can't bet on a %s game" % st, not ok)
    ok, _ = wager.place([], "Erol", fx(status="TIMED", utc=PAST), "HOME", 5, settled_points=99, comp_home=80, comp_away=40)
    ck("can't bet after kickoff time even if status lags", not ok)

    # --- max simultaneous open bets ---
    many = []
    for i in range(wager.MAX_PENDING):
        wager.place(many, "Erol", fx(mid="g%d" % i), "HOME", 1, settled_points=999, comp_home=80, comp_away=40)
    ok, res = wager.place(many, "Erol", fx(mid="gX"), "HOME", 1, settled_points=999, comp_home=80, comp_away=40)
    ck("max open bets enforced", (not ok) and "open bets" in res, res)

    # --- settlement: win pays out, loss pays nothing, void refunds ---
    bets = []
    wager.place(bets, "Erol", fx(mid="WIN"), "HOME", 5, settled_points=50, comp_home=80, comp_away=40)
    wager.place(bets, "Erol", fx(mid="LOSE"), "AWAY", 5, settled_points=50, comp_home=80, comp_away=40)
    wager.place(bets, "Erol", fx(mid="VOID"), "HOME", 5, settled_points=50, comp_home=80, comp_away=40)
    win_ret = bets[0]["return"]
    wager.settle(bets, fx(mid="WIN", status="FINISHED", winner="HOME", hs=2, as_=0))
    wager.settle(bets, fx(mid="LOSE", status="FINISHED", winner="HOME", hs=2, as_=0))   # bet AWAY, home won -> lose
    wager.settle(bets, fx(mid="VOID", status="POSTPONED"))
    ck("winning bet marked won", bets[0]["status"] == "won", bets[0])
    ck("losing bet marked lost with 0 return", bets[1]["status"] == "lost" and bets[1]["return"] == 0, bets[1])
    ck("voided bet refunded", bets[2]["status"] == "void", bets[2])
    d = wager.player_deltas(bets)["Erol"]
    ck("settled net = win profit - lost stake", round(d["settled_net"], 1) == round((win_ret - 5) - 5, 1), d)
    ck("no pending left after settling all three", d["pending_count"] == 0, d)

    # --- a pens-decided knockout settles to the advancing side ---
    ko = []
    wager.place(ko, "Erol", fx(mid="KO", stage="LAST_16"), "HOME", 5, settled_points=50, comp_home=60, comp_away=55)
    wager.settle(ko, fx(mid="KO", stage="LAST_16", status="FINISHED", winner="HOME", hs=1, as_=1))  # 1-1, home won on pens
    ck("pens win settles the advancing side as won", ko[0]["status"] == "won", ko[0])

    # --- applied points never go below zero, and reflect held stakes + settled net ---
    held = []
    wager.place(held, "Erol", fx(mid="H"), "HOME", 5, settled_points=10, comp_home=80, comp_away=40)
    ck("open stake is held (applied points drop)", wager.applied_points(10, "Erol", held) == 5.0,
       wager.applied_points(10, "Erol", held))
    ck("applied points floored at 0", wager.applied_points(2, "Erol", held) == 0.0,
       wager.applied_points(2, "Erol", held))

    if FAILS:
        print("\nFAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        sys.exit(1)
    print("\nAll wager-engine tests passed.")


if __name__ == "__main__":
    run()
