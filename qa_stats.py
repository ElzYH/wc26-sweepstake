#!/usr/bin/env python3
"""
Stats QA: the over/under-performer cards must never collapse onto the SAME team (the live bug), must read the
right way round on a chalk result, and must flip on an upset.
"""
import os, sys, json, tempfile, shutil

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
FAILS = []
def ck(name, cond, got=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -> %s" % (got,)))
    if not cond:
        FAILS.append(name)

t = tempfile.mkdtemp(prefix="wc26_stats_")
os.environ["WC26_DATA"] = t
os.environ["WC26_CONFIG"] = os.path.join(t, "config.json")
json.dump({"configured": True}, open(os.environ["WC26_CONFIG"], "w"))
shutil.copy(os.path.join(REPO, "teams.json"), os.path.join(t, "teams.json"))
import scoring

def run(matches, draw):
    json.dump({"players": draw}, open(os.path.join(t, "draw_result.json"), "w"))
    json.dump({"matches": matches}, open(os.path.join(t, "results.json"), "w"))
    return scoring.compute(teams_path=os.path.join(t, "teams.json"),
                           draw_path=os.path.join(t, "draw_result.json"),
                           results_path=os.path.join(t, "results.json"),
                           out=os.path.join(t, "td.json"))["stats"]

print("== over/under-performer team never collapses onto one team ==")
DRAW = [{"name": "James", "teams": [{"name": "Mexico", "tier": 2, "group": "A"}, {"name": "South Africa", "tier": 3, "group": "A"}]},
        {"name": "Erol", "teams": [{"name": "Brazil", "tier": 1, "group": "B"}]}]
chalk = [{"id": "m1", "home": "Mexico", "away": "South Africa", "status": "FINISHED", "homeScore": 2, "awayScore": 0,
          "stage": "GROUP_STAGE", "utcDate": "2026-06-11T19:00:00Z", "group": "A", "winner": "HOME"}]
s = run(chalk, DRAW)
ck("over and under team are different", s.get("over_team") != s.get("under_team"), (s.get("over_team"), s.get("under_team")))
ck("winner is the over-performer", s.get("over_team") == "Mexico", s.get("over_team"))
ck("loser is the under-performer", s.get("under_team") == "South Africa", s.get("under_team"))

upset = [{"id": "m2", "home": "South Africa", "away": "Brazil", "status": "FINISHED", "homeScore": 1, "awayScore": 0,
          "stage": "GROUP_STAGE", "utcDate": "2026-06-12T19:00:00Z", "group": "B", "winner": "HOME"}]
s2 = run(upset, DRAW)
ck("an upset makes the underdog the over-performer", s2.get("over_team") == "South Africa", s2.get("over_team"))

shutil.rmtree(t, ignore_errors=True)
if FAILS:
    print("\nSTATS QA FAILED (%d):" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nAll stats QA passed.")
