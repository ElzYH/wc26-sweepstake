"""
Local live-tournament simulator — watch the tracker behave during 'games'.

It plays a believable World Cup (group stage -> knockouts) and writes tracker_data.json
in steps, pausing between each so you can watch /tracker update. It deliberately fires the
tricky cases: a live in-play moment, a FORFEIT (awarded), and a penalty-shootout FINAL.

RUN (from the repo root):
    python3 demo_live.py                 # ~8s between steps, into ./wc26-demo
    python3 demo_live.py --fast          # no pauses (just builds the final state)
    python3 demo_live.py --step 4        # seconds between steps

Then, in a SECOND terminal, serve the demo folder and open the tracker:
    cd wc26-demo && python3 -m http.server 8080
    # open http://localhost:8080/tracker.html  (it auto-refreshes every 60s; tap the page's
    # refresh or just wait — the leaderboard is also printed here in the terminal each step)
"""
import argparse
import json
import math
import os
import random
import shutil
import sys
import time
from collections import defaultdict

import scoring
from draw import Draw

PLAYERS = ["Erol", "James", "Louis", "Ismail", "Reuben"]
REPO = os.path.dirname(os.path.abspath(__file__))
KO_ROUNDS = ["LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "FINAL"]
_OWNER = {}
_PREV = {"data": None}
_LIVE = ("IN_PLAY", "PAUSED", "LIVE")


def preview_alerts(cur):
    """Mirror server.notify_changes: what phone/Discord alerts this step would send."""
    prev = _PREV["data"]
    _PREV["data"] = json.loads(json.dumps(cur))
    if not prev:
        return []
    out = []
    try:
        ol = prev["leaderboards"]["hybrid"][0]["name"]
        nl = cur["leaderboards"]["hybrid"][0]["name"]
        if ol != nl:
            out.append("📈 EVERYONE — New leader: %s now tops the table" % nl)
    except Exception:
        pass
    fx = lambda d: {(m["home"], m["away"], m["stage"]): m for m in d.get("fixtures", [])}
    of, nf = fx(prev), fx(cur)
    for k, m in nf.items():
        h, a, _ = k
        om = of.get(k)
        if m["status"] in _LIVE and (not om or om["status"] not in _LIVE):
            out.append("🔵 %s & %s — kicked off: %s vs %s" % (_OWNER.get(h, "—"), _OWNER.get(a, "—"), h, a))
        if om and None not in (m.get("homeScore"), m.get("awayScore"), om.get("homeScore"), om.get("awayScore")):
            if m["homeScore"] > om["homeScore"]:
                out.append("⚽ %s — %s scored! (%s %d–%d %s)" % (_OWNER.get(h, "—"), h, h, m["homeScore"], m["awayScore"], a))
            if m["awayScore"] > om["awayScore"]:
                out.append("⚽ %s — %s scored! (%s %d–%d %s)" % (_OWNER.get(a, "—"), a, h, m["homeScore"], m["awayScore"], a))
    alive = lambda d: {t["name"]: t["status"] == "alive" for p in d.get("players", []) for t in p["teams"]}
    oa, na = alive(prev), alive(cur)
    for t, was in oa.items():
        if was and not na.get(t, True):
            out.append("❌ %s — %s is out" % (_OWNER.get(t, "—"), t))
    oc = (prev.get("champion_decided") or {}).get("team")
    nc = (cur.get("champion_decided") or {}).get("team")
    if nc and nc != oc:
        out.append("🏆 EVERYONE — %s are World Cup champions! (%s)" % (nc, (cur.get("champion_decided") or {}).get("owner", "—")))
    return out


def poisson(lam):
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


def score(a, b, comp):
    """Plausible scoreline from team strengths."""
    la = 0.5 + comp[a] / 60.0
    lb = 0.5 + comp[b] / 60.0
    return poisson(la), poisson(lb)


def setup_dir(d):
    os.makedirs(d, exist_ok=True)
    for f in ("tracker.html", "teams.json", "sw.js", "manifest.webmanifest", "icon.svg"):
        src = os.path.join(REPO, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(d, f))


def write_and_compute(d, matches, standings):
    json.dump({"competition": "WC", "matches": matches, "standings": standings},
              open(os.path.join(d, "results.json"), "w"))
    return scoring.compute(
        teams_path=os.path.join(d, "teams.json"),
        draw_path=os.path.join(d, "draw_result.json"),
        results_path=os.path.join(d, "results.json"),
        out=os.path.join(d, "tracker_data.json"))


def show(label, data, pause):
    alerts = preview_alerts(data)
    board = data["leaderboards"]["hybrid"]
    print("\n— %s —" % label)
    for i, p in enumerate(board):
        print("   %d. %-8s %3d  (%d teams in)" % (i + 1, p["name"], p["score"], p["alive_teams"]))
    st = data.get("stats", {})
    print("   played %s · %s goals · teams remaining %s"
          % (st.get("matches_played", 0), st.get("goals", 0), st.get("teams_remaining", "?")))
    if alerts:
        print("   🔔 notifications that just fired (to phones + Discord):")
        for x in alerts[:10]:
            print("        " + x)
        if len(alerts) > 10:
            print("        …and %d more" % (len(alerts) - 10))
    if pause:
        time.sleep(pause)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(REPO, "wc26-demo"))
    ap.add_argument("--step", type=float, default=8.0, help="seconds between steps")
    ap.add_argument("--fast", action="store_true", help="no pauses")
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()
    pause = 0 if args.fast else args.step
    random.seed(args.seed)

    d = args.dir
    setup_dir(d)
    teams = json.load(open(os.path.join(d, "teams.json")))["teams"]
    comp = {t["name"]: t["composite"] for t in teams}
    groups = defaultdict(list)
    for t in teams:
        groups[t["group"]].append(t["name"])

    # real draw
    draw = Draw(mode="snake", leftover_policy="pool", seed=args.seed)
    draw.add_players(PLAYERS)
    draw.add_all_teams(os.path.join(d, "teams.json"))
    draw.sort_teams_to_players()
    draw.export_result(os.path.join(d, "draw_result.json"))
    global _OWNER
    _dr = json.load(open(os.path.join(d, "draw_result.json")))
    _OWNER = {t["name"]: p["name"] for p in _dr["players"] for t in p["teams"]}

    print("=" * 56)
    print("WC26 LIVE DEMO — players:", ", ".join(PLAYERS))
    print("Serve it:  cd %s && python3 -m http.server 8080" % os.path.relpath(d))
    print("Open:      http://localhost:8080/tracker.html")
    print("=" * 56)

    matches, mid = [], 1
    # empty standings first (pre-tournament look)
    standings = [{"group": g, "table": [{"position": i + 1, "team": nm, "playedGames": 0, "won": 0,
                  "draw": 0, "lost": 0, "goalsFor": 0, "goalsAgainst": 0, "goalDifference": 0, "points": 0}
                  for i, nm in enumerate(sorted(groups[g], key=lambda n: -comp[n]))]} for g in sorted(groups)]
    data = write_and_compute(d, matches, standings)
    show("Pre-tournament (everyone 0, all alive)", data, pause)

    # ---- GROUP STAGE: each group plays a full round-robin (6 games/group) ----
    gstats = {nm: {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "pts": 0} for nm in comp}
    group_matches = []
    for g in sorted(groups):
        gt = groups[g]
        for i in range(len(gt)):
            for j in range(i + 1, len(gt)):
                a, b = gt[i], gt[j]
                ga, gb = score(a, b, comp)
                group_matches.append((g, a, b, ga, gb))

    # LIVE MOMENT: first few group games kick off at 0-0 (kickoff alerts), then goals arrive (goal alerts)
    live = group_matches[:4]
    live_ids = {}
    for (g, a, b, ga, gb) in live:
        live_ids[(a, b)] = mid
        matches.append({"id": mid, "stage": "GROUP_STAGE", "group": g,
                        "utcDate": "2026-06-11T18:00:00Z", "status": "IN_PLAY",
                        "home": a, "away": b, "homeScore": 0, "awayScore": 0,
                        "winner": None, "minute": random.randint(3, 15), "duration": "REGULAR",
                        "aet": False, "shootout": False, "penHome": None, "penAway": None})
        mid += 1
    data = write_and_compute(d, matches, _standings(gstats, groups, comp))
    show("KICK-OFF — 4 games live at 0–0 (scores show, but 0 points until full time)", data, pause)

    for m in matches:                                            # the goals roll in (still live)
        for (g, a, b, ga, gb) in live:
            if m["id"] == live_ids[(a, b)]:
                m["homeScore"], m["awayScore"] = ga, gb
                m["minute"] = random.randint(60, 85)
    data = write_and_compute(d, matches, _standings(gstats, groups, comp))
    show("GOALS — those 4 games are live with goals in (watch the goal alerts)", data, pause)

    # finish all group games, build standings progressively in 3 chunks
    matches = [m for m in matches if m["status"] != "IN_PLAY"]  # clear the live ones; replay as finished
    chunks = [group_matches[:len(group_matches) // 3],
              group_matches[len(group_matches) // 3: 2 * len(group_matches) // 3],
              group_matches[2 * len(group_matches) // 3:]]
    for ci, chunk in enumerate(chunks, 1):
        for (g, a, b, ga, gb) in chunk:
            w = "HOME" if ga > gb else ("AWAY" if gb > ga else "DRAW")
            matches.append({"id": mid, "stage": "GROUP_STAGE", "group": g,
                            "utcDate": "2026-06-%02dT18:00:00Z" % (11 + ci), "status": "FINISHED",
                            "home": a, "away": b, "homeScore": ga, "awayScore": gb, "winner": w,
                            "minute": None, "duration": "REGULAR", "aet": False, "shootout": False,
                            "penHome": None, "penAway": None})
            mid += 1
            _tally(gstats, a, b, ga, gb)
        data = write_and_compute(d, matches, _standings(gstats, groups, comp))
        show("GROUP STAGE — matchday %d done (points + goals climbing)" % ci, data, pause)

    # ---- qualifiers: top 2 per group + 8 best third-placed ----
    table = _standings(gstats, groups, comp)
    top2, thirds = [], []
    for grp in table:
        rows = grp["table"]
        top2 += [rows[0]["team"], rows[1]["team"]]
        if len(rows) > 2:
            thirds.append(rows[2]["team"])
    thirds.sort(key=lambda n: -comp[n])
    bracket = top2 + thirds[:max(0, 32 - len(top2))]
    random.shuffle(bracket)
    bracket = bracket[:32]

    # ---- KNOCKOUTS: each round live-then-finished; QF has a forfeit; FINAL a shootout ----
    for r, rnd in enumerate(KO_ROUNDS):
        pairs = [(bracket[i], bracket[i + 1]) for i in range(0, len(bracket), 2)]
        # live snapshot for this round
        live_ms = []
        for (a, b) in pairs:
            ga, gb = score(a, b, comp)
            live_ms.append({"id": mid, "stage": rnd, "group": None,
                            "utcDate": "2026-07-0%dT18:00:00Z" % (r + 1), "status": "IN_PLAY",
                            "home": a, "away": b, "homeScore": ga, "awayScore": gb, "winner": None,
                            "minute": random.randint(20, 80), "duration": "REGULAR", "aet": False,
                            "shootout": False, "penHome": None, "penAway": None})
            mid += 1
        data = write_and_compute(d, matches + live_ms, table)
        show("%s — LIVE" % rnd.replace("_", " ").title(), data, pause)

        # finish the round
        winners = []
        for idx, (a, b) in enumerate(pairs):
            ga, gb = score(a, b, comp)
            forfeit = (rnd == "QUARTER_FINALS" and idx == 0)        # demo a walkover
            shootout = (rnd == "FINAL")                              # demo a shootout decider
            if forfeit:
                ga, gb, status, w, dur = 3, 0, "AWARDED", "HOME", "REGULAR"
            elif shootout:
                ga = gb = max(ga, gb)                               # level at full time
                status, dur = "FINISHED", "PENALTY_SHOOTOUT"
                w = "HOME" if random.random() < 0.5 else "AWAY"
            else:
                if ga == gb:
                    ga += 1                                         # no draws in the knockouts
                status, dur, w = "FINISHED", "REGULAR", ("HOME" if ga > gb else "AWAY")
            m = {"id": mid, "stage": rnd, "group": None,
                 "utcDate": "2026-07-1%dT18:00:00Z" % (r), "status": status,
                 "home": a, "away": b, "homeScore": ga, "awayScore": gb, "winner": w,
                 "minute": None, "duration": dur, "aet": dur != "REGULAR",
                 "shootout": dur == "PENALTY_SHOOTOUT",
                 "penHome": (5 if w == "HOME" else 4) if shootout else None,
                 "penAway": (4 if w == "HOME" else 5) if shootout else None}
            matches.append(m)
            mid += 1
            winners.append(a if w == "HOME" else b)
        data = write_and_compute(d, matches, table)
        extra = "  (one game was a FORFEIT)" if rnd == "QUARTER_FINALS" else (
                "  (decided on PENALTIES)" if rnd == "FINAL" else "")
        show("%s — finished%s" % (rnd.replace("_", " ").title(), extra), data, pause)
        bracket = winners

    champ = (data.get("champion_decided") or {})
    print("\n🏆 CHAMPION: %s (%s)" % (champ.get("team", "?"), champ.get("owner", "?")))
    print("Demo complete. The tracker now shows the full tournament + champion banner.")


def _tally(s, a, b, ga, gb):
    for tm, gf, gaa in ((a, ga, gb), (b, gb, ga)):
        s[tm]["P"] += 1
        s[tm]["GF"] += gf
        s[tm]["GA"] += gaa
    if ga > gb:
        s[a]["W"] += 1; s[a]["pts"] += 3; s[b]["L"] += 1
    elif gb > ga:
        s[b]["W"] += 1; s[b]["pts"] += 3; s[a]["L"] += 1
    else:
        s[a]["D"] += 1; s[b]["D"] += 1; s[a]["pts"] += 1; s[b]["pts"] += 1


def _standings(s, groups, comp):
    out = []
    for g in sorted(groups):
        rows = sorted(groups[g], key=lambda n: (-s[n]["pts"], -(s[n]["GF"] - s[n]["GA"]), -s[n]["GF"], -comp[n]))
        out.append({"group": g, "table": [
            {"position": i + 1, "team": nm, "playedGames": s[nm]["P"], "won": s[nm]["W"],
             "draw": s[nm]["D"], "lost": s[nm]["L"], "goalsFor": s[nm]["GF"], "goalsAgainst": s[nm]["GA"],
             "goalDifference": s[nm]["GF"] - s[nm]["GA"], "points": s[nm]["pts"]}
            for i, nm in enumerate(rows)]})
    return out


if __name__ == "__main__":
    sys.exit(main())
