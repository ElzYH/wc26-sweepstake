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
import random
import secrets
import shutil
import threading
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:                                              # optional — only needed for native Web Push (Path A)
    import base64
    from pywebpush import webpush, WebPushException
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    HAVE_WEBPUSH = True
except Exception:
    HAVE_WEBPUSH = False

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
          "teams.json", "tracker_data.json", "draw_result.json", "sw.js",
          "manifest.webmanifest", "icon.svg"}
_lock = threading.Lock()


def _atomic_write_json(path, obj, mode=None):
    """Write JSON via a temp file + rename, so a crash mid-write can't corrupt `path`
    (a half-written temp is discarded; the old file stays intact until the rename)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)          # atomic on the same filesystem


def load_config():
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG) as f:
                return json.load(f)
        except Exception as e:
            # never let a corrupt/partial config take the whole server down — stay up, degraded
            print("[warn] config unreadable (%s): %r — using empty config until fixed/restored" % (CONFIG, e))
            return {}
    return {}


def save_config(c):
    _atomic_write_json(CONFIG, c, mode=0o600)   # 600: token + admin key are owner-only


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
    try:
        _draw_state["gen"] += 1         # signal any running server reveal to stop
        _draw_state["running"] = False
    except Exception:
        pass
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
    _atomic_write_json(LIVE_FILE, state)


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


# ---------------- Discord webhook (Path B) — pure stdlib, group channel ----------------
def discord_send(text):
    cfg = load_config()
    url = (cfg.get("discord_webhook") or "").strip()
    if not url.startswith("https://"):
        return
    site = (cfg.get("site_url") or "").strip()
    if site.startswith("https://"):
        payload = {"embeds": [{"description": "%s\n[📊 Open the tracker](%s)" % (text[:1800], site), "color": 0x2ecc71}]}
    else:
        payload = {"content": text[:1900]}
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "WC26-Sweepstake/1.0 (+https://bbmsweepstake.co.uk)"})
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        log("discord send failed:", e)


# ---------------- Web Push (Path A) — needs pywebpush; guarded so missing lib is harmless ----------------
PUSH_FILE = "push_subs.json"          # {player: [{"sub": <subscription>, "prefs": {etype: bool}}, ...]}
EVENT_TYPES = ("goal", "kickoff", "flow", "knockout", "leader", "winner")


def ensure_vapid():
    """Generate a VAPID keypair once (only if the push lib is installed). Returns the public key (b64url)."""
    if not HAVE_WEBPUSH:
        return None
    cfg = load_config()
    if cfg.get("vapid_private") and cfg.get("vapid_public"):
        return cfg["vapid_public"]
    try:
        pk = ec.generate_private_key(ec.SECP256R1())
        priv = pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        pub = pk.public_key().public_bytes(serialization.Encoding.X962,
                                            serialization.PublicFormat.UncompressedPoint)
        pub_b64 = base64.urlsafe_b64encode(pub).rstrip(b"=").decode()
        cfg["vapid_private"], cfg["vapid_public"] = priv, pub_b64
        save_config(cfg)
        log("generated VAPID keys for web push")
        return pub_b64
    except Exception as e:
        log("vapid gen failed:", e)
        return None


def push_enabled():
    return bool(HAVE_WEBPUSH and load_config().get("vapid_public"))


def _load_push():
    try:
        with open(PUSH_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_push(d):
    _atomic_write_json(PUSH_FILE, d)


def _entry_sub(e):
    return e.get("sub", e) if isinstance(e, dict) else e


def _entry_wants(e, etype):
    prefs = (e.get("prefs") if isinstance(e, dict) else None) or {}
    return prefs.get(etype, True)         # default: everything on


def _entry_endpoint(e):
    return (_entry_sub(e) or {}).get("endpoint")


_VAPID_CACHE = {"pem": None, "obj": None}


def _vapid_key():
    """Return a py_vapid key object built from our stored PEM.

    pywebpush mis-parses a PEM *string*, so we load it with cryptography and hand
    over a Vapid object, which it accepts directly.
    """
    pem = load_config().get("vapid_private")
    if not pem:
        return None
    if _VAPID_CACHE["pem"] != pem or _VAPID_CACHE["obj"] is None:
        from py_vapid import Vapid01
        key = serialization.load_pem_private_key(pem.encode() if isinstance(pem, str) else pem, password=None)
        _VAPID_CACHE["pem"], _VAPID_CACHE["obj"] = pem, Vapid01(private_key=key)
    return _VAPID_CACHE["obj"]


def _webpush_one(sub, title, body):
    """Send one push. Returns True to keep the subscription, False if it's dead (expired)."""
    cfg = load_config()
    try:
        webpush(subscription_info=sub, data=json.dumps({"title": title, "body": body}),
                vapid_private_key=_vapid_key(),
                vapid_claims={"sub": cfg.get("vapid_sub", "mailto:admin@bbmsweepstake.co.uk")})
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            return False                  # subscription gone — prune it
        log("webpush failed:", code, e)
        return True
    except Exception as e:
        log("webpush error:", e)
        return True


def push_player(player, etype, title, body):
    if not push_enabled():
        return
    subs = _load_push()
    lst = subs.get(player, [])
    keep, changed = [], False
    for e in lst:
        if not _entry_wants(e, etype):    # this device opted out of this event type
            keep.append(e)
            continue
        if _webpush_one(_entry_sub(e), title, body):
            keep.append(e)
        else:
            changed = True
    if changed:
        subs[player] = keep
        _save_push(subs)


def push_broadcast(etype, title, body):
    if not push_enabled():
        return
    for player in list(_load_push().keys()):
        push_player(player, etype, title, body)


# ---------------- unified dispatch: both channels at once ----------------
def alert_player(player, etype, title, body, group_line):
    if player and player not in ("—", "-"):
        push_player(player, etype, title, body)
    discord_send(group_line)


def alert_all(etype, title, body, group_line):
    push_broadcast(etype, title, body)
    discord_send(group_line)


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
PLAYING = ("IN_PLAY", "LIVE")


def _final_result(d):
    """Return (champion_team, owner) once the FINAL is finished, else (None, None)."""
    for m in (d.get("fixtures") or []):
        if m.get("stage") == "FINAL" and m.get("status") == "FINISHED":
            hs, as_ = m.get("homeScore"), m.get("awayScore")
            w = m.get("winner")
            if hs is not None and as_ is not None and hs != as_:
                return (m.get("home"), m.get("homeOwner")) if hs > as_ else (m.get("away"), m.get("awayOwner"))
            if w == "HOME_TEAM":
                return m.get("home"), m.get("homeOwner")
            if w == "AWAY_TEAM":
                return m.get("away"), m.get("awayOwner")
    return None, None


def notify_changes(old):
    """Compare previous tracker snapshot to the new one and alert players (Web Push + Discord)."""
    new = _load_tracker()
    if not new or old is None:
        return                              # first compute / no data: nothing to compare
    if (new.get("stats") or {}).get("matches_played", 0) == 0:
        return

    def match_event(etype, recipients, group_line):
        # recipients: list of (owner, title, body); one Discord line for the whole match event
        for ow, ti, bo in recipients:
            if ow and ow not in ("—", "-"):
                push_player(ow, etype, ti, bo)
        discord_send(group_line)

    def own(o):
        return o if (o and o not in ("—", "-")) else "—"

    # overall (Both) leader change -> everyone
    try:
        ol = (old["leaderboards"]["hybrid"][0] or {}).get("name")
        nl = (new["leaderboards"]["hybrid"][0] or {}).get("name")
        if nl and ol and nl != ol:
            alert_all("leader", "New leader 📈", "%s now tops the table." % nl,
                      "📈 New leader: **%s** now tops the table." % nl)
    except Exception:
        pass
    # per-match transitions: kickoff, half-time, second half, goals
    try:
        of, nf = _fixture_status(old), _fixture_status(new)
        for key, nv in nf.items():
            h, a = key
            st, ho, ao, nhs, nas = nv
            ov = of.get(key)
            was = ov[0] if ov else None
            ho_ok = ho and ho not in ("—", "-")
            ao_ok = ao and ao not in ("—", "-")
            if st in LIVE_STATUSES and was not in LIVE_STATUSES:                      # kickoff
                match_event("kickoff",
                            [(ho, "%s vs %s" % (h, a), "Kicked off — your team %s is playing!" % h),
                             (ao, "%s vs %s" % (h, a), "Kicked off — your team %s is playing!" % a)],
                            "🔵 Kicked off — **%s** (%s) vs **%s** (%s)" % (h, own(ho), a, own(ao)))
            elif st == "PAUSED" and was in PLAYING:                                   # half-time
                sc = "%s %s–%s %s" % (h, nhs, nas, a) if None not in (nhs, nas) else "%s vs %s" % (h, a)
                match_event("flow",
                            [(ho, "Half-time ⏸️", sc), (ao, "Half-time ⏸️", sc)],
                            "⏸️ Half-time — %s" % sc)
            elif st in PLAYING and was == "PAUSED":                                   # second half
                match_event("flow",
                            [(ho, "Second half ▶️", "%s vs %s under way" % (h, a)),
                             (ao, "Second half ▶️", "%s vs %s under way" % (h, a))],
                            "▶️ Second half under way — %s vs %s" % (h, a))
            if ov is not None:                                                        # goals (any time score rises)
                ohs, oas = ov[3], ov[4]
                if None not in (nhs, nas, ohs, oas):
                    score = "%s %d–%d %s" % (h, nhs, nas, a)
                    if nhs > ohs and ho_ok:
                        alert_player(ho, "goal", "%s scored! ⚽" % h, score, "⚽ **%s** (%s) scored — %s" % (h, ho, score))
                    if nas > oas and ao_ok:
                        alert_player(ao, "goal", "%s scored! ⚽" % a, score, "⚽ **%s** (%s) scored — %s" % (a, ao, score))
    except Exception:
        pass
    # a player's team is knocked out
    try:
        oa, na = _alive_owners(old), _alive_owners(new)
        for t in oa:
            if oa[t][0] and t in na and not na[t][0]:
                owner = na[t][1]
                alert_player(owner, "knockout", "%s is out ❌" % t, "Check the leaderboard to see where you stand.",
                             "❌ **%s** (%s) is out." % (t, own(owner)))
    except Exception:
        pass
    # champion decided -> everyone, with the winner's standing in the active scoring mode
    try:
        oc, nc = _final_result(old), _final_result(new)
        if nc[0] and nc != oc:
            team, owner = nc
            mode = (load_config().get("scoring_mode") or "hybrid")
            mode = mode if mode in ("points", "survival", "hybrid") else "hybrid"
            board = (new.get("leaderboards") or {}).get(mode) or []
            entry = next((p for p in board if p.get("name") == owner), None)
            tail = (" — %s finishes on %s pts." % (owner, entry[mode])) if (entry and own(owner) != "—") else "."
            alert_all("winner", "🏆 Champions: %s" % team,
                      "%s won the World Cup%s" % (team, tail),
                      "🏆 **%s** (%s) are World Cup champions%s" % (team, own(owner), tail))
    except Exception:
        pass


def _owner_of(d, team):
    for p in (d.get("players") or []):
        for t in (p.get("teams") or []):
            if t.get("name") == team:
                return p.get("name")
    return "—"


def build_summary():
    """A short digest used by the in-app card and the Discord 'post summary' button."""
    d = _load_tracker()
    if not d:
        return ["No data yet — set up the draw first."]
    stats = d.get("stats") or {}
    mp = stats.get("matches_played", 0) or 0
    if not mp:
        return ["📊 WC26 Sweepstake", "Tournament hasn't kicked off yet — the summary fills in once games start (11 June)."]
    mode = (load_config().get("scoring_mode") or "hybrid")
    mode = mode if mode in ("points", "survival", "hybrid") else "hybrid"
    label = {"points": "pts", "survival": "survival", "hybrid": "pts"}[mode]
    lines = ["📊 WC26 Sweepstake — summary"]
    board = (d.get("leaderboards") or {}).get(mode) or []
    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(board[:3]):
        lines.append("%s %s — %s %s" % (medals[i], p.get("name", "?"), p.get(mode, 0), label))
    if stats.get("teams_remaining") is not None:
        lines.append("🛡️ Teams still in: %s" % stats["teams_remaining"])
    if stats.get("top_team"):
        lines.append("🔥 Top team: %s (%s) — %s goals" % (stats["top_team"], _owner_of(d, stats["top_team"]), stats.get("top_team_goals", 0)))
    if stats.get("top_scorer_player"):
        lines.append("⚽ Most goals: %s (%s)" % (stats["top_scorer_player"], stats.get("top_scorer_player_goals", 0)))
    lines.append("📅 Played: %s · ⚽ %s goals (%s/game)" % (mp, stats.get("goals", 0), stats.get("goals_per_match", 0)))
    if (stats.get("teams_remaining") or 0) <= 1:
        champ = next((t for t in (d.get("teams") or []) if t.get("status") == "alive"), None)
        if champ:
            lines.append("🏆 Champions: %s (%s)" % (champ.get("name"), _owner_of(d, champ.get("name"))))
    return lines


def _active_mode():
    m = (load_config().get("scoring_mode") or "hybrid")
    return m if m in ("points", "survival", "hybrid") else "hybrid"


def discord_command(name, opts):
    """Build a read-only reply for a slash command. No admin actions."""
    d = _load_tracker() or {}
    mode = _active_mode()
    label = "survival" if mode == "survival" else "pts"
    if name == "help":
        return ("**WC26 bot commands**\n"
                "/leaderboard - top of the table\n"
                "/summary - current standings digest\n"
                "/groups - all 12 group tables with owners\n"
                "/odds - who's most likely to win\n"
                "/stats - fun stats (top team, favourite, dark horse…)\n"
                "/fixtures - live and upcoming games\n"
                "/myteams <player> - that player's teams\n"
                "/players - everyone's team counts\n"
                "/team <name> - look up one team (owner, group, points)\n"
                "/help - this list")
    if name == "summary":
        return "\n".join(build_summary())
    if name == "leaderboard":
        board = (d.get("leaderboards") or {}).get(mode) or []
        if not board:
            return "No standings yet — the tournament hasn't started."
        rows = ["**Leaderboard** (%s)" % ("Both" if mode == "hybrid" else mode)]
        for i, p in enumerate(board[:10]):
            rows.append("%2d. %s — %s %s" % (i + 1, p.get("name"), p.get(mode, 0), label))
        return "\n".join(rows)
    if name == "odds":
        champ = d.get("champion") or []
        if not champ or (d.get("stats") or {}).get("matches_played", 0) == 0:
            return "Win odds appear once games start (pre-tournament odds come from team strength)."
        rows = ["**Win odds**"]
        for p in champ[:8]:
            rows.append("%s — %s%% (%s teams in)" % (p.get("name"), p.get("odds", 0), p.get("alive_teams", 0)))
        return "\n".join(rows)
    if name == "fixtures":
        fx = d.get("fixtures") or []
        live = [m for m in fx if m.get("status") in ("IN_PLAY", "PAUSED", "LIVE", "SUSPENDED")]
        upcoming = sorted([m for m in fx if m.get("status") in ("TIMED", "SCHEDULED")],
                          key=lambda m: m.get("utcDate") or "")[:6]
        out = []
        if live:
            out.append("**Live now**")
            for m in live:
                out.append("🔴 %s%s %s-%s %s" % (("%s' " % m["minute"]) if m.get("minute") else "",
                                                  m.get("home"), m.get("homeScore", 0), m.get("awayScore", 0), m.get("away")))
        if upcoming:
            out.append("**Next up**")
            for m in upcoming:
                t = (m.get("utcDate") or "")[5:16].replace("T", " ")
                out.append("- %s vs %s  %s UTC" % (m.get("home"), m.get("away"), t))
        return "\n".join(out) if out else "No fixtures available yet."
    if name == "myteams":
        who = str(opts.get("player", "")).strip().lower()
        pl = next((p for p in (d.get("players") or []) if (p.get("name", "").lower() == who)), None)
        if not pl:
            names = ", ".join(p.get("name", "") for p in (d.get("players") or []))
            return "No player called that. Players: %s" % (names or "-")
        body = ["**%s's teams**" % pl.get("name")]
        for t in (pl.get("teams") or []):
            body.append(("✅ " if t.get("status") == "alive" else "❌ ") + str(t.get("name")))
        return "\n".join(body)
    if name == "groups":
        groups = d.get("groups") or []
        if not groups:
            return "No group tables yet."
        out = ["**Groups**"]
        for g in groups:
            out.append("__Group %s__" % g.get("group"))
            for r in g.get("table", []):
                out.append("%d. %s — %s pts (%s)" % (r.get("position", 0), r.get("team"),
                                                     r.get("points", 0), r.get("owner", "—")))
        txt = "\n".join(out)
        return txt[:1900] + ("\n…(open the tracker for the full tables)" if len(txt) > 1900 else "")
    if name == "stats":
        s = d.get("stats") or {}
        if not s:
            return "No stats yet — they fill in once games are played."
        out = ["**Fun stats**", "Teams still in: %s" % s.get("teams_remaining", "—")]
        if s.get("top_team"):
            out.append("🔥 Top team: %s (%s) — %s goals" % (s["top_team"], _owner_of(d, s["top_team"]) or "—",
                                                            s.get("top_team_goals", 0)))
        if s.get("top_scorer_player"):
            out.append("⚽ Most goals: %s — %s" % (s["top_scorer_player"], s.get("top_scorer_player_goals", 0)))
        if s.get("favourite_team"):
            out.append("⭐ Favourite: %s (%s) — %s%% to win" % (s["favourite_team"], s.get("favourite_owner", "—"),
                                                               s.get("favourite_odds", 0)))
        if s.get("dark_horse"):
            out.append("🐎 Dark horse: %s (%s) — %s%%" % (s["dark_horse"], s.get("dark_horse_owner", "—"),
                                                          s.get("dark_horse_odds", 0)))
        if s.get("most_favourites_player"):
            out.append("👑 Most top-tier teams: %s (%s)" % (s["most_favourites_player"], s.get("most_favourites", 0)))
        out.append("📅 Played: %s · ⚽ %s goals (%s/game)" % (s.get("matches_played", 0), s.get("goals", 0),
                                                            s.get("goals_per_match", 0)))
        return "\n".join(out)
    if name == "players":
        pls = d.get("players") or []
        if not pls:
            return "No players yet — run the draw first."
        out = ["**Players**"]
        for p in sorted(pls, key=lambda p: -(p.get(mode, 0))):
            out.append("%s — %s %s · %s/%s teams in" % (p.get("name"), p.get(mode, 0), label,
                                                        p.get("alive_teams", 0), p.get("total_teams", 0)))
        return "\n".join(out)
    if name == "team":
        q = str(opts.get("name", "")).strip().lower()
        if not q:
            return "Give a team name, e.g. /team Brazil."
        for p in (d.get("players") or []):
            for t in (p.get("teams") or []):
                if str(t.get("name", "")).lower() == q:
                    alive = t.get("status") == "alive"
                    return ("**%s** — owned by %s\n"
                            "Group %s · Tier %s · %s pts · record %s\n"
                            "%s") % (t.get("name"), p.get("name"), t.get("group", "?"),
                                     t.get("tier", "?"), t.get("points", 0), t.get("record", "0-0-0"),
                                     "✅ still in" if alive else "❌ knocked out")
        return "No team called that in the draw. Try /groups to see the names."
    return "Unknown command."


def register_discord_commands():
    """Register the slash commands with Discord (guild = instant; global can take ~1h)."""
    cfg = load_config()
    app_id = (cfg.get("discord_app_id") or "").strip()
    token = (cfg.get("discord_bot_token") or "").strip()
    guild = (cfg.get("discord_guild_id") or "").strip()
    if not app_id or not token:
        return False, "Set the Application ID and Bot token first."
    cmds = [
        {"name": "help", "description": "List the available commands", "type": 1},
        {"name": "summary", "description": "Current sweepstake summary", "type": 1},
        {"name": "leaderboard", "description": "Top of the table", "type": 1},
        {"name": "groups", "description": "All 12 group tables with owners", "type": 1},
        {"name": "odds", "description": "Who's most likely to win", "type": 1},
        {"name": "stats", "description": "Fun stats: top team, favourite, dark horse", "type": 1},
        {"name": "fixtures", "description": "Live and upcoming games", "type": 1},
        {"name": "myteams", "description": "A player's teams", "type": 1,
         "options": [{"name": "player", "description": "Player name", "type": 3, "required": True}]},
        {"name": "players", "description": "Every player and how many teams are still in", "type": 1},
        {"name": "team", "description": "Look up a team's owner, group and points", "type": 1,
         "options": [{"name": "name", "description": "Team name, e.g. Brazil", "type": 3, "required": True}]},
    ]
    base = "https://discord.com/api/v10/applications/%s" % app_id
    url = (base + "/guilds/%s/commands" % guild) if guild else (base + "/commands")
    try:
        req = urllib.request.Request(url, data=json.dumps(cmds).encode(), method="PUT",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bot " + token,
                                              "User-Agent": "WC26-Sweepstake/1.0 (+https://bbmsweepstake.co.uk)"})
        urllib.request.urlopen(req, timeout=12)
        return True, None
    except Exception as e:
        return False, str(e)


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


def maybe_send_daily_digest(cfg):
    """Post the summary to Discord once per day at/after the configured UTC hour.
    Idempotent: a persisted last_digest_date means a restart can't double-post."""
    if not (cfg.get("digest_enabled") and cfg.get("discord_webhook") and draw_locked()):
        return
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if cfg.get("last_digest_date") == today:
        return
    try:
        hour = int(cfg.get("digest_hour", 9))
    except (TypeError, ValueError):
        hour = 9
    if time.gmtime().tm_hour < max(0, min(23, hour)):
        return
    lines = build_summary()
    if not lines:
        return
    discord_send("📰 **Daily summary**\n" + "\n".join(lines))
    cfg["last_digest_date"] = today
    save_config(cfg)
    log("daily digest sent for", today)


def poller():
    while True:
        cfg = load_config()
        mins = cfg.get("poll_minutes", 10)
        if cfg.get("players") and cfg.get("token") and os.path.exists("draw_result.json"):
            with _lock:
                ok, err = update_now(cfg)
            if not ok:
                print("[poller] update failed:", err)
        try:
            maybe_send_daily_digest(load_config())
        except Exception as e:
            print("[digest] error:", e)
        time.sleep(max(60, mins * 60))


def _team_brief(t):
    return {"name": t["name"], "tier": t["tier"], "group": t["group"],
            "composite": t.get("composite", 0), "confederation": t.get("confederation", "?")}


def compute_assignment(mode, players, t1_cap=None, leftover="pool", seed=None):
    """Return ({player: [team dicts]}, bonus_pool list). 'fair' is ported from the wheel."""
    teams = json.load(open("teams.json"))["teams"]
    n = len(players)
    per_player = len(teams) // n
    in_play_n = per_player * n
    rng = random.Random(seed)
    if mode == "fair":
        J = 0.5                                             # keep in sync with wheel.html computeFair
        pool = sorted(teams, key=lambda t: -(t.get("composite", 0) * (1 + (rng.random() * 2 - 1) * J)))[:in_play_n]
        assign = {p: [] for p in players}
        for pot in range(per_player):
            band = pool[pot * n:pot * n + n][:]
            rng.shuffle(band)                               # every team in the band equally likely
            seq = players if pot % 2 == 0 else players[::-1]
            for i, p in enumerate(seq):
                assign[p].append(_team_brief(band[i]))
        return assign, []
    d = draw_mod.Draw(mode=("weighted" if str(mode).startswith("weighted") else "snake"),
                      leftover_policy=leftover, t1_cap=t1_cap, seed=seed)
    d.add_players(players)
    d.add_all_teams("teams.json")
    d.sort_teams_to_players()
    assign = {p.name: [{"name": t.name, "tier": t.tier, "group": t.group,
                        "composite": t.composite, "confederation": t.confederation} for t in p.teams]
              for p in d.players}
    bonus = [{"name": t.name, "tier": t.tier, "group": t.group,
              "composite": t.composite, "confederation": t.confederation} for t in d.bonus_pool]
    return assign, bonus


_draw_state = {"gen": 0, "running": False}


def run_auto_draw(gen, players, mode, t1_cap, leftover, order_gap=1.0, pick_gap=1.4):
    """Reveal the draw pick-by-pick on the server, writing live_draw.json as it goes,
    so /watch (and the host) follow along even with every tab closed. Then lock + recompute."""
    def stale():
        return _draw_state["gen"] != gen
    try:
        assign, bonus = compute_assignment(mode, players, t1_cap, leftover)
        per_player = max(len(v) for v in assign.values()) if assign else 0
        order = players[:]
        random.shuffle(order)
        with _lock:
            live_save({"phase": "order", "active": True, "done": False, "server": True,
                       "order": [], "picks": [], "updated": None})
        revealed = []
        for p in order:
            time.sleep(order_gap)
            if stale():
                return
            revealed.append(p)
            with _lock:
                st = live_load(); st["order"] = revealed[:]; st["phase"] = "order"; st["active"] = True; live_save(st)
        time.sleep(order_gap)
        if stale():
            return
        with _lock:
            st = live_load(); st["phase"] = "teams"; st["order"] = order[:]; st["picks"] = []; live_save(st)
        picks = []
        for idx in range(per_player):
            seq = order if idx % 2 == 0 else order[::-1]    # snake reveal, like a draft
            for p in seq:
                if idx >= len(assign.get(p, [])):
                    continue
                t = assign[p][idx]
                time.sleep(pick_gap)
                if stale():
                    return
                picks.append({"player": p, "team": t["name"], "tier": t["tier"], "group": t["group"]})
                with _lock:
                    st = live_load(); st["picks"] = picks[:]; st["phase"] = "teams"; st["active"] = True; live_save(st)
        if stale():
            return
        payload = {"mode": mode, "leftover": leftover,
                   "players": [{"name": p, "teams": [t["name"] for t in assign[p]]} for p in order],
                   "bonus_pool": [t["name"] for t in bonus]}
        with _lock:
            json.dump(build_draw_result(payload), open("draw_result.json", "w"), indent=2)
            cfg = load_config()
            ok, err = update_now(cfg)
            st = live_load(); st["done"] = True; st["active"] = False; st["phase"] = "done"; live_save(st)
        log("server draw complete" if ok else "server draw lock FAILED:", err or "")
        if ok:
            discord_send("🏆 The WC26 draw is locked — open the tracker to see your teams!")
    except Exception as e:
        log("auto-draw error:", e)
    finally:
        if _draw_state["gen"] == gen:
            _draw_state["running"] = False


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
        if name.endswith(".html"):
            ctype = "text/html"
        elif name.endswith(".js"):
            ctype = "text/javascript"
        elif name.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        elif name.endswith(".svg"):
            ctype = "image/svg+xml"
        else:
            ctype = "application/json"
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
        if path == "/api/summary":
            return self._send(200, json.dumps({"ok": True, "lines": build_summary()}))
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
                "has_telegram": bool(cfg.get("telegram_token")),
                "push_enabled": push_enabled(),
                "vapid_public": (cfg.get("vapid_public") if push_enabled() else None),
                "discord": bool(cfg.get("discord_webhook")),
                "has_invite": bool(cfg.get("discord_invite")),
                "invite": cfg.get("discord_invite", ""),
                "bot_ready": bool(cfg.get("discord_pubkey") and cfg.get("discord_app_id")),
                "digest_enabled": bool(cfg.get("digest_enabled")),
                "digest_hour": cfg.get("digest_hour", 9),
                "site_url": cfg.get("site_url", "")}))
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

    def _discord_interactions(self, raw):
        """Discord slash-command endpoint: verify Ed25519 signature, then reply (read-only)."""
        pub = (load_config().get("discord_pubkey") or "").strip()
        sig = self.headers.get("X-Signature-Ed25519", "")
        ts = self.headers.get("X-Signature-Timestamp", "")
        if not (pub and sig and ts):
            return self._send(401, "missing signature", "text/plain")
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(bytes.fromhex(sig), ts.encode() + raw)
        except Exception:
            return self._send(401, "bad signature", "text/plain")
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return self._send(400, "bad json", "text/plain")
        if body.get("type") == 1:                          # PING -> PONG (endpoint validation)
            return self._send(200, json.dumps({"type": 1}))
        if body.get("type") == 2:                          # APPLICATION_COMMAND
            data = body.get("data") or {}
            opts = {o.get("name"): o.get("value") for o in (data.get("options") or [])}
            try:
                content = discord_command(data.get("name"), opts)
            except Exception as e:
                log("discord command error:", e)
                content = "Something went wrong building that."
            return self._send(200, json.dumps({"type": 4, "data": {"content": (content or "—")[:1900]}}))
        return self._send(200, json.dumps({"type": 4, "data": {"content": "Unsupported interaction."}}))

    def _do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        if length > 100_000:
            return self._send(413, json.dumps({"ok": False, "error": "request too large"}))
        raw = self.rfile.read(length) if length else b""
        if path == "/api/discord_interactions":            # verified on the RAW bytes, before any parsing
            return self._discord_interactions(raw)
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return self._send(400, json.dumps({"ok": False, "error": "bad JSON"}))
        if not isinstance(body, dict):
            return self._send(400, json.dumps({"ok": False, "error": "bad request"}))
        ip = self.client_address[0]
        if path == "/api/live_pick":
            klass, limit = "live", 300          # turbo fires ~1/team in a burst; its own bucket
        elif path in ("/api/setup", "/api/settings", "/api/redraw", "/api/save_draw", "/api/export", "/api/import", "/api/discord_invite"):
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
        if path == "/api/start_draw":
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True,
                    "error": "Admin key required to run the draw."}))
            if draw_locked():
                return self._send(403, json.dumps({"ok": False, "error": "draw already locked"}))
            cfg = load_config()
            players = cfg.get("players") or [p.get("name") for p in (body.get("players") or [])
                                             if isinstance(p, dict) and p.get("name")]
            players = [p for p in players if p]
            if len(players) < 2:
                return self._send(400, json.dumps({"ok": False, "error": "need at least 2 players"}))
            if not cfg.get("players"):
                cfg["players"] = players
                save_config(cfg)
            if _draw_state["running"]:
                return self._send(400, json.dumps({"ok": False, "error": "a draw is already running"}))
            mode = body.get("mode") or cfg.get("draw_mode") or "fair"
            _draw_state["gen"] += 1
            _draw_state["running"] = True
            gen = _draw_state["gen"]
            threading.Thread(target=run_auto_draw,
                             args=(gen, players, mode, cfg.get("t1_cap"), cfg.get("leftover", "pool")),
                             daemon=True).start()
            log("server draw started:", mode, len(players), "players")
            return self._send(200, json.dumps({"ok": True}))
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
            if "discord_webhook" in body:
                w = str(body["discord_webhook"]).strip()
                cfg["discord_webhook"] = w if (w == "" or w.startswith("https://")) else cfg.get("discord_webhook", "")
            if "discord_invite" in body:
                inv = str(body["discord_invite"]).strip()
                cfg["discord_invite"] = inv if (inv == "" or inv.startswith("https://")) else cfg.get("discord_invite", "")
            if "join_code" in body:
                cfg["join_code"] = str(body["join_code"]).strip()[:60]
            if "site_url" in body:
                s = str(body["site_url"]).strip().rstrip("/")
                cfg["site_url"] = s if (s == "" or s.startswith("https://")) else cfg.get("site_url", "")
            if "digest_enabled" in body:
                cfg["digest_enabled"] = bool(body["digest_enabled"])
            if "digest_hour" in body:
                try:
                    cfg["digest_hour"] = max(0, min(23, int(body["digest_hour"])))
                except (TypeError, ValueError):
                    pass
            for f in ("discord_app_id", "discord_guild_id", "discord_pubkey", "discord_bot_token"):
                if f in body:
                    cfg[f] = str(body[f]).strip()[:120]
            if body.get("vapid_sub"):
                cfg["vapid_sub"] = str(body["vapid_sub"]).strip()[:120]
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
        if path == "/api/discord_invite":          # OPEN: anyone on the site can get the invite link
            invite = (load_config().get("discord_invite") or "").strip()
            if not invite:
                return self._send(404, json.dumps({"ok": False, "error": "no invite set"}))
            return self._send(200, json.dumps({"ok": True, "invite": invite}))
        if path == "/api/check_key":
            return self._send(200, json.dumps({"ok": key_ok(body)}))
        if path == "/api/discord_test":
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            if not (load_config().get("discord_webhook") or "").startswith("https://"):
                return self._send(400, json.dumps({"ok": False, "error": "Add a Discord webhook URL first."}))
            discord_send("✅ WC26 test — Discord alerts are working.")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/discord_demo":
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            if not (load_config().get("discord_webhook") or "").startswith("https://"):
                return self._send(400, json.dumps({"ok": False, "error": "Add a Discord webhook URL first."}))
            for line in ["🔵 Kicked off — **Brazil** (demo) vs **Spain**",
                         "⚽ **Brazil** (demo) scored — Brazil 1–0 Spain",
                         "⚽ **Brazil** (demo) scored — Brazil 2–0 Spain",
                         "❌ **Spain** (demo) is out.",
                         "🏆 Demo over — that's what live alerts look like."]:
                discord_send(line)
                time.sleep(0.5)             # stay under Discord's webhook burst limit
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/register_commands":
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            ok, err = register_discord_commands()
            return self._send(200 if ok else 400, json.dumps({"ok": ok, "error": err}))
        if path == "/api/discord_summary":
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            if not (load_config().get("discord_webhook") or "").startswith("https://"):
                return self._send(400, json.dumps({"ok": False, "error": "Add a Discord webhook URL first."}))
            discord_send("\n".join(build_summary()))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/push_subscribe":          # OPEN: a player subscribes this device to push
            cfg = load_config()
            players = [(p if isinstance(p, str) else p.get("name", "")) for p in cfg.get("players", [])]
            player = str(body.get("player", "")).strip()
            sub = body.get("subscription")
            if player not in players or not isinstance(sub, dict) or not sub.get("endpoint"):
                return self._send(400, json.dumps({"ok": False, "error": "bad subscription"}))
            prefs = body.get("prefs") if isinstance(body.get("prefs"), dict) else {}
            prefs = {k: bool(prefs.get(k, True)) for k in EVENT_TYPES}
            ep = sub["endpoint"]
            with _lock:
                subs = _load_push()
                lst = subs.setdefault(player, [])
                found = False
                for e in lst:
                    if _entry_endpoint(e) == ep:
                        e["sub"], e["prefs"] = sub, prefs   # upsert (also updates choices)
                        found = True
                if not found:
                    lst.append({"sub": sub, "prefs": prefs})
                for other in list(subs.keys()):            # an endpoint belongs to one player only
                    if other != player:
                        subs[other] = [e for e in subs[other] if _entry_endpoint(e) != ep]
                _save_push(subs)
            log("push subscribe:", player)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/push_prefs":               # OPEN: update which events this device wants
            ep = str(body.get("endpoint", "")).strip()
            prefs = body.get("prefs") if isinstance(body.get("prefs"), dict) else {}
            prefs = {k: bool(prefs.get(k, True)) for k in EVENT_TYPES}
            if ep:
                with _lock:
                    subs = _load_push()
                    for k in list(subs.keys()):
                        for e in subs[k]:
                            if _entry_endpoint(e) == ep:
                                e["prefs"] = prefs
                    _save_push(subs)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/push_test":               # OPEN: server sends a real push to this player's devices
            player = str(body.get("player", "")).strip()
            if not push_enabled():
                return self._send(400, json.dumps({"ok": False, "error": "push not enabled"}))
            lst = _load_push().get(player)
            if not lst:
                return self._send(400, json.dumps({"ok": False, "error": "no devices subscribed yet"}))
            for e in lst:                          # a test ignores per-event choices
                _webpush_one(_entry_sub(e), "WC26 Sweepstake", "✅ Push alerts are working, %s!" % player)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/push_unsubscribe":        # OPEN: remove this device
            ep = str(body.get("endpoint", "")).strip()
            if ep:
                with _lock:
                    subs = _load_push()
                    for k in list(subs.keys()):
                        subs[k] = [e for e in subs[k] if _entry_endpoint(e) != ep]
                    _save_push(subs)
            return self._send(200, json.dumps({"ok": True}))
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
            if not key_ok(body):                       # always key-gated: nobody can wipe/interrupt a draw without it
                return self._send(403, json.dumps({"ok": False, "need_key": True,
                    "error": "Enter the admin key to reset the draw."}))
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
    if HAVE_WEBPUSH:
        ensure_vapid()                    # one-time keypair so native Web Push (Path A) can work
    threading.Thread(target=poller, daemon=True).start()
    print(f"Sweepstake server on http://{HOST}:{PORT}  (Ctrl-C to stop)")
    print(f"Admin key (needed only to overwrite a finished draw): {_key}")
    print("Web Push: " + ("ENABLED" if push_enabled() else "off (pip install pywebpush to turn on)"))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
