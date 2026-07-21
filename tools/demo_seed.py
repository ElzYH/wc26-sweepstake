#!/usr/bin/env python3
"""
demo_seed.py — build a SELF-CONTAINED local demo instance of the sweepstake so you can click around
(and bet on the new Over/Under market) in a real browser, with zero risk to the live site.

It seeds a folder (default ./demo) with:
  teams.json         (copied from the repo — the real WC2026 teams + composites)
  draw_result.json   (the 5 players each given a few real teams)
  results.json       (a schedule: some FINISHED, some IN_PLAY/live, lots of upcoming = bettable)
  config.json        (wagering ON, known passcodes, and NO football-data token so the poller
                      will NEVER overwrite results.json — the demo stays exactly as seeded)
  wagers.json        (empty — you place your own)
  tracker_data.json  (computed so the board renders immediately)

Run:   python3 demo_seed.py            # seeds ./demo
       python3 demo_seed.py mydemo     # seeds ./mydemo
Then:  WC26_DATA=demo PORT=8011 python3 server.py
       open http://localhost:8011/tracker
Log in as any player with the passcode  DEMO  and start betting.
"""
import json, os, random, sys, time, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else "demo"
PLAYERS = ["Erol", "James", "Louis", "Ismail", "Reuben"]
PASSCODE = "DEMO"
NGROUPS = 4                      # how many real groups to include (4 groups = 16 teams, 24 group games)

os.makedirs(OUT, exist_ok=True)
random.seed(42)                  # deterministic demo

teams_all = json.load(open(os.path.join(HERE, "teams.json")))
shutil.copy2(os.path.join(HERE, "teams.json"), os.path.join(OUT, "teams.json"))
by_name = {t["name"]: t for t in teams_all["teams"]}

# --- pick the first NGROUPS groups (alphabetical) and their teams ---
groups = sorted({t["group"] for t in teams_all["teams"]})[:NGROUPS]
group_teams = {g: [t["name"] for t in teams_all["teams"] if t["group"] == g] for g in groups}
pool_teams = [n for g in groups for n in group_teams[g]]     # all teams in the demo

# --- draw: hand the demo teams round-robin to the 5 players; remainder -> bonus pool ---
draw_players = {p: [] for p in PLAYERS}
i = 0
leftovers = []
for n in pool_teams:
    if i < len(PLAYERS) * 3:                                  # 3 each = 15 teams
        draw_players[PLAYERS[i % len(PLAYERS)]].append(n)
        i += 1
    else:
        leftovers.append(n)                                   # the 16th -> pool

def expand(name):
    t = by_name.get(name, {"name": name, "tier": 4, "group": "?", "confederation": "?", "composite": 0})
    return {"name": t["name"], "tier": t["tier"], "group": t["group"],
            "confederation": t.get("confederation", "?"), "composite": t.get("composite", 0)}

draw = {"mode": "weighted-clockwork", "leftover_policy": "pool",
        "players": [{"name": p, "teams": [expand(n) for n in draw_players[p]]} for p in PLAYERS],
        "bonus_pool": [expand(n) for n in leftovers]}
json.dump(draw, open(os.path.join(OUT, "draw_result.json"), "w"), indent=2)

# --- a plausible scoreline from team strengths (deterministic) ---
def score(home, away):
    ch = by_name.get(home, {}).get("composite", 40) + 1
    ca = by_name.get(away, {}).get("composite", 40) + 1
    lam_h = max(0.3, 1.35 * (ch / (ch + ca)) * 2.4)
    lam_a = max(0.3, 1.35 * (ca / (ch + ca)) * 2.4)
    def pois(lam):
        # tiny Knuth Poisson sampler
        L, k, p = pow(2.718281828, -lam), 0, 1.0
        while True:
            k += 1; p *= random.random()
            if p <= L: return k - 1
    return pois(lam_h), pois(lam_a)

# --- round-robin pairings for a group of 4 ---
def pairings(ts):
    a, b, c, d = ts
    return [[(a, b), (c, d)], [(a, c), (d, b)], [(a, d), (b, c)]]   # MD1, MD2, MD3

DAY = 86400
now = int(time.time())
matches = []
mid = 1
# status plan per group index: how each of MD1/MD2/MD3 behaves
plan = {
    0: ["FINISHED", "FINISHED", "FINISHED"],                 # fully settled group
    1: ["FINISHED", "MIXED", "TIMED"],                       # one live game in MD2, MD3 upcoming
    2: ["FINISHED", "TIMED", "TIMED"],                       # lots upcoming
    3: ["TIMED", "TIMED", "TIMED"],                          # all upcoming
}
for gi, g in enumerate(groups):
    ts = group_teams[g]
    mds = pairings(ts)
    for di, md in enumerate(mds):
        st = plan[gi][di]
        # date: past for finished/live, future for timed
        when = now - (NGROUPS - di) * DAY if st in ("FINISHED", "MIXED") else now + (di + 1) * DAY + gi * 3600
        for j, (h, a) in enumerate(md):
            rec = {"id": "D%03d" % mid, "home": h, "away": a, "group": g, "stage": "GROUP_STAGE",
                   "utcDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when + j * 7200))}
            this = st
            if st == "MIXED":                                # first match live, second already finished
                this = "IN_PLAY" if j == 0 else "FINISHED"
            if this == "FINISHED":
                hs, as_ = score(h, a)
                rec.update(status="FINISHED", homeScore=hs, awayScore=as_)
            elif this == "IN_PLAY":
                hs, as_ = score(h, a)
                hs, as_ = min(hs, 2), min(as_, 1)            # a believable in-progress scoreline
                rec.update(status="IN_PLAY", homeScore=hs, awayScore=as_, minute=63)
            else:
                rec.update(status="TIMED", homeScore=None, awayScore=None)
            matches.append(rec); mid += 1

json.dump({"matches": matches, "competition": "FIFA World Cup 2026"},
          open(os.path.join(OUT, "results.json"), "w"), indent=2)

json.dump([], open(os.path.join(OUT, "wagers.json"), "w"))     # you place your own bets

config = {
    "players": PLAYERS,
    "wagering_enabled": True,
    "wager_pins": {p: PASSCODE for p in PLAYERS},              # log in as anyone with passcode DEMO
    "default_mode": "points",
    "poll_minutes": 10,
    # A deliberately-INVALID token: the frontend auto-calls /api/poll, and update_now() with NO token would
    # rewrite results.json to an empty pre-tournament state (wiping the demo). With a token set, update_now
    # instead tries to fetch, gets a fast 403, and KEEPS the seeded results.json (it only swaps in good data).
    # So the games stay frozen exactly as seeded, and settlement still runs when you advance scores + /api/poll.
    "token": "demo-invalid-token-do-not-fetch",
    "competition": "WC",
    "discord_invite": "", "discord_bot_token": "", "discord_webhook": "",
    "admin_key": "demo-admin-key"
}
json.dump(config, open(os.path.join(OUT, "config.json"), "w"), indent=2)

# --- compute the tracker board so it renders immediately (and fixtures get odds + ouOdds) ---
sys.path.insert(0, HERE)
import scoring, wager
cwd = os.getcwd()
os.chdir(OUT)
try:
    scoring.compute(default_mode="points", wagers=[])
finally:
    os.chdir(cwd)

bettable = sum(1 for m in matches if m["status"] in ("TIMED", "SCHEDULED"))
live = sum(1 for m in matches if m["status"] == "IN_PLAY")
fin = sum(1 for m in matches if m["status"] == "FINISHED")
print("Seeded ./%s — %d teams across groups %s" % (OUT, len(pool_teams), ", ".join(groups)))
print("  matches: %d finished, %d live, %d upcoming (bettable)" % (fin, live, bettable))
print("  players: %s   passcode: %s" % (", ".join(PLAYERS), PASSCODE))
print("\nRun it:")
print("  WC26_DATA=%s PORT=8011 python3 server.py" % OUT)
print("  open http://localhost:8011/tracker   (Bets tab -> log in as a player -> passcode %s)" % PASSCODE)
