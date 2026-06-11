#!/usr/bin/env python3
"""
Stats QA:
  - the over/under-performer cards must never collapse onto the SAME team (the live bug), and must read the
    right way round on a chalk result and flip on an upset;
  - the yellow/red-card pipeline: parsing match bookings, aggregating per team + per player, and the server's
    cached, capped, give-up-after-3 refresh.
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
import scoring, update_results

def run(matches, draw, cards=None):
    json.dump({"players": draw}, open(os.path.join(t, "draw_result.json"), "w"))
    json.dump({"matches": matches}, open(os.path.join(t, "results.json"), "w"))
    cp = os.path.join(t, "cards.json")
    if cards is not None:
        json.dump(cards, open(cp, "w"))
    elif os.path.exists(cp):
        os.remove(cp)
    return scoring.compute(teams_path=os.path.join(t, "teams.json"),
                           draw_path=os.path.join(t, "draw_result.json"),
                           results_path=os.path.join(t, "results.json"),
                           out=os.path.join(t, "td.json"), cards_path=cp)["stats"]

print("== over/under-performer team never collapses onto one team ==")
DRAW = [{"name": "James", "teams": [{"name": "Mexico", "tier": 2, "group": "A"}, {"name": "South Africa", "tier": 3, "group": "A"}]},
        {"name": "Erol", "teams": [{"name": "Brazil", "tier": 1, "group": "B"}]}]
chalk = [{"id": "m1", "home": "Mexico", "away": "South Africa", "status": "FINISHED", "homeScore": 2, "awayScore": 0,
          "stage": "GROUP_STAGE", "utcDate": "2026-06-11T19:00:00Z", "group": "A", "winner": "HOME"}]
s = run(chalk, DRAW)
ck("over and under team are different", s.get("over_team") != s.get("under_team"), (s.get("over_team"), s.get("under_team")))
ck("winner is the over-performer", s.get("over_team") == "Mexico", s.get("over_team"))
ck("loser is the under-performer", s.get("under_team") == "South Africa", s.get("under_team"))

# an upset: a much weaker team beats a strong one -> the weak team is the over-performer
upset = [{"id": "m2", "home": "South Africa", "away": "Brazil", "status": "FINISHED", "homeScore": 1, "awayScore": 0,
          "stage": "GROUP_STAGE", "utcDate": "2026-06-12T19:00:00Z", "group": "B", "winner": "HOME"}]
s2 = run(upset, DRAW)
ck("an upset makes the underdog the over-performer", s2.get("over_team") == "South Africa", s2.get("over_team"))

print("\n== card parsing (YELLOW / RED / YELLOW_RED / no-bookings / error) ==")
update_results._get = lambda path, token: {
    "homeTeam": {"id": 1, "name": "Mexico"}, "awayTeam": {"id": 2, "name": "South Africa"},
    "bookings": [
        {"team": {"id": 1, "name": "Mexico"}, "card": "YELLOW"},
        {"team": {"id": 2, "name": "South Africa"}, "card": "YELLOW"},
        {"team": {"id": 2, "name": "South Africa"}, "card": "YELLOW"},
        {"team": {"id": 2, "name": "South Africa"}, "card": "RED"},
        {"team": {"id": 1, "name": "Mexico"}, "card": "YELLOW_RED"},   # second yellow -> a red
    ]}
c = update_results.fetch_match_cards("m1", token="x")
ck("home yellow counted", c["home_yellow"] == 1, c)
ck("home YELLOW_RED counts as a red", c["home_red"] == 1, c)
ck("away yellows counted", c["away_yellow"] == 2, c)
ck("away red counted", c["away_red"] == 1, c)
update_results._get = lambda path, token: {"homeTeam": {"id": 1}, "awayTeam": {"id": 2}}     # no bookings field
ck("no bookings array -> None", update_results.fetch_match_cards("m1", token="x") is None)
def _boom(path, token): raise RuntimeError("net")
update_results._get = _boom
ck("network error -> None", update_results.fetch_match_cards("m1", token="x") is None)

print("\n== card aggregation per team + per player ==")
cards = {"m1": {"home_team": "Mexico", "away_team": "South Africa",
                "home_yellow": 1, "home_red": 0, "away_yellow": 2, "away_red": 1}}
s = run(chalk, DRAW, cards=cards)
ck("most yellow team", s.get("yellow_team") == "South Africa" and s.get("yellow_team_count") == 2, (s.get("yellow_team"), s.get("yellow_team_count")))
ck("most red team", s.get("red_team") == "South Africa" and s.get("red_team_count") == 1, (s.get("red_team"), s.get("red_team_count")))
ck("most yellow player (owns both teams -> 3)", s.get("yellow_player") == "James" and s.get("yellow_player_count") == 3, (s.get("yellow_player"), s.get("yellow_player_count")))
ck("most red player", s.get("red_player") == "James" and s.get("red_player_count") == 1, (s.get("red_player"), s.get("red_player_count")))
# no cards file -> cards stats are empty (graceful '—')
s = run(chalk, DRAW)
ck("no cards file -> no yellow/red stats", s.get("yellow_team") is None and s.get("red_player") is None)

print("\n== server card refresh: fetch each finished match once, cap, give up after 3 ==")
import server as S
json.dump({"matches": [{"id": "g1", "home": "Mexico", "away": "South Africa", "status": "FINISHED"},
                       {"id": "g2", "home": "Brazil", "away": "Serbia", "status": "FINISHED"},
                       {"id": "g3", "home": "France", "away": "Spain", "status": "IN_PLAY"}]},
          open(os.path.join(t, "results.json"), "w"))
if os.path.exists(os.path.join(t, "cards.json")):
    os.remove(os.path.join(t, "cards.json"))
calls = []
update_results.fetch_match_cards = lambda mid, token: (calls.append(mid) or {"home_yellow": 1, "home_red": 0, "away_yellow": 0, "away_red": 0})
S._refresh_match_cards({"token": "x", "competition": "WC"}, max_per_cycle=3)
cache = S._load_cards()
ck("only finished matches are fetched (live g3 skipped)", "g3" not in cache, list(cache))
ck("both finished matches cached", "g1" in cache and "g2" in cache, list(cache))
n1 = len(calls)
S._refresh_match_cards({"token": "x", "competition": "WC"}, max_per_cycle=3)   # second run
ck("already-cached matches are not re-fetched", len(calls) == n1, (n1, len(calls)))

# give up after 3 failures (e.g. a tier with no bookings)
if os.path.exists(os.path.join(t, "cards.json")):
    os.remove(os.path.join(t, "cards.json"))
json.dump({"matches": [{"id": "f1", "home": "A", "away": "B", "status": "FINISHED"}]},
          open(os.path.join(t, "results.json"), "w"))
fcalls = []
update_results.fetch_match_cards = lambda mid, token: (fcalls.append(mid) or None)
for _ in range(6):
    S._refresh_match_cards({"token": "x", "competition": "WC"}, max_per_cycle=3)
ck("a never-available match is tried at most 3 times then dropped", len(fcalls) == 3, len(fcalls))

shutil.rmtree(t, ignore_errors=True)
if FAILS:
    print("\nSTATS QA FAILED (%d):" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nAll stats QA passed.")
