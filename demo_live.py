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
import wager
from draw import Draw

PLAYERS = ["Erol", "James", "Louis", "Ismail", "Reuben"]
REPO = os.path.dirname(os.path.abspath(__file__))
KO_ROUNDS = ["LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "FINAL"]
_OWNER = {}
_PREV = {"data": None}
_LIVE = ("IN_PLAY", "PAUSED", "LIVE")
DIR = os.path.join(REPO, "wc26-demo")
WAGER = False                 # --wager turns on the betting trial
WAGERS = []                   # in-memory bet log for the demo
COMP = {}                     # team composites, for live pricing
CUR_MATCHES = []              # latest match list, for placing/settling from the web handler
PINS = {}                     # demo bet passcodes {player: code} so the demo enforces "only you bet your points"
LINKS = {}                    # demo discord-style links {token: player} (not used by web, here for parity)


def _gen_demo_pin():
    import random as _r
    return "".join(_r.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(5))

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
        path = self.path.split("?")[0]
        if WAGER and path in ("/api/place_wager", "/api/place_acca"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or "{}")
            except Exception:
                return self._send(400, json.dumps({"ok": False, "error": "bad request"}))
            res = _demo_place_acca(body) if path == "/api/place_acca" else _demo_place(body)
            return self._send(200, json.dumps(res))
        self._send(200, json.dumps({"ok": True}))           # demo: accept poll/etc. no-ops

    def _get_extra(self, path):
        if WAGER and path == "/api/wagers":
            who = ""
            if "player=" in self.path:
                import urllib.parse
                who = urllib.parse.unquote_plus(self.path.split("player=", 1)[1].split("&")[0])
            wl = [w for w in WAGERS if (not who or w["player"] == who)]
            self._send(200, json.dumps({"ok": True, "wagers": wl}))
            return True
        return False

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send_response(302); self.send_header("Location", "/tracker.html"); self.end_headers(); return
        if self._get_extra(path):
            return
        if path == "/api/status":
            return self._send(200, json.dumps({
                "configured": True, "players": PLAYERS, "scoring_mode": "hybrid", "draw_mode": "fair",
                "competition": "WC", "drawn": True, "needs_key": True, "has_token": True,
                "push_enabled": False, "discord": False, "has_invite": False, "bot_ready": False,
                "digest_enabled": False, "leftover": "pool", "poll_minutes": 1, "site_url": "",
                "wagering_enabled": WAGER,
                "wager_pins_set": bool(PINS),
                "wager_caps": ({"min_stake": wager.MIN_STAKE, "max_stake": _demo_round_max(),
                                "base_max_stake": wager.MAX_STAKE, "max_return": wager.MAX_RETURN,
                                "max_pending": wager.MAX_PENDING, "max_acca_legs": wager.MAX_ACCA_LEGS} if WAGER else None)}))
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
    html = html.replace("setInterval(load,30000)", "setInterval(load,2500)")
    open(os.path.join(d, "tracker.html"), "w").write(html)


def write_and_compute(d, matches, standings):
    global CUR_MATCHES
    CUR_MATCHES = matches
    json.dump({"competition": "WC", "matches": matches, "standings": standings},
              open(os.path.join(d, "results.json"), "w"))
    wl = None
    if WAGER:
        wager.settle_all(WAGERS, matches)
        wl = WAGERS
    return scoring.compute(
        teams_path=os.path.join(d, "teams.json"),
        draw_path=os.path.join(d, "draw_result.json"),
        results_path=os.path.join(d, "results.json"),
        out=os.path.join(d, "tracker_data.json"), wagers=wl)


def _demo_round_max():
    """Highest single-bet cap among games you can currently bet on in the demo (current round)."""
    caps = [wager.stage_max_stake(m.get("stage")) for m in CUR_MATCHES if wager.can_bet_on(m)]
    return max(caps) if caps else wager.MAX_STAKE


def _demo_place(body):
    """Place a bet from the web UI during the demo (mirrors the server's validation, incl. the passcode)."""
    player = str(body.get("player", "")).strip()
    selection = str(body.get("selection", "")).strip().upper()
    mid = str(body.get("matchId", "")).strip()
    stake = body.get("stake")
    if player not in PLAYERS:
        return {"ok": False, "error": "Pick a valid player."}
    if PINS and str(body.get("pin", "")).strip().upper() != PINS.get(player):
        return {"ok": False, "bad_pin": True, "error": "Wrong bet passcode for %s." % player}
    data = json.load(open(os.path.join(DIR, "tracker_data.json")))
    if wager.betting_locked(data):
        return {"ok": False, "error": "Betting is closed — the tournament is over."}
    match = next((m for m in CUR_MATCHES if wager.match_id(m) == mid), None)
    if not match:
        return {"ok": False, "error": "That game could not be found."}
    prow = next((p for p in data.get("players", []) if p.get("name") == player), {})
    settled = prow.get("points_settled")
    if settled is None:
        settled = round((prow.get("points", 0) or 0) - (prow.get("live", 0) or 0), 1)
    ch = COMP.get(match.get("home"), 0)
    ca = COMP.get(match.get("away"), 0)
    ok, res = wager.place(WAGERS, player, match, selection, stake, settled, ch, ca)
    if ok:
        write_and_compute(DIR, CUR_MATCHES, json.load(open(os.path.join(DIR, "results.json")))["standings"])
        return {"ok": True, "wager": res}
    return {"ok": False, "error": res}


def _demo_place_acca(body):
    """Place a 1-5 leg accumulator from the web UI during the demo (mirrors the server, incl. the passcode)."""
    player = str(body.get("player", "")).strip()
    legs_in = body.get("legs") if isinstance(body.get("legs"), list) else []
    stake = body.get("stake")
    if player not in PLAYERS:
        return {"ok": False, "error": "Pick a valid player."}
    if PINS and str(body.get("pin", "")).strip().upper() != PINS.get(player):
        return {"ok": False, "bad_pin": True, "error": "Wrong bet passcode for %s." % player}
    data = json.load(open(os.path.join(DIR, "tracker_data.json")))
    if wager.betting_locked(data):
        return {"ok": False, "error": "Betting is closed — the tournament is over."}
    if not (1 <= len(legs_in) <= wager.MAX_ACCA_LEGS):
        return {"ok": False, "error": "An accumulator is 1 to %d picks." % wager.MAX_ACCA_LEGS}
    selections = []
    for lg in legs_in:
        m = next((x for x in CUR_MATCHES if wager.match_id(x) == str(lg.get("matchId", ""))), None)
        if not m:
            return {"ok": False, "error": "One of those games could not be found."}
        selections.append({"match": m, "selection": str(lg.get("selection", "")).upper(),
                           "comp_home": COMP.get(m.get("home"), 0), "comp_away": COMP.get(m.get("away"), 0)})
    prow = next((p for p in data.get("players", []) if p.get("name") == player), {})
    settled = prow.get("points_settled")
    if settled is None:
        settled = round((prow.get("points", 0) or 0) - (prow.get("live", 0) or 0), 1)
    ok, res = wager.place_acca(WAGERS, player, selections, stake, settled)
    if ok:
        write_and_compute(DIR, CUR_MATCHES, json.load(open(os.path.join(DIR, "results.json")))["standings"])
        return {"ok": True, "wager": res}
    return {"ok": False, "error": res}


def _seed_sample_bets(data):
    """Auto-place a few example bets so the betting UI + analysis have something to show."""
    if WAGERS:
        return
    fx = [f for f in data.get("fixtures", []) if f.get("odds") and f.get("matchId")][:6]
    picks = [("Erol", "HOME", 8), ("James", "AWAY", 5), ("Louis", "HOME", 12),
             ("Ismail", "DRAW", 4), ("Reuben", "HOME", 6), ("Erol", "AWAY", 10)]
    pts = {p["name"]: round(p.get("points", 0) - p.get("live", 0), 1) for p in data.get("players", [])}
    for (who, sel, stake), f in zip(picks, fx):
        m = next((x for x in CUR_MATCHES if wager.match_id(x) == f["matchId"]), None)
        if not m:
            continue
        if sel == "DRAW" and f.get("stage") not in (None, "GROUP_STAGE"):
            sel = "HOME"
        ch = COMP.get(f["home"], 0); ca = COMP.get(f["away"], 0)
        ok, res = wager.place(WAGERS, who, m, sel, min(stake, pts.get(who, 0)), pts.get(who, 0), ch, ca)
        if ok:
            print("   [bet] %-7s %s on %s v %s @ %s  (returns %s)"
                  % (res["player"], res["selection"], res["home"], res["away"], res["frac"], res["return"]))
    # one example 3-fold accumulator so the acca display has something to show
    acca_fx = [f for f in data.get("fixtures", []) if f.get("odds") and f.get("matchId")][6:9]
    if len(acca_fx) == 3:
        sels = []
        for f in acca_fx:
            m = next((x for x in CUR_MATCHES if wager.match_id(x) == f["matchId"]), None)
            if not m:
                sels = []
                break
            s = "HOME" if (f.get("stage") not in (None, "GROUP_STAGE")) else "HOME"
            sels.append({"match": m, "selection": s, "comp_home": COMP.get(f["home"], 0), "comp_away": COMP.get(f["away"], 0)})
        if sels:
            ok, res = wager.place_acca(WAGERS, "Louis", sels, min(5, pts.get("Louis", 0)), pts.get("Louis", 0))
            if ok:
                print("   [acca] Louis %d-fold @ ~%sx (returns %s)" % (len(res["legs"]), res.get("decimal"), res["return"]))
    if WAGERS:
        write_and_compute(DIR, CUR_MATCHES, json.load(open(os.path.join(DIR, "results.json")))["standings"])


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


def _spec(m, a, b, comp, label, force_pens=False, ga=None, gb=None):
    """Pre-roll a match's goal minutes so several can be ticked together."""
    if ga is None or gb is None:
        ga, gb = goals(a, b, comp)
    if force_pens:
        gb = ga                       # showcase game: force level so it goes to a shootout
    return {"m": m, "a": a, "b": b, "label": label, "force_pens": force_pens,
            "hg": sorted(random.sample(range(1, 91), min(ga, 89))) if ga else [],
            "ag": sorted(random.sample(range(1, 91), min(gb, 89))) if gb else []}


def _pens_seq(scored, kicks=5):
    seq = [True] * scored + [False] * (kicks - scored)
    random.shuffle(seq)
    return seq


def _shootout(d, matches, standings, s, delay):
    """Tick a penalty shootout kick-by-kick; the live card shows the running tally."""
    m, a, b = s["m"], s["a"], s["b"]
    print("   \U0001F945 PENALTY SHOOTOUT: %s v %s" % (a, b))
    finalH = random.choice([3, 4, 5]); finalA = max(0, finalH - random.choice([1, 2]))
    if random.random() < 0.5:
        finalH, finalA = finalA, finalH
    m["shootout"] = True; m["penHome"] = 0; m["penAway"] = 0; m["minute"] = 120
    seqH, seqA = _pens_seq(finalH), _pens_seq(finalA)
    for i in range(5):
        if seqH[i]:
            m["penHome"] += 1
        show("   pens %s %d-%d %s  (%s %s)" % (a, m["penHome"], m["penAway"], b, a, "scores" if seqH[i] else "misses"),
             write_and_compute(d, matches, standings), 0)
        time.sleep(delay * 1.5)
        if seqA[i]:
            m["penAway"] += 1
        show("   pens %s %d-%d %s  (%s %s)" % (a, m["penHome"], m["penAway"], b, b, "scores" if seqA[i] else "misses"),
             write_and_compute(d, matches, standings), 0)
        time.sleep(delay * 1.5)
    return m["penHome"], m["penAway"], ("HOME" if m["penHome"] > m["penAway"] else "AWAY")


def _finish_ko(d, matches, standings, s, speed, delay):
    """Extra time + (live) penalties for one KO match that's level at 90'."""
    m, a, b = s["m"], s["a"], s["b"]
    dur, penH, penA, winner = "REGULAR", None, None, None
    is_ko = m.get("stage") != "GROUP_STAGE"
    if is_ko and (m["homeScore"] == m["awayScore"] or s.get("force_pens")):
        print("   \u23F1 %s level - EXTRA TIME" % s["label"])
        m["status"] = "PAUSED"; write_and_compute(d, matches, standings); time.sleep(delay * 2)
        m["status"] = "IN_PLAY"
        et_home = (not s.get("force_pens")) and random.random() < 0.30
        et_away = (not s.get("force_pens")) and (not et_home) and random.random() < 0.30
        for minute in range(91, 121):
            if et_home and minute == random.choice([100, 105, 113]):
                m["homeScore"] += 1
            if et_away and minute == random.choice([98, 108, 118]):
                m["awayScore"] += 1
            m["minute"] = minute
            if minute % 10 == 0 or minute == 120:
                show("%s - %d' a.e.t. (%s %d-%d %s)" % (s["label"], minute, a, m["homeScore"], m["awayScore"], b),
                     write_and_compute(d, matches, standings), 0)
            time.sleep(delay)
        dur = "EXTRA_TIME"
        if m["homeScore"] == m["awayScore"]:
            dur = "PENALTY_SHOOTOUT"
            penH, penA, winner = _shootout(d, matches, standings, s, delay)
    if winner is None:
        winner = "HOME" if m["homeScore"] > m["awayScore"] else ("AWAY" if m["awayScore"] > m["homeScore"] else ("DRAW" if not is_ko else "HOME"))
    m.update({"status": "FINISHED", "winner": winner, "duration": dur, "aet": dur != "REGULAR",
              "shootout": dur == "PENALTY_SHOOTOUT", "penHome": penH, "penAway": penA})
    write_and_compute(d, matches, standings)
    tail = "" if dur == "REGULAR" else ("  (a.e.t.)" if dur == "EXTRA_TIME" else "  (pens %d-%d)" % (penH, penA))
    show("%s - FULL TIME: %s %d-%d %s%s" % (s["label"], a, m["homeScore"], m["awayScore"], b, tail),
         write_and_compute(d, matches, standings), 0)
    return a if winner == "HOME" else b


def tick_live(d, matches, standings, specs, speed):
    """Tick several matches at once, minute by minute (the website shows them all live together)."""
    delay = 1.0 / max(1, speed)
    print("\n   \u25B6 LIVE (%d game%s at once): %s"
          % (len(specs), "" if len(specs) == 1 else "s", "  |  ".join("%s v %s" % (s["a"], s["b"]) for s in specs)))
    for s in specs:
        s["m"].update({"status": "IN_PLAY", "homeScore": 0, "awayScore": 0, "minute": 0, "winner": None})
    for minute in range(1, 91):
        flash = False
        for s in specs:
            m = s["m"]
            if minute in s["hg"]:
                m["homeScore"] += 1; flash = True
            if minute in s["ag"]:
                m["awayScore"] += 1; flash = True
            m["minute"] = minute
        data = write_and_compute(d, matches, standings)
        if flash or minute % 10 == 0 or minute == 90:
            line = "   |  ".join("%s %d-%d %s" % (s["a"], s["m"]["homeScore"], s["m"]["awayScore"], s["b"]) for s in specs)
            show("%d' - %s" % (minute, line), data, 0)
        time.sleep(delay)
    return [_finish_ko(d, matches, standings, s, speed, delay) for s in specs]


def main():
    global DIR, _OWNER, COMP, WAGER, PINS
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--speed", type=float, default=3.0, help="match-minutes per real second")
    ap.add_argument("--pause", type=float, default=6.0, help="seconds between phases")
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--wager", action="store_true", help="turn on the betting trial + seed example bets")
    ap.add_argument("--pins", action="store_true", help="with --wager: require a per-player passcode (test that you can't bet as someone else)")
    ap.add_argument("--bet-test", dest="bet_test", action="store_true",
                    help="fast betting sandbox: skips to the knockouts and pauses before each round so you can place bets (implies --wager --pins)")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--max-acca", dest="max_acca", type=int, default=None,
                    help="simulate the admin setting the accumulator leg limit (default 3)")
    ap.add_argument("--max-return", dest="max_return", type=float, default=None,
                    help="simulate the admin capping winnings per bet (default: no cap)")
    args = ap.parse_args()
    if args.bet_test:                  # betting sandbox: fast tournament, paused before each knockout round
        args.fast = True
        args.wager = True
        # passcodes are OFF here by default so you can test betting freely;
        # add --pins to also test the "only you can bet your points" protection.
    if args.max_acca is not None:      # admin-configurable acca legs (engine default is 3)
        wager.MAX_ACCA_LEGS = max(2, min(10, args.max_acca))
    if args.max_return is not None:    # admin-optional winnings cap (engine default None = unlimited)
        wager.MAX_RETURN = max(1.0, args.max_return)
    pause = 0 if args.fast else args.pause
    speed = 999 if args.fast else args.speed
    random.seed(args.seed)
    if args.dir:
        DIR = args.dir
    setup_dir(DIR)

    teams = json.load(open(os.path.join(DIR, "teams.json")))["teams"]
    comp = {t["name"]: t["composite"] for t in teams}
    COMP = comp
    WAGER = args.wager
    if args.wager and args.pins:
        PINS = {p: _gen_demo_pin() for p in PLAYERS}
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

    serve_site = (not args.fast) or args.bet_test     # bet-test is fast but MUST serve so you can place bets
    if serve_site:
        serve(args.port)
    print("=" * 58)
    print("WC26 LIVE DEMO - players:", ", ".join(PLAYERS))
    if serve_site:
        print(">> OPEN:  http://localhost:%d/   (refreshes every ~2.5s while it runs)" % args.port)
        print("   Leave this terminal running; notifications print here.")
    print("=" * 58)
    print("Squads:")
    for p in _dr["players"]:
        print("   %-8s: %s" % (p["name"], ", ".join(t["name"] for t in p["teams"])))
    if PINS:
        print("-" * 58)
        print("BET PASSCODES (this run only) — each player can ONLY bet their own points:")
        for p in PLAYERS:
            print("   %-8s  %s" % (p, PINS[p]))
        print("   TEST: pick a player on the Bets tab and try someone else's code — it's rejected.")
        print("-" * 58)

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

    GM_ID = {(a, b): "GP%03d" % i for i, (g, a, b, ga, gb) in enumerate(group_matches)}   # stable ids so group bets settle
    _fut = time.strftime("%Y-%m-%dT18:00:00Z", time.gmtime(time.time() + 21 * 86400))      # always in the future -> bettable

    # two featured group games, ticking live AT ONCE (you see two scoreboards move together) — pick games with no shared team
    i1 = next((j for j in range(1, len(group_matches))
               if not ({group_matches[j][1], group_matches[j][2]} & {group_matches[0][1], group_matches[0][2]})), 1)
    featured_idx = {0, i1}
    featured_g = [group_matches[0], group_matches[i1]]
    rest_g = [gm for j, gm in enumerate(group_matches) if j not in featured_idx]
    matches = []
    if not args.fast:
        fspecs = []
        for (g, a, b, ga, gb) in featured_g:
            fm = {"id": mid, "stage": "GROUP_STAGE", "group": g, "utcDate": "2026-06-11T18:00:00Z",
                  "status": "IN_PLAY", "home": a, "away": b, "homeScore": 0, "awayScore": 0,
                  "winner": None, "minute": 0, "duration": "REGULAR", "aet": False, "shootout": False,
                  "penHome": None, "penAway": None}
            matches.append(fm); mid += 1
            fspecs.append(_spec(fm, a, b, comp, "Group game", ga=ga, gb=gb))
        tick_live(DIR, matches, _standings(gstats, groups, comp), fspecs, speed)
        for fm in matches:                                  # tally the two featured results (now FINISHED)
            _tally(gstats, fm["home"], fm["away"], fm["homeScore"], fm["awayScore"])
    else:
        for (g, a, b, ga, gb) in featured_g:
            w = "HOME" if ga > gb else ("AWAY" if gb > ga else "DRAW")
            matches.append({"id": GM_ID[(a, b)] if args.bet_test else mid, "stage": "GROUP_STAGE", "group": g, "utcDate": "2026-06-11T18:00:00Z",
                            "status": "FINISHED", "home": a, "away": b, "homeScore": ga, "awayScore": gb, "winner": w,
                            "minute": None, "duration": "REGULAR", "aet": False, "shootout": False, "penHome": None, "penAway": None})
            if not args.bet_test:
                mid += 1
            _tally(gstats, a, b, ga, gb)

    # finish the remaining group games in 3 matchday chunks
    chunks = [rest_g[:len(rest_g) // 3], rest_g[len(rest_g) // 3: 2 * len(rest_g) // 3], rest_g[2 * len(rest_g) // 3:]]
    for ci, chunk in enumerate(chunks, 1):
        if args.bet_test and ci == 2 and chunk:        # matchday-1 points are in -> bet the remaining group games (draws allowed)
            remaining = [gm for ch in chunks[1:] for gm in ch]
            _up = [{"id": GM_ID[(a, b)], "stage": "GROUP_STAGE", "group": g, "utcDate": _fut, "status": "TIMED",
                    "home": a, "away": b, "homeScore": None, "awayScore": None, "winner": None}
                   for (g, a, b, ga, gb) in remaining]
            write_and_compute(DIR, matches + _up, _standings(gstats, groups, comp))   # finished so far + upcoming remainder
            print("\n" + "=" * 60)
            print(">>> GROUP STAGE — matchday 1 is in, so you now have points to bet.")
            print(">>> Bet on the remaining group games — DRAWS ARE ALLOWED here (HOME / DRAW / AWAY).")
            print(">>> Open http://localhost:%d/ -> Bets, try a DRAW + a 2-fold acca, then press Enter." % args.port)
            print("=" * 60)
            try:
                input()
            except EOFError:
                pass
        for (g, a, b, ga, gb) in chunk:
            w = "HOME" if ga > gb else ("AWAY" if gb > ga else "DRAW")
            matches.append({"id": GM_ID[(a, b)] if args.bet_test else mid, "stage": "GROUP_STAGE", "group": g, "utcDate": "2026-06-%02dT18:00:00Z" % (12 + ci),
                            "status": "FINISHED", "home": a, "away": b, "homeScore": ga, "awayScore": gb, "winner": w,
                            "minute": None, "duration": "REGULAR", "aet": False, "shootout": False, "penHome": None, "penAway": None})
            if not args.bet_test:
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

    # ---- KNOCKOUTS: two matches tick live each round; SF/final tick in full; the FINAL goes to a live shootout ----
    sf_losers = []
    for r, rnd in enumerate(KO_ROUNDS):
        pairs = [(bracket[i], bracket[i + 1]) for i in range(0, len(bracket), 2)]
        # create EVERY match in the round up front (scheduled), so all teams in the round count as "still in" while the live games tick
        rndm = [km(mid + i, a, b, rnd, status="TIMED") for i, (a, b) in enumerate(pairs)]
        for x in rndm:
            x["utcDate"] = "2026-07-1%dT18:00:00Z" % r
        mid += len(pairs)
        matches.extend(rndm)
        if WAGER and r == 0:                         # R32 just created (upcoming) + players have group points
            seed_data = write_and_compute(DIR, matches, table)
            print("   seeding example bets on the Round of 32...")
            _seed_sample_bets(seed_data)
        if args.bet_test:                            # pause so you can place bets on this round before it kicks off
            write_and_compute(DIR, matches, table)
            cap = wager.stage_max_stake(rnd)
            label = rnd.replace("_", " ").title()
            print("\n" + "=" * 60)
            print(">>> %s coming up — max single bet is now %d points." % (label, cap))
            print(">>> Open http://localhost:%d/ -> Bets and place a single, a 2-fold and a 3-fold acca." % args.port)
            if PINS:
                print(">>> Passcodes are ON. Each player's code is listed above — paste it in, and try a")
                print(">>> wrong one to see it rejected. (Run without --pins for friction-free betting.)")
            else:
                print(">>> No passcode needed in this test. (Add --pins to test the 'only you can bet")
                print(">>> your own points' protection.)")
            print(">>> Press Enter here to play %s and settle those bets..." % label)
            print("=" * 60)
            try:
                input()
            except EOFError:
                pass
        winners = [None] * len(pairs)
        n_feat = len(pairs) if len(pairs) <= 2 else 2       # SF/final tick in full; earlier rounds tick 2 live
        feat = set(range(n_feat))
        is_final = (rnd == "FINAL")
        # featured matches live, two at a time, minute by minute
        if not args.fast:
            specs = [_spec(rndm[i], pairs[i][0], pairs[i][1], comp,
                           rnd.replace("_", " ").title() + (" FINAL" if is_final else ""), force_pens=is_final)
                     for i in range(n_feat)]
            res = tick_live(DIR, matches, table, specs, speed)
            for i in range(n_feat):
                winners[i] = res[i]
        else:
            for i in range(n_feat):
                fa, fb = pairs[i]
                ga, gb = goals(fa, fb, comp)
                if ga == gb: ga += 1
                rndm[i].update({"status": "FINISHED", "homeScore": ga, "awayScore": gb, "winner": "HOME" if ga > gb else "AWAY"})
                winners[i] = fa if ga > gb else fb
        # resolve the rest of the round instantly (one QF is a forfeit)
        for idx, (a, b) in enumerate(pairs):
            if idx in feat:
                continue
            ga, gb = goals(a, b, comp)
            forfeit = (rnd == "QUARTER_FINALS" and idx == n_feat)
            if forfeit:
                ga, gb, status, w = 3, 0, "AWARDED", "HOME"
            else:
                if ga == gb: ga += 1
                status, w = "FINISHED", ("HOME" if ga > gb else "AWAY")
            rndm[idx].update({"status": status, "homeScore": ga, "awayScore": gb, "winner": w, "minute": None})
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
    if serve_site:
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
