"""
Unexpected-scenario tests for live tournament handling.

Cycles the scoring + normalizer through the messy real-world cases that break naive
scoreboards: kickoff/live, abandoned/suspended, postponed, cancelled, forfeit (AWARDED),
results changing AFTER they were final, and penalty shootouts. Each asserts that points,
eliminations and the live view do the right thing.
"""
import json
import os
import shutil
import tempfile

import scoring
import update_results

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -> " + str(detail)))
    if not cond:
        FAILS.append(name)


# --- tiny fixture: 2 players, 4 teams, one group + a final ----------------------------
TEAMS = {"teams": [
    {"name": "Brazil", "tier": 1, "tier_label": "T1", "weight": 8, "group": "A", "composite": 90, "implied_prob": 0.20},
    {"name": "Spain", "tier": 1, "tier_label": "T1", "weight": 8, "group": "A", "composite": 88, "implied_prob": 0.18},
    {"name": "Japan", "tier": 2, "tier_label": "T2", "weight": 4, "group": "A", "composite": 55, "implied_prob": 0.05},
    {"name": "Ghana", "tier": 3, "tier_label": "T3", "weight": 2, "group": "A", "composite": 30, "implied_prob": 0.02},
]}
_T = {t["name"]: t for t in TEAMS["teams"]}


def _brief(name):
    t = _T[name]
    return {"name": name, "tier": t["tier"], "group": t["group"], "composite": t["composite"],
            "confederation": t.get("confederation", "?")}


DRAW = {"players": [
    {"name": "Erol", "teams": [_brief("Brazil"), _brief("Japan")]},
    {"name": "James", "teams": [_brief("Spain"), _brief("Ghana")]},
]}


def M(mid, home, away, status, stage="GROUP_STAGE", hs=None, as_=None, winner=None,
      duration="REGULAR", penH=None, penA=None, minute=None):
    """Build a normalized match (the shape scoring.compute reads)."""
    return {"id": mid, "stage": stage, "group": "A" if stage == "GROUP_STAGE" else None,
            "utcDate": "2026-06-%02dT18:00:00Z" % mid, "status": status,
            "home": home, "away": away, "homeScore": hs, "awayScore": as_, "winner": winner,
            "minute": minute, "duration": duration,
            "aet": duration in ("EXTRA_TIME", "PENALTY_SHOOTOUT"),
            "shootout": duration == "PENALTY_SHOOTOUT", "penHome": penH, "penAway": penA}


def run_compute(matches, tmp):
    json.dump(TEAMS, open(os.path.join(tmp, "teams.json"), "w"))
    json.dump(DRAW, open(os.path.join(tmp, "draw_result.json"), "w"))
    json.dump({"matches": matches}, open(os.path.join(tmp, "results.json"), "w"))
    return scoring.compute(
        teams_path=os.path.join(tmp, "teams.json"),
        draw_path=os.path.join(tmp, "draw_result.json"),
        results_path=os.path.join(tmp, "results.json"),
        out=os.path.join(tmp, "tracker_data.json"))


def pts(d, player):
    return next((p["score"] for p in d["leaderboards"]["points"] if p["name"] == player), None)


def team_status(d, team):
    for p in d["players"]:
        for t in p["teams"]:
            if t["name"] == team:
                return t["status"]
    return None


def run():
    tmp = tempfile.mkdtemp()
    try:
        # 1) KICKOFF / live: points accrue LIVE (fantasy-style) but the game is not yet "played" and nobody is eliminated
        d = run_compute([M(1, "Brazil", "Japan", "IN_PLAY", hs=1, as_=0, minute=30)], tmp)
        # Brazil live 1-0: 1 goal + provisional win 3 + provisional clean sheet 1 = 5
        check("live game scores provisionally (fantasy-style)", pts(d, "Erol") == 5, pts(d, "Erol"))
        check("live game not counted as played", d["stats"]["matches_played"] == 0, d["stats"])
        check("both teams still alive during live group game",
              team_status(d, "Brazil") == "alive" and team_status(d, "Japan") == "alive", "")

        # 1b) live points climb as goals go in, and a VAR-disallowed goal lowers them again on recompute
        d = run_compute([M(1, "Brazil", "Japan", "IN_PLAY", hs=2, as_=0, minute=55)], tmp)
        check("live points rise with a 2nd goal (2 goals + win + CS = 6)", pts(d, "Erol") == 6, pts(d, "Erol"))
        d = run_compute([M(1, "Brazil", "Japan", "IN_PLAY", hs=1, as_=0, minute=56)], tmp)
        check("VAR disallows a goal -> live points drop back (5)", pts(d, "Erol") == 5, pts(d, "Erol"))

        # 1c) scheduled KO matches must keep their teams "alive" while a different KO game is live (the R32 false-elimination bug)
        d = run_compute([M(1, "Brazil", "Japan", "IN_PLAY", stage="LAST_32", hs=0, as_=0, minute=20),
                         M(2, "Spain", "Ghana", "TIMED", stage="LAST_32")], tmp)
        check("scheduled R32 teams stay alive while another R32 game is live",
              all(team_status(d, t) == "alive" for t in ["Brazil", "Japan", "Spain", "Ghana"]),
              [(t, team_status(d, t)) for t in ["Brazil", "Japan", "Spain", "Ghana"]])

        # 2) FINISHED group game: full points (goals + win + clean sheet)
        d = run_compute([M(1, "Brazil", "Japan", "FINISHED", hs=3, as_=0, winner="HOME")], tmp)
        # Brazil: 3 goals*1 + win 3 + clean sheet 1 = 7 ; Erol also owns Japan (0)
        check("finished game scores correctly (3 goals + win + CS = 7)", pts(d, "Erol") == 7, pts(d, "Erol"))
        check("finished game counts as played", d["stats"]["matches_played"] == 1, d["stats"])

        # 3) FORFEIT / AWARDED: walkover counts like a finished result (winner + score given by the API)
        d = run_compute([M(1, "Spain", "Ghana", "AWARDED", hs=3, as_=0, winner="HOME")], tmp)
        # James owns Spain: 3 goals + win 3 + clean sheet 1 = 7
        check("forfeit (AWARDED) is scored, not ignored", pts(d, "James") == 7, pts(d, "James"))
        check("forfeit counts as a played match", d["stats"]["matches_played"] == 1, d["stats"])

        # 4) FORFEIT in a knockout eliminates the loser
        ko = [M(10, "Brazil", "Spain", "AWARDED", stage="FINAL", hs=0, as_=3, winner="AWAY")]
        d = run_compute(ko, tmp)
        check("KO forfeit eliminates the loser", team_status(d, "Brazil") == "out", team_status(d, "Brazil"))
        check("KO forfeit advances/keeps the winner alive", team_status(d, "Spain") == "alive", team_status(d, "Spain"))
        check("forfeit final still crowns a champion",
              (d.get("champion_decided") or {}).get("team") == "Spain", d.get("champion_decided"))

        # 5) ABANDONED / SUSPENDED mid-game: no points, nobody eliminated, no crash
        d = run_compute([M(1, "Brazil", "Japan", "SUSPENDED", hs=1, as_=1, minute=55)], tmp)
        check("suspended game scores nothing", pts(d, "Erol") == 0, pts(d, "Erol"))
        check("suspended game not counted as played", d["stats"]["matches_played"] == 0, d["stats"])
        check("suspended group game leaves teams alive", team_status(d, "Brazil") == "alive", "")

        # 6) POSTPONED / CANCELLED: ignored entirely, teams stay alive
        for st in ("POSTPONED", "CANCELLED"):
            d = run_compute([M(1, "Brazil", "Japan", st)], tmp)
            check("%s game scores nothing / not played" % st,
                  pts(d, "Erol") == 0 and d["stats"]["matches_played"] == 0, (st, d["stats"]))

        # 7) RESULT CHANGES AFTER IT WAS FINAL: recompute reflects the correction, no double-count
        d1 = run_compute([M(1, "Brazil", "Spain", "FINISHED", hs=2, as_=1, winner="HOME")], tmp)
        first = pts(d1, "Erol")                                   # Brazil: 2 goals + win 3 = 5
        d2 = run_compute([M(1, "Brazil", "Spain", "FINISHED", hs=1, as_=1, winner="DRAW")], tmp)
        second = pts(d2, "Erol")                                  # corrected: 1 goal + draw 1 = 2
        check("score correction reflected on recompute (was 2-1 -> now 1-1)", first == 5 and second == 2,
              "first=%s second=%s" % (first, second))
        check("correction does not accumulate (still 1 match played)", d2["stats"]["matches_played"] == 1, d2["stats"])

        # 8) WIN OVERTURNED TO LOSS after the fact: eliminated team comes back alive on recompute
        koA = run_compute([M(10, "Brazil", "Spain", "FINISHED", stage="FINAL", hs=2, as_=1, winner="HOME")], tmp)
        check("KO win: loser eliminated", team_status(koA, "Spain") == "out", team_status(koA, "Spain"))
        koB = run_compute([M(10, "Brazil", "Spain", "FINISHED", stage="FINAL", hs=1, as_=2, winner="AWAY")], tmp)
        check("overturned KO result flips who is eliminated",
              team_status(koB, "Spain") == "alive" and team_status(koB, "Brazil") == "out",
              (team_status(koB, "Spain"), team_status(koB, "Brazil")))

        # 9) PENALTY SHOOTOUT: a knockout tie won on penalties counts as a WIN for the advancing team; loser eliminated
        sh = [M(10, "Brazil", "Spain", "FINISHED", stage="FINAL", hs=1, as_=1, winner="HOME",
                duration="PENALTY_SHOOTOUT", penH=4, penA=2)]
        d = run_compute(sh, tmp)
        brec = next((t["record"] for p in d["players"] for t in p["teams"] if t["name"] == "Brazil"), None)
        srec = next((t["record"] for p in d["players"] for t in p["teams"] if t["name"] == "Spain"), None)
        check("penalty-shootout win counts as a win (Brazil 1-0-0)", brec == "1-0-0", brec)
        check("penalty-shootout loss counts as a loss (Spain 0-0-1)", srec == "0-0-1", srec)
        check("shootout loser eliminated", team_status(d, "Spain") == "out", team_status(d, "Spain"))
        check("shootout winner is champion", (d.get("champion_decided") or {}).get("team") == "Brazil",
              d.get("champion_decided"))

        # 11) THIRD-PLACE PLAY-OFF: both semi losers are OUT for survival (capped at the semi value, no bronze
        #     survival bump), but the bronze game still earns POINTS so the winner is "seen in points/hybrid"
        tp = [M(20, "Brazil", "Japan", "FINISHED", stage="SEMI_FINALS", hs=1, as_=0, winner="HOME"),
              M(21, "Spain", "Ghana", "FINISHED", stage="SEMI_FINALS", hs=1, as_=0, winner="HOME"),
              M(22, "Japan", "Ghana", "FINISHED", stage="THIRD_PLACE", hs=2, as_=1, winner="HOME")]
        d = run_compute(tp, tmp)
        jp = next(t for p in d["players"] for t in p["teams"] if t["name"] == "Japan")
        gh = next(t for p in d["players"] for t in p["teams"] if t["name"] == "Ghana")
        check("3rd-place gives NO survival bump — bronze winner capped at the semi value (44)", jp["survival"] == 44, jp["survival"])
        check("4th place also stays at the semi value (44)", gh["survival"] == 44, gh["survival"])
        check("bronze game still earns points — winner outscores the loser", jp["points"] > gh["points"], (jp["points"], gh["points"]))
        check("both semi losers stay 'out' for survival (no chance to win survival)",
              team_status(d, "Japan") == "out" and team_status(d, "Ghana") == "out",
              (team_status(d, "Japan"), team_status(d, "Ghana")))

        # 10) normalizer turns a raw AWARDED API match into a scored result end-to-end
        api = [{"id": 1, "stage": "GROUP_STAGE", "group": "GROUP_A", "utcDate": "2026-06-11T18:00:00Z",
                "status": "AWARDED", "homeTeam": {"name": "Brazil"}, "awayTeam": {"name": "Japan"},
                "score": {"winner": "HOME_TEAM", "duration": "REGULAR",
                          "fullTime": {"home": 3, "away": 0}}}]
        norm = update_results.normalize_matches(api, lambda n: n)
        check("normalizer keeps AWARDED status + score", norm[0]["status"] == "AWARDED"
              and norm[0]["homeScore"] == 3 and norm[0]["winner"] == "HOME", norm[0])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print("FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("All scenario tests passed.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(run())
