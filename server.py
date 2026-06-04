"""
Self-hosted sweepstake server (zero dependencies, stdlib only).

Run on any always-on box (e.g. Oracle Cloud Free Tier):
    python3 server.py            # serves on 0.0.0.0:8000

Open  http://<server-ip>:8000/  -> setup wizard (players, modes, API token).
After setup it serves the wheel reveal and the auto-updating live tracker,
and a background thread refreshes results every few minutes.

The API token is stored in config.json (gitignored) — keep your box private.
"""
import json
import os
import secrets
import shutil
import threading
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(os.environ.get("WC26_DATA", APP_DIR))

def log(*a):
    print("[wc26]", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), *a, flush=True)

_hits = defaultdict(list)
_rl_lock = threading.Lock()
def rate_ok(key, limit, window=60):     # key is (ip, class) so endpoints don't share a bucket
    now = time.time()
    with _rl_lock:
        q = _hits[key]
        while q and q[0] < now - window:
            q.pop(0)
        if len(q) >= limit:
            return False
        q.append(now)
        return True


import draw as draw_mod
import scoring as scoring_mod

CONFIG = os.environ.get("WC26_CONFIG", "config.json")
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")   # set HOST=127.0.0.1 when behind a reverse proxy
STATIC = {"tracker.html", "wheel.html", "setup.html", "me.html", "watch.html",
          "teams.json", "tracker_data.json", "draw_result.json"}
_lock = threading.Lock()


def load_config():
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            return json.load(f)
    return {}


def save_config(c):
    with open(CONFIG, "w") as f:
        json.dump(c, f, indent=2)
    try:
        os.chmod(CONFIG, 0o600)   # token + admin key: owner read/write only
    except OSError:
        pass


def backup_draw():
    if os.path.exists("draw_result.json"):
        try:
            os.makedirs("backups/draws", exist_ok=True)
            shutil.copy2("draw_result.json", f"backups/draws/draw-{time.strftime('%Y%m%d-%H%M%S')}.json")
            snaps = sorted(f for f in os.listdir("backups/draws") if f.startswith("draw-"))
            for old in snaps[:-20]:                 # keep only the 20 most recent draw snapshots
                os.remove(os.path.join("backups/draws", old))
        except OSError:
            pass


def backup_data():
    try:
        os.makedirs("backups/last_good", exist_ok=True)
        for f in ("draw_result.json", "results.json", "tracker_data.json"):
            if os.path.exists(f):
                shutil.copy2(f, os.path.join("backups/last_good", f))
    except OSError:
        pass


def reset_draw():
    backup_draw()                       # keep a copy before wiping, so a re-draw is recoverable
    for f in ("draw_result.json", "tracker_data.json", "results.json", LIVE_FILE):
        if os.path.exists(f):
            os.remove(f)


LIVE_FILE = "live_draw.json"


def live_load():
    try:
        with open(LIVE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"phase": "idle", "active": False, "done": False, "order": [], "picks": [], "updated": None}


def live_save(state):
    state["updated"] = time.time()
    with open(LIVE_FILE, "w") as f:
        json.dump(state, f)


def build_draw_result(payload):
    """Turn the wheel's {players:[{name,teams:[teamName]}], bonus_pool:[name]} into a
    full draw_result.json, looking up tier/group/composite from teams.json."""
    teams = {t["name"]: t for t in json.load(open("teams.json"))["teams"]}
    def expand(name):
        t = teams.get(name, {"name": name, "tier": 4, "group": "?", "confederation": "?", "composite": 0})
        return {"name": t["name"], "tier": t["tier"], "group": t["group"],
                "confederation": t.get("confederation", "?"), "composite": t.get("composite", 0)}
    players = [{"name": p["name"], "teams": [expand(n) for n in p.get("teams", [])]}
               for p in payload.get("players", [])]
    return {"mode": payload.get("mode", "weighted-clockwork"),
            "leftover_policy": payload.get("leftover", "pool"),
            "players": players,
            "bonus_pool": [expand(n) for n in payload.get("bonus_pool", [])]}


def _write_pretournament(competition):
    """No token yet: show the real groups (0-0-0) from teams.json so the board isn't empty."""
    teams = json.load(open("teams.json"))["teams"]
    groups = {}
    for t in teams:
        groups.setdefault(t["group"], []).append(t)
    standings = []
    for g in sorted(groups):
        tbl = sorted(groups[g], key=lambda t: -t["composite"])
        standings.append({"group": g, "table": [
            {"position": i + 1, "team": t["name"], "playedGames": 0, "won": 0, "draw": 0,
             "lost": 0, "goalsFor": 0, "goalsAgainst": 0, "goalDifference": 0, "points": 0}
            for i, t in enumerate(tbl)]})
    json.dump({"competition": competition, "matches": [], "standings": standings},
              open("results.json", "w"))


def ensure_admin_key():
    cfg = load_config()
    env = os.environ.get("ADMIN_KEY")
    if env and len(env) < 15:
        print(f"[warn] ADMIN_KEY '{env}' is under 15 chars; generating a strong one instead.")
        env = None
    key = env or cfg.get("admin_key")
    if not key or len(key) < 15:
        key = secrets.token_urlsafe(16)   # ~22 chars; upgrades any old short key
    if cfg.get("admin_key") != key:
        cfg["admin_key"] = key
        save_config(cfg)
    return key


def draw_locked():
    return os.path.exists("draw_result.json")


def key_ok(body):
    import hmac
    return hmac.compare_digest(str(body.get("admin_key", "")).strip(),
                               str(load_config().get("admin_key", "")))


SUBS_FILE = "telegram_subs.json"          # {player_name: [chat_id, ...]}
_bot_user = {"name": None}


def _tg_token():
    return (load_config().get("telegram_token") or "").strip()


def tg_send(chat_id, text):
    """Low-level fire-and-forget message to one chat. Never raises."""
    tok = _tg_token()
    if not tok:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tok, data=data, timeout=8)
    except Exception as e:
        log("telegram send failed:", e)


def _load_subs():
    try:
        with open(SUBS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_subs(subs):
    with open(SUBS_FILE, "w") as f:
        json.dump(subs, f)


def tg_player(player, text):
    """Message every chat subscribed as this player."""
    for cid in _load_subs().get(player, []):
        tg_send(cid, text)


def tg_broadcast(text):
    """Message every subscribed chat once."""
    seen = set()
    for chats in _load_subs().values():
        for cid in chats:
            if cid not in seen:
                seen.add(cid)
                tg_send(cid, text)


def bot_username():
    if _bot_user["name"]:
        return _bot_user["name"]
    tok = _tg_token()
    if not tok:
        return None
    try:
        with urllib.request.urlopen("https://api.telegram.org/bot%s/getMe" % tok, timeout=8) as r:
            _bot_user["name"] = (json.loads(r.read().decode()).get("result") or {}).get("username")
    except Exception as e:
        log("telegram getMe failed:", e)
    return _bot_user["name"]


def _load_tracker():
    try:
        with open("tracker_data.json") as f:
            return json.load(f)
    except Exception:
        return None


def _alive_owners(td):
    """{team_name: (alive_bool, owner)} from a tracker_data snapshot."""
    out = {}
    for p in (td or {}).get("players", []):
        for t in p.get("teams", []):
            out[t.get("name")] = (t.get("status") == "alive", p.get("name"))
    return out


def _fixture_status(td):
    """{(home,away): (status, homeOwner, awayOwner, homeScore, awayScore)} for kickoff + goal alerts."""
    out = {}
    for m in (td or {}).get("fixtures", []):
        out[(m.get("home"), m.get("away"))] = (m.get("status"), m.get("homeOwner"), m.get("awayOwner"),
                                               m.get("homeScore"), m.get("awayScore"))
    return out


LIVE_STATUSES = ("IN_PLAY", "PAUSED", "LIVE", "SUSPENDED")


def notify_changes(old):
    """Compare previous tracker snapshot to the new one and ping each player about their teams."""
    new = _load_tracker()
    if not new or old is None:
        return                              # first compute / no data: nothing to compare
    if (new.get("stats") or {}).get("matches_played", 0) == 0:
        return
    # overall (Both) leader change -> broadcast
    try:
        ol = (old["leaderboards"]["hybrid"][0] or {}).get("name")
        nl = (new["leaderboards"]["hybrid"][0] or {}).get("name")
        if nl and ol and nl != ol:
            tg_broadcast("📈 New leader: <b>%s</b> now tops the table." % nl)
    except Exception:
        pass
    # a player's team kicks off or scores -> tell that player
    try:
        of, nf = _fixture_status(old), _fixture_status(new)
        for key, nv in nf.items():
            h, a = key
            st, ho, ao, nhs, nas = nv
            ov = of.get(key)
            was = ov[0] if ov else None
            home_owned = ho and ho not in ("—", "-")
            away_owned = ao and ao not in ("—", "-")
            if st in LIVE_STATUSES and was not in LIVE_STATUSES:        # kickoff
                if home_owned: tg_player(ho, "🔵 Your team <b>%s</b> is playing now — vs %s." % (h, a))
                if away_owned: tg_player(ao, "🔵 Your team <b>%s</b> is playing now — vs %s." % (a, h))
            elif st in LIVE_STATUSES and ov:                            # goal during play
                ohs, oas = ov[3], ov[4]
                if None not in (nhs, nas, ohs, oas):
                    score = "%s %d–%d %s" % (h, nhs, nas, a)
                    if nhs > ohs and home_owned: tg_player(ho, "⚽ <b>%s</b> scored! %s" % (h, score))
                    if nas > oas and away_owned: tg_player(ao, "⚽ <b>%s</b> scored! %s" % (a, score))
    except Exception:
        pass
    # a player's team is knocked out -> tell that player
    try:
        oa, na = _alive_owners(old), _alive_owners(new)
        for t in oa:
            if oa[t][0] and t in na and not na[t][0]:
                owner = na[t][1]
                if owner and owner not in ("—", "-"):
                    tg_player(owner, "❌ Your team <b>%s</b> is out. Check the leaderboard to see where you stand." % t)
    except Exception:
        pass


def update_now(cfg):
    """Fetch results (if a token is set) and recompute the tracker."""
    if not os.path.exists("draw_result.json"):
        return True, None
    old_snapshot = _load_tracker()
    token = cfg.get("token")
    try:
        if token:
            import update_results
            update_results.COMPETITION = cfg.get("competition", "WC")
            data = update_results.fetch(out="results.tmp.json", token=token)
            if data and data.get("matches"):
                os.replace("results.tmp.json", "results.json")        # atomic; only swap in good data
            else:
                if os.path.exists("results.tmp.json"):
                    os.remove("results.tmp.json")
                if not os.path.exists("results.json"):
                    return False, "feed returned no matches"
                # otherwise keep the last good results.json untouched
        else:
            _write_pretournament(cfg.get("competition", "WC"))
        scoring_mod.compute(out="tracker_data.json", default_mode=cfg.get("scoring_mode", "hybrid"))
        cfg["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_config(cfg)
        notify_changes(old_snapshot)        # personalised alerts (if any subscribers)
        backup_data()
        try:
            td = _load_tracker() or {}
            log("recomputed:", (td.get("stats") or {}).get("matches_played", 0), "matches played")
        except Exception:
            pass
        return True, None
    except Exception as e:
        if os.path.exists("results.tmp.json"):
            try:
                os.remove("results.tmp.json")
            except OSError:
                pass
        return False, str(e)          # results.json + tracker_data.json left intact


def poller():
    while True:
        cfg = load_config()
        mins = cfg.get("poll_minutes", 10)
        if cfg.get("players") and cfg.get("token") and os.path.exists("draw_result.json"):
            with _lock:
                ok, err = update_now(cfg)
            if not ok:
                print("[poller] update failed:", err)
        time.sleep(max(60, mins * 60))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src 'self' https://fonts.gstatic.com; "
                         "script-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name):
        generated = {"draw_result.json", "tracker_data.json"}
        full = name if name in generated else os.path.join(APP_DIR, name)
        if name not in STATIC or not os.path.exists(full):
            return self._send(404, "not found", "text/plain")
        ctype = "text/html" if name.endswith(".html") else "application/json"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            cfg = load_config()
            if not cfg.get("players"):
                dest = "/setup"
            elif not os.path.exists("draw_result.json"):
                dest = "/wheel"
            else:
                dest = "/tracker"
            self.send_response(302); self.send_header("Location", dest); self.end_headers(); return
        if path in ("/draw.html", "/draw", "/reveal"):     # legacy/dead links -> the real draw page
            self.send_response(302); self.send_header("Location", "/wheel"); self.end_headers(); return
        if path == "/setup":   return self._file("setup.html")
        if path == "/tracker": return self._file("tracker.html")
        if path == "/wheel":   return self._file("wheel.html")
        if path == "/me":      return self._file("me.html")
        if path == "/watch":   return self._file("watch.html")
        if path == "/api/live_state": return self._send(200, json.dumps(live_load()))
        if path == "/api/draw_result": return self._file("draw_result.json")
        if path == "/api/telegram_links":          # OPEN read: players self-subscribe, no admin key
            cfg = load_config()
            players = [(p if isinstance(p, str) else p.get("name", "")) for p in cfg.get("players", [])]
            subs = _load_subs()
            return self._send(200, json.dumps({"ok": True,
                "configured": bool(_tg_token()), "bot_username": bot_username(),
                "players": [{"name": nm, "code": "p%d" % i, "subscribed": len(subs.get(nm, []))}
                            for i, nm in enumerate(players)]}))
        if path == "/api/status":
            cfg = load_config()
            return self._send(200, json.dumps({
                "configured": bool(cfg.get("players")), "has_token": bool(cfg.get("token")),
                "players": cfg.get("players", []), "draw_mode": cfg.get("draw_mode"),
                "scoring_mode": cfg.get("scoring_mode"), "last_update": cfg.get("last_update"),
                "competition": cfg.get("competition", "WC"),
                "drawn": draw_locked(), "needs_key": draw_locked(),
                "leftover": cfg.get("leftover", "pool"),
                "max_per_player": cfg.get("max_per_player"),
                "poll_minutes": cfg.get("poll_minutes", 10),
                "has_telegram": bool(cfg.get("telegram_token"))}))
        return self._file(path.lstrip("/"))

    def do_POST(self):
        try:
            self._do_POST()
        except Exception:
            import traceback; traceback.print_exc()
            log("ERROR handling POST", self.path)
            try:
                self._send(500, json.dumps({"ok": False, "error": "server error — check the logs"}))
            except Exception:
                pass

    def _do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        if length > 100_000:
            return self._send(413, json.dumps({"ok": False, "error": "request too large"}))
        try:
            body = json.loads(self.rfile.read(length) or "{}") if length else {}
        except Exception:
            return self._send(400, json.dumps({"ok": False, "error": "bad JSON"}))
        if not isinstance(body, dict):
            return self._send(400, json.dumps({"ok": False, "error": "bad request"}))
        ip = self.client_address[0]
        if path == "/api/live_pick":
            klass, limit = "live", 300          # turbo fires ~1/team in a burst; its own bucket
        elif path in ("/api/setup", "/api/settings", "/api/redraw", "/api/save_draw", "/api/export", "/api/import"):
            klass, limit = "strict", 10
        else:
            klass, limit = "norm", 60
        if not rate_ok((ip, klass), limit):
            return self._send(429, json.dumps({"ok": False, "error": "too many requests — slow down"}))
        if path not in ("/api/poll", "/api/status", "/api/live_pick"):
            log("POST", path, "from", ip)
        if path == "/api/setup":
            def _clean_name(x):
                s = "".join(c for c in str(x) if c.isprintable() and c not in "<>")
                return s.strip()[:40]
            players = [_clean_name(p) for p in body.get("players", []) if _clean_name(p)]
            if len(players) < 2:
                return self._send(400, json.dumps({"ok": False, "error": "need at least 2 players"}))
            if len(players) > 32:
                return self._send(400, json.dumps({"ok": False, "error": "max 32 players"}))
            if len(set(players)) != len(players):
                return self._send(400, json.dumps({"ok": False, "error": "player names must be unique"}))
            cfg = load_config()
            cfg.update({
                "players": players,
                "draw_mode": body.get("draw_mode", "weighted"),
                "max_per_player": (int(body["max_per_player"]) if body.get("max_per_player") else None),
                "leftover": body.get("leftover", "pool"),
                "t1_cap": body.get("t1_cap") or None,
                "scoring_mode": body.get("scoring_mode", "hybrid"),
                "poll_minutes": int(body.get("poll_minutes", 10)),
                "competition": body.get("competition", "WC"),
            })
            if body.get("token"):
                cfg["token"] = body["token"].strip()
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True,
                    "error": "Admin key required to change setup."}))
            with _lock:
                save_config(cfg)
                reset_draw()
            log("setup saved:", len(cfg.get("players", [])), "players, mode", cfg.get("draw_mode"))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/save_draw":
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True,
                    "error": "Admin key required to run the draw."}))
            cfg = load_config()
            body_players = [p.get("name") for p in (body.get("players") or [])
                            if isinstance(p, dict) and p.get("name")]
            if not cfg.get("players"):
                if body_players:                       # recover from the wheel payload so a real draw always saves
                    cfg["players"] = body_players
                    save_config(cfg)
                else:
                    return self._send(400, json.dumps({"ok": False, "error": "not configured"}))
            if draw_locked():
                return self._send(403, json.dumps({"ok": False, "error": "draw already locked"}))
            with _lock:
                json.dump(build_draw_result(body), open("draw_result.json", "w"), indent=2)
                ok, err = update_now(cfg)
            log("draw locked" if ok else "draw save FAILED:", err or "")
            if ok:
                tg_broadcast("🏆 The WC26 draw is locked — open the tracker to see your teams!")
            return self._send(200 if ok else 500, json.dumps({"ok": ok, "error": err}))
        if path == "/api/settings":
            cfg = load_config()
            if not cfg.get("players"):
                return self._send(400, json.dumps({"ok": False, "error": "not configured"}))
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True,
                    "error": "Enter the admin key to change settings."}))
            if "token" in body and body["token"].strip():
                cfg["token"] = body["token"].strip()
            if body.get("poll_minutes"):
                cfg["poll_minutes"] = int(body["poll_minutes"])
            if body.get("competition"):
                cfg["competition"] = str(body["competition"]).strip()[:8]
            if "telegram_token" in body:
                cfg["telegram_token"] = str(body["telegram_token"]).strip()
                _bot_user["name"] = None        # re-fetch username for the new bot
            with _lock:
                save_config(cfg)
                ok, err = update_now(cfg)
            log("settings updated: comp", cfg.get("competition"), "poll", cfg.get("poll_minutes"),
                "token" if cfg.get("token") else "no-token")
            return self._send(200 if ok else 500, json.dumps({"ok": ok, "error": err}))
        if path == "/api/export":
            if draw_locked() and not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True,
                    "error": "Enter the admin key to export."}))
            cfg = load_config()
            bundle = {"version": 1,
                      "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "config": {k: val for k, val in cfg.items() if k not in ("token", "admin_key")},
                      "draw_result": (json.load(open("draw_result.json")) if os.path.exists("draw_result.json") else None),
                      "results": (json.load(open("results.json")) if os.path.exists("results.json") else None)}
            return self._send(200, json.dumps(bundle))
        if path == "/api/import":
            if draw_locked() and not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True,
                    "error": "Enter the admin key to restore."}))
            b = body.get("bundle")
            if not isinstance(b, dict) or not isinstance(b.get("draw_result"), dict) \
                    or not b["draw_result"].get("players"):
                return self._send(400, json.dumps({"ok": False, "error": "not a valid backup file"}))
            cfg = load_config()
            if isinstance(b.get("config"), dict):
                for k in ("players", "draw_mode", "scoring_mode", "competition",
                          "poll_minutes", "leftover", "max_per_player", "t1_cap"):
                    if k in b["config"]:
                        cfg[k] = b["config"][k]
            with _lock:
                backup_draw()                       # snapshot current state before replacing it
                json.dump(b["draw_result"], open("draw_result.json", "w"), indent=2)
                if isinstance(b.get("results"), dict):
                    json.dump(b["results"], open("results.json", "w"), indent=2)
                elif not os.path.exists("results.json"):
                    _write_pretournament(cfg.get("competition", "WC"))
                save_config(cfg)
                try:                                 # rebuild the tracker from the restored data (no network needed)
                    scoring_mod.compute(out="tracker_data.json",
                                        default_mode=cfg.get("scoring_mode", "hybrid"))
                    ok, err = True, None
                except Exception as e:
                    ok, err = False, str(e)
                backup_data()
            log("data imported:", len(cfg.get("players", [])), "players, ok", ok, err or "")
            return self._send(200 if ok else 500, json.dumps({"ok": ok, "error": err}))
        if path == "/api/check_key":
            return self._send(200, json.dumps({"ok": key_ok(body)}))
        if path == "/api/telegram_test":
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            tg_broadcast("✅ WC26 test — alerts are working.")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/telegram_subscribe":      # OPEN: link the chat that sent /start <code>
            cfg = load_config()
            players = [(p if isinstance(p, str) else p.get("name", "")) for p in cfg.get("players", [])]
            code = str(body.get("code", "")).strip()
            if not code.startswith("p") or not code[1:].isdigit() or int(code[1:]) >= len(players):
                return self._send(400, json.dumps({"ok": False, "error": "bad code"}))
            name = players[int(code[1:])]
            tok = _tg_token()
            if not tok:
                return self._send(400, json.dumps({"ok": False, "error": "Alerts aren't set up yet."}))
            try:
                with urllib.request.urlopen("https://api.telegram.org/bot%s/getUpdates" % tok, timeout=8) as r:
                    upd = json.loads(r.read().decode())
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
            chat = None
            for u in reversed(upd.get("result", [])):     # newest first
                msg = u.get("message") or u.get("edited_message") or {}
                if str(msg.get("text", "")).strip() == "/start " + code:
                    chat = (msg.get("chat") or {}).get("id")
                    break
            if chat is None:
                return self._send(200, json.dumps({"ok": False,
                    "error": "Couldn't find your message. Tap the Telegram link, send /start, then try Link me again."}))
            subs = _load_subs(); lst = subs.setdefault(name, [])
            if chat not in lst:
                lst.append(chat); _save_subs(subs)
            tg_send(chat, "✅ You're set, %s! You'll get alerts about your teams." % name)
            return self._send(200, json.dumps({"ok": True, "name": name}))
        if path == "/api/rotate_key":
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            with _lock:
                cfg = load_config()
                newk = secrets.token_urlsafe(16)
                cfg["admin_key"] = newk
                save_config(cfg)
            log("admin key rotated")
            return self._send(200, json.dumps({"ok": True, "key": newk}))
        if path == "/api/poll":
            cfg = load_config()
            with _lock:
                ok, err = update_now(cfg)
            return self._send(200 if ok else 500, json.dumps({"ok": ok, "error": err}))
        if path == "/api/live_pick":
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            act = body.get("action")
            with _lock:
                st = live_load()
                if act == "order_start":
                    st = {"phase": "order", "active": True, "done": False, "order": [], "picks": [], "updated": None}
                elif act == "order_pick":
                    st["phase"] = "order"; st["active"] = True; st["done"] = False
                    pl = body.get("player")
                    if pl and pl not in st.get("order", []):
                        st.setdefault("order", []).append(pl)
                elif act == "reset":
                    st = {"phase": "teams", "active": True, "done": False, "order": body.get("order", []),
                          "picks": [], "updated": None}
                elif act == "pick":
                    st["phase"] = "teams"; st["active"] = True; st["done"] = False
                    st.setdefault("picks", []).append({"player": body.get("player"), "team": body.get("team"),
                                        "tier": body.get("tier"), "group": body.get("group")})
                elif act == "done":
                    st["done"] = True; st["active"] = False; st["phase"] = "done"
                live_save(st)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/redraw":
            if draw_locked() and not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True,
                    "error": "Enter the admin key to reset the locked draw."}))
            with _lock:
                reset_draw()
            log("draw reset (redraw)")
            return self._send(200, json.dumps({"ok": True}))
        return self._send(404, json.dumps({"ok": False, "error": "unknown route"}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("[warn] running as root is risky — create a normal user to run this server.")
    _key = ensure_admin_key()
    threading.Thread(target=poller, daemon=True).start()
    print(f"Sweepstake server on http://{HOST}:{PORT}  (Ctrl-C to stop)")
    print(f"Admin key (needed only to overwrite a finished draw): {_key}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
