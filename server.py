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
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(os.environ.get("WC26_DATA", APP_DIR))
_hits = defaultdict(list)
_rl_lock = threading.Lock()
def rate_ok(ip, limit, window=60):
    now = time.time()
    with _rl_lock:
        q = _hits[ip]
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
STATIC = {"tracker.html", "wheel.html", "setup.html", "me.html",
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
    for f in ("draw_result.json", "tracker_data.json", "results.json"):
        if os.path.exists(f):
            os.remove(f)


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


def update_now(cfg):
    """Fetch results (if a token is set) and recompute the tracker."""
    if not os.path.exists("draw_result.json"):
        return True, None
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
        backup_data()
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
        if path == "/api/draw_result": return self._file("draw_result.json")
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
                "poll_minutes": cfg.get("poll_minutes", 10)}))
        return self._file(path.lstrip("/"))

    def do_POST(self):
        try:
            self._do_POST()
        except Exception:
            import traceback; traceback.print_exc()
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
        strict = path in ("/api/check_key", "/api/setup", "/api/settings", "/api/redraw", "/api/save_draw", "/api/export", "/api/import")
        if not rate_ok(ip, 10 if strict else 60):
            return self._send(429, json.dumps({"ok": False, "error": "too many requests — slow down"}))
        if path == "/api/setup":
            players = [str(p).strip()[:40] for p in body.get("players", []) if str(p).strip()]
            if len(players) < 2:
                return self._send(400, json.dumps({"ok": False, "error": "need at least 2 players"}))
            if len(players) > 32:
                return self._send(400, json.dumps({"ok": False, "error": "max 32 players"}))
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
            return self._send(200 if ok else 500, json.dumps({"ok": ok, "error": err}))
        if path == "/api/settings":
            cfg = load_config()
            if not cfg.get("players"):
                return self._send(400, json.dumps({"ok": False, "error": "not configured"}))
            if draw_locked() and not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True,
                    "error": "Enter the admin key to change settings."}))
            if "token" in body and body["token"].strip():
                cfg["token"] = body["token"].strip()
            if body.get("poll_minutes"):
                cfg["poll_minutes"] = int(body["poll_minutes"])
            if body.get("competition"):
                cfg["competition"] = str(body["competition"]).strip()[:8]
            with _lock:
                save_config(cfg)
                ok, err = update_now(cfg)
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
            return self._send(200 if ok else 500, json.dumps({"ok": ok, "error": err}))
        if path == "/api/check_key":
            return self._send(200, json.dumps({"ok": key_ok(body)}))
        if path == "/api/poll":
            cfg = load_config()
            with _lock:
                ok, err = update_now(cfg)
            return self._send(200 if ok else 500, json.dumps({"ok": ok, "error": err}))
        if path == "/api/redraw":
            if draw_locked() and not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True,
                    "error": "Enter the admin key to reset the locked draw."}))
            with _lock:
                reset_draw()
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
