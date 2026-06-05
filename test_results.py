"""
Result-correctness tests: build specific finished results and assert the EXACT scoring outcome
(points, survival, records, champion, best defence). Complements test_scenarios.py (edge cases)
and test_2022.py (full real replay). Hand-verified against the SCORING / SURVIVAL_VALUE tables.
"""
import json
import os
import sys
import tempfile
import scoring

FAILS = []


def check(name, got, want):
    ok = got == want
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "  -> got %r want %r" % (got, want)))
    if not ok:
        FAILS.append(name)


TEAMS = {"teams": [
    {"name": "Brazil", "tier": 1, "tier_label": "T1", "weight": 8, "group": "A", "composite": 90, "implied_prob": 0.20},
    {"name": "Spain", "tier": 1, "tier_label": "T1", "weight": 8, "group": "A", "composite": 88, "implied_prob": 0.18},
    {"name": "Japan", "tier": 2, "tier_label": "T2", "weight": 4, "group": "A", "composite": 55, "implied_prob": 0.05},
    {"name": "Ghana", "tier": 3, "tier_label": "T3", "weight": 2, "group": "A", "composite": 30, "implied_prob": 0.02},
]}
_T = {t["name"]: t for t in TEAMS["teams"]}


def _brief(n):
    t = _T[n]
    return {"name": n, "tier": t["tier"], "group": t["group"], "composite": t["composite"], "confederation": "?"}


DRAW = {"players": [
    {"name": "Erol", "teams": [_brief("Brazil"), _brief("Japan")]},
    {"name": "James", "teams": [_brief("Spain"), _brief("Ghana")]},
]}


def M(mid, home, away, stage, hs, as_, winner, status="FINISHED", duration="REGULAR", penH=None, penA=None):
    return {"id": mid, "stage": stage, "group": "A" if stage == "GROUP_STAGE" else None,
            "utcDate": "2026-06-%02dT18:00:00Z" % mid, "status": status, "home": home, "away": away,
            "homeScore": hs, "awayScore": as_, "winner": winner, "minute": None, "duration": duration,
            "aet": duration in ("EXTRA_TIME", "PENALTY_SHOOTOUT"),
            "shootout": duration == "PENALTY_SHOOTOUT", "penHome": penH, "penAway": penA}


def run(matches, tmp):
    json.dump(TEAMS, open(os.path.join(tmp, "teams.json"), "w"))
    json.dump(DRAW, open(os.path.join(tmp, "draw_result.json"), "w"))
    json.dump({"matches": matches}, open(os.path.join(tmp, "results.json"), "w"))
    return scoring.compute(teams_path=os.path.join(tmp, "teams.json"),
                           draw_path=os.path.join(tmp, "draw_result.json"),
                           results_path=os.path.join(tmp, "results.json"),
                           out=os.path.join(tmp, "tracker_data.json"))


def team(d, name):
    return next(t for p in d["players"] for t in p["teams"] if t["name"] == name)


def player(d, name, key):
    return next(p[key] for p in d["players"] if p["name"] == name)


def run_all():
    SC, SV = scoring.SCORING, scoring.SURVIVAL_VALUE
    tmp = tempfile.mkdtemp()

    # 1) Champion in regulation: Brazil beats Spain 2-1 in the final.
    #    Brazil = 2 goals + win + WINNER bonus ; survival = WINNER value ; status alive @ WINNER.
    d = run([M(1, "Brazil", "Spain", "FINAL", 2, 1, "HOME")], tmp)
    b = team(d, "Brazil")
    check("champion points = goals + win + WINNER bonus",
          b["points"], 2 * SC["per_goal"] + SC["win"] + SC["stage_bonus"]["WINNER"])
    check("champion survival = WINNER value", b["survival"], SV["WINNER"])
    check("champion status is alive", b["status"], "alive")
    check("champion furthest stage is WINNER", b["stage"], "WINNER")
    s = team(d, "Spain")
    check("runner-up points = goal + FINAL bonus", s["points"], 1 * SC["per_goal"] + SC["stage_bonus"]["FINAL"])
    check("runner-up survival = FINAL value", s["survival"], SV["FINAL"])
    check("runner-up is out", s["status"], "out")
    check("champion_decided team", (d.get("champion_decided") or {}).get("team"), "Brazil")
    check("champion_decided runner-up", (d.get("champion_decided") or {}).get("runnerUp"), "Spain")

    # 2) Champion on penalties: a shootout win counts as a WIN (not a draw) and still earns the WINNER bonus.
    d = run([M(1, "Brazil", "Spain", "FINAL", 1, 1, "HOME", duration="PENALTY_SHOOTOUT", penH=4, penA=3)], tmp)
    b = team(d, "Brazil")
    check("pens champion record shows a win (1-0-0)", b["record"], "1-0-0")
    check("pens champion points = goal + win + WINNER bonus",
          b["points"], 1 * SC["per_goal"] + SC["win"] + SC["stage_bonus"]["WINNER"])
    check("pens runner-up record shows a loss (0-0-1)", team(d, "Spain")["record"], "0-0-1")
    check("pens winner is champion", (d.get("champion_decided") or {}).get("team"), "Brazil")

    # 3) Owner aggregation: Erol (champion Brazil) clearly outscores James (runner-up Spain) on points AND both.
    d = run([M(1, "Brazil", "Spain", "FINAL", 2, 0, "HOME")], tmp)
    check("champion owner outscores on points", player(d, "Erol", "points") > player(d, "James", "points"), True)
    check("champion owner outscores on hybrid", player(d, "Erol", "hybrid") > player(d, "James", "hybrid"), True)
    check("champion owner has a team still alive", player(d, "Erol", "alive_teams"), 1)

    # 4) Best defence = most clean sheets (not fewest conceded): Brazil keeps 2 clean sheets, Ghana 1.
    d = run([M(1, "Brazil", "Japan", "GROUP_STAGE", 1, 0, "HOME"),
             M(2, "Brazil", "Ghana", "GROUP_STAGE", 2, 0, "HOME"),
             M(3, "Ghana", "Japan", "GROUP_STAGE", 0, 0, "DRAW"),
             M(4, "Spain", "Japan", "GROUP_STAGE", 3, 2, "HOME")], tmp)
    check("best defence is the team with most clean sheets", d["stats"]["best_defence_team"], "Brazil")
    check("best defence reports its clean-sheet count", d["stats"]["best_defence_cs"], 2)

    # 5) Score correction is idempotent: re-running with a corrected score gives the corrected total, no double-count.
    d1 = run([M(1, "Brazil", "Japan", "GROUP_STAGE", 3, 0, "HOME")], tmp)
    first = team(d1, "Brazil")["points"]                       # 3 goals + win + clean sheet
    d2 = run([M(1, "Brazil", "Japan", "GROUP_STAGE", 1, 1, "DRAW")], tmp)
    second = team(d2, "Brazil")["points"]                      # corrected: 1 goal + draw
    check("score before correction (3-0)", first, 3 * SC["per_goal"] + SC["win"] + SC["clean_sheet"])
    check("score after correction (1-1)", second, 1 * SC["per_goal"] + SC["draw"])

    # 6) FAIR when your own two teams meet: you bank BOTH teams' points (winner's win+goals+CS, loser's goals) — you can't lose out.
    d = run([M(1, "Brazil", "Japan", "GROUP_STAGE", 2, 1, "HOME")], tmp)   # both are Erol's
    check("own-team game: owner banks both teams' points",
          player(d, "Erol", "points"), (2 * SC["per_goal"] + SC["win"]) + (1 * SC["per_goal"]))
    check("own-team game: both your teams stay alive", player(d, "Erol", "alive_teams"), 2)
    check("own-team game: the other player is unaffected", player(d, "James", "points"), 0)

    # 7) DEPTH beats a short run: a team that reaches the final outscores one knocked out in the round of 32,
    #    on points AND on Both — reaching later rounds is worth more.
    deep = run([M(1, "Brazil", "X1", "LAST_32", 1, 0, "HOME"),
                M(2, "Brazil", "X2", "LAST_16", 1, 0, "HOME"),
                M(3, "Brazil", "X3", "QUARTER_FINALS", 1, 0, "HOME"),
                M(4, "Brazil", "X4", "SEMI_FINALS", 1, 0, "HOME"),
                M(5, "Brazil", "Spain", "FINAL", 1, 0, "HOME"),          # Brazil to the final & wins it; Spain runner-up
                M(6, "Japan", "X5", "LAST_32", 0, 1, "AWAY")], tmp)      # Erol's other team out in the R32
    b, jp = team(deep, "Brazil"), team(deep, "Japan")
    check("a deep run massively out-points an early exit (points)", b["points"] > jp["points"] + 50, True)
    check("a deep run out-scores on Both too (survival + points)",
          (b["points"] + b["survival"]) > (jp["points"] + jp["survival"]) + 50, True)
    check("the deeper team is worth more survival", b["survival"] > jp["survival"], True)

    if FAILS:
        print("\nFAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        sys.exit(1)
    print("\nAll result-correctness tests passed.")


if __name__ == "__main__":
    run_all()
