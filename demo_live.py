"""
Local live-tournament simulator — watch the REAL tracker behave during 'games'.

It serves the tracker itself (no separate web server needed), plays a believable World Cup,
and ticks a featured match MINUTE-BY-MINUTE with goals, extra time and penalties so you can
watch the score, live win-probability and notifications move in real time.

RUN (from the repo root):
    python3 demo_live.py                 # serves on http://localhost:8080 , ~8-10 min run
    python3 demo_live.py --fast          # no pauses, just builds the final state (for testing)
    python3 demo_live.py --speed 6       # match-minutes per real second (default 3 = 3x speed)
    python3 demo_live.py --port 9000

Then open the URL it prints. The page refreshes every ~2.5s during the demo, so live games,
the win-probability bar and the leaderboard update as you watch. Notifications are printed here.
"""
import argparse
import json
import math
import os
import random
import shutil
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import scoring
from draw import Draw

PLAYERS = ["Erol", "James", "Louis", "Ismail", "Reuben"]
REPO = os.path.dirname(os.path.abspath(__file__))
KO_ROUNDS = ["LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "FINAL"]
_OWNER = {}
_PREV = {"data": None}
_LIVE = ("IN_PLAY", "PAUSED", "LIVE")
DIR = os.path.join(REPO, "wc26-demo")

CTYPES = {".html": "text/html", ".js": "text/javascript", ".json": "application/json",
          ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self._send(200, json.dumps({"ok": True}))           # demo: accept poll/etc. no-ops

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send_response(302); self.send_header("Location", "/tracker.html"); self.end_headers(); return
        if path == "/api/status":
            return self._send(200, json.dumps({
                "configured": True, "players": PLAYERS, "scoring_mode": "hybrid", "draw_mode": "fair",
                "competition": "WC", "drawn": True, "needs_key": True, "has_token": True,
                "push_enabled": False, "discord": False, "has_invite": False, "bot_ready": False,
                "digest_enabled": False, "leftover": "pool", "poll_minutes": 1, "site_url": ""}))
        if path == "/api/live_state":
            return self._send(200, json.dumps({}))
        if path == "/api/summary":
            return self._send(200, json.dumps({"ok": True, "lines": []}))
        # static file from the demo dir
        name = os.path.basename(path)
        full = os.path.join(DIR, name)
        if os.path.isfile(full):
            ext = os.path.splitext(name)[1]
            with open(full, "rb") as f:
                return self._send(200, f.read(), CTYPES.get(ext, "application/octet-stream"))
        self._send(404, "not found", "text/plain")


def serve(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def poisson(lam):
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


def goals(a, b, comp):
    return poisson(0.5 + comp[a] / 60.0), poisson(0.5 + comp[b] / 60.0)


def preview_alerts(cur):
    """Mirror server.notify_changes: what phone/Discord alerts this step would send."""
    prev = _PREV["data"]
    _PREV["data"] = json.loads(json.dumps(cur))
    if not prev:
        return []
    out = []
    try:
        ob = [p["name"] for p in prev["leaderboards"]["hybrid"]]
        nb = [p["name"] for p in cur["leaderboards"]["hybrid"]]
        if ob and nb and ob[0] != nb[0]:
            out.append("\U0001F4C8 EVERYONE - New leader: %s now tops the table" % nb[0])
        orank = {n: i for i, n in enumerate(ob)}
        for i in range(1, len(nb) - 1):                 # overtakes below 1st
            x, y = nb[i], nb[i + 1]
            if x in orank and y in orank and orank[x] > orank[y]:
                out.append("\U0001F4CA %s - overtook %s for %d%s place" % (x, y, i + 1,
                            {1: "st", 2: "nd", 3: "rd"}.get((i + 1) % 10, "th")))
    except Exception:
        pass
    fx = lambda d: {(m["home"], m["away"], m["stage"]): m for m in d.get("fixtures", [])}
    of, nf = fx(prev), fx(cur)
    for k, m in nf.items():
        h, a, _ = k
        om = of.get(k)
        if m["status"] in _LIVE and (not om or om["status"] not in _LIVE):
            out.append("\U0001F535 %s & %s - kicked off: %s vs %s" % (_OWNER.get(h, "-"), _OWNER.get(a, "-"), h, a))
        if om and None not in (m.get("homeScore"), m.get("awayScore"), om.get("homeScore"), om.get("awayScore")):
            if m["homeScore"] > om["homeScore"]:
                out.append("\u26BD %s - %s scored! (%s %d-%d %s)" % (_OWNER.get(h, "-"), h, h, m["homeScore"], m["awayScore"], a))
            if m["awayScore"] > om["awayScore"]:
                out.append("\u26BD %s - %s scored! (%s %d-%d %s)" % (_OWNER.get(a, "-"), a, h, m["homeScore"], m["awayScore"], a))
    alive = lambda d: {t["name"]: t["status"] == "alive" for p in d.get("players", []) for t in p["teams"]}
    oa, na = alive(prev), alive(cur)
    for t, was in oa.items():
        if was and not na.get(t, True):
            out.append("\u274C %s - %s is out" % (_OWNER.get(t, "-"), t))
    oc = (prev.get("champion_decided") or {}).get("team")
    nc = (cur.get("champion_decided") or {}).get("team")
    if nc and nc != oc:
        out.append("\U0001F3C6 EVERYONE - %s are World Cup champions! (%s)" % (nc, (cur.get("champion_decided") or {}).get("owner", "-")))
    return out


def setup_dir(d):
    os.makedirs(d, exist_ok=True)
    for f in ("teams.json", "sw.js", "manifest.webmanifest", "icon.svg"):
        src = os.path.join(REPO, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(d, f))
    # serve a copy of the tracker that refreshes fast, so the demo feels live
    html = open(os.path.join(REPO, "tracker.html")).read()
    html = html.replace("setInterval(load,60000)", "setInterval(load,2500)")
    open(os.path.join(d, "tracker.html"), "w").write(html)


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
    print("\n- %s -" % label)
    for i, p in enumerate(board):
        print("   %d. %-8s %3d  (%d teams in)" % (i + 1, p["name"], p["score"], p["alive_teams"]))
    st = data.get("stats", {})
    print("   played %s | %s goals | teams remaining %s"
          % (st.get("matches_played", 0), st.get("goals", 0), st.get("teams_remaining", "?")))
    if alerts:
        print("   \U0001F514 notifications that just fired (to phones + Discord):")
        for x in alerts[:10]:
            print("        " + x)
        if len(alerts) > 10:
            print("        ...and %d more" % (len(alerts) - 10))
    if pause:
        time.sleep(pause)


def tick_match(d, matches, standings, m, a, b, comp, speed, label):
    """Play one match minute-by-minute: goals during the game, extra time + penalties if level."""
    ga, gb = goals(a, b, comp)
    home_goals = sorted(random.sample(range(1, 91), min(ga, 89))) if ga else []
    away_goals = sorted(random.sample(range(1, 91), min(gb, 89))) if gb else []
    print("\n   \u25B6 LIVE: %s vs %s (%s) - watch the score + win-probability move" % (a, b, label))
    m.update({"status": "IN_PLAY", "homeScore": 0, "awayScore": 0, "minute": 0, "winner": None})
    delay = 1.0 / max(1, speed)
    for minute in range(1, 91):
        scored = False
        if minute in home_goals:
            m["homeScore"] += 1; scored = True
        if minute in away_goals:
            m["awayScore"] += 1; scored = True
        m["minute"] = minute
        data = write_and_compute(d, matches, standings)        # write every minute so the website ticks smoothly
        if scored or minute % 10 == 0 or minute == 90:
            show("%s - %d' (%s %d-%d %s)" % (label, minute, a, m["homeScore"], m["awayScore"], b), data, 0)
        time.sleep(delay)
    # extra time + penalties if level and it's a knockout
    dur, penH, penA, winner = "REGULAR", None, None, None
    if m["homeScore"] == m["awayScore"]:
        print("   \u23F1 Level at 90' - EXTRA TIME")
        m["status"] = "PAUSED"; write_and_compute(d, matches, standings); time.sleep(delay * 2)
        m["status"] = "IN_PLAY"
        et_home = random.random() < 0.35
        et_away = (not et_home) and random.random() < 0.35
        for minute in range(91, 121):
            if et_home and minute == random.choice([100, 105, 113]):
                m["homeScore"] += 1
            if et_away and minute == random.choice([98, 108, 118]):
                m["awayScore"] += 1
            m["minute"] = minute
            if minute % 10 == 0 or minute == 120:
                data = write_and_compute(d, matches, standings)
                show("%s - %d' a.e.t. (%s %d-%d %s)" % (label, minute, a, m["homeScore"], m["awayScore"], b), data, 0)
            time.sleep(delay)
        dur = "EXTRA_TIME"
        if m["homeScore"] == m["awayScore"]:
            print("   \U0001F945 Still level - PENALTY SHOOTOUT")
            dur = "PENALTY_SHOOTOUT"
            penH, penA = (5, 4) if random.random() < 0.5 else (4, 5)
            winner = "HOME" if penH > penA else "AWAY"
    if winner is None:
        winner = "HOME" if m["homeScore"] > m["awayScore"] else ("AWAY" if m["awayScore"] > m["homeScore"] else "HOME")
    m.update({"status": "FINISHED", "winner": winner, "duration": dur,
              "aet": dur != "REGULAR", "shootout": dur == "PENALTY_SHOOTOUT", "penHome": penH, "penAway": penA})
    data = write_and_compute(d, matches, standings)
    tail = "" if dur == "REGULAR" else ("  (a.e.t.)" if dur == "EXTRA_TIME" else "  (pens %d-%d)" % (penH, penA))
    show("%s - FULL TIME: %s %d-%d %s%s" % (label, a, m["homeScore"], m["awayScore"], b, tail), data, 0)
    return a if winner == "HOME" else b


def km(mid, a, b, stage, status="IN_PLAY"):
    return {"id": mid, "stage": stage, "group": None, "utcDate": "2026-07-0%dT18:00:00Z" % (KO_ROUNDS.index(stage) + 1),
            "status": status, "home": a, "away": b, "homeScore": 0, "awayScore": 0, "winner": None,
            "minute": 0, "duration": "REGULAR", "aet": False, "shootout": False, "penHome": None, "penAway": None}


def main():
    global DIR, _OWNER
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--speed", type=float, default=2.0, help="match-minutes per real second")
    ap.add_argument("--pause", type=float, default=9.0, help="seconds between phases")
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()
    pause = 0 if args.fast else args.pause
    speed = 999 if args.fast else args.speed
    random.seed(args.seed)
    if args.dir:
        DIR = args.dir
    setup_dir(DIR)

    teams = json.load(open(os.path.join(DIR, "teams.json")))["teams"]
    comp = {t["name"]: t["composite"] for t in teams}
    groups = defaultdict(list)
    for t in teams:
        groups[t["group"]].append(t["name"])

    draw = Draw(mode="snake", leftover_policy="pool", seed=args.seed)
    draw.add_players(PLAYERS)
    draw.add_all_teams(os.path.join(DIR, "teams.json"))
    draw.sort_teams_to_players()
    draw.export_result(os.path.join(DIR, "draw_result.json"))
    _dr = json.load(open(os.path.join(DIR, "draw_result.json")))
    _OWNER = {t["name"]: p["name"] for p in _dr["players"] for t in p["teams"]}

    if not args.fast:
        serve(args.port)
    print("=" * 58)
    print("WC26 LIVE DEMO - players:", ", ".join(PLAYERS))
    if not args.fast:
        print(">> OPEN:  http://localhost:%d/   (refreshes every ~2.5s while it runs)" % args.port)
        print("   Leave this terminal running; notifications print here.")
    print("=" * 58)
    print("Squads:")
    for p in _dr["players"]:
        print("   %-8s: %s" % (p["name"], ", ".join(t["name"] for t in p["teams"])))

    matches, mid = [], 1
    gstats = {nm: {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "pts": 0} for nm in comp}
    data = write_and_compute(DIR, matches, _standings(gstats, groups, comp))
    show("Pre-tournament (everyone 0, all alive)", data, pause)

    # ---- GROUP STAGE ----
    group_matches = []
    for g in sorted(groups):
        gt = groups[g]
        for i in range(len(gt)):
            for j in range(i + 1, len(gt)):
                a, b = gt[i], gt[j]
                ga, gb = goals(a, b, comp)
                group_matches.append((g, a, b, ga, gb))

    # featured live group games, minute by minute (two of them, so you see several teams play)
    for fg in group_matches[:2]:
        fm = {"id": mid, "stage": "GROUP_STAGE", "group": fg[0], "utcDate": "2026-06-11T18:00:00Z",
              "status": "IN_PLAY", "home": fg[1], "away": fg[2], "homeScore": 0, "awayScore": 0,
              "winner": None, "minute": 0, "duration": "REGULAR", "aet": False, "shootout": False,
              "penHome": None, "penAway": None}
        matches.append(fm); mid += 1
        if not args.fast:
            tick_match(DIR, matches, _standings(gstats, groups, comp), fm, fg[1], fg[2], comp, speed, "Group game")
        matches = [m for m in matches if m["id"] != fm["id"]]   # fold results into the tally below

    # finish all group games in 3 matchday chunks
    chunks = [group_matches[:len(group_matches) // 3], group_matches[len(group_matches) // 3: 2 * len(group_matches) // 3], group_matches[2 * len(group_matches) // 3:]]
    matches = []
    for ci, chunk in enumerate(chunks, 1):
        for (g, a, b, ga, gb) in chunk:
            w = "HOME" if ga > gb else ("AWAY" if gb > ga else "DRAW")
            matches.append({"id": mid, "stage": "GROUP_STAGE", "group": g, "utcDate": "2026-06-%02dT18:00:00Z" % (11 + ci),
                            "status": "FINISHED", "home": a, "away": b, "homeScore": ga, "awayScore": gb, "winner": w,
                            "minute": None, "duration": "REGULAR", "aet": False, "shootout": False, "penHome": None, "penAway": None})
            mid += 1
            _tally(gstats, a, b, ga, gb)
        data = write_and_compute(DIR, matches, _standings(gstats, groups, comp))
        show("GROUP STAGE - matchday %d done" % ci, data, pause)

    # ---- qualifiers ----
    table = _standings(gstats, groups, comp)
    top2, thirds = [], []
    for grp in table:
        rows = grp["table"]
        top2 += [rows[0]["team"], rows[1]["team"]]
        if len(rows) > 2:
            thirds.append(rows[2]["team"])
    thirds.sort(key=lambda n: -comp[n])
    bracket = (top2 + thirds[:max(0, 32 - len(top2))])
    random.shuffle(bracket)
    bracket = bracket[:32]

    # ---- KNOCKOUTS: several matches tick live each round; later rounds tick in full; FINAL goes to the wire ----
    sf_losers = []
    for r, rnd in enumerate(KO_ROUNDS):
        pairs = [(bracket[i], bracket[i + 1]) for i in range(0, len(bracket), 2)]
        winners = [None] * len(pairs)
        n_feat = len(pairs) if len(pairs) <= 2 else 2       # SF/final tick in full; earlier rounds tick 2 live
        feat = set(range(n_feat))
        # featured matches live, minute by minute
        for fi in range(n_feat):
            fa, fb = pairs[fi]
            fmatch = km(mid, fa, fb, rnd); matches.append(fmatch); mid += 1
            if not args.fast:
                winners[fi] = tick_match(DIR, matches, table, fmatch, fa, fb, comp, speed,
                                         rnd.replace("_", " ").title() + (" FINAL" if rnd == "FINAL" else ""))
            else:
                ga, gb = goals(fa, fb, comp)
                if ga == gb: ga += 1
                fmatch.update({"status": "FINISHED", "homeScore": ga, "awayScore": gb,
                               "winner": "HOME" if ga > gb else "AWAY"})
                winners[fi] = fa if ga > gb else fb
        # resolve the rest of the round instantly
        for idx, (a, b) in enumerate(pairs):
            if idx in feat:
                continue
            ga, gb = goals(a, b, comp)
            forfeit = (rnd == "QUARTER_FINALS" and idx == n_feat)
            if forfeit:
                ga, gb, status, w, dur = 3, 0, "AWARDED", "HOME", "REGULAR"
            else:
                if ga == gb: ga += 1
                status, w, dur = "FINISHED", ("HOME" if ga > gb else "AWAY"), "REGULAR"
            matches.append({"id": mid, "stage": rnd, "group": None, "utcDate": "2026-07-1%dT18:00:00Z" % r,
                            "status": status, "home": a, "away": b, "homeScore": ga, "awayScore": gb, "winner": w,
                            "minute": None, "duration": dur, "aet": False, "shootout": dur == "PENALTY_SHOOTOUT",
                            "penHome": None, "penAway": None})
            mid += 1
            winners[idx] = a if w == "HOME" else b
        if rnd == "SEMI_FINALS":
            sf_losers = [(b if winners[i] == a else a) for i, (a, b) in enumerate(pairs)]
        data = write_and_compute(DIR, matches, table)
        extra = "  (one game was a FORFEIT)" if rnd == "QUARTER_FINALS" else ""
        show("%s - round complete%s" % (rnd.replace("_", " ").title(), extra), data, pause)
        # 3rd-place playoff after the semis (the two semi losers)
        if rnd == "SEMI_FINALS" and len(sf_losers) >= 2:
            a, b = sf_losers[0], sf_losers[1]
            ga, gb = goals(a, b, comp)
            if ga == gb: ga += 1
            matches.append({"id": mid, "stage": "THIRD_PLACE", "group": None, "utcDate": "2026-07-12T18:00:00Z",
                            "status": "FINISHED", "home": a, "away": b, "homeScore": ga, "awayScore": gb,
                            "winner": "HOME" if ga > gb else "AWAY", "minute": None, "duration": "REGULAR",
                            "aet": False, "shootout": False, "penHome": None, "penAway": None})
            mid += 1
            data = write_and_compute(DIR, matches, table)
            third = a if ga > gb else b
            show("THIRD-PLACE PLAYOFF - %s beat %s for the bronze" % (third, b if third == a else a), data, pause)
        bracket = winners

    champ = (data.get("champion_decided") or {})
    print("\n\U0001F3C6 CHAMPION: %s (%s)" % (champ.get("team", "?"), champ.get("owner", "?")))
    print("\nDemo complete. The tracker shows the full tournament + champion banner.")
    if not args.fast:
        print("Page still live at http://localhost:%d/  (Ctrl-C to stop)" % args.port)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


def _tally(s, a, b, ga, gb):
    for tm, gf, gaa in ((a, ga, gb), (b, gb, ga)):
        s[tm]["P"] += 1; s[tm]["GF"] += gf; s[tm]["GA"] += gaa
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
            {"position": i + 1, "team": nm, "playedGames": s[nm]["P"], "won": s[nm]["W"], "draw": s[nm]["D"],
             "lost": s[nm]["L"], "goalsFor": s[nm]["GF"], "goalsAgainst": s[nm]["GA"],
             "goalDifference": s[nm]["GF"] - s[nm]["GA"], "points": s[nm]["pts"]}
            for i, nm in enumerate(rows)]})
    return out


if __name__ == "__main__":
    sys.exit(main())
