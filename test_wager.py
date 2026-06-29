"""Tests for the wagering engine: odds, payout maths, caps, pre-kickoff lock, settlement, balances."""
import sys
import wager

FAILS = []


def ck(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -> " + str(detail)))
    if not cond:
        FAILS.append(name)


FUTURE = "2099-01-01T18:00:00Z"
PAST = "2000-01-01T18:00:00Z"


def fx(status="TIMED", utc=FUTURE, winner=None, hs=None, as_=None, mid="m1", stage="GROUP_STAGE",
       penHome=None, penAway=None):
    return {"id": mid, "home": "Brazil", "away": "Japan", "stage": stage, "status": status,
            "utcDate": utc, "winner": winner, "homeScore": hs, "awayScore": as_,
            "penHome": penHome, "penAway": penAway}


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
    ck("available = earned + free bonus - held stake", wager.available_points("Erol", 20, w) == 20.0, wager.available_points("Erol", 20, w))

    # --- can't stake more than you have (settled points only) ---
    ok, res = wager.place(w, "Erol", fx(mid="m2"), "HOME", 25, settled_points=20, comp_home=80, comp_away=40)
    ck("can't stake more than available", (not ok) and "available" in res, res)
    ck("nothing was appended on failure", len(w) == 1, len(w))

    # --- floor at zero: never negative available ---
    ck("with 0 earned you still have the free bonus to stake", wager.available_points("Nobody", 0, w) == float(wager.STARTING_BONUS), wager.available_points("Nobody", 0, w))

    # --- stake bounds + caps ---
    ok, res = wager.place([], "Erol", fx(), "HOME", 0.5, settled_points=999, comp_home=80, comp_away=40)
    ck("below-minimum stake rejected (min is 1)", not ok, res)
    ck("MIN_STAKE is exactly 1", wager.MIN_STAKE == 1, wager.MIN_STAKE)
    # 2 decimal places are allowed and preserved
    ok, res = wager.place([], "Erol", fx(), "HOME", 1.23, settled_points=999, comp_home=80, comp_away=40)
    ck("a 2-decimal stake (1.23) is accepted and stored exactly", ok and res["stake"] == 1.23, res)
    # more than 2 decimals is clamped to 2 (not rejected, not left as a long float)
    ok, res = wager.place([], "Erol", fx(), "HOME", 1.234, settled_points=999, comp_home=80, comp_away=40)
    ck("a 3-decimal stake (1.234) is clamped to 1.23", ok and res["stake"] == 1.23, res)
    ok, res = wager.place([], "Erol", fx(), "HOME", 2.999, settled_points=999, comp_home=80, comp_away=40)
    ck("2.999 clamps to 3.0 (round to 2dp)", ok and res["stake"] == 3.0, res)
    # --- malformed wager records must NEVER crash the hot path (player_deltas/stats/free_bonus/settle run every request) ---
    junk_log = [
        {}, {"player": "Erol"}, {"player": "Erol", "status": "pending", "stake": "5"},
        {"player": None, "status": "pending", "stake": 5}, {"status": "pending", "stake": 5},
        "not a dict", None, 12345, {"player": "Erol", "status": "won", "stake": 5, "return": "abc"},
        {"player": "Erol", "credit": True, "amount": "xyz"},
        {"player": "Erol", "status": "pending", "stake": 5, "legs": "notalist"},
        {"player": "Erol", "status": "pending", "stake": 5, "legs": ["x", "y"]},
        {"player": "Erol", "status": "pending", "stake": 5, "legs": [{"matchId": "m1", "selection": "HOME"}]},
        {"player": "Erol", "status": "pending", "stake": 5, "matchId": "m1"},
    ]
    _fm = {"id": "m1", "home": "Brazil", "away": "Serbia", "status": "FINISHED",
           "homeScore": 2, "awayScore": 1, "winner": "HOME", "stage": "GROUP_STAGE"}
    crashed = None
    try:
        wager.player_deltas(junk_log); wager.stats(junk_log); wager.leaders(junk_log)
        wager.free_bonus("Erol", junk_log); wager.available_points("Erol", 20, junk_log)
        wager.applied_points(20, "Erol", junk_log); wager.leaderboard_net("Erol", junk_log)
        wager.settle([dict(x) if isinstance(x, dict) else x for x in junk_log], _fm)
        wager.settle_all([dict(x) if isinstance(x, dict) else x for x in junk_log], [_fm])
    except Exception as e:
        crashed = repr(e)
    ck("malformed wager records never crash the money/settlement functions", crashed is None, crashed)
    # non-numeric / junk stakes are rejected cleanly (no crash, nothing appended) — covers letters, empty, NaN, inf, mixed
    for bad in ["abc", "", "   ", None, [], {}, "5abc", "1.2.3", "ten", "$5", float("nan"), float("inf"), "1e9999"]:
        _w = []
        try:
            _ok, _res = wager.place(_w, "Erol", fx(), "HOME", bad, settled_points=999, comp_home=80, comp_away=40)
            _crash = False
        except Exception as e:
            _ok, _res, _crash = True, e, True
        ck("junk stake %r rejected, no wager, no crash" % (bad,), (_ok is False) and len(_w) == 0 and not _crash, (_ok, _res, len(_w)))
    # a big-odds underdog hitting the return cap — set an explicit cap since default is now unlimited
    _saved_mr = wager.MAX_RETURN
    wager.MAX_RETURN = 140
    ok, res = wager.place([], "Erol", fx(), "AWAY", 25, settled_points=9999, comp_home=99, comp_away=1)
    ck("return cap enforced when admin sets one", (not ok and "cap" in res) or (ok and res["return"] <= 140), res)
    wager.MAX_RETURN = None
    ok2, res2 = wager.place([], "Erol", fx(), "AWAY", 25, settled_points=9999, comp_home=99, comp_away=1)
    ck("no return cap by default (big win allowed)", ok2 and res2["return"] > 140, res2)
    wager.MAX_RETURN = _saved_mr

    # --- pre-kickoff lock: can't bet once it's live / finished / past kickoff ---
    for st in ("IN_PLAY", "PAUSED", "FINISHED", "AWARDED"):
        ok, _ = wager.place([], "Erol", fx(status=st), "HOME", 5, settled_points=99, comp_home=80, comp_away=40)
        ck("can't bet on a %s game" % st, not ok)
    ok, _ = wager.place([], "Erol", fx(status="TIMED", utc=PAST), "HOME", 5, settled_points=99, comp_home=80, comp_away=40)
    ck("can't bet after kickoff time even if status lags", not ok)

    # --- a game that kicks off past midnight stays bettable until its REAL kickoff (calendar day is irrelevant) ---
    late_now = wager._utc_ts("2026-06-17T22:30:00Z")            # 22:30 on the 17th
    ck("a 03:00-next-day game is bettable at 22:30 the night before",
       wager.can_bet_on({"status": "TIMED", "utcDate": "2026-06-18T03:00:00Z"}, now=late_now))
    ck("...and is closed once that 03:00 kickoff passes",
       not wager.can_bet_on({"status": "TIMED", "utcDate": "2026-06-18T03:00:00Z"},
                            now=wager._utc_ts("2026-06-18T03:00:01Z")))
    ok, _ = wager.place([], "Erol", {"home": "Brazil", "away": "Serbia", "stage": "GROUP_STAGE",
                                     "utcDate": "2026-06-18T03:00:00Z", "status": "TIMED"},
                        "HOME", 5, settled_points=99, comp_home=80, comp_away=40, now=late_now)
    ck("can place on a past-midnight game before it starts", ok)

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
    # Stake 10 against 10 earned + a 5 free bonus: the bonus covers 5 of the stake, so only 5 of *real* points are
    # held off the board (free-funded stake never drags the leaderboard down — mirrors how losses are cushioned).
    held = []
    wager.place(held, "Erol", fx(mid="H"), "HOME", 10, settled_points=10, comp_home=80, comp_away=40)
    ck("real (beyond-bonus) open stake is held (applied points drop)", wager.applied_points(10, "Erol", held) == 5.0,
       wager.applied_points(10, "Erol", held))
    ck("applied points floored at 0", wager.applied_points(2, "Erol", held) == 0.0,
       wager.applied_points(2, "Erol", held))

    # --- a settled win still shows on the leaderboard even while free-funded stakes are riding (the live bug) ---
    # 0 earned points, a claimed free-points drop (free bonus = 10), one settled WIN (+1.2) and one settled LOSS
    # (-1.0) => +0.2 net, plus 8 points riding on open bets — all of it funded by the free bonus. The +0.2 must
    # reach the board (free-funded opens don't hold real points), not be buried to 0 by the held stake.
    erol = [{"player": "Erol", "credit": True, "amount": 5, "status": "credit"},
            {"player": "Erol", "status": "won", "stake": 1, "return": 2.2},
            {"player": "Erol", "status": "lost", "stake": 1, "return": 0},
            {"player": "Erol", "status": "pending", "stake": 5},
            {"player": "Erol", "status": "pending", "stake": 1},
            {"player": "Erol", "status": "pending", "stake": 2}]
    ck("settled win shows despite free-funded open stakes", wager.applied_points(0.0, "Erol", erol) == 0.2,
       wager.applied_points(0.0, "Erol", erol))
    ck("no real points are held when opens are free-funded", wager.leaderboard_held("Erol", erol) == 0.0,
       wager.leaderboard_held("Erol", erol))

    # --- stats + leaders: most wagered / won / lost ---
    sw = []
    wager.place(sw, "Erol", fx(mid="s1"), "HOME", 5, 99, 80, 40)
    wager.place(sw, "Erol", fx(mid="s2"), "AWAY", 4, 99, 40, 80)
    wager.place(sw, "James", fx(mid="s3"), "HOME", 10, 99, 70, 50)
    win_ret = sw[0]["return"]
    wager.settle(sw, fx(mid="s1", status="FINISHED", winner="HOME", hs=1, as_=0))   # Erol wins
    wager.settle(sw, fx(mid="s2", status="FINISHED", winner="HOME", hs=1, as_=0))   # Erol loses (bet AWAY)
    wager.settle(sw, fx(mid="s3", status="FINISHED", winner="HOME", hs=2, as_=0))   # James wins
    st = wager.stats(sw)
    ck("stats: staked totals", st["Erol"]["staked"] == 9.0 and st["James"]["staked"] == 10.0, st)
    ck("stats: lost tally", st["Erol"]["lost"] == 4.0, st["Erol"])
    L = wager.leaders(sw)
    ck("leader: most wagered = James (10)", L["most_wagered"]["player"] == "James", L["most_wagered"])
    ck("leader: most lost = Erol", L["most_lost"]["player"] == "Erol", L["most_lost"])

    # --- lock after the final game ---
    ck("betting locked when final done", wager.betting_locked({"stats": {"teams_remaining": 1, "matches_played": 64}}))
    ck("betting open mid-tournament", not wager.betting_locked({"stats": {"teams_remaining": 8, "matches_played": 50}}))
    ck("betting open pre-tournament", not wager.betting_locked({"stats": {"teams_remaining": 48, "matches_played": 0}}))

    # --- example / integration: a bet flows through scoring into points (survival untouched) ---
    import json
    import tempfile
    import scoring
    t = tempfile.mkdtemp()
    T = {"teams": [{"name": n, "tier": 1, "tier_label": "T1", "weight": 8, "group": "A", "composite": c,
                    "implied_prob": 0.1} for n, c in [("Brazil", 85), ("Spain", 80), ("Japan", 55), ("Ghana", 40)]]}
    br = lambda n: {"name": n, "tier": 1, "group": "A", "composite": 80, "confederation": "?"}
    DR = {"players": [{"name": "Erol", "teams": [br("Brazil"), br("Japan")]},
                      {"name": "James", "teams": [br("Spain"), br("Ghana")]}]}
    base_m = {"stage": "GROUP_STAGE", "group": "A", "minute": None, "duration": "REGULAR",
              "aet": False, "shootout": False, "penHome": None, "penAway": None}
    ms = [dict(base_m, id=1, utcDate="2026-06-11T18:00:00Z", status="FINISHED", home="Brazil", away="Ghana",
               homeScore=3, awayScore=0, winner="HOME"),
          dict(base_m, id=2, utcDate="2099-06-15T18:00:00Z", status="TIMED", home="Spain", away="Japan",
               homeScore=None, awayScore=None, winner=None)]
    json.dump(T, open(t + "/teams.json", "w")); json.dump(DR, open(t + "/draw_result.json", "w"))
    json.dump({"matches": ms}, open(t + "/results.json", "w"))
    comp = lambda wg: scoring.compute(teams_path=t + "/teams.json", draw_path=t + "/draw_result.json",
                                      results_path=t + "/results.json", out=t + "/o.json", wagers=wg)
    d0 = comp(None)
    e0 = next(p for p in d0["players"] if p["name"] == "Erol")
    ck("no-wager path adds no wager fields (no-op when off)", "wager_held" not in e0, list(e0)[:12])
    # turning betting ON with no bets placed must not move any score
    dE = comp([])
    def _scores(d):
        return {m: [(r["name"], r["score"]) for r in (d.get("leaderboards") or {}).get(m, [])]
                for m in ("points", "survival")}
    ck("betting on with zero bets leaves every leaderboard identical", _scores(dE) == _scores(d0),
       (_scores(dE), _scores(d0)))
    bets = []
    ok, _ = wager.place(bets, "Erol", dict(base_m, id=2, home="Spain", away="Japan", status="TIMED",
                                           utcDate="2099-06-15T18:00:00Z"), "HOME", 10,
                        settled_points=e0["points"], comp_home=80, comp_away=55)
    d1 = comp(bets)
    e1 = next(p for p in d1["players"] if p["name"] == "Erol")
    # stake 10 with a 5 free bonus: the bonus covers 5, so 5 of Erol's real points are held off the board
    ck("placing holds the (beyond-bonus) stake in points", e1["points"] == e0["points"] - 5, (e0["points"], e1["points"]))
    ck("survival untouched by a bet", e1["survival"] == e0["survival"], (e0["survival"], e1["survival"]))
    pts_row = next(r for r in d1["leaderboards"]["points"] if r["name"] == "Erol")
    ck("held stake shows in the points leaderboard", pts_row["score"] == e0["points"] - 5, (pts_row["score"],))
    ms[1].update(status="FINISHED", homeScore=2, awayScore=0, winner="HOME")
    json.dump({"matches": ms}, open(t + "/results.json", "w"))
    wager.settle_all(bets, ms)
    d2 = comp(bets)
    e2 = next(p for p in d2["players"] if p["name"] == "Erol")
    profit = round(bets[0]["return"] - 10, 1)
    ck("winning bet adds profit to points", e2["points"] == round(e0["points"] + profit, 1), (e0["points"], e2["points"], profit))

    # --- no underflow: heavy losses never push points or available below zero ---
    neg = []
    for i in range(6):
        ok, _ = wager.place(neg, "Erol", fx(mid="n%d" % i), "HOME", 5, settled_points=10, comp_home=80, comp_away=40)
    # only the first three (10 earned + 5 free bonus = 15, at 5 each) should be accepted; the rest exceed available
    ck("can't stake beyond earned + bonus (no overdraw)", len([w for w in neg if w["status"] == "pending"]) == 3,
       len(neg))
    for w in neg:                                   # lose them all
        wager.settle([w], fx(mid=w["matchId"], status="FINISHED", winner="AWAY", hs=0, as_=1))
    ck("available floored at 0 after total loss", wager.available_points("Erol", 10, neg) == 0.0,
       wager.available_points("Erol", 10, neg))
    ck("applied points floored at 0 after total loss", wager.applied_points(10, "Erol", neg) == 0.0,
       wager.applied_points(10, "Erol", neg))
    ck("no negative ever in applied points", wager.applied_points(0, "Erol", neg) >= 0)

    # --- accumulators (up to 3 legs; all must win; odds multiply) ---
    def leg(mid, ch, ca, sel="HOME"):
        return {"match": fx(mid=mid, utc=FUTURE), "selection": sel, "comp_home": ch, "comp_away": ca}
    acc = []
    ok, res = wager.place_acca(acc, "Erol", [leg("a1", 80, 40), leg("a2", 70, 50), leg("a3", 60, 55)], 5,
                               settled_points=50)
    ck("3-leg acca accepted", ok and res.get("type") == "acca" and len(res["legs"]) == 3, res)
    if ok:
        prod = 1.0
        for lg in res["legs"]:
            prod *= (1 + lg["num"] / lg["den"])
        ck("acca odds multiply (return = stake x product)", res["return"] == round(5 * prod, 2),
           (res["return"], round(5 * prod, 1)))
    ck("acca default leg limit is 5", wager.MAX_ACCA_LEGS == 5, wager.MAX_ACCA_LEGS)
    ck("acca rejects more than the leg limit",
       not wager.place_acca([], "Erol", [leg("b%d" % i, 80, 40) for i in range(wager.MAX_ACCA_LEGS + 1)], 5, 200)[0],
       wager.MAX_ACCA_LEGS)
    # admin can raise the limit
    _saved_legs = wager.MAX_ACCA_LEGS
    wager.MAX_ACCA_LEGS = 5
    ck("admin-raised limit: 5-leg acca accepted", wager.place_acca([], "Erol", [leg("e%d" % i, 80, 40) for i in range(5)], 5, 200)[0])
    ck("admin-raised limit: 6-leg still rejected", not wager.place_acca([], "Erol", [leg("f%d" % i, 80, 40) for i in range(6)], 5, 200)[0])
    wager.MAX_ACCA_LEGS = _saved_legs
    ck("acca rejects duplicate game", not wager.place_acca([], "Erol", [leg("dup", 80, 40), leg("dup", 70, 50)], 5, 50)[0])
    ck("acca can't bet nothing", not wager.place_acca([], "Erol", [leg("c1", 80, 40), leg("c2", 70, 50)], 0, 50)[0])
    # settle: all legs win -> acca pays the combined return
    aw = []
    wager.place_acca(aw, "Erol", [leg("w1", 80, 40), leg("w2", 70, 50)], 4, 50)
    combined = aw[0]["return"]
    wager.settle(aw, fx(mid="w1", status="FINISHED", winner="HOME", hs=2, as_=0))
    ck("acca still pending after one leg lands", aw[0]["status"] == "pending", aw[0]["status"])
    wager.settle(aw, fx(mid="w2", status="FINISHED", winner="HOME", hs=1, as_=0))
    ck("acca pays combined return when all legs win", aw[0]["status"] == "won" and aw[0]["return"] == combined,
       (aw[0]["status"], aw[0]["return"], combined))
    # settle: one leg loses -> whole acca lost, returns 0
    al = []
    wager.place_acca(al, "Erol", [leg("l1", 80, 40), leg("l2", 70, 50)], 4, 50)
    wager.settle(al, fx(mid="l1", status="FINISHED", winner="HOME", hs=2, as_=0))
    wager.settle(al, fx(mid="l2", status="FINISHED", winner="AWAY", hs=0, as_=1))   # second leg loses
    ck("acca lost if any leg loses (return 0)", al[0]["status"] == "lost" and al[0]["return"] == 0, al[0])
    # a void leg drops out, acca pays on the remaining winners
    av = []
    wager.place_acca(av, "Erol", [leg("v1", 80, 40), leg("v2", 70, 50)], 4, 50)
    one = (1 + av[0]["legs"][0]["num"] / av[0]["legs"][0]["den"])
    wager.settle(av, fx(mid="v1", status="FINISHED", winner="HOME", hs=2, as_=0))
    wager.settle(av, fx(mid="v2", status="POSTPONED"))
    ck("void leg drops out of the acca", av[0]["status"] == "won" and av[0]["return"] == round(4 * one, 1), av[0])

    # --- max stake rises a clean +5 each round: 30 → 35 → 40 → 45 → 50 → 55 → 60 ---
    ck("group cap is the base 30", wager.stage_max_stake("GROUP_STAGE") == 30)
    ck("caps step +5 every round through the final", [wager.stage_max_stake(s) for s in
       ("LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL")] == [35, 40, 45, 50, 55, 60],
       [wager.stage_max_stake(s) for s in ("LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL")])
    ck("final cap is 60 (+5 from the semis)", wager.stage_max_stake("FINAL") == 60, wager.stage_max_stake("FINAL"))
    ck("31 rejected on a group game", not wager.place([], "Erol", fx(stage="GROUP_STAGE"), "HOME", 31, 200, 80, 40)[0])
    ck("35 allowed on an R32 game", wager.place([], "Erol", fx(mid="r32", stage="LAST_32"), "HOME", 35, 200, 80, 40)[0])
    ck("36 rejected on an R32 game", not wager.place([], "Erol", fx(mid="r32b", stage="LAST_32"), "HOME", 36, 200, 80, 40)[0])
    # the per-round budget is always 20 above that round's cap, so the cap is reachable: 60 in the final is allowed, 61 rejected.
    ck("the final's 60 cap is reachable (budget is 80) and 61 is rejected",
       wager.place([], "Erol", fx(mid="fin", stage="FINAL"), "HOME", 60, 200, 200, 40)[0]
       and not wager.place([], "Erol", fx(mid="fin2", stage="FINAL"), "HOME", 61, 200, 200, 40)[0])
    # an acca uses the LOWEST leg's cap (most conservative)
    mix = [{"match": fx(mid="mg", stage="GROUP_STAGE"), "selection": "HOME", "comp_home": 80, "comp_away": 40},
           {"match": fx(mid="mf", stage="FINAL"), "selection": "HOME", "comp_home": 70, "comp_away": 50}]
    ck("acca cap is the lowest leg's cap", not wager.place_acca([], "Erol", mix, 31, 200)[0] and
       wager.place_acca([], "Erol", mix, 30, 200)[0])

    # --- settlement across FT / draw / AET / pens / void / forfeit (singles) ---
    def settle_one(sel, **mk):
        w = []
        ok, _ = wager.place(w, "Erol", fx(mid="s_%s_%s" % (sel, mk.get("mid", "x")), stage="LAST_16"), sel, 5, 200, 80, 40)
        wager.settle(w, fx(status="FINISHED", mid="s_%s_%s" % (sel, mk.get("mid", "x")), stage="LAST_16", **{k: v for k, v in mk.items() if k != "mid"}))
        return w[0]
    # full-time home win: HOME wins, AWAY loses
    ck("FT home win pays HOME", settle_one("HOME", mid="ft", winner="HOME", hs=2, as_=0)["status"] == "won")
    ck("FT home win sinks AWAY", settle_one("AWAY", mid="ft", winner="HOME", hs=2, as_=0)["status"] == "lost")
    # extra time: feed marks winner after AET (score includes ET)
    ck("AET winner pays the side that went through", settle_one("AWAY", mid="aet", winner="AWAY", hs=1, as_=2)["status"] == "won")
    # penalties WITH a winner field (the normal feed case): level score, shootout decides
    ck("pens (winner field) pays the advancing side", settle_one("HOME", mid="pf", winner="HOME", hs=1, as_=1, penHome=4, penAway=2)["status"] == "won")
    ck("pens (winner field) sinks the loser", settle_one("AWAY", mid="pf", winner="HOME", hs=1, as_=1, penHome=4, penAway=2)["status"] == "lost")
    # penalties with NO winner field — must fall back to the shootout, NOT call it a draw
    ck("pens (no winner field) resolves to shootout winner", settle_one("AWAY", mid="pn", hs=1, as_=1, penHome=2, penAway=4)["status"] == "won")
    ck("pens (no winner field) sinks the shootout loser", settle_one("HOME", mid="pn2", hs=1, as_=1, penHome=2, penAway=4)["status"] == "lost")
    # void (postponed/abandoned) -> refund
    vw = []
    wager.place(vw, "Erol", fx(mid="vp", stage="LAST_16"), "HOME", 5, 200, 80, 40)
    wager.settle(vw, fx(mid="vp", stage="LAST_16", status="POSTPONED"))
    ck("postponed game voids (refund)", vw[0]["status"] == "void")
    # forfeit / awarded result still settles
    fw = []
    wager.place(fw, "Erol", fx(mid="aw", stage="LAST_16"), "HOME", 5, 200, 80, 40)
    wager.settle(fw, fx(mid="aw", stage="LAST_16", status="AWARDED", winner="HOME", hs=3, as_=0))
    ck("AWARDED (forfeit) settles the bet", fw[0]["status"] == "won", fw[0])
    # an acca leg decided on pens settles correctly (advancing side wins the leg)
    apw = []
    wager.place_acca(apw, "Erol",
                     [{"match": fx(mid="ap1", stage="LAST_16"), "selection": "HOME", "comp_home": 80, "comp_away": 40},
                      {"match": fx(mid="ap2", stage="LAST_16"), "selection": "AWAY", "comp_home": 50, "comp_away": 60}], 4, 200)
    wager.settle(apw, fx(mid="ap1", stage="LAST_16", status="FINISHED", winner="HOME", hs=2, as_=1))
    wager.settle(apw, fx(mid="ap2", stage="LAST_16", status="FINISHED", hs=1, as_=1, penHome=2, penAway=4))  # away win on pens
    ck("acca leg on pens settles (advancing side wins the leg)", apw[0]["status"] == "won", apw[0])

    # --- total open stake can't exceed the current cap (frees up as bets settle) ---
    agg = []
    a1 = wager.place(agg, "Erol", fx(mid="ag1"), "HOME", 20, 500, 80, 40)[0]
    a2 = wager.place(agg, "Erol", fx(mid="ag2"), "HOME", 10, 500, 80, 40)[0]   # 20+10=30 == cap, ok
    a3ok, a3msg = wager.place(agg, "Erol", fx(mid="ag3"), "HOME", 1, 500, 80, 40)  # would be 31 > 30
    ck("open stakes capped at the per-round max (30)", a1 and a2 and not a3ok, (a1, a2, a3ok, a3msg))
    wager.settle(agg, fx(mid="ag1", status="FINISHED", winner="HOME", hs=2, as_=0))   # frees 20
    a4ok, _ = wager.place(agg, "Erol", fx(mid="ag4"), "HOME", 15, 500, 80, 40)         # now 10+15=25 ok
    ck("open-stake headroom returns once a bet settles", a4ok, a4ok)
    # the cap also covers accas + singles together
    mix2 = []
    wager.place(mix2, "Erol", fx(mid="mx1", stage="LAST_32"), "HOME", 20, 500, 80, 40)   # cap 35
    accmix = wager.place_acca(mix2, "Erol",
                              [{"match": fx(mid="mx2", stage="LAST_32"), "selection": "HOME", "comp_home": 80, "comp_away": 40},
                               {"match": fx(mid="mx3", stage="LAST_32"), "selection": "HOME", "comp_home": 70, "comp_away": 50}], 20, 500)
    ck("acca blocked when it would breach the open-stake cap", not accmix[0], accmix)

    # --- no draw bets on knockout games (draws can't happen — it goes to ET/pens) ---
    ck("draw rejected on a knockout single", not wager.place([], "Erol", fx(mid="koD", stage="QUARTER_FINALS"), "DRAW", 5, 200, 70, 60)[0])
    ck("home still allowed on a knockout", wager.place([], "Erol", fx(mid="koH", stage="QUARTER_FINALS"), "HOME", 5, 200, 70, 60)[0])
    ck("draw still allowed in the group stage", wager.place([], "Erol", fx(mid="gpD", stage="GROUP_STAGE"), "DRAW", 5, 200, 70, 60)[0])
    ck("draw leg rejected in an acca", not wager.place_acca([], "Erol",
       [{"match": fx(mid="kaL", stage="LAST_16"), "selection": "DRAW", "comp_home": 70, "comp_away": 60},
        {"match": fx(mid="kbL", stage="LAST_16"), "selection": "HOME", "comp_home": 80, "comp_away": 40}], 4, 200)[0])

    # --- per-epoch staking budget (STAGE_BUDGET); both this and the per-bet cap apply ---
    B = wager.STAGE_BUDGET
    g1 = "2026-06-15T18:00:00Z"; g2 = "2026-06-25T18:00:00Z"
    mid = wager._utc_ts("2026-06-20T00:00:00Z")          # split the group stage in half here
    g1_now = wager._utc_ts("2026-06-15T17:30:00Z")       # anchor "now" just before g1 kicks off so these stay time-independent
    ck("fresh epoch budget == STAGE_BUDGET", wager.budget_remaining([], "Erol", "GROUP_1") == B)
    ck("group first/second halves are different epochs",
       wager.epoch_of(fx(utc=g1), mid) == "GROUP_1" and wager.epoch_of(fx(utc=g2), mid) == "GROUP_2")
    ck("each knockout round is its own epoch", wager.epoch_of(fx(stage="LAST_16"), mid) == "LAST_16")
    # budget = budget - stakes + returns(won), clamped [0, B]
    net = [{"player": "Erol", "epoch": "GROUP_1", "stake": 50, "status": "lost", "return": 0},
           {"player": "Erol", "epoch": "GROUP_1", "stake": 10, "status": "won", "return": 25}]
    ck("budget tracks net losses+wins (B-60+25)", wager.budget_remaining(net, "Erol", "GROUP_1") == max(0.0, min(B, B - 60 + 25)),
       wager.budget_remaining(net, "Erol", "GROUP_1"))
    topped = [{"player": "Erol", "epoch": "GROUP_1", "stake": 20, "status": "lost", "return": 0},
              {"player": "Erol", "epoch": "GROUP_1", "stake": 10, "status": "won", "return": 40}]
    ck("winnings top budget up but never above B", wager.budget_remaining(topped, "Erol", "GROUP_1") == B,
       wager.budget_remaining(topped, "Erol", "GROUP_1"))
    locked = [{"player": "Erol", "epoch": "GROUP_1", "stake": 100, "status": "lost", "return": 0}]
    ck("budget can hit 0", wager.budget_remaining(locked, "Erol", "GROUP_1") == 0)
    lk_ok, lk_msg = wager.place(locked, "Erol", fx(mid="lk1", utc=g1), "HOME", 1, 500, 80, 40, group_mid_ts=mid, now=g1_now)
    ck("0 budget = locked out of this epoch", not lk_ok and "budget" in lk_msg.lower(), lk_msg)
    ck("a different epoch is unaffected by GROUP_1 losses (and carries its own +5/round budget)",
       wager.budget_remaining(locked, "Erol", "GROUP_2") == wager.stage_budget("GROUP_2")
       and wager.budget_remaining(locked, "Erol", "LAST_16") == wager.stage_budget("LAST_16"))
    ck("budget rises +5 each round and is always 20 above that round's per-bet cap",
       [wager.stage_budget(e) for e in ("GROUP_1", "LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL")]
       == [50, 55, 60, 65, 70, 75, 80]
       and all(wager.stage_budget(e) == wager.stage_max_stake(s) + 20
               for e, s in (("GROUP_1", "GROUP_STAGE"), ("LAST_32", "LAST_32"), ("FINAL", "FINAL"))))
    cap_ok, cap_msg = wager.place([], "Erol", fx(mid="cp1", utc=g1), "HOME", 40, 500, 80, 40, group_mid_ts=mid, now=g1_now)
    ck("per-bet cap (30) still applies even with full budget", not cap_ok and "30" in cap_msg, cap_msg)
    # a real placement lands in the right epoch and reduces that epoch's budget
    liveb = []
    wager.place(liveb, "Erol", fx(mid="e1", utc=g1), "HOME", 20, 500, 80, 40, group_mid_ts=mid, now=g1_now)
    ck("placement tags the epoch + spends from it", liveb[0]["epoch"] == "GROUP_1"
       and wager.budget_remaining(liveb, "Erol", "GROUP_1") == B - 20, liveb[0].get("epoch"))

    # --- free bets: free stake never costs the player; only a WIN's profit is credited ---
    fb = []
    fok, fw = wager.place_free(fb, "Erol", fx(mid="fb1"), "HOME", 80, 40)
    ck("place_free makes a free 5-point bet", fok and fw.get("free") and fw["stake"] == wager.FREE_BET_STAKE, fw)
    ck("free pending holds no points (available unchanged)", wager.available_points("Erol", 100, fb) == wager.available_points("Erol", 100, []),
       wager.available_points("Erol", 100, fb))
    ck("free pending uses no staking budget", wager.budget_remaining(fb, "Erol", "GROUP_1") == B,
       wager.budget_remaining(fb, "Erol", "GROUP_1"))
    # settle that free bet as a WIN -> only profit (return-5) is credited
    wager.settle(fb, fx(mid="fb1", status="FINISHED", winner="HOME", hs=1, as_=0))
    won_profit = round(fb[0]["return"] - wager.FREE_BET_STAKE, 1)
    ck("free WIN credits profit only (not the 5 stake)",
       round(wager.player_deltas(fb)["Erol"]["settled_net"], 1) == won_profit, (fb[0], won_profit))
    # a LOST free bet costs nothing
    fb2 = []
    wager.place_free(fb2, "Erol", fx(mid="fb2"), "HOME", 80, 40)
    wager.settle(fb2, fx(mid="fb2", status="FINISHED", winner="AWAY", hs=0, as_=2))
    ck("free LOSS costs the player nothing", wager.player_deltas(fb2).get("Erol", {}).get("settled_net", 0.0) == 0.0,
       wager.player_deltas(fb2))
    ck("free bet never counts toward the budget after losing",
       wager.budget_remaining(fb2, "Erol", "GROUP_1") == B, wager.budget_remaining(fb2, "Erol", "GROUP_1"))
    ck("free draw rejected on a knockout", not wager.place_free([], "Erol", fx(mid="fk", stage="LAST_16"), "DRAW", 70, 60)[0])
    ck("free bet rejected once the game is closed",
       not wager.place_free([], "Erol", fx(mid="fc", status="FINISHED", utc=PAST), "HOME", 80, 40)[0])

    # --- 5-point starting bonus: lets you bet from 0 earned; only winnings hit the leaderboard ---
    ck("everyone can stake the free bonus at 0 earned", wager.available_points("Erol", 0, []) == wager.STARTING_BONUS,
       wager.available_points("Erol", 0, []))
    lost5 = [{"player": "Erol", "epoch": "GROUP_1", "stake": 5, "status": "lost", "return": 0}]
    ck("losing the bonus costs 0 available", wager.available_points("Erol", 0, lost5) == 0, wager.available_points("Erol", 0, lost5))
    ck("losing the bonus does NOT dent the leaderboard", wager.leaderboard_net("Erol", lost5) == 0.0, wager.leaderboard_net("Erol", lost5))
    ck("losing the bonus leaves earned points intact", wager.applied_points(40, "Erol", lost5) == 40, wager.applied_points(40, "Erol", lost5))
    lost8 = [{"player": "Erol", "epoch": "GROUP_1", "stake": 8, "status": "lost", "return": 0}]
    ck("losses beyond the 5 bonus do hit the leaderboard (8 lost -> -3)", wager.leaderboard_net("Erol", lost8) == -3.0, wager.leaderboard_net("Erol", lost8))
    won = [{"player": "Erol", "epoch": "GROUP_1", "stake": 5, "status": "won", "return": 12}]
    ck("a win adds profit to the leaderboard (12-5=7)", wager.leaderboard_net("Erol", won) == 7.0, wager.leaderboard_net("Erol", won))
    ck("the bonus is on top of earned points for staking", wager.available_points("Erol", 40, []) == 40 + wager.STARTING_BONUS,
       wager.available_points("Erol", 40, []))

    # --- at most 2 OPEN accumulators per player (single bets don't count) ---
    accs = []
    ck("first acca accepted", wager.place_acca(accs, "Erol", [leg("ac1a", 80, 40), leg("ac1b", 70, 50)], 5, 200)[0])
    ck("second acca accepted", wager.place_acca(accs, "Erol", [leg("ac2a", 80, 40), leg("ac2b", 70, 50)], 5, 200)[0])
    _ok3, _r3 = wager.place_acca(accs, "Erol", [leg("ac3a", 80, 40), leg("ac3b", 70, 50)], 5, 200)
    ck("a 3rd open acca is rejected", (not _ok3) and "accumulators running" in _r3.lower(), _r3)
    ck("a single bet is still allowed alongside 2 open accas",
       wager.place(accs, "Erol", fx(mid="solo1"), "HOME", 5, 200, 80, 40)[0], None)

    # --- free-points drops: claiming adds free betting points (bet-only; leaderboard untouched) ---
    fp = []
    gok, gw = wager.grant_free_points(fp, "Erol", "drop-1")
    ck("grant_free_points adds a credit", gok and gw.get("credit") and gw["amount"] == wager.FREE_BET_STAKE, gw)
    ck("a claimed drop lifts free betting points by 5", wager.free_bonus("Erol", fp) == wager.STARTING_BONUS + 5, wager.free_bonus("Erol", fp))
    ck("a claimed drop lifts available-to-stake by 5", wager.available_points("Erol", 0, fp) == wager.STARTING_BONUS + 5, wager.available_points("Erol", 0, fp))
    ck("a claimed drop does NOT touch the leaderboard", wager.applied_points(40, "Erol", fp) == 40, wager.applied_points(40, "Erol", fp))
    _dd = wager.player_deltas(fp).get("Erol", {})
    ck("a credit isn't a bet (no pending stake/count)", _dd.get("pending_stake", 0) == 0 and _dd.get("pending_count", 0) == 0, _dd)
    wager.grant_free_points(fp, "Erol", "drop-2")
    ck("two claimed drops stack (free = start + 10)", wager.free_bonus("Erol", fp) == wager.STARTING_BONUS + 10, wager.free_bonus("Erol", fp))
    fp2 = fp + [{"player": "Erol", "epoch": "GROUP_1", "stake": 8, "status": "lost", "return": 0}]
    ck("losing 8 with start+10 free -> leaderboard unaffected", wager.leaderboard_net("Erol", fp2) == 0.0, wager.leaderboard_net("Erol", fp2))

    # --- form-adjusted odds (bounded, deterministic, see==get) ---
    FM = [{"home": "A", "away": "B", "homeScore": 4, "awayScore": 0, "winner": "HOME", "status": "FINISHED"},
          {"home": "C", "away": "B", "homeScore": 3, "awayScore": 0, "winner": "HOME", "status": "FINISHED"}]
    ck("no games played -> no form change (1.0)", wager.team_form("Z", FM) == 1.0, wager.team_form("Z", FM))
    ck("a winner is stronger (>1) but bounded to +12%", 1.0 < wager.team_form("A", FM) <= 1.12 + 1e-9, wager.team_form("A", FM))
    ck("a loser is weaker (<1) but bounded to -12%", 0.88 - 1e-9 <= wager.team_form("B", FM) < 1.0, wager.team_form("B", FM))
    ck("form is deterministic (same data, same number)", wager.team_form("B", FM) == wager.team_form("B", FM), True)
    _live = [{"home": "A", "away": "W", "homeScore": 0, "awayScore": 3, "winner": "AWAY", "status": "IN_PLAY"}]
    ck("a LIVE match does not move form (only finished games count)", wager.team_form("A", _live) == 1.0, wager.team_form("A", _live))
    _seen = wager.match_odds(wager.live_strength(80, "A", FM), wager.live_strength(55, "B", FM))
    _got = wager.match_odds(wager.live_strength(80, "A", FM), wager.live_strength(55, "B", FM))
    ck("see == get: display and placement price identically", _seen == _got, True)

    if FAILS:
        print("\nFAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        sys.exit(1)
    print("\nAll wager-engine tests passed.")


if __name__ == "__main__":
    run()
