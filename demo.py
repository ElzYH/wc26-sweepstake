#!/usr/bin/env python3
"""Standalone local demo — press run:

    python3 demo.py                     ->  http://localhost:8000

A self-contained mini-app (stdlib only) that simulates a tournament and replays it live: games go
TIMED -> IN_PLAY -> FINISHED on a virtual clock, points score, and betting works (every player's
PIN is DEMO). It imports the engine libraries (scoring, wager, the simulator) but never touches,
configures, or runs the real server — nothing here can affect a production site.

    --mode sim|irl                sim: an invented tournament (default). irl: replay the REAL WC26
                                  (needs results_wc2026.json + draw_result_wc2026.json committed;
                                  add --irl-bets to preload the real wagers and watch them settle)
    --speed fast|matchday|slow    10 / 30 / 120 seconds per tournament day (default matchday)
    --no-betting                  pure sweepstake: no odds, no bet endpoints
    --seed N                      reproducible tournament        --players a,b,c   named crew
    --port 8000                   --reset                        wipe and start a new game

Sim tournaments come with fabricated deep data — scorers with minutes, bookings (cards market on),
starting XIs — so match sheets, timelines and the cards market all light up; live games reveal
their goals and cards minute by minute.
"""
import argparse
import json
import math
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(ROOT, "demo-site")
sys.path.insert(0, ROOT)
import scoring  # noqa: E402
import wager    # noqa: E402

SPEEDS = {"fast": 10, "matchday": 30, "slow": 120}
NAMES = ["Ava", "Zed", "Kofi", "Mika", "Ines", "Theo", "Nadia", "Ravi"]
PAGES = {"/": "tracker.html", "/tracker.html": "tracker.html", "/me.html": "me.html",
         "/watch.html": "watch.html", "/wheel.html": "wheel.html", "/icon.svg": "icon.svg",
         "/manifest.webmanifest": "manifest.webmanifest", "/sw.js": "sw.js"}

_lock = threading.Lock()


def setup(a):
    if a.reset:
        shutil.rmtree(SITE, ignore_errors=True)
    if os.path.exists(os.path.join(SITE, "replay_source.json")):
        print("Resuming the previous demo game (use --reset for a fresh one)")
        return
    os.makedirs(SITE, exist_ok=True)
    if a.mode == "irl":
        for f in ("results_wc2026.json", "draw_result_wc2026.json"):
            if not os.path.exists(os.path.join(ROOT, f)):
                sys.exit("IRL mode needs %s in the repo (run archive.py on the box, commit it)." % f)
        src = json.load(open(os.path.join(ROOT, "results_wc2026.json")))
        json.dump(src, open(os.path.join(SITE, "replay_source.json"), "w"))
        shutil.copy2(os.path.join(ROOT, "draw_result_wc2026.json"), os.path.join(SITE, "draw_result.json"))
        names = [p["name"] for p in json.load(open(os.path.join(SITE, "draw_result.json")))["players"]]
        wag = []
        if a.irl_bets and os.path.exists(os.path.join(ROOT, "wagers_wc2026.json")):
            wag = json.load(open(os.path.join(ROOT, "wagers_wc2026.json")))
            for w in wag:                                     # reset so the replay settles them again
                if isinstance(w, dict) and not w.get("credit"):
                    for l in (w.get("legs") or []):
                        l.pop("result", None)
                    if w.get("status") in ("won", "lost", "void"):
                        w["status"] = "pending"
                        w.pop("result", None); w.pop("settled_at", None)
        json.dump(wag, open(os.path.join(SITE, "wagers.json"), "w"))
        label = "the REAL WC26" + (" with the real bets riding" if wag else "")
    else:
        names = [n.strip() for n in a.players.split(",") if n.strip()] or \
            random.Random(a.seed).sample(NAMES, 5)
        teams = json.load(open(os.path.join(ROOT, "teams.json")))["teams"]
        rng = random.Random(a.seed)
        rng.shuffle(teams)
        players = [{"name": n, "teams": [{"name": t["name"], "tier": t.get("tier"),
                                          "group": t.get("group")} for t in teams[i::len(names)]]}
                   for i, n in enumerate(names)]
        json.dump({"players": players, "mode": "snake"}, open(os.path.join(SITE, "draw_result.json"), "w"))
        shutil.copy2(os.path.join(ROOT, "teams.json"), os.path.join(SITE, "teams.json"))
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "simulate_2026.py"), str(a.seed)],
                       cwd=SITE, check=True, stdout=subprocess.DEVNULL)
        src = json.load(open(os.path.join(SITE, "results.json")))
        src["matches"] = _fabricate_deep(src.get("matches", []), a.seed)
        json.dump(src, open(os.path.join(SITE, "replay_source.json"), "w"))
        os.remove(os.path.join(SITE, "results.json"))
        json.dump([], open(os.path.join(SITE, "wagers.json"), "w"))
        label = "an invented tournament (seed %d)" % a.seed
    shutil.copy2(os.path.join(ROOT, "teams.json"), os.path.join(SITE, "teams.json"))
    for junk in ("tracker_data.json",):
        p = os.path.join(SITE, junk)
        if os.path.exists(p):
            os.remove(p)
    json.dump({"started": int(time.time()), "seconds_per_day": SPEEDS[a.speed], "names": names,
               "betting": not a.no_betting},
              open(os.path.join(SITE, "demo_meta.json"), "w"))
    print("New demo game: %s · crew %s · %s pace (%ds/day) · betting %s"
          % (label, ", ".join(names), a.speed, SPEEDS[a.speed],
             "OFF" if a.no_betting else "on (PIN: DEMO)"))


FIRST = ["Marco", "Jude", "Kai", "Leo", "Sam", "Nico", "Youssef", "Ade", "Tom", "Rafa", "Iker",
         "Dan", "Musa", "Erik", "Jan", "Luca", "Pedro", "Alex", "Omar", "Vik"]
LAST = ["Silva", "Kane", "Mbeki", "Larsson", "Costa", "Haaland", "Ali", "Okafor", "Novak", "Reyes",
        "Petit", "Weber", "Moreau", "Santos", "Berg", "Ricci", "Nakamura", "Diallo", "Kovacs", "Byrne"]


def _squad(team, rng):
    return ["%s %s" % (rng.choice(FIRST), rng.choice(LAST)) for _ in range(11)]


def _fabricate_deep(matches, seed):
    """Give a simulated tournament the deep-data layer a paid feed would: scorers with minutes,
    bookings with players (cards market auto-gates ON), and starting XIs — seeded, so a given demo
    game always tells the same story."""
    rng = random.Random(seed * 7 + 1)
    squads = {}
    for m in matches:
        if m.get("status") not in ("FINISHED", "AWARDED"):
            continue
        for side, n in (("home", m.get("homeScore") or 0), ("away", m.get("awayScore") or 0)):
            t = m.get(side)
            squads.setdefault(t, _squad(t, rng))
        m["homeLineup"] = [{"name": p, "shirtNumber": i + 1} for i, p in enumerate(squads[m["home"]])]
        m["awayLineup"] = [{"name": p, "shirtNumber": i + 1} for i, p in enumerate(squads[m["away"]])]
        scorers, mins_used = [], set()
        for side, n in (("HOME", m.get("homeScore") or 0), ("AWAY", m.get("awayScore") or 0)):
            for _ in range(n):
                mn = rng.choice([x for x in range(2, 118 if m.get("aet") else 90) if x not in mins_used])
                mins_used.add(mn)
                team = m["home"] if side == "HOME" else m["away"]
                scorers.append({"minute": mn, "team": side, "player": rng.choice(squads[team])})
        m["scorers"] = sorted(scorers, key=lambda g: g["minute"])
        events, ch, ca, rh, ra = [], 0, 0, 0, 0
        for _ in range(rng.randint(1, 6)):
            side = rng.choice(("HOME", "AWAY"))
            mn = rng.randint(5, 90)
            red = rng.random() < 0.06
            team = m["home"] if side == "HOME" else m["away"]
            events.append({"minute": mn, "team": side, "red": red, "player": rng.choice(squads[team])})
            if side == "HOME":
                ch += 1; rh += 1 if red else 0
            else:
                ca += 1; ra += 1 if red else 0
        m["cardEvents"] = sorted(events, key=lambda e: e["minute"])
        m["cardsHome"], m["cardsAway"], m["redHome"], m["redAway"] = ch, ca, rh, ra
    return matches


def _meta():
    return json.load(open(os.path.join(SITE, "demo_meta.json")))


def revealed_results():
    """The simulated tournament as of the virtual clock: past days final, the frontier day
    drip-revealed through TIMED -> IN_PLAY (live clock, half score) -> FINISHED."""
    src = json.load(open(os.path.join(SITE, "replay_source.json")))
    meta = _meta()
    spd = meta["seconds_per_day"]
    matches = src.get("matches", [])
    days = sorted({(m.get("utcDate") or "")[:10] for m in matches if m.get("utcDate")})
    pos = (time.time() - meta["started"]) / spd
    cur, frac = int(math.floor(pos)), pos - int(math.floor(pos))
    out, order = [], {}
    for m in matches:
        m = dict(m)
        d = (m.get("utcDate") or "")[:10]
        idx = days.index(d) if d in days else 0
        k = order.get(d, 0)
        order[d] = k + 1
        ko = meta["started"] + idx * spd + int(spd * 0.4) + k       # kickoffs live on the replay clock,
        m["utcDate"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ko))  # so betting windows behave
        if idx > cur or (idx == cur and frac < 0.4):
            m["status"] = "TIMED"
            for f in ("homeScore", "awayScore", "winner", "penHome", "penAway", "aet", "shootout",
                      "scorers", "cardsHome", "cardsAway", "redHome", "redAway", "cardEvents"):
                m.pop(f, None)
            if idx > cur:
                m.pop("homeLineup", None); m.pop("awayLineup", None)   # XIs drop just before kick-off
        elif idx == cur and frac < 0.8 and m.get("status") in ("FINISHED", "AWARDED"):
            m["status"] = "IN_PLAY"
            for f in ("winner", "penHome", "penAway", "aet", "shootout"):
                m.pop(f, None)
            mn = int(90 * (frac - 0.4) / 0.4)
            m["minute"] = mn
            if m.get("scorers"):                       # the timeline reveals itself minute by minute
                m["scorers"] = [g for g in m["scorers"] if (g.get("minute") or 0) <= mn]
                m["homeScore"] = sum(1 for g in m["scorers"] if g["team"] == "HOME")
                m["awayScore"] = sum(1 for g in m["scorers"] if g["team"] == "AWAY")
            else:
                m["homeScore"] = (m.get("homeScore") or 0) // 2
                m["awayScore"] = (m.get("awayScore") or 0) // 2
            if m.get("cardEvents"):
                m["cardEvents"] = [e for e in m["cardEvents"] if (e.get("minute") or 0) <= mn]
                m["cardsHome"] = sum(1 for e in m["cardEvents"] if e["team"] == "HOME")
                m["cardsAway"] = sum(1 for e in m["cardEvents"] if e["team"] == "AWAY")
            m.pop("homeLineup", None) if frac < 0.45 else None
        out.append(m)
    return {"competition": src.get("competition", "WC"), "matches": out,
            "standings": src.get("standings", [])}


def rebuild():
    """One demo tick: reveal, settle every bet against the revealed games, recompute the tracker."""
    with _lock:
        res = revealed_results()
        json.dump(res, open(os.path.join(SITE, "results.json"), "w"))
        ws = json.load(open(os.path.join(SITE, "wagers.json")))
        if wager.settle_all(ws, res["matches"]):
            json.dump(ws, open(os.path.join(SITE, "wagers.json"), "w"))
        scoring.compute(teams_path=os.path.join(SITE, "teams.json"),
                        draw_path=os.path.join(SITE, "draw_result.json"),
                        results_path=os.path.join(SITE, "results.json"),
                        out=os.path.join(SITE, "tracker_data.json"),
                        wagers=(ws if _meta().get("betting", True) else None))


def ticker(interval=3):
    while True:
        try:
            rebuild()
        except Exception as e:
            print("[demo] tick error:", e)
        time.sleep(interval)


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in PAGES:
            p = os.path.join(ROOT, PAGES[path])
            ct = "text/html" if p.endswith("html") else ("image/svg+xml" if p.endswith("svg")
                 else "application/javascript" if p.endswith("js") else "application/json")
            return self._send(200, open(p, "rb").read(), ct)
        if path in ("/tracker_data.json", "/results.json", "/draw_result.json", "/teams.json"):
            return self._send(200, open(os.path.join(SITE, path[1:]), "rb").read())
        if path == "/api/wagers":
            with _lock:
                return self._send(200, {"ok": True, "wagers": json.load(open(os.path.join(SITE, "wagers.json")))})
        if path == "/api/status":
            return self._send(200, {"ok": True, "configured": True, "demo": True, "wagering": True})
        if path.startswith("/api/"):
            return self._send(200, {"ok": False, "demo": True, "error": "Not available in the demo."})
        return self._send(404, {"ok": False})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception:
            body = {}
        if path not in ("/api/place_wager", "/api/place_acca"):
            return self._send(200, {"ok": False, "demo": True, "error": "Not available in the demo."})
        if not _meta().get("betting", True):
            return self._send(400, {"ok": False, "error": "Betting is OFF in this demo (--no-betting)."})
        if (body.get("pin") or "").upper() != "DEMO":
            return self._send(400, {"ok": False, "error": "Demo PIN is DEMO (for every player)."})
        with _lock:
            td = json.load(open(os.path.join(SITE, "tracker_data.json")))
            teams = {t["team"]: t for t in td.get("team_table", [])} if td.get("team_table") else {}
            comp = json.load(open(os.path.join(SITE, "teams.json")))["teams"]
            comps = {t["name"]: t.get("composite", 50) for t in comp}
            fx = {m.get("matchId"): m for m in td.get("fixtures", [])}
            ws = json.load(open(os.path.join(SITE, "wagers.json")))
            def strengths(m):
                return comps.get(m.get("home"), 50), comps.get(m.get("away"), 50)
            if path == "/api/place_wager":
                m = fx.get(body.get("matchId"))
                if not m:
                    return self._send(400, {"ok": False, "error": "Unknown game."})
                m = dict(m, id=m.get("matchId"))          # bets must key by the results id, not the composite
                ch, ca = strengths(m)
                ok, w = wager.place(ws, body.get("player"), m, body.get("selection"),
                                    body.get("stake"), 1000, ch, ca,
                                    market=body.get("market", "result"), line=body.get("line"))
                if not ok:
                    return self._send(400, {"ok": False, "error": w})
            else:
                legs = []
                for l in body.get("legs") or []:
                    m = fx.get(l.get("matchId"))
                    if not m:
                        return self._send(400, {"ok": False, "error": "Unknown game in the acca."})
                    ch, ca = strengths(m)
                    legs.append({"match": dict(m, id=m.get("matchId")), "selection": l.get("selection"), "market": l.get("market", "result"),
                                 "line": l.get("line"), "comp_home": ch, "comp_away": ca})
                ok, w = wager.place_acca(ws, body.get("player"), legs, body.get("stake"), 1000)
                if not ok:
                    return self._send(400, {"ok": False, "error": w})
            json.dump(ws, open(os.path.join(SITE, "wagers.json"), "w"))
            return self._send(200, {"ok": True, "wager": w})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("sim", "irl"), default="sim")
    ap.add_argument("--irl-bets", action="store_true")
    ap.add_argument("--no-betting", action="store_true")
    ap.add_argument("--speed", choices=SPEEDS, default="matchday")
    ap.add_argument("--seed", type=int, default=random.randrange(10 ** 6))
    ap.add_argument("--players", default="")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    setup(a)
    rebuild()
    threading.Thread(target=ticker, daemon=True).start()
    print("\n  ->  http://localhost:%d/tracker.html   (betting PIN for everyone: DEMO)\n" % a.port)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
