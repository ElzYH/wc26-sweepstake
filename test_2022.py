"""
Test harness: feed the real 2022 World Cup knockout stage through the engine.
Writes tracker_data.json so you can open tracker.html and see a populated
bracket + leaderboard using historical data (no API / live games needed).
"""
import json
import scoring

# 16 round-of-16 teams (minimal teams file: tier just drives chip colour)
R16 = {
    "Netherlands": (2, "A"), "USA": (3, "B"), "Argentina": (1, "C"), "Australia": (4, "D"),
    "France": (1, "D"), "Poland": (4, "C"), "England": (1, "B"), "Senegal": (3, "A"),
    "Japan": (3, "E"), "Croatia": (2, "F"), "Brazil": (1, "G"), "South Korea": (4, "H"),
    "Morocco": (2, "F"), "Spain": (1, "E"), "Portugal": (2, "H"), "Switzerland": (3, "G"),
}
json.dump({"teams": [{"name": n, "confederation": "-", "group": g, "tier": t,
                      "tier_label": "", "weight": 1, "composite": 0,
                      "decimal_odds": 0, "fifa_points": 0} for n, (t, g) in R16.items()]},
          open("teams_2022.json", "w"), indent=2)

# a 4-player draw over those 16
draw = {"mode": "snake", "leftover_policy": "drop", "players": [
    {"name": "Alex", "teams": ["Argentina", "England", "Japan", "Spain"]},
    {"name": "Sam",  "teams": ["France", "Netherlands", "Brazil", "Switzerland"]},
    {"name": "Jo",   "teams": ["Portugal", "Croatia", "USA", "Senegal"]},
    {"name": "Ria",  "teams": ["Morocco", "Poland", "Australia", "South Korea"]},
]}
draw = {"mode": draw["mode"], "leftover_policy": draw["leftover_policy"], "players": [
    {"name": p["name"], "strength": 0,
     "teams": [{"name": n, "tier": R16[n][0], "group": R16[n][1],
                "confederation": "-", "composite": 0} for n in p["teams"]]}
    for p in draw["players"]]}
json.dump(draw, open("draw_result_2022.json", "w"), indent=2)

# real 2022 knockout results; winner accounts for penalties
def M(stage, h, a, hs, as_, win):
    return {"id": M.i, "stage": stage, "group": None, "utcDate": "2022-12-01T00:00:00Z",
            "status": "FINISHED", "home": h, "away": a, "homeScore": hs, "awayScore": as_,
            "winner": win} if not setattr(M, "i", M.i + 1) else None
M.i = 0
matches = [
    M("LAST_16", "Netherlands", "USA", 3, 1, "HOME"),
    M("LAST_16", "Argentina", "Australia", 2, 1, "HOME"),
    M("LAST_16", "France", "Poland", 3, 1, "HOME"),
    M("LAST_16", "England", "Senegal", 3, 0, "HOME"),
    M("LAST_16", "Japan", "Croatia", 1, 1, "AWAY"),        # Croatia win pens
    M("LAST_16", "Brazil", "South Korea", 4, 1, "HOME"),
    M("LAST_16", "Morocco", "Spain", 0, 0, "HOME"),        # Morocco win pens
    M("LAST_16", "Portugal", "Switzerland", 6, 1, "HOME"),
    M("QUARTER_FINALS", "Croatia", "Brazil", 1, 1, "HOME"),     # Croatia win pens
    M("QUARTER_FINALS", "Netherlands", "Argentina", 2, 2, "AWAY"),  # Argentina win pens
    M("QUARTER_FINALS", "Morocco", "Portugal", 1, 0, "HOME"),
    M("QUARTER_FINALS", "England", "France", 1, 2, "AWAY"),
    M("SEMI_FINALS", "Argentina", "Croatia", 3, 0, "HOME"),
    M("SEMI_FINALS", "France", "Morocco", 2, 0, "HOME"),
    M("THIRD_PLACE", "Croatia", "Morocco", 2, 1, "HOME"),
    M("FINAL", "Argentina", "France", 3, 3, "HOME"),       # Argentina win pens
]
json.dump({"competition": "WC2022", "matches": matches, "standings": []},
          open("results_wc2022.json", "w"), indent=2)

d = scoring.compute(teams_path="teams_2022.json", draw_path="draw_result_2022.json",
                    results_path="results_wc2022.json", out="tracker_data.json")

def team(n):
    return next(t for p in d["players"] for t in p["teams"] if t["name"] == n)

print("LEADERBOARD (hybrid):")
for r in d["leaderboards"]["hybrid"]:
    print(f"  {r['score']:>4}  {r['name']:6} ({r['alive_teams']}/{r['total_teams']} alive)")
print()
checks = {
    "Argentina alive/WINNER": team("Argentina")["status"] == "alive" and team("Argentina")["survival"] == scoring.SURVIVAL_VALUE["WINNER"],
    "France out at FINAL": team("France") == team("France") and team("France")["status"] == "out" and team("France")["stage"] == "FINAL",
    "Spain out at LAST_16 (lost pens)": team("Spain")["status"] == "out" and team("Spain")["stage"] == "LAST_16",
    "Brazil out at QF (lost pens)": team("Brazil")["status"] == "out" and team("Brazil")["stage"] == "QUARTER_FINALS",
    "Netherlands out at QF (lost pens)": team("Netherlands")["stage"] == "QUARTER_FINALS",
    "Croatia out at SEMI": team("Croatia")["status"] == "out" and team("Croatia")["stage"] == "SEMI_FINALS",
}
for k, v in checks.items():
    print(("  PASS  " if v else "  FAIL  ") + k)
print("\nall passed:", all(checks.values()))
