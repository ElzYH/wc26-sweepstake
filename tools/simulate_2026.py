"""
Test run for 2026: invents a full fake tournament (group stage -> knockouts ->
a champion) from your real draw, so you can SEE the tracker, win-odds and bracket
move before a real ball is kicked.

    python3 simulate_2026.py            # random tournament
    python3 simulate_2026.py 7          # same seed every time (reproducible)

It writes results.json + tracker_data.json. Open the tracker to view it.
NOTE: if you have an API token set, raise "refresh mins" in Settings first
(e.g. 999) so the live poller doesn't overwrite this fake data.
"""
import json
import random
import sys
import datetime
import scoring

if len(sys.argv) > 1:
    random.seed(int(sys.argv[1]))

T = json.load(open("teams.json"))["teams"]
by = {t["name"]: t for t in T}
groups = {}
for t in T:
    groups.setdefault(t["group"], []).append(t["name"])

try:
    draw = json.load(open("draw_result.json"))
except FileNotFoundError:
    sys.exit("No draw_result.json — run a draw on the wheel first, then re-run this.")

base = datetime.datetime(2026, 6, 11, 18, 0, 0)
matches = []
tbl = {t["name"]: dict(P=0, W=0, D=0, L=0, GF=0, GA=0, PTS=0) for t in T}


def goals(a, b):
    ga = max(0, round(random.gauss(0.7 + by[a]["composite"] / 55, 1.0)))
    gb = max(0, round(random.gauss(0.7 + by[b]["composite"] / 55, 1.0)))
    return ga, gb


# ---- group stage: round robin ----
day = 0
for g in sorted(groups):
    tm = groups[g]
    for i, j in [(0, 1), (2, 3), (0, 2), (1, 3), (0, 3), (1, 2)]:
        a, b = tm[i], tm[j]
        ga, gb = goals(a, b)
        win = "DRAW" if ga == gb else ("HOME_TEAM" if ga > gb else "AWAY_TEAM")
        matches.append({"id": len(matches) + 1, "stage": "GROUP_STAGE", "group": g, "status": "FINISHED",
                        "home": a, "away": b, "homeScore": ga, "awayScore": gb, "winner": win,
                        "utcDate": (base + datetime.timedelta(days=day)).isoformat() + "Z"})
        day = (day + 1) % 13
        for who, f, ag in ((a, ga, gb), (b, gb, ga)):
            tbl[who]["P"] += 1; tbl[who]["GF"] += f; tbl[who]["GA"] += ag
        if ga == gb:
            tbl[a]["D"] += 1; tbl[b]["D"] += 1; tbl[a]["PTS"] += 1; tbl[b]["PTS"] += 1
        elif ga > gb:
            tbl[a]["W"] += 1; tbl[a]["PTS"] += 3; tbl[b]["L"] += 1
        else:
            tbl[b]["W"] += 1; tbl[b]["PTS"] += 3; tbl[a]["L"] += 1


def rank(n):
    return (-tbl[n]["PTS"], -(tbl[n]["GF"] - tbl[n]["GA"]), -tbl[n]["GF"])


standings, ranked = [], {}
for g in sorted(groups):
    rows = sorted(groups[g], key=rank); ranked[g] = rows
    standings.append({"group": g, "table": [
        {"position": i + 1, "team": n, "playedGames": tbl[n]["P"], "won": tbl[n]["W"], "draw": tbl[n]["D"],
         "lost": tbl[n]["L"], "goalsFor": tbl[n]["GF"], "goalsAgainst": tbl[n]["GA"],
         "goalDifference": tbl[n]["GF"] - tbl[n]["GA"], "points": tbl[n]["PTS"]} for i, n in enumerate(rows)]})

# ---- knockouts: top 2 of each group + 8 best thirds = 32 ----
top2 = [ranked[g][k] for g in sorted(groups) for k in (0, 1)]
thirds = sorted([ranked[g][2] for g in sorted(groups)], key=rank)[:8]
bracket = top2 + thirds
random.shuffle(bracket)


def play(teams_list, stage, day0):
    winners, losers = [], []
    for k in range(0, len(teams_list), 2):
        a, b = teams_list[k], teams_list[k + 1]
        ca, cb = by[a]["composite"] + 1, by[b]["composite"] + 1
        a_wins = random.random() < ca / (ca + cb)
        w, l = (a, b) if a_wins else (b, a)
        matches.append({"id": len(matches) + 1, "stage": stage, "group": None, "status": "FINISHED",
                        "home": a, "away": b, "homeScore": 2 if a_wins else 1, "awayScore": 1 if a_wins else 2,
                        "winner": "HOME_TEAM" if a_wins else "AWAY_TEAM",
                        "utcDate": (base + datetime.timedelta(days=day0)).isoformat() + "Z"})
        winners.append(w); losers.append(l)
    return winners, losers


r16, _ = play(bracket, "LAST_32", 18)
qf, _ = play(r16, "LAST_16", 22)
sf, _ = play(qf, "QUARTER_FINALS", 26)
finalists, sf_losers = play(sf, "SEMI_FINALS", 30)
play(sf_losers, "THIRD_PLACE", 33)
champ_w, _ = play(finalists, "FINAL", 34)

json.dump({"competition": "WC", "matches": matches, "standings": standings}, open("results.json", "w"), indent=2)
try:
    mode = json.load(open("config.json")).get("scoring_mode", "points")
except Exception:
    mode = "points"
data = scoring.compute(out="tracker_data.json", default_mode=mode)

print(f"Simulated {len(matches)} matches.  Champion: {champ_w[0]}  (owner: "
      f"{ {tm['name']: p['name'] for p in draw['players'] for tm in p['teams']}.get(champ_w[0], '— bonus pool') })")
print(f"\nFinal leaderboard ({mode}):")
for r in data['leaderboards'][mode]:
    print(f"  {r['name']:10} {r['score']}")
print("\nLive win-odds now:")
for c in data['champion']:
    print(f"  {c['name']:10} {c['odds']:4}%   ({c['alive_teams']}/{c['total_teams']} alive)")
print("\nOpen the tracker to see it. Run again for a different tournament.")
