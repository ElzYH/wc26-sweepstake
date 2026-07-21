"""
Build a pre-tournament 2026 state for testing the 2026 wiring:
real draw + all 12 group tables (everyone 0-0-0, alive). Fixtures/bracket stay
empty until the live poller runs. -> tracker_data.json
"""
import json
from collections import defaultdict
from draw import Draw
import scoring

# real 2026 draw
d = Draw(mode="snake", leftover_policy="pool", seed=42)
d.add_players(["Erol", "Bailey", "Oliver", "David", "Natalie"])
d.add_all_teams("teams.json")
d.sort_teams_to_players()
d.export_result("draw_result.json")

# synthesise empty group tables from teams.json groups
teams = json.load(open("teams.json"))["teams"]
groups = defaultdict(list)
for t in teams:
    groups[t["group"]].append(t)
standings = []
for g in sorted(groups):
    table = sorted(groups[g], key=lambda t: -t["composite"])
    standings.append({"group": g, "table": [
        {"position": i + 1, "team": t["name"], "playedGames": 0, "won": 0, "draw": 0,
         "lost": 0, "goalsFor": 0, "goalsAgainst": 0, "goalDifference": 0, "points": 0}
        for i, t in enumerate(table)]})

json.dump({"competition": "WC", "matches": [], "standings": standings},
          open("results.json", "w"), indent=2)
data = scoring.compute(default_mode="points")
print("2026 pre-tournament: groups =", len(data["groups"]),
      "| owners shown =", all(r["owner"] != "—" for grp in data["groups"] for r in grp["table"]),
      "| all alive =", all(p["alive_teams"] == p["total_teams"] for p in data["players"]),
      "| total pts =", sum(p["points"] for p in data["players"]))
