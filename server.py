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
import re
import os
import random
import secrets
import hmac
import hashlib
import shutil
import threading
import time
import math
import calendar
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


# ---- Ed25519 (Discord interaction signatures) — pure-stdlib fallback so the server needs NO third-party crypto ----
_ED_Q = 2**255 - 19
_ED_L = 2**252 + 27742317777372353535851937790883648493
def _ed_inv(x): return pow(x, _ED_Q - 2, _ED_Q)
_ED_D = (-121665 * _ed_inv(121666)) % _ED_Q
_ED_I = pow(2, (_ED_Q - 1) // 4, _ED_Q)
def _ed_xrecover(y):
    xx = (y * y - 1) * _ed_inv(_ED_D * y * y + 1)
    x = pow(xx, (_ED_Q + 3) // 8, _ED_Q)
    if (x * x - xx) % _ED_Q != 0: x = (x * _ED_I) % _ED_Q
    if x % 2 != 0: x = _ED_Q - x
    return x
_ED_BY = (4 * _ed_inv(5)) % _ED_Q
_ED_B = (_ed_xrecover(_ED_BY) % _ED_Q, _ED_BY % _ED_Q)
def _ed_add(P, Q):
    x1, y1 = P; x2, y2 = Q
    dm = _ED_D * x1 * x2 * y1 * y2
    return (((x1 * y2 + x2 * y1) * _ed_inv(1 + dm)) % _ED_Q,
            ((y1 * y2 + x1 * x2) * _ed_inv(1 - dm)) % _ED_Q)
def _ed_mul(P, e):
    Q = (0, 1)
    while e > 0:
        if e & 1: Q = _ed_add(Q, P)
        P = _ed_add(P, P); e >>= 1
    return Q
def _ed_oncurve(P):
    x, y = P
    return (-x * x + y * y - 1 - _ED_D * x * x * y * y) % _ED_Q == 0
def _ed_decpoint(s):
    n = int.from_bytes(s, "little"); y = n & ((1 << 255) - 1); sign = (n >> 255) & 1
    x = _ed_xrecover(y)
    if (x & 1) != sign: x = _ED_Q - x
    P = (x, y)
    if not _ed_oncurve(P): raise ValueError("point off curve")
    return P
def _ed25519_verify_pure(public, sig, message):
    """Pure-Python Ed25519 verify (RFC 8032). True iff valid. Only used when 'cryptography' isn't installed."""
    if len(sig) != 64 or len(public) != 32:
        return False
    try:
        S = int.from_bytes(sig[32:], "little")
        if S >= _ED_L:
            return False
        R = _ed_decpoint(sig[:32]); A = _ed_decpoint(public)
        h = int.from_bytes(hashlib.sha512(sig[:32] + public + message).digest(), "little") % _ED_L
        return _ed_mul(_ED_B, S) == _ed_add(R, _ed_mul(A, h))
    except Exception:
        return False
def _verify_ed25519(pub_hex, sig_hex, message):
    """True iff sig_hex is a valid Ed25519 signature of `message` under public key pub_hex. Prefers the
    'cryptography' library if installed (fast); otherwise falls back to the pure-stdlib verifier above —
    so Discord slash commands verify correctly on a plain Python install with no extra packages."""
    try:
        pub = bytes.fromhex(pub_hex or "")
        sig = bytes.fromhex(sig_hex or "")
    except (ValueError, TypeError):
        return False
    if len(pub) != 32 or len(sig) != 64:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, message)
            return True
        except Exception:
            return False
    except ImportError:
        return _ed25519_verify_pure(pub, sig, message)


import draw as draw_mod
import scoring as scoring_mod

CONFIG = os.environ.get("WC26_CONFIG", "config.json")
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")   # set HOST=127.0.0.1 when behind a reverse proxy
STATIC = {"tracker.html", "wheel.html", "setup.html", "me.html", "watch.html",
          "teams.json", "tracker_data.json", "draw_result.json", "sw.js",
          "manifest.webmanifest", "icon.svg"}
_lock = threading.RLock()
NEUTRAL_COMPOSITE = 34.0   # an unknown / unmatched team is priced ~mid-table, never as exploitable free money
def _comp(teams, name):
    """A team's composite for pricing, with a safe fallback. A name that isn't in teams.json (e.g. a feed
    spelling we haven't aliased yet) must NOT collapse to 0 — that would price its opponent at ~98%. Fall
    back to a neutral mid-table strength so a stray name can never become a value bet against the house."""
    t = teams.get(name)
    if isinstance(t, dict):
        c = t.get("composite", NEUTRAL_COMPOSITE)
        if isinstance(c, (int, float)) and c > 0:
            return c
    return NEUTRAL_COMPOSITE


# ---- per-instance odds calibration (gitignored overrides; NEVER mutates the tracked teams.json) ----
CALIBRATION_FILE = "calibration.json"
GOALS_BASE_MIN, GOALS_BASE_MAX = 2.0, 3.2   # the goals knob may only live in a sane band, whatever calibration says


def _load_calibration():
    """The per-instance calibration overlay: {'composites': {name: val}, 'goals_base': float, ...}. Missing or
    corrupt -> {} (the app falls straight back to the base teams.json / wager defaults). Never raises."""
    try:
        d = _load_json_resilient(CALIBRATION_FILE, dict, validate=lambda x: isinstance(x, dict))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_teams():
    """The single source of team records for PRICING + display. Reads teams.json, then overlays any calibrated
    composite from calibration.json. A junk override (NaN / out-of-band / non-numeric) is ignored, so a bad
    calibration file can never poison the board — it just falls back to the base strength. Never raises."""
    try:
        teams = json.load(open("teams.json"))["teams"]
    except Exception:
        return []
    co = (_load_calibration().get("composites") or {})
    if isinstance(co, dict) and co:
        for t in teams:
            v = co.get(t.get("name"))
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v == v and 0 < v <= 105:                 # finite, in-band -> apply; else keep base composite
                t["composite"] = v
    return teams


def _calibrated_goals_base():
    """The calibrated goals base if present + sane, else the wager default. Always returns a value in-band."""
    base = getattr(wager_mod, "GOALS_BASE", 2.6) if wager_mod else 2.6
    g = _load_calibration().get("goals_base")
    try:
        g = float(g)
        if g == g and GOALS_BASE_MIN <= g <= GOALS_BASE_MAX:
            return g
    except (TypeError, ValueError):
        pass
    return base


def _apply_goals_base():
    """Push the calibrated goals base into the wager engine (expected_goals reads GOALS_BASE at call time).
    Called at startup and after each calibration write. No-op/sane if anything is off. Never raises."""
    if wager_mod is None:
        return
    try:
        g = _load_calibration().get("goals_base")
        g = float(g)
        if g == g and GOALS_BASE_MIN <= g <= GOALS_BASE_MAX:
            wager_mod.GOALS_BASE = g
    except (TypeError, ValueError, AttributeError):
        pass


_last_manual_poll = [0.0]
MANUAL_POLL_MIN_INTERVAL = 25.0   # seconds; cap manual /api/poll upstream fetches so spamming can't burn the API quota

# ---- lightweight access log (admin-only): who's opening the site, in-memory, capped ----
import collections
_access = collections.deque(maxlen=600)         # most recent page views
_visitors = {}                                  # ip -> {hits, first, last, ua, paths}
_access_lock = threading.Lock()
RECORD_PATHS = {"/setup", "/tracker", "/wheel", "/me", "/watch"}   # real page views, not API polls


def _client_ip(handler):
    xff = handler.headers.get("X-Forwarded-For", "")            # behind Caddy the socket IP is the proxy's
    if xff:
        return xff.split(",")[0].strip()[:45]
    return handler.client_address[0]


def record_access(ip, path, ua):
    ts = time.time()
    with _access_lock:
        _access.append({"t": ts, "ip": ip, "p": path, "ua": (ua or "")[:160]})
        v = _visitors.get(ip)
        if v is None:
            if len(_visitors) > 1500:                          # soft cap: evict least-recently-seen
                _visitors.pop(min(_visitors, key=lambda k: _visitors[k]["last"]), None)
            v = _visitors.setdefault(ip, {"hits": 0, "first": ts, "last": ts, "ua": "", "paths": {}})
        v["hits"] += 1
        v["last"] = ts
        v["ua"] = (ua or "")[:160]
        v["paths"][path] = v["paths"].get(path, 0) + 1


def access_summary():
    with _access_lock:
        recent = list(_access)[-60:][::-1]
        day_ago = time.time() - 86400
        today = {e["ip"] for e in _access if e["t"] >= day_ago}
        visitors = sorted(
            ({"ip": ip, "hits": v["hits"], "first": v["first"], "last": v["last"], "ua": v["ua"],
              "top": (max(v["paths"], key=v["paths"].get) if v["paths"] else "")}
             for ip, v in _visitors.items()),
            key=lambda x: -x["last"])[:50]
        total = sum(v["hits"] for v in _visitors.values())
    return {"unique": len(_visitors), "today_unique": len(today), "total_views": total,
            "visitors": visitors, "recent": recent}


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


def _load_json_resilient(path, default, validate=None):
    """Load JSON, but if the live file is missing or corrupt, fall back to the last-good backup
    before giving up. This means a single bad write (crash mid-save, disk hiccup) can never make
    real data — bets, claims, links — silently vanish; the worst case is rolling back a few minutes."""
    base = os.path.basename(path)
    for candidate in (path, os.path.join("backups", "last_good", base), os.path.join("backups", base)):
        try:
            with open(candidate) as f:
                d = json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            print("[warn] %s unreadable (%r) — trying a backup" % (candidate, e))
            continue
        if validate is None or validate(d):
            if candidate != path:
                print("[recover] %s was unusable — restored from %s" % (path, candidate))
            return d
    return default() if callable(default) else default


def load_config():
    return _load_json_resilient(CONFIG, dict, validate=lambda d: isinstance(d, dict))


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
        for f in ("draw_result.json", "results.json", "tracker_data.json", "wagers.json"):
            if os.path.exists(f):
                shutil.copy2(f, os.path.join("backups/last_good", f))
    except OSError:
        pass


_LAST_SNAPSHOT = [0.0]
def backup_snapshot(every_seconds=6 * 3600, keep=28):
    """Every ~6 hours, keep a full timestamped copy of the data in backups/snapshots/<stamp>/.
    Unlike last_good (overwritten each poll), these accumulate, so there's always a rollback
    history to a known-good point. Keeps the most recent `keep` (~1 week at 6h). Best-effort."""
    now = time.time()
    root = os.path.join("backups", "snapshots")
    if _LAST_SNAPSHOT[0] == 0.0:                       # on first run, seed from the newest existing snapshot so a restart doesn't over-snapshot
        try:
            existing = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
            if existing:
                _LAST_SNAPSHOT[0] = os.path.getmtime(os.path.join(root, existing[-1]))
        except OSError:
            pass
    if now - _LAST_SNAPSHOT[0] < every_seconds:
        return
    try:
        dst = os.path.join(root, time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)))
        os.makedirs(dst, exist_ok=True)
        for f in ("draw_result.json", "results.json", "tracker_data.json", "wagers.json", CONFIG):
            if os.path.exists(f):
                try:
                    shutil.copy2(f, os.path.join(dst, os.path.basename(f)))
                except OSError:
                    pass
        snaps = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
        for old in snaps[:-keep]:
            shutil.rmtree(os.path.join(root, old), ignore_errors=True)
        _LAST_SNAPSHOT[0] = now
        log("snapshot backup written:", dst)
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
    teams = {t["name"]: t for t in load_teams()}
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
        link = site.rstrip("/") + "/tracker"          # site_url is the base (used for OAuth); the tracker lives at /tracker
        payload = {"embeds": [{"description": "%s\n[📊 Open the tracker](%s)" % (text[:1800], link), "color": 0x2ecc71}]}
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


def discord_send_lines(lines):
    """Post a long list of lines as several Discord messages, each under the size limit."""
    buf, size = [], 0
    for ln in lines:
        if buf and size + len(ln) + 1 > 1700:
            discord_send("\n".join(buf)); buf, size = [], 0
        buf.append(ln); size += len(ln) + 1
    if buf:
        discord_send("\n".join(buf))


def _discord_subs():
    """{discord_user_id: player_name} — who opted into personal pings via /notifyme."""
    s = load_config().get("discord_subs")
    return s if isinstance(s, dict) else {}


def _mention_uids(uids, text):
    """@mention specific Discord user-ids in the channel. Shared by discord_mention and the DM fallback."""
    try:
        uids = [str(u) for u in uids][:25]
        if not uids:
            return
        url = (load_config().get("discord_webhook") or "").strip()
        if not url.startswith("https://"):
            return
        mention = " ".join("<@%s>" % u for u in uids)
        payload = {"content": ("%s %s" % (mention, text))[:1900], "allowed_mentions": {"users": uids}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "User-Agent": "WC26-Sweepstake/1.0 (+https://bbmsweepstake.co.uk)"})
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        log("discord mention failed:", e)


def discord_mention(player, text):
    """Ping the Discord users who subscribed to this player. No-op if nobody subscribed / no webhook."""
    if not player or player in ("—", "-"):
        return
    _mention_uids([u for u, pl in _discord_subs().items() if pl == player], text)


def _dm_optout():
    """Discord accounts that turned personal DMs OFF (overrides default-on). Set of uid strings."""
    s = load_config().get("discord_dm_off")
    return set(str(u) for u in s) if isinstance(s, list) else set()


def _dm_mutes():
    """Per-game DM mutes: {uid: [matchId, ...]} — games this account doesn't want pinged about."""
    m = load_config().get("discord_mutes")
    return m if isinstance(m, dict) else {}


def _uids_for_player(player):
    """Every Discord account known to be this player — linked for betting (Connect Discord) OR opted into
    alerts (/notifyme). Personal alerts are DEFAULT-ON for all of these; opting out is explicit."""
    cfg = load_config()
    links = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
    subs = cfg.get("discord_subs") if isinstance(cfg.get("discord_subs"), dict) else {}
    seen, out = set(), []
    for u in [u for u, pl in links.items() if pl == player] + [u for u, pl in subs.items() if pl == player]:
        u = str(u)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _bot_dm_player(player, text, match_id=None):
    """DM every Discord account known to be this player (default-on: betting-linked or /notifyme'd), unless they
    turned DMs off or muted this specific game. If a DM can't be delivered AND the user explicitly opted in via
    /notifyme, fall back to a channel @mention so an opt-in alert is never silently lost (betting-link-only users
    are skipped silently, so the channel never gets noisier just because more people connected). Best-effort."""
    try:
        if not player or player in ("—", "-"):
            return 0
        optout, mutes = _dm_optout(), _dm_mutes()
        cfg = load_config()
        subs = cfg.get("discord_subs") if isinstance(cfg.get("discord_subs"), dict) else {}
        sub_uids = set(str(u) for u, pl in subs.items() if pl == player)   # explicit /notifyme accounts
        uids = [u for u in _uids_for_player(player)[:25]
                if u not in optout and not (match_id and match_id in (mutes.get(u) or []))]
        if not uids:
            return 0
        sent, failed = 0, []
        for u in uids:
            ok, _ = _bot_dm(u, text)
            if ok:
                sent += 1
            elif u in sub_uids:
                failed.append(u)        # only opt-in users get the @mention fallback
        if failed:
            _mention_uids(failed, text)
        return sent
    except Exception as e:
        log("bot dm player failed:", e)
        return 0


def _game_channel_on():
    """Admin kill switch for the COMMUNAL channel feed of game events (kickoff/HT/FT/goals). Default on.
    When off, personal DMs to opted-in players still go out — only the public channel posts are silenced."""
    return load_config().get("game_channel_alerts", True) is not False


def _bettors_on_match(match_id):
    """Players holding an OPEN bet (single or any acca leg) on this match — for default-on bet reminders."""
    if not match_id:
        return set()
    out = set()
    try:
        for w in load_wagers():
            if w.get("status") != "pending":
                continue
            if w.get("matchId") == match_id or any(lg.get("matchId") == match_id for lg in (w.get("legs") or [])):
                if w.get("player"):
                    out.add(w["player"])
    except Exception as e:
        log("bettors-on-match failed:", e)
    return out


def _dm_all_games_uids():
    s = load_config().get("discord_dm_all")
    return [str(u) for u in s] if isinstance(s, list) else []


def _dm_all_games(text, exclude_players=None):
    """DM the all-games match feed to everyone who opted in with `/notifyme all`. Anyone in `exclude_players`
    (the owner[s] who ALREADY got a personal DM for this exact event) is skipped, so an owner who also opted
    into the all-games feed doesn't receive the same goal/event twice. Best-effort; never raises."""
    try:
        skip = set()
        for pl in (exclude_players or []):
            if pl and pl not in ("—", "-"):
                try:
                    skip.update(str(u) for u in _uids_for_player(pl))
                except Exception:
                    pass
        for u in _dm_all_games_uids()[:50]:
            if u in skip:
                continue
            _bot_dm(u, text)
    except Exception as e:
        log("dm all-games failed:", e)


# ---------------- Web Push (Path A) — needs pywebpush; guarded so missing lib is harmless ----------------
PUSH_FILE = "push_subs.json"          # {player: [{"sub": <subscription>, "prefs": {etype: bool}}, ...]}
EVENT_TYPES = ("goal", "kickoff", "flow", "knockout", "leader", "winner", "rivalry")


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


WELCOMED_FILE = "welcomed.json"               # ["uid", ...] — Discord users we've already greeted (DM once)
def _maybe_welcome(uid):
    """First time a person ever uses the bot, DM them a short welcome with the few key commands.
    The bot is interactions-only (no gateway), so 'joined the server' isn't observable — first
    command is the closest reliable signal. Once per user, best-effort, never blocks the reply."""
    if not uid:
        return
    try:
        with _lock:
            try:
                with open(WELCOMED_FILE) as f:
                    seen = json.load(f)
            except Exception:
                seen = []
            if uid in seen:
                return
            seen.append(uid)
            _atomic_write_json(WELCOMED_FILE, seen)
        _bot_dm(uid, WELCOME_TEXT)
    except Exception as e:
        log("welcome DM failed:", e)


WELCOME_TEXT = (
    "👋 **Welcome to the WC26 Sweepstake!**\n"
    "A few things you can do right here:\n"
    "• `/leaderboard` — who's winning\n"
    "• `/myteams` — the teams you were drawn\n"
    "• `/fixtures` — what's coming up\n"
    "• `/notifyme` — I'll DM you when your teams play & score\n"
    "• `/bet` — place a bet (first link up: tracker → 💷 Bets → **Connect Discord**, then `/linkdiscord`)\n"
    "Type `/help` any time for the full list. Good luck! 🍀"
)


try:
    import wager as wager_mod                 # optional wagering engine
except Exception:
    wager_mod = None
# Remember the engine's built-in cap defaults so an admin who clears a setting gets the default back.
_WAGER_DEFAULTS = {
    "MAX_PENDING": getattr(wager_mod, "MAX_PENDING", 8) if wager_mod else 8,
    "MAX_ACTIVE_ACCAS": getattr(wager_mod, "MAX_ACTIVE_ACCAS", 2) if wager_mod else 2,
}
WAGERS_FILE = "wagers.json"


def load_wagers():
    return _load_json_resilient(WAGERS_FILE, list, validate=lambda d: isinstance(d, list))


def save_wagers(w):
    # Wagers are append-only — they never legitimately go from non-empty back to empty.
    # So refuse to clobber a healthy non-empty log with an empty list (a sign of an upstream bug
    # or a transient read failure); stash the current file first so nothing is ever lost.
    if not w:
        try:
            with open(WAGERS_FILE) as f:
                existing = json.load(f)
            if isinstance(existing, list) and existing:
                os.makedirs(os.path.join("backups", "last_good"), exist_ok=True)
                shutil.copy2(WAGERS_FILE, os.path.join("backups", "last_good", "wagers.json"))
                print("[guard] refused to overwrite %d existing wagers with an empty list — kept the file intact" % len(existing))
                return
        except FileNotFoundError:
            pass
        except Exception:
            pass
    _atomic_write_json(WAGERS_FILE, w)


def _dedup_wager(wl, player, nonce):
    """Idempotency: if this player already placed a wager tagged with this nonce, return it instead of
    creating another. Stops a dropped connection (client retry) or a Discord interaction retry from
    turning one bet into two. The nonce is stored on the wager record, so it survives a restart too."""
    if not nonce:
        return None
    for w in wl:
        if w.get("nonce") == nonce and w.get("player") == player:
            return w
    return None


_PIN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"     # no ambiguous 0/O/1/I/L


def _gen_pin(n=5):
    return "".join(secrets.choice(_PIN_ALPHABET) for _ in range(n))


def _free_bet_drops():
    """Days free betting points are on offer: the day before the first game, plus roughly one match-day per week
    (deterministic 'random' pick, stable across restarts). At most 5 drops in total."""
    if wager_mod is None:
        return []
    try:
        ms = json.load(open("results.json")).get("matches", [])
    except Exception:
        return []
    day_ts = {}                                          # first kickoff epoch per UTC date
    for m in ms:
        t = wager_mod._utc_ts(m.get("utcDate") or "")
        if t is None:
            continue
        d = time.strftime("%Y-%m-%d", time.gmtime(t))
        day_ts[d] = min(day_ts.get(d, t), t)
    if not day_ts:
        return []
    days = sorted(day_ts)
    DAY = 86400
    drops = []
    first = day_ts[days[0]]
    drops.append({"id": "pre", "opens": first - DAY, "closes": first})   # the day before matchday 1, until kickoff
    cfg = load_config()
    seed = cfg.get("free_bet_seed")
    if not isinstance(seed, int):
        seed = secrets.randbelow(10**9)
        cfg["free_bet_seed"] = seed
        save_config(cfg)
    pool = days[1:] if len(days) > 1 else days           # don't double up the first day
    rnd = random.Random(seed)
    weeks = {}                                           # bucket match-days into 7-day weeks from matchday 1
    for d in pool:
        weeks.setdefault(int((day_ts[d] - first) // (7 * DAY)), []).append(d)
    pick = [rnd.choice(sorted(weeks[wk])) for wk in sorted(weeks)]   # ~one drop per week (deterministic)
    pick = sorted(pick)[: max(0, 5 - len(drops))]        # at most 5 drops total (the pre-drop counts as one)
    # Each match-day drop is claimable for the WHOLE day (00:00 → 24:00 UTC), not just up to the first kickoff,
    # and if a day ends with NOBODY having claimed it the window rolls forward a couple of days so it isn't simply
    # lost — that "extra day" is exactly the case where everyone missed the original window.
    GRACE = 2 * DAY
    _claims = cfg.get("free_bet_claims") if isinstance(cfg.get("free_bet_claims"), dict) else {}
    for d in pick:
        midnight = wager_mod._utc_ts(d + "T00:00:00Z")   # 00:00 UTC of that match-day
        if midnight is not None:
            close = midnight + DAY                        # the whole match-day
            if not (_claims.get(d) or {}):                # nobody claimed it yet -> keep it open a couple more days
                close += GRACE
            drops.append({"id": d, "opens": midnight, "closes": close})
    return sorted(drops, key=lambda x: x["opens"])


def _open_free_drop(now=None):
    """The free-points drop currently open (you can still claim it), or None."""
    now = now if now is not None else time.time()
    for d in _free_bet_drops():
        if d["opens"] <= now < d["closes"]:
            return d
    return None


def _claim_free_drop(player, drop_id):
    """Atomically claim a free-points drop, one per player per drop. Returns (status, result):
      ("ok", credit) granted; ("already", None) this player already claimed THIS drop; ("error", reason) other.
    The check and the record happen together under the single global lock, re-reading config inside it, so
    two fast taps — or a web claim and a Discord /claim at the same instant — can never both succeed."""
    with _lock:
        cfg = load_config()
        claims = cfg.get("free_bet_claims") if isinstance(cfg.get("free_bet_claims"), dict) else {}
        taken = claims.get(drop_id) if isinstance(claims.get(drop_id), dict) else {}
        if player in taken:                              # authoritative re-check inside the lock
            return "already", None
        wl = load_wagers()
        ok, res = wager_mod.grant_free_points(wl, player, drop_id)
        if not ok:
            return "error", res
        save_wagers(wl)
        taken[player] = res["id"]; claims[drop_id] = taken
        cfg["free_bet_claims"] = claims; save_config(cfg)
        return "ok", res


def _group_mid_ts():
    """Calendar midpoint (UTC epoch) of the group-stage games, so the staking budget resets halfway
    through the group stage. Games before it are epoch GROUP_1, on/after it GROUP_2. None if unknown."""
    if wager_mod is None:
        return None
    try:
        ms = json.load(open("results.json")).get("matches", [])
    except Exception:
        return None
    ts = [t for m in ms if (m.get("stage") or "GROUP_STAGE") == "GROUP_STAGE"
          for t in (wager_mod._utc_ts(m.get("utcDate") or ""),) if t is not None]
    if len(ts) < 2:
        return None
    return (min(ts) + max(ts)) / 2.0


def _apply_wager_caps(cfg=None):
    """Apply admin-configurable betting limits to the engine and return the caps dict.
    max_return: None = unlimited winnings (default); a number caps each bet's return.
    max_acca_legs: how many legs an accumulator may have (default 3)."""
    if wager_mod is None:
        return None
    cfg = cfg if cfg is not None else load_config()
    mr = cfg.get("max_return", None)
    if mr in (None, "", 0) or str(mr).strip().lower() in ("0", "none", ""):
        wager_mod.MAX_RETURN = None                       # blank / 0 / none = unlimited winnings
    else:
        try:
            wager_mod.MAX_RETURN = max(1.0, float(mr))    # a real cap, but never below the minimum (no negative/zero returns)
        except (TypeError, ValueError):
            wager_mod.MAX_RETURN = None
    try:
        legs = int(cfg.get("max_acca_legs", 3))
        wager_mod.MAX_ACCA_LEGS = max(2, min(10, legs))
    except (TypeError, ValueError):
        wager_mod.MAX_ACCA_LEGS = 3
    try:
        mp = cfg.get("max_pending_bets", None)
        wager_mod.MAX_PENDING = max(1, min(50, int(mp))) if mp not in (None, "") else _WAGER_DEFAULTS["MAX_PENDING"]
    except (TypeError, ValueError):
        wager_mod.MAX_PENDING = _WAGER_DEFAULTS["MAX_PENDING"]
    try:
        ma = cfg.get("max_active_accas", None)
        wager_mod.MAX_ACTIVE_ACCAS = max(0, min(20, int(ma))) if ma not in (None, "") else _WAGER_DEFAULTS["MAX_ACTIVE_ACCAS"]
    except (TypeError, ValueError):
        wager_mod.MAX_ACTIVE_ACCAS = _WAGER_DEFAULTS["MAX_ACTIVE_ACCAS"]
    return {"min_stake": wager_mod.MIN_STAKE, "max_stake": _current_round_max_stake(),
            "base_max_stake": wager_mod.MAX_STAKE, "max_return": wager_mod.MAX_RETURN,
            "max_pending": wager_mod.MAX_PENDING, "max_acca_legs": wager_mod.MAX_ACCA_LEGS,
            "max_active_accas": wager_mod.MAX_ACTIVE_ACCAS}


def _current_round_max_stake():
    """Highest single-bet cap among games you can ACTUALLY bet on now (both teams known + not kicked off).
    Knockout fixtures have TBD teams until those rounds, so during the group stage this stays 30, not 65."""
    if wager_mod is None:
        return 30
    try:
        results = json.load(open("results.json"))
    except Exception:
        return wager_mod.MAX_STAKE
    try:
        known = {t["name"] for t in load_teams()}
    except Exception:
        known = None
    caps = [wager_mod.stage_max_stake(m.get("stage")) for m in results.get("matches", [])
            if wager_mod.can_bet_on(m) and (known is None or (m.get("home") in known and m.get("away") in known))]
    return max(caps) if caps else wager_mod.MAX_STAKE


# Round order + friendly labels for the betting explainer. WC2026 schedule for reference (UTC, subject to FIFA):
#   Group stage 11–27 Jun · Round of 32 28 Jun–3 Jul · Round of 16 4–7 Jul · Quarter-finals 9–11 Jul
#   Semi-finals 14–15 Jul · Third place 18 Jul · Final 19 Jul. The per-game cap rises each round (30→35→40→45→50→65),
#   and the 50-pt staking budget resets at the group midpoint and again at the start of each of these rounds.
_STAGE_LABELS = [("GROUP_STAGE", "Group stage"), ("LAST_32", "Round of 32"), ("LAST_16", "Round of 16"),
                 ("QUARTER_FINALS", "Quarter-finals"), ("SEMI_FINALS", "Semi-finals"),
                 ("THIRD_PLACE", "Third place"), ("FINAL", "Final")]


def _stage_schedule():
    """For the betting explainer: each round's per-game max stake and when that round starts (from the loaded fixtures)."""
    if wager_mod is None:
        return []
    try:
        ms = json.load(open("results.json")).get("matches", [])
    except Exception:
        ms = []
    earliest = {}
    for m in ms:
        st = m.get("stage") or "GROUP_STAGE"
        t = wager_mod._utc_ts(m.get("utcDate") or "")
        if t is None:
            continue
        earliest[st] = min(earliest.get(st, t), t)
    out = []
    for st, label in _STAGE_LABELS:
        d = earliest.get(st)
        out.append({"stage": st, "label": label, "cap": wager_mod.stage_max_stake(st),
                    "from": (time.strftime("%d %b", time.gmtime(d)).lstrip("0") if d else None)})
    return out


def _discord_err(e):
    """Turn a urllib error from a Discord call into a human message (esp. 429 rate limits)."""
    try:
        import urllib.error
        if isinstance(e, urllib.error.HTTPError):
            body = {}
            try:
                body = json.loads(e.read().decode() or "{}")
            except Exception:
                body = {}
            dmsg = body.get("message") if isinstance(body, dict) else ""
            if e.code == 429:
                ra = (e.headers.get("Retry-After") if e.headers else None) or (body.get("retry_after") if isinstance(body, dict) else None)
                secs = int(float(ra)) + 1 if ra else 30
                return "Discord is rate-limiting us (429). Wait about %d second%s and try again — don't tap it repeatedly." % (secs, "" if secs == 1 else "s")
            if e.code == 401:
                return "Discord rejected the bot token (401) — re-check the Bot Token in Settings."
            if e.code == 403:
                return "Discord blocked it (403) — the bot isn't in that server / can't DM you. Re-invite the bot, or use the website instead."
            if e.code in (400, 404):
                errs = body.get("errors") if isinstance(body, dict) else None
                if errs:                                   # dig out the exact field Discord rejected
                    paths = []
                    def _walk(node, trail):
                        if isinstance(node, dict):
                            if "_errors" in node and isinstance(node["_errors"], list):
                                for er in node["_errors"]:
                                    paths.append(("/".join(trail) + ": " + er.get("message", "")).strip(" :"))
                            for kk, vv in node.items():
                                if kk != "_errors":
                                    _walk(vv, trail + [str(kk)])
                    _walk(errs, [])
                    detail = "; ".join(paths[:4]) if paths else dmsg
                    return "Discord rejected the command list (HTTP %s): %s. This is a command-definition problem, not your IDs." % (e.code, detail)
                tail = (": “%s”" % dmsg) if dmsg else ""
                return ("Discord rejected the request (HTTP %s)%s. For Register commands this is almost always a wrong "
                        "Application ID or Server (Guild) ID — check both in Settings (the Guild ID must be your server's, all digits)." % (e.code, tail))
            return "Discord returned HTTP %s%s." % (e.code, (": “%s”" % dmsg) if dmsg else "")
    except Exception:
        pass
    return str(e)


def _bot_dm(user_id, text):
    """Send a private DM from the bot to one Discord user (used to hand out bet passcodes)."""
    tok = (load_config().get("discord_bot_token") or "").strip()
    if not tok:
        return False, "Set the Discord bot token first."
    hdr = {"Authorization": "Bot %s" % tok, "Content-Type": "application/json",
           "User-Agent": "WC26-Sweepstake/1.0"}
    try:
        r = urllib.request.Request("https://discord.com/api/v10/users/@me/channels",
                                   data=json.dumps({"recipient_id": str(user_id)}).encode(), headers=hdr)
        cid = json.loads(urllib.request.urlopen(r, timeout=8).read()).get("id")
        if not cid:
            return False, "couldn't open a DM channel"
        r2 = urllib.request.Request("https://discord.com/api/v10/channels/%s/messages" % cid,
                                    data=json.dumps({"content": text[:1900]}).encode(), headers=hdr)
        urllib.request.urlopen(r2, timeout=8)
        return True, None
    except Exception as e:
        log("bot dm failed:", e)
        return False, _discord_err(e)


def _pick_label(w, draw="the draw"):
    """Human label for a bet/leg in announcements — result (team/draw) or Over/Under goals."""
    if (w.get("market") or "result") == "ou":
        try:
            return ("Over %g goals" if w.get("selection") == "OVER" else "Under %g goals") % float(w.get("line"))
        except (TypeError, ValueError):
            return "Over/Under goals"
    return w["home"] if w.get("selection") == "HOME" else (w["away"] if w.get("selection") == "AWAY" else draw)


def _announce_bet(player, w):
    """Post a placed bet to the group (Discord webhook + Telegram). Never reveals passcodes."""
    if w.get("legs"):
        picks = " + ".join("%s (%s)" % (_pick_label(lg, "draw"), lg["frac"]) for lg in w["legs"])
        line = "🎲 %s put %g on a %d-fold acca: %s — returns %g if it all lands." % (
            player, w["stake"], len(w["legs"]), picks, w["return"])
    else:
        pick = _pick_label(w, "a draw")
        if w.get("free"):
            line = "🎁 %s claimed a free bet on %s (%s v %s) at %s — returns %g if it wins (a loss costs nothing)." % (
                player, pick, w["home"], w["away"], w["frac"], w["return"])
        else:
            line = "🎲 %s staked %g on %s (%s v %s) at %s — returns %g if it wins." % (
                player, w["stake"], pick, w["home"], w["away"], w["frac"], w["return"])
    try:
        discord_send(line)
    except Exception as e:
        log("bet announce (discord) failed:", e)
    try:
        tg_broadcast(line)
    except Exception as e:
        log("bet announce (tg) failed:", e)


def _wager_desc(w):
    if w.get("legs"):
        return "%d-fold acca" % len(w["legs"])
    return _pick_label(w, "the draw")


def _announce_wins(won):
    """One grouped channel post + a personal DM per winner when bets land.
    Called only with bets that just flipped to WON, so an acca appears here only once every leg has won.
    Grouped per settlement batch so a finishing game produces one message, not a flood."""
    if not won:
        return
    lines = ["%s won +%g (%s)" % (w["player"], round(w["return"] - w["stake"], 1), _wager_desc(w)) for w in won]
    blast = "🎉 Bets landed — " + "; ".join(lines) + "."
    try:
        discord_send(blast)
    except Exception as e:
        log("win announce (discord) failed:", e)
    try:
        tg_broadcast(blast)
    except Exception as e:
        log("win announce (tg) failed:", e)
    cfg = load_config()
    links = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
    subs = _discord_subs()
    byp = {}
    for w in won:
        byp.setdefault(w["player"], []).append(w)
    for player, ws in byp.items():
        uid = next((u for u, pl in links.items() if pl == player), None) or next((u for u, pl in subs.items() if pl == player), None)
        if not uid:
            continue
        tot = round(sum(x["return"] - x["stake"] for x in ws), 1)
        msg = ("🎉 Your bet won! +%g points (%s) — it's in your total now." % (tot, _wager_desc(ws[0]))
               if len(ws) == 1 else
               "🎉 %d of your bets won! +%g points total — they're in your total now." % (len(ws), tot))
        try:
            _bot_dm(uid, msg)
        except Exception as e:
            log("win dm failed:", e)


def _wager_pins():
    p = load_config().get("wager_pins")
    return p if isinstance(p, dict) else {}


def _pin_ok(player, pin):
    """Constant-time check that `pin` is the bet passcode issued to `player`."""
    want = _wager_pins().get(player)
    if not want or not pin:
        return False
    return hmac.compare_digest(str(want), str(pin).strip().upper())


def _session_secret():
    """Key used to sign browser session tokens. Tied to the admin key + a rotatable salt,
    so the organiser can log everyone out by bumping the salt (via /api/logout_all)."""
    c = load_config()
    return (str(c.get("admin_key") or "wc26-no-key") + str(c.get("session_salt") or "")).encode()


def _make_session(discord_id, days=30):
    """A stateless, signed 'this browser is Discord user X' token (no server-side storage)."""
    exp = int(time.time()) + int(days * 86400)
    body = "%s.%d" % (str(discord_id), exp)
    sig = hmac.new(_session_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return body + "." + sig


def _read_session(token):
    """Return the Discord id from a valid, unexpired session token, else None."""
    try:
        did, exp, sig = (token or "").split(".")
        good = hmac.new(_session_secret(), ("%s.%s" % (did, exp)).encode(), hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(good, sig) and int(exp) > int(time.time()):
            return did
    except Exception:
        pass
    return None


def _oauth_enabled():
    """Discord login only turns on once the organiser has filled in the OAuth app credentials."""
    c = load_config()
    return bool(c.get("discord_oauth_client_id") and c.get("discord_oauth_client_secret"))


def _member_from_status(code):
    """Map Discord's guild-member lookup HTTP status to a membership verdict:
       True  = confirmed member, False = confirmed NOT a member (404),
       None  = couldn't determine (bad token/intent 401/403, rate-limit 429, 5xx, network) -> fail closed but allow retry."""
    if code == 200:
        return True
    if code == 404:
        return False
    return None


def _is_guild_member(user_id, cfg=None):
    """Ask Discord whether this user is in our server, using the bot token. Returns True / False / None
    (None = couldn't check). Never raises. Needs the bot in the guild + 'Server Members Intent' enabled."""
    cfg = cfg if cfg is not None else load_config()
    token = (cfg.get("discord_bot_token") or "").strip()
    guild = (cfg.get("discord_guild_id") or "").strip()
    if not token or not guild or not user_id:
        return None
    url = "https://discord.com/api/v10/guilds/%s/members/%s" % (guild, user_id)
    req = urllib.request.Request(url, headers={"Authorization": "Bot " + token,
                                               "User-Agent": "WC26-Sweepstake/1.0 (+https://bbmsweepstake.co.uk)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return _member_from_status(r.status)
    except urllib.error.HTTPError as e:
        return _member_from_status(e.code)
    except Exception:
        return None


def _guild_gate_on(cfg):
    """The membership gate is active only when a bot token AND guild id are configured (so a check is even
    possible) and it hasn't been explicitly switched off. If they're not set, the gate is OFF automatically —
    so deploying this can never lock anyone out of a site that isn't wired for it."""
    return bool((cfg.get("discord_bot_token") or "").strip()
                and (cfg.get("discord_guild_id") or "").strip()
                and cfg.get("discord_guild_gate", True))


def _is_blocked(user_id, cfg):
    """True if this Discord account has been blocked by the organiser (can never claim a name)."""
    return str(user_id) in [str(x) for x in (cfg.get("discord_blocklist") or [])]


def _guild_claim_check(user_id, cfg):
    """Decision for whether a Discord account may claim a player name:
       'blocked'    -> the organiser has blocked this account -> always refuse
       'ok'         -> allowed (gate off, or confirmed member)
       'not_member' -> in our server check, they're NOT a member -> refuse
       'unverified' -> couldn't check right now -> fail closed, ask them to retry."""
    if _is_blocked(user_id, cfg):
        return "blocked"
    if not _guild_gate_on(cfg):
        return "ok"
    m = _is_guild_member(user_id, cfg)
    if m is True:
        return "ok"
    if m is False:
        return "not_member"
    return "unverified"



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
    """Send one push. Returns (keep, err): keep=False means the subscription is dead (prune it);
    err is None on success, otherwise a short human-readable reason.
    TTL matters: pywebpush defaults to TTL=0 ("deliver this instant or drop"), and iPhones that are
    locked/asleep silently lose TTL-0 pushes — so we give every push a 6-hour shelf life."""
    cfg = load_config()
    try:
        webpush(subscription_info=sub, data=json.dumps({"title": title, "body": body}),
                vapid_private_key=_vapid_key(), ttl=21600, headers={"Urgency": "high"},
                vapid_claims={"sub": cfg.get("vapid_sub", "mailto:admin@bbmsweepstake.co.uk")})
        return True, None
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        detail = ""
        try:
            detail = (getattr(e, "response", None).text or "")[:120]
        except Exception:
            pass
        if code in (404, 410):
            return False, "subscription expired (pruned)"
        log("webpush failed:", code, e)
        return True, "push service said %s %s" % (code if code is not None else "?", detail)
    except Exception as e:
        log("webpush error:", e)
        return True, str(e)[:160]


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
        keep_it, _err = _webpush_one(_entry_sub(e), title, body)
        if keep_it:
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
def alert_all(etype, title, body, group_line):
    push_broadcast(etype, title, body)
    discord_send(group_line)


def _load_tracker():
    try:
        with open("tracker_data.json") as f:
            return json.load(f)
    except Exception:
        return None


MATCH_CLOCKS_FILE = "match_clocks.json"   # per-match real-time clock state: {matchId: {ko, htp, ps}}


def _load_match_clocks():
    try:
        with open(MATCH_CLOCKS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _update_match_clocks(matches, now=None):
    """Track real kick-off time + accumulated half-time per match, so the tracker can show an accurate ticking
    match clock (seconds), not just the feed's minute. Per matchId we store:
        ko  = epoch the match clock is anchored to (back-dated by the feed minute the first time we can anchor)
        htp = total paused (half-time) seconds banked so far
        ps  = epoch the current pause began, or None
    elapsed-while-playing = now - ko - htp. We only anchor when the feed gives us a minute (so we never guess),
    which means a game with no broadcast minute simply shows 'LIVE'. Fully defensive: never raises, because the
    live clock must never be able to affect scoring, settlement or alerts."""
    try:
        now = time.time() if now is None else now
        clocks = _load_match_clocks()
        if not isinstance(clocks, dict):
            clocks = {}
        changed = False
        PLAYING_ST = ("IN_PLAY", "LIVE", "SUSPENDED")
        for m in (matches or []):
            try:
                if not isinstance(m, dict):
                    continue
                mid = wager_mod.match_id(m) if wager_mod else str(m.get("id") or "")
                if not mid:
                    continue
                st = m.get("status")
                playing = st in PLAYING_ST
                paused = st == "PAUSED"
                rec = clocks.get(mid)
                if playing or paused:
                    if not isinstance(rec, dict):
                        mn = m.get("minute")
                        if isinstance(mn, (int, float)) and mn is not None and mn >= 0:
                            ko = now - float(mn) * 60.0       # back-date from the broadcast minute when we have one (most accurate)
                        else:
                            ko = now                          # no feed minute (e.g. free plan): start the clock at the kickoff the SERVER just detected
                        clocks[mid] = {"ko": ko, "htp": 0.0, "ps": (now if paused else None)}
                        changed = True
                    else:
                        if paused and not rec.get("ps"):
                            rec["ps"] = now           # a pause (half-time) just began
                            changed = True
                        elif playing and rec.get("ps"):
                            rec["htp"] = (rec.get("htp") or 0.0) + max(0.0, now - rec["ps"])  # bank the paused time
                            rec["ps"] = None
                            changed = True
                        if playing and not rec.get("ps"):     # keep the clock roughly tied to the broadcast minute WITHOUT yanking it backward
                            mn = m.get("minute")
                            if isinstance(mn, (int, float)) and mn is not None and mn >= 0:
                                target = float(mn) * 60.0
                                computed = now - rec["ko"] - (rec.get("htp") or 0.0)
                                # The feed minute is AHEAD of our clock (we missed time, or the feed jumped) -> catch up forward.
                                # Our clock running AHEAD of the feed minute is EXPECTED on a delayed / "sticky" feed (the free plan
                                # lags the real minute and sometimes freezes it), and must NOT pull the clock back — doing so made the
                                # clock keep restarting to a stale minute and fall ~minutes behind. Only snap back on a BIG lead
                                # (~a missed half-time), never for ordinary feed lag. Use a wide forward threshold too, so the feed
                                # briefly OVER-reading the minute (stoppage time is sometimes folded in) can't yank the clock ahead.
                                if computed < target - 180:      # >3 min BEHIND the feed => we genuinely missed time, catch up
                                    rec["ko"] = now - (rec.get("htp") or 0.0) - target
                                    changed = True
                                elif computed > target + 480:    # >8 min ahead => a real gap (e.g. a half-time we never saw), not feed lag
                                    rec["ko"] = now - (rec.get("htp") or 0.0) - target
                                    changed = True
                else:
                    if isinstance(rec, dict) and rec.get("ps"):   # match left live while paused: bank it (defensive)
                        rec["htp"] = (rec.get("htp") or 0.0) + max(0.0, now - rec["ps"])
                        rec["ps"] = None
                        changed = True
            except Exception:
                continue
        if changed:
            try:
                tmp = MATCH_CLOCKS_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(clocks, f)
                os.replace(tmp, MATCH_CLOCKS_FILE)
            except Exception:
                pass
    except Exception:
        pass


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
        if m.get("stage") == "FINAL" and m.get("status") in ("FINISHED", "AWARDED"):
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
    _mp = (new.get("stats") or {}).get("matches_played", 0)
    _old_mp = (old.get("stats") or {}).get("matches_played", 0) or 0   # so we can tell a SETTLED change from live provisional churn
    _any_live = any((m.get("status") in LIVE_STATUSES) for m in (new.get("fixtures") or []))
    if _mp == 0 and not _any_live:
        return                              # truly nothing happening yet (pre-tournament): stay quiet

    def match_event(etype, recipients, group_line, ping=False, important=False, match_id=None):
        # recipients: list of (owner, title, body). Personal alerts always go to the owner (push + DM).
        # The communal CHANNEL line fires for EVERY game event (kickoff/half-time/full-time) whenever the admin
        # has the channel feed switched on (game_channel_alerts) — turning it on means "post game events here".
        # When it's off, only personal DMs (owners + all-games opt-ins) go out. `important` is kept for callers
        # but no longer gates the channel — the single admin switch is the control.
        for ow, ti, bo in recipients:
            if ow and ow not in ("—", "-"):
                push_player(ow, etype, ti, bo)
                _bot_dm_player(ow, "%s — %s" % (ti, bo), match_id=match_id)   # personal -> always DM (falls back to @mention for opt-ins)
        if _game_channel_on():
            discord_send(group_line)        # communal feed: every game event while the channel is on
        _dm_all_games(group_line, exclude_players=[r[0] for r in recipients])   # ...and DMs all-games opt-ins, minus owners who already got the personal DM

    def own(o):
        return o if (o and o not in ("—", "-")) else "—"

    # overall (Both) leader change -> everyone
    try:
        ol = (old["leaderboards"]["hybrid"][0] or {}).get("name")
        nl = (new["leaderboards"]["hybrid"][0] or {}).get("name")
        if _mp > _old_mp and nl and ol and nl != ol:   # only when a match SETTLES — points accrue live, so a single goal must not ping "new leader"
            alert_all("leader", "New leader 📈", "%s now tops the table." % nl,
                      "📈 New leader: **%s** now tops the table." % nl)
    except Exception:
        pass
    # head-to-head overtakes (positions below 1st), in the active scoring mode. An overtake involves two
    # players, so it's a genuine head-to-head moment -> it posts to the channel AND DMs both players.
    try:
        for x, y, pos in (rivalry_alerts(old, new, _active_mode()) if _mp > _old_mp else []):   # overtakes only when a result settles — live points churn the board on every goal
            push_player(x, "rivalry", "Moved up 📊", "You overtook %s for %s." % (y, _ord(pos)))
            push_player(y, "rivalry", "Overtaken 📉", "%s just passed you for %s." % (x, _ord(pos)))
            _bot_dm_player(x, "📊 You overtook **%s** for %s." % (y, _ord(pos)))
            _bot_dm_player(y, "📉 **%s** just passed you for %s." % (x, _ord(pos)))
            discord_send("📊 **%s** overtakes **%s** for %s." % (x, y, _ord(pos)))   # 2-player event = channel-worthy
    except Exception:
        pass
    # per-match transitions: kickoff, half-time, second half, goals
    try:
        of, nf = _fixture_status(old), _fixture_status(new)
        nmatch = {(m.get("home"), m.get("away")): m for m in (new.get("fixtures") or [])}
        FT_STATUSES = ("FINISHED", "AWARDED")
        for key, nv in nf.items():
            h, a = key
            st, ho, ao, nhs, nas = nv
            ov = of.get(key)
            was = ov[0] if ov else None
            ho_ok = ho and ho not in ("—", "-")
            ao_ok = ao and ao not in ("—", "-")
            mm0 = nmatch.get(key) or {}
            mid = mm0.get("matchId")
            _ko = str(mm0.get("stage") or "").upper() not in ("", "GROUP_STAGE", "GROUP")  # knockout = always important
            if st in LIVE_STATUSES and was not in LIVE_STATUSES:                      # kickoff
                match_event("kickoff",
                            [(ho, "%s vs %s" % (h, a), "Kicked off — your team %s is playing!" % h),
                             (ao, "%s vs %s" % (h, a), "Kicked off — your team %s is playing!" % a)],
                            "🔵 Kicked off — **%s** (%s) vs **%s** (%s)" % (h, own(ho), a, own(ao)), ping=True, important=_ko, match_id=mid)
                for _pl in _bettors_on_match(mid):                                    # bet on this game -> DM (default-on)
                    if _pl not in (ho, ao):
                        _bot_dm_player(_pl, "🎲 Your bet is live — **%s** v **%s** has kicked off. Good luck!" % (h, a), match_id=mid)
            elif st == "PAUSED" and was in PLAYING:                                   # half-time
                sc = "%s %s–%s %s" % (h, nhs, nas, a) if None not in (nhs, nas) else "%s vs %s" % (h, a)
                match_event("flow",
                            [(ho, "Half-time ⏸️", sc), (ao, "Half-time ⏸️", sc)],
                            "⏸️ Half-time — %s" % sc, important=_ko, match_id=mid)
            elif st in FT_STATUSES and was in LIVE_STATUSES:                          # full-time (incl. a.e.t. / pens)
                mm = nmatch.get(key) or {}
                sc = "%s %s–%s %s" % (h, nhs, nas, a) if None not in (nhs, nas) else "%s vs %s" % (h, a)
                extra = ""
                if mm.get("shootout"):
                    ph, pa = mm.get("penHome"), mm.get("penAway")
                    extra = " — %s–%s on penalties" % (ph, pa) if None not in (ph, pa) else " — won on penalties"
                elif mm.get("aet"):
                    extra = " — after extra time"
                match_event("flow",
                            [(ho, "Full-time ⏱️", sc + extra), (ao, "Full-time ⏱️", sc + extra)],
                            "⏱️ Full-time — %s%s" % (sc, extra), ping=True, important=_ko, match_id=mid)
                for _pl in _bettors_on_match(mid):                                    # bet on this game -> FT DM (default-on)
                    if _pl not in (ho, ao):
                        _bot_dm_player(_pl, "🎲 Full-time on a game you bet — **%s%s**. Any winnings are settled automatically." % (sc, extra), match_id=mid)
            if ov is not None:                                                        # goals (any time score rises)
                ohs, oas = ov[3], ov[4]
                if None not in (nhs, nas, ohs, oas):
                    score = "%s %d–%d %s" % (h, nhs, nas, a)
                    if nhs > ohs and ho_ok:
                        push_player(ho, "goal", "%s scored! ⚽" % h, score)
                        _bot_dm_player(ho, "⚽ **%s** scored — %s" % (h, score), match_id=mid)     # personal -> DM
                        if _game_channel_on():
                            discord_send("⚽ **%s** (%s) scored — %s" % (h, ho, score))
                        _dm_all_games("⚽ **%s** (%s) scored — %s" % (h, ho, score), exclude_players=[ho])   # owner already got the personal DM above
                    if nas > oas and ao_ok:
                        push_player(ao, "goal", "%s scored! ⚽" % a, score)
                        _bot_dm_player(ao, "⚽ **%s** scored — %s" % (a, score), match_id=mid)     # personal -> DM
                        if _game_channel_on():
                            discord_send("⚽ **%s** (%s) scored — %s" % (a, ao, score))
                        _dm_all_games("⚽ **%s** (%s) scored — %s" % (a, ao, score), exclude_players=[ao])   # owner already got the personal DM above
    except Exception:
        pass
    # a player's team is knocked out
    try:
        oa, na = _alive_owners(old), _alive_owners(new)
        for t in oa:
            if oa[t][0] and t in na and not na[t][0]:
                owner = na[t][1]
                push_player(owner, "knockout", "%s is out ❌" % t, "Check the leaderboard to see where you stand.")
                _bot_dm_player(owner, "❌ **%s** is out." % t)     # personal -> DM only
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
            tail = (" — %s finishes on %s pts." % (owner, entry.get("score", 0))) if (entry and own(owner) != "—") else "."
            alert_all("winner", "🏆 Champions: %s" % team,
                      "%s won the World Cup%s" % (team, tail),
                      "🏆 **%s** (%s) are World Cup champions%s" % (team, own(owner), tail))
            wrap = build_wrapup()
            if wrap:
                discord_send("\n".join(wrap))      # full recap to the channel, once
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
    _live_now = any((m.get("status") in ("IN_PLAY", "PAUSED", "LIVE", "SUSPENDED")) for m in (d.get("fixtures") or []))
    if not mp and not _live_now:
        return ["📊 WC26 Sweepstake", "Tournament hasn't kicked off yet — the summary fills in once games start (11 June)."]
    today = time.strftime("%a %d %b", time.gmtime())
    mode = (load_config().get("scoring_mode") or "hybrid")
    mode = mode if mode in ("points", "survival", "hybrid") else "hybrid"
    label = {"points": "pts", "survival": "teams in", "hybrid": "total"}[mode]
    lines = ["📊 **WC26 Sweepstake** — %s" % today]
    board = (d.get("leaderboards") or {}).get(mode) or []
    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(board[:3]):
        lines.append("%s %s — %s %s" % (medals[i], p.get("name", "?"), p.get("score", 0), label))
    if stats.get("teams_remaining") is not None:
        lines.append("🛡️ Teams still in: %s" % stats["teams_remaining"])
    if stats.get("top_team"):
        lines.append("🔥 Top team: %s (%s) — %s goals" % (stats["top_team"], _owner_of(d, stats["top_team"]), stats.get("top_team_goals", 0)))
    if stats.get("top_scorer_player"):
        lines.append("⚽ Most goals: %s (%s)" % (stats["top_scorer_player"], stats.get("top_scorer_player_goals", 0)))
    lines.append("📅 Played: %s · ⚽ %s goals (%s/game)" % (mp, stats.get("goals", 0), stats.get("goals_per_match", 0))
                 if mp else "🔴 Games in progress — live standings above")
    cd = d.get("champion_decided") or {}
    if cd.get("team"):
        lines.append("🏆 Champions: %s (%s)" % (cd["team"], cd.get("owner") or _owner_of(d, cd["team"])))
    return lines


def _hhmm(utc):
    try:
        return time.strftime("%H:%M", time.strptime((utc or "")[:16], "%Y-%m-%dT%H:%M"))
    except Exception:
        return ""


def _ord(n):
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _day_by_player(d):
    """{player: ["Team vs Opp (HH:MM)", ...]} for each player's teams playing today (UTC), not yet finished.
    A player who owns BOTH teams in a match sees it once (not mirrored), and exact duplicates are dropped."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    by = {}
    def add(player, text):
        if player and player not in ("—", "-"):
            lst = by.setdefault(player, [])
            if text not in lst:                        # never list the same fixture twice for one player
                lst.append(text)
    for m in (d.get("fixtures") or []):
        if (m.get("utcDate") or "")[:10] != today or m.get("status") == "FINISHED":
            continue
        t = _hhmm(m.get("utcDate"))
        suffix = (" (%s)" % t if t else "")
        ho, ao = m.get("homeOwner"), m.get("awayOwner")
        ho_ok = ho and ho not in ("—", "-")
        ao_ok = ao and ao not in ("—", "-")
        if ho_ok and ho == ao:                         # same player owns both teams: one line, not two mirrored ones
            add(ho, "%s vs %s%s" % (m.get("home"), m.get("away"), suffix))
        else:
            if ho_ok:
                add(ho, "%s vs %s%s" % (m.get("home"), m.get("away"), suffix))
            if ao_ok:
                add(ao, "%s vs %s%s" % (m.get("away"), m.get("home"), suffix))
    return by


def build_day_lines(d):
    """A 'your teams today' section appended to the daily Discord digest."""
    by = _day_by_player(d)
    if not by:
        return []
    lines = ["", "📅 **Today's games**"]
    for pl in sorted(by):
        lines.append("• %s: %s" % (pl, ", ".join(by[pl])))
    return lines


def push_day_fixtures(d):
    """Personal morning push to each subscribed player whose teams play today."""
    for pl, games in _day_by_player(d).items():
        push_player(pl, "flow", "Your games today ⚽", " · ".join(games[:6]))


def rivalry_alerts(old, new, mode):
    """A player overtaking another in the active leaderboard (positions 2+; the leader change covers the top)."""
    ob = [p.get("name") for p in (old.get("leaderboards") or {}).get(mode) or []]
    nb = [p.get("name") for p in (new.get("leaderboards") or {}).get(mode) or []]
    if len(nb) < 2:
        return []
    orank = {n: i for i, n in enumerate(ob)}
    out = []
    for i in range(1, len(nb) - 1):                    # skip i==0: 1st place is the leader-change alert
        x, y = nb[i], nb[i + 1]                         # x is directly above y now
        if x in orank and y in orank and orank[x] > orank[y]:
            out.append((x, y, i + 1))                   # x overtook y, claiming (i+1)th
    return out[:3]


def _third_place(d):
    for m in (d.get("fixtures") or []):
        if m.get("stage") == "THIRD_PLACE" and m.get("status") in ("FINISHED", "AWARDED"):
            hs, as_ = m.get("homeScore"), m.get("awayScore")
            if hs is not None and as_ is not None and hs != as_:
                return m["home"] if hs > as_ else m["away"]
            w = m.get("winner")
            if w in ("HOME", "HOME_TEAM"):
                return m.get("home")
            if w in ("AWAY", "AWAY_TEAM"):
                return m.get("away")
    return None


def build_wrapup():
    """End-of-tournament recap — champion, podium, final table, golden-boot team. Posted once the final is done."""
    d = _load_tracker()
    if not d:
        return []
    cd = d.get("champion_decided") or {}
    if not cd.get("team"):
        return []
    stats = d.get("stats") or {}
    mode = _active_mode()
    lines = ["🏁 **WC26 — that's a wrap!**",
             "🏆 Champions: **%s** (%s)" % (cd["team"], cd.get("owner") or _owner_of(d, cd["team"]))]
    if cd.get("runnerUp"):
        lines.append("🥈 Runners-up: %s (%s)" % (cd["runnerUp"], _owner_of(d, cd["runnerUp"])))
    third = _third_place(d)
    if third:
        lines.append("🥉 Third place: %s (%s)" % (third, _owner_of(d, third)))
    board = (d.get("leaderboards") or {}).get(mode) or []
    if board:
        lbl = {"points": "Points", "survival": "Survival", "hybrid": "Both"}.get(mode, "Both")
        lines.append("")
        lines.append("**Final table — %s**" % lbl)
        medals = ["🥇", "🥈", "🥉"]
        for i, p in enumerate(board[:3]):
            lines.append("%s %s — %s" % (medals[i], p.get("name", "?"), p.get("score", 0)))
        if len(board) > 3:
            lines.append("…and %d more" % (len(board) - 3))
    # three separate prizes — each mode can have a DIFFERENT winner
    lbs = d.get("leaderboards") or {}
    prizes = []
    for key, name in (("points", "Points"), ("survival", "Survival"), ("hybrid", "Both")):
        b = lbs.get(key) or []
        if b:
            prizes.append("🏅 %s winner: **%s** (%s)" % (name, b[0].get("name", "?"), b[0].get("score", 0)))
    if prizes:
        lines.append("")
        lines.append("**Prizes** — one per scoring mode")
        lines += prizes
        winners = {lbs[k][0]["name"] for k in ("points", "survival", "hybrid") if lbs.get(k)}
        if len(winners) > 1:
            lines.append("(%d different winners across the three modes!)" % len(winners))
    if stats.get("top_team"):
        lines.append("⚽ Golden-boot team: %s (%s) — %s goals"
                     % (stats["top_team"], _owner_of(d, stats["top_team"]), stats.get("top_team_goals", 0)))
    lines.append("📅 %s games · %s goals (%s/game)"
                 % (stats.get("matches_played", 0), stats.get("goals", 0), stats.get("goals_per_match", 0)))
    return lines


def build_draw_announcement():
    """Round-by-round picks + each player's final squad, posted to Discord when the draw locks."""
    try:
        dr = json.load(open("draw_result.json"))
    except Exception:
        return []
    players = dr.get("players") or []
    if not players:
        return []
    maxr = max((len(p.get("teams", [])) for p in players), default=0)
    lines = ["🎲 **The WC26 draw is in!**"]
    for r in range(maxr):
        picks = ["• %s → %s" % (p["name"], p["teams"][r]["name"])
                 for p in players if r < len(p.get("teams", []))]
        if picks:
            lines.append("")
            lines.append("**Round %d**" % (r + 1))
            lines += picks
    lines.append("")
    lines.append("**Final squads**")
    for p in players:
        lines.append("**%s** (%d): %s" % (p["name"], len(p.get("teams", [])),
                                          ", ".join(t["name"] for t in p.get("teams", []))))
    pool = dr.get("bonus_pool") or []
    if pool:
        lines.append("")
        lines.append("Leftover pool: %s" % ", ".join(t["name"] for t in pool))
    return lines


def _active_mode():
    m = (load_config().get("scoring_mode") or "hybrid")
    return m if m in ("points", "survival", "hybrid") else "hybrid"


def _bet_match_choices(partial=""):
    """Autocomplete choices for /bet `match`: each upcoming bettable game, shown as 'Brazil v Serbia — Sat 14:00',
    value = the matchId so the handler resolves it exactly. Filtered by what's typed (matches either team or the
    'A v B' label); Discord caps at 25."""
    try:
        d = _load_tracker() or {}
        fx = sorted([m for m in (d.get("fixtures") or []) if m.get("odds") and m.get("matchId")],
                    key=lambda m: m.get("utcDate") or "")
    except Exception:
        return []
    q = (partial or "").strip().lower()
    out = []
    for m in fx:
        home, away = m.get("home", ""), m.get("away", "")
        if not home or not away:
            continue
        label = "%s v %s" % (home, away)
        if q and q not in home.lower() and q not in away.lower() and q not in label.lower():
            continue
        when = ""
        try:
            when = " — " + (m.get("utcDate") or "")[:16].replace("T", " ")
        except Exception:
            when = ""
        out.append({"name": (label + when)[:100], "value": str(m["matchId"])[:100]})
        if len(out) >= 25:
            break
    return out


def _bet_team_choices(partial=""):
    """Autocomplete choices for /bet `team`: every team in an upcoming bettable game,
    shown as 'Brazil — v Spain' so you're really picking the GAME + team. Filtered by what's typed; Discord caps at 25."""
    try:
        d = _load_tracker() or {}
        fx = sorted([m for m in (d.get("fixtures") or []) if m.get("odds") and m.get("matchId")],
                    key=lambda m: m.get("utcDate") or "")
    except Exception:
        return []
    q = (partial or "").strip().lower()
    out, seen = [], set()
    for m in fx:
        for team, opp in ((m.get("home", ""), m.get("away", "")), (m.get("away", ""), m.get("home", ""))):
            if not team or team in seen:
                continue
            if q and q not in team.lower():
                continue
            out.append({"name": ("%s — v %s" % (team, opp))[:100], "value": team[:100]})
            seen.add(team)
            if len(out) >= 25:
                return out
    return out


def discord_command(name, opts, uid=None, interaction_id=None):
    """Build a read-only reply for a slash command. No admin actions."""
    _maybe_welcome(uid)                 # greet a brand-new user once, with the few key commands
    d = _load_tracker() or {}
    mode = _active_mode()
    label = "survival" if mode == "survival" else "pts"
    if name == "notifyme":
        if not uid:
            return "Couldn't read your Discord account — run this from inside the server."
        who = str(opts.get("player", "")).strip()
        if who.lower() in ("all", "everything", "every game", "everyone"):
            cfg = load_config(); allg = cfg.get("discord_dm_all"); allg = allg if isinstance(allg, list) else []
            if str(uid) not in [str(x) for x in allg]:
                allg.append(str(uid))
            cfg["discord_dm_all"] = allg
            cfg["discord_dm_off"] = [str(x) for x in (cfg.get("discord_dm_off") or []) if str(x) != str(uid)]   # re-enable DMs
            save_config(cfg)
            ok, _ = _bot_dm(uid, "🌍 You're set up for **all-games** alerts — I'll DM you every kickoff, goal, "
                                 "half-time and full-time across the tournament. `/stopnotify` turns it back off.")
            return ("🌍 Done — I'll **DM** you every game's kickoffs, goals and results." if ok else
                    "🌍 You're on the all-games list, but I couldn't DM you — open your DMs (Privacy Settings → "
                    "allow DMs from server members) and I'll start sending them.")
        pl = next((p for p in (d.get("players") or []) if p.get("name", "").lower() == who.lower()), None)
        if not pl:
            names = ", ".join(p.get("name", "") for p in (d.get("players") or []))
            return ("No player called **%s**. Players: %s\nTry `/notifyme <player>` for your own teams, "
                    "or `/notifyme all` for every game." % (who or "?", names or "-"))
        cfg = load_config(); subs = cfg.get("discord_subs"); subs = subs if isinstance(subs, dict) else {}
        subs[str(uid)] = pl["name"]; cfg["discord_subs"] = subs
        cfg["discord_dm_off"] = [str(x) for x in (cfg.get("discord_dm_off") or []) if str(x) != str(uid)]   # re-enable DMs
        save_config(cfg)
        ok, _ = _bot_dm(uid, "🔔 You're linked to **%s**. I'll DM you here whenever your teams kick off, score, "
                             "reach full-time or go out. Want every game too? Run `/notifyme all`. "
                             "`/stopnotify` turns alerts off." % pl["name"])
        return ("🔔 Done — I'll **DM** you when **%s**'s teams kick off, score, reach full-time or go out. "
                "(`/notifyme all` for every game.)" % pl["name"] if ok else
                "🔔 You'll be pinged in the channel on **%s**'s events. To get them as **DMs** instead, open your "
                "DMs (Privacy Settings → allow DMs from server members) and run `/notifyme %s` again." % (pl["name"], pl["name"]))
    if name == "stopnotify":
        if not uid:
            return "Couldn't read your Discord account — run this from inside the server."
        cfg = load_config(); subs = cfg.get("discord_subs"); subs = subs if isinstance(subs, dict) else {}
        allg = cfg.get("discord_dm_all"); allg = allg if isinstance(allg, list) else []
        off = [str(x) for x in (cfg.get("discord_dm_off") or [])] if isinstance(cfg.get("discord_dm_off"), list) else []
        subs.pop(str(uid), None)
        allg = [x for x in allg if str(x) != str(uid)]
        if str(uid) not in off:
            off.append(str(uid))
        cfg["discord_subs"] = subs; cfg["discord_dm_all"] = allg; cfg["discord_dm_off"] = off; save_config(cfg)
        return ("🔕 Personal DMs are off — I won't DM you about your teams or your bets. "
                "Use `/notifyme <your name>` to switch them back on, or `/mute` to silence just one game.")
    if name in ("mute", "unmute"):
        if not uid:
            return "Couldn't read your Discord account — run this from inside the server."
        match_val = str(opts.get("match", "")).strip()
        fxall = [m for m in (d.get("fixtures") or []) if m.get("matchId")]
        m = next((x for x in fxall if str(x.get("matchId")) == match_val), None)
        if not m:
            mv = match_val.lower()
            m = next((x for x in fxall if mv and (mv in x.get("home", "").lower() or mv in x.get("away", "").lower()
                                                  or mv in ("%s v %s" % (x.get("home", ""), x.get("away", ""))).lower())), None)
        if not m:
            return "Couldn't find that game — start typing a team and pick it from the list. `/games` shows what's on."
        mid = str(m["matchId"])
        cfg = load_config(); mutes = cfg.get("discord_mutes"); mutes = mutes if isinstance(mutes, dict) else {}
        mine = [str(x) for x in (mutes.get(str(uid)) or [])]
        if name == "mute":
            if mid not in mine:
                mine.append(mid)
            mutes[str(uid)] = mine; cfg["discord_mutes"] = mutes; save_config(cfg)
            return "🔕 Muted — no DMs about **%s v %s** (kickoff, goals, full-time). `/unmute` to undo." % (m["home"], m["away"])
        mine = [x for x in mine if x != mid]
        mutes[str(uid)] = mine; cfg["discord_mutes"] = mutes; save_config(cfg)
        return "🔔 Unmuted — you'll get DMs about **%s v %s** again." % (m["home"], m["away"])
    if name == "mutes":
        if not uid:
            return "Couldn't read your Discord account — run this from inside the server."
        mutes = load_config().get("discord_mutes") or {}
        mids = [str(x) for x in (mutes.get(str(uid)) or [])]
        if not mids:
            return "You haven't muted any games. Use `/mute` to silence DMs for one specific game."
        byid = {str(m.get("matchId")): m for m in (d.get("fixtures") or [])}
        lines = []
        for mid in mids:
            mm = byid.get(mid)
            lines.append(("• %s v %s" % (mm["home"], mm["away"])) if mm else "• (a finished/unknown game)")
        return "🔕 You've muted DMs for:\n" + "\n".join(lines) + "\n`/unmute` to turn one back on."
    if name == "linkdiscord":
        if not uid:
            return "Couldn't read your Discord account — run this from inside the server."
        cfg = load_config()
        code = str(opts.get("code", "")).strip().upper()
        codes = cfg.get("wager_link_codes") if isinstance(cfg.get("wager_link_codes"), dict) else {}
        rec = codes.get(code)
        if not rec or rec.get("exp", 0) < time.time():
            return "That link code is wrong or has expired. Get a fresh one: tracker → 💷 Bets → **Connect Discord**."
        player = rec["player"]
        lk = cfg.get("wager_links"); lk = lk if isinstance(lk, dict) else {}
        holder = next((u for u, pl in lk.items() if pl == player and str(u) != str(uid)), None)
        if holder is not None:        # first-come lock (same as the web claim): another account already holds this name
            return ("**%s** is already linked to a different Discord account. If that's you, disconnect it first "
                    "(tracker → 💷 Bets → **Disconnect**) or ask the organiser to reset it — then try the code again." % player)
        subs = cfg.get("discord_subs"); subs = subs if isinstance(subs, dict) else {}
        lk[str(uid)] = player; subs[str(uid)] = player
        codes.pop(code, None)                         # single use
        cfg["wager_links"] = lk; cfg["discord_subs"] = subs; cfg["wager_link_codes"] = codes
        save_config(cfg)
        _bot_dm(uid, "👋 Welcome — your Discord is now linked to **%s**. I'll DM you when your teams kick off, "
                     "score, reach full-time or go out, and you can bet here with `/games` then `/bet` (no passcode "
                     "needed). Want every game's alerts too? Run `/notifyme all`. Turn alerts off any time with "
                     "`/stopnotify`." % player)
        return ("✅ Linked — this Discord is now **%s** for betting. Use `/games` then `/bet` (no passcode needed here). "
                "You'll also get %s's match pings." % (player, player))
    if name == "mypin":
        if not uid:
            return "Couldn't read your Discord account — run this from inside the server."
        player = (load_config().get("wager_links") or {}).get(str(uid))
        if not player:
            return "Link your account first: tracker → 💷 Bets → **Connect Discord** → `/linkdiscord code:…`."
        pin = _wager_pins().get(player)
        if not pin:
            return "No passcode is set yet — ask the organiser."
        ok, _ = _bot_dm(uid, "🔒 Your WC26 bet passcode for **%s** is **%s**. Keep it private — it's only needed on the website." % (player, pin))
        return "📩 I've sent your passcode to your DMs." if ok else ("Your passcode for **%s** (only you can see this): ||%s||" % (player, pin))
    if name == "resetpin":
        if not uid:
            return "Couldn't read your Discord account — run this from inside the server."
        cfg = load_config()
        player = (cfg.get("wager_links") or {}).get(str(uid))     # the link proves who you are — you can only reset YOUR OWN
        if not player:
            return "Link your account first: tracker → 💷 Bets → **Connect Discord** → `/linkdiscord code:…`. (Or ask the organiser to reset it.)"
        pins = cfg.get("wager_pins") if isinstance(cfg.get("wager_pins"), dict) else {}
        pins[player] = _gen_pin()
        cfg["wager_pins"] = pins
        save_config(cfg)
        log("self-reset bet passcode for", player, "via Discord")
        ok, _ = _bot_dm(uid, "🔒 New WC26 bet passcode for **%s**: **%s**. Your old one no longer works. Enter this on the website's 💷 Bets tab." % (player, pins[player]))
        return "📩 Done — your old passcode is now dead and I've DM'd you the new one." if ok else ("Your new passcode for **%s** (only you can see this): ||%s|| — the old one no longer works." % (player, pins[player]))
    if name == "unlink":
        if not uid:
            return "Couldn't read your Discord account — run this from inside the server."
        cfg = load_config()
        lk = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
        was = lk.pop(str(uid), None)
        cfg["wager_links"] = lk
        save_config(cfg)
        return ("✅ Unlinked — this Discord can no longer place bets. Re-link any time on the tracker's 💷 Bets tab."
                if was else "This Discord wasn't linked for betting.")
    if name == "points":
        if not uid:
            return "Couldn't read your Discord account — run this from inside the server."
        player = (load_config().get("wager_links") or {}).get(str(uid)) or _discord_subs().get(str(uid))
        if not player:
            return "Link your account first: tracker → 💷 Bets → **Connect Discord** → `/linkdiscord code:…`."
        pr = next((p for p in (d.get("players") or []) if p.get("name") == player), {})
        if wager_mod is None or not load_config().get("wagering_enabled"):
            return "**%s** — %s points." % (player, pr.get("points", 0))
        wl = load_wagers()
        settled = pr.get("points_settled")
        if settled is None:
            settled = round((pr.get("points", 0) or 0) - (pr.get("live", 0) or 0), 1)
        avail = wager_mod.available_points(player, settled, wl)
        dlt = wager_mod.player_deltas(wl).get(player, {})
        cap = _current_round_max_stake()
        return ("**%s** — **%g** points available to bet (max %d per bet right now).\n%g on open bets · %s total."
                % (player, avail, cap, dlt.get("pending_stake", 0), pr.get("points", 0)))
    if name == "allbets":
        if wager_mod is None or not load_config().get("wagering_enabled"):
            return "Betting isn't switched on."
        wl = load_wagers()
        ob = sorted([w for w in wl if w.get("status") == "pending"], key=lambda w: w.get("placed_at", 0))
        if not ob:
            return "No open bets right now."
        livemids = {wager_mod.match_id(m) for m in (d.get("fixtures") or [])
                    if m.get("status") in ("IN_PLAY", "PAUSED", "LIVE", "SUSPENDED")}
        out = ["**Everyone's open bets**"]
        for w in ob[:20]:
            if w.get("legs"):
                pick = "%d-fold acca" % len(w["legs"])
                isl = any(lg.get("matchId") in livemids for lg in w["legs"])
            else:
                pick = w["home"] if w["selection"] == "HOME" else (w["away"] if w["selection"] == "AWAY" else "Draw")
                isl = w.get("matchId") in livemids
            out.append("%s%s — %s @ %s · %g → %g" % ("🔴 " if isl else "", w["player"], pick, w["frac"],
                                                       w["stake"], w["return"]))
        return "\n".join(out)
    if name in ("games", "bet", "mybets", "claim"):
        cfg = load_config()
        if not cfg.get("wagering_enabled") or wager_mod is None:
            return "Betting isn't switched on for this sweepstake."
        fx = sorted([m for m in (d.get("fixtures") or []) if m.get("odds") and m.get("matchId")],
                    key=lambda m: m.get("utcDate") or "")
        if name == "games":
            if wager_mod.betting_locked(d):
                return "Betting is closed — the tournament is over."
            if not fx:
                return "No upcoming games to bet on right now — check back before kick-off."
            rows = ["**Upcoming games & odds**", "_Bet with_ `/bet team:<name> stake:<n>` _(your team to win), or add_ `pick:draw`_._"]
            if _open_free_drop():
                rows.append("🎁 _**Free betting points** are on offer right now — claim 5 with_ `/claim` _and bet them on any game (win and it's all profit; a loss costs nothing)._")
            for m in fx[:12]:
                ko = m.get("stage") and m["stage"] != "GROUP_STAGE"
                o = m["odds"]
                rows.append("**%s v %s** — H `%s` · %sA `%s`"
                            % (m["home"], m["away"], o["HOME"]["frac"],
                               ("" if ko else "D `%s` · " % o["DRAW"]["frac"]), o["AWAY"]["frac"]))
            return "\n".join(rows)
        if not uid:
            return "Couldn't read your Discord account — run this from inside the server."
        player = _discord_subs().get(str(uid))
        if not player:
            return "Link your account first with `/notifyme <your player name>`, then you can bet."
        if name == "mybets":
            wl = [w for w in load_wagers() if w["player"] == player][-15:]
            if not wl:
                return "You haven't placed any bets yet. Try `/games`, then `/bet`."
            tag = {"pending": "🟡 OPEN", "won": "🟢 WON", "lost": "🔴 LOST", "void": "⚪ VOID"}
            rows = ["**%s's bets**" % player]
            for w in reversed(wl):
                pick = w["home"] if w["selection"] == "HOME" else (w["away"] if w["selection"] == "AWAY" else "Draw")
                out = ("+%g" % round(w["return"] - w["stake"], 1)) if w["status"] == "won" else \
                      ("−%g" % w["stake"] if w["status"] == "lost" else
                       ("refunded" if w["status"] == "void" else "stake %g" % w["stake"]))
                rows.append("%s %s v %s — %s @ %s · %s"
                            % (tag.get(w["status"], ""), w["home"], w["away"], pick, w["frac"], out))
            return "\n".join(rows)
        # ---- /claim (today's free betting points) ----
        if name == "claim":
            if wager_mod.betting_locked(d):
                return "Betting is closed — the tournament is over."
            links = load_config().get("wager_links"); links = links if isinstance(links, dict) else {}
            if links.get(str(uid)) != player:
                return ("🔒 Link your Discord for betting first: open the tracker → **💷 Bets**, enter your passcode, "
                        "tap **Connect Discord**, then `/linkdiscord code:<the code>`. Then `/claim` works here.")
            drop = _open_free_drop()
            if not drop:
                return "No free points on offer right now — drops land on a handful of match-days through the tournament. I'll post here the moment one is live."
            cfgc = load_config()
            claims = cfgc.get("free_bet_claims") if isinstance(cfgc.get("free_bet_claims"), dict) else {}
            taken = claims.get(drop["id"]) if isinstance(claims.get(drop["id"]), dict) else {}
            if player in taken:
                return "You've already claimed today's free points 🎁 — the next drop is another day."
            status, res = _claim_free_drop(player, drop["id"])   # atomic: web + Discord share one lock, re-check inside
            if status == "already":
                return "You've already claimed today's free points 🎁 — the next drop is another day."
            if status == "ok":
                update_now(load_config())
                return ("🎁 **%g free betting points added to your balance!** Bet them on any game with `/bet` — "
                        "win and the winnings are yours; lose and it costs you nothing. One claim per drop." % res["amount"])
            return "❌ %s" % res
        # ---- /bet ----
        if wager_mod.betting_locked(d):
            return "Betting is closed — the tournament is over."
        # you can only bet as the player your Discord is LINKED to — and the link never exposes your passcode
        links = load_config().get("wager_links")
        links = links if isinstance(links, dict) else {}
        if links.get(str(uid)) != player:
            return ("🔒 Your Discord isn't linked for betting yet (so your passcode never has to be typed here).\n"
                    "Open the tracker → **💷 Bets**, enter your passcode, tap **Connect Discord**, then run "
                    "`/linkdiscord code:<the code it shows>` (DM me to keep it private). Then `/bet` works with no passcode.")
        match_val = str(opts.get("match", "")).strip()
        team = str(opts.get("team", "")).strip()
        result = (str(opts.get("result", "")).strip().lower() or "win")
        confirm = bool(opts.get("confirm"))
        try:
            stake = float(opts.get("stake"))
        except (TypeError, ValueError):
            return "Stake must be a number of points."
        # resolve the match: the dropdown sends the matchId; fall back to a text match on the team names
        m = next((x for x in fx if str(x.get("matchId")) == match_val), None)
        if not m:
            mv = match_val.lower()
            m = next((x for x in fx if mv and (mv in x["home"].lower() or mv in x["away"].lower()
                                               or mv in ("%s v %s" % (x["home"], x["away"])).lower())), None)
        if not m:
            return "Couldn't find that match — start typing in the **match** box and pick from the list. `/games` shows what's on."

        # ---- Over/Under total-goals bet (no team needed) ----
        if result in ("over", "under", "o", "u"):
            selection = "OVER" if result in ("over", "o") else "UNDER"
            try:
                line = float(opts.get("goals"))
            except (TypeError, ValueError):
                return "Pick a **goals** line for an over/under bet (e.g. 2.5)."
            if line not in wager_mod.OU_LINES:
                return "That goals line isn't offered — pick one from 0.5 to 8.5."
            ou = (m.get("ouOdds") or {}).get(("%g" % line))
            if not ou or selection not in ou:
                return "Couldn't price that goals line right now — try `/games`."
            o = ou[selection]
            who = "%s %g goals (%s v %s)" % ("Over" if selection == "OVER" else "Under", line, m["home"], m["away"])
            ret = wager_mod.potential_return(stake, o["num"], o["den"])
            prow = next((p for p in (d.get("players") or []) if p.get("name") == player), {})
            settled = prow.get("points_settled")
            if settled is None:
                settled = round((prow.get("points", 0) or 0) - (prow.get("live", 0) or 0), 1)
            wl_now = load_wagers()
            avail = wager_mod.available_points(player, settled, wl_now)
            round_max = _current_round_max_stake()
            held = wager_mod.player_deltas(wl_now).get(player, {}).get("pending_stake", 0.0)
            can_stake = max(0.0, round(min(avail, round_max - held), 1))
            if not confirm:
                warn = ""
                if stake > avail:
                    warn = "\n⚠ That's more than your **%g** available points." % avail
                elif stake > can_stake:
                    warn = "\n⚠ Most you can stake right now is **%g** (round cap %g, %g already riding on open bets)." % (can_stake, round_max, held)
                return ("🎲 **Bet preview** — %s v %s\nBacking **%s** at **%s**.\n"
                        "Stake **%g** → returns **%g** if it wins (profit **%g**).\n"
                        "💰 You have **%g** points · max stake this round **%g** · you can still stake **%g**.%s\n"
                        "_Bets are final once placed — odds lock in when you confirm._\n"
                        "Run the same command again with **confirm: True** to place it."
                        % (m["home"], m["away"], who, o["frac"], stake, ret, round(ret - stake, 1),
                           avail, round_max, can_stake, warn))
            try:
                results = json.load(open("results.json"))
                teams = {t["name"]: t for t in load_teams()}
            except Exception:
                return "Couldn't load the data to place that bet."
            raw = next((x for x in results.get("matches", []) if wager_mod.match_id(x) == m["matchId"]), None)
            _M = results.get("matches", [])
            ch = wager_mod.live_strength(_comp(teams, m["home"]), m["home"], _M)
            ca = wager_mod.live_strength(_comp(teams, m["away"]), m["away"], _M)
            with _lock:
                wl = load_wagers()
                dup = _dedup_wager(wl, player, interaction_id)
                if dup is not None:
                    ok, res = True, dup
                else:
                    ok, res = wager_mod.place(wl, player, raw, selection, stake, settled, ch, ca,
                                              group_mid_ts=_group_mid_ts(), market="ou", line=line)
                    if ok:
                        if interaction_id:
                            res["nonce"] = interaction_id
                        save_wagers(wl)
            if ok:
                update_now(load_config())
                _announce_bet(player, res)
                _wl2 = load_wagers()
                _left = max(0.0, round(min(wager_mod.available_points(player, settled, _wl2),
                                           round_max - wager_mod.player_deltas(_wl2).get(player, {}).get("pending_stake", 0.0)), 1))
                return ("✅ **Bet placed!** %g on **%s** @ %s — returns **%g** if it wins.\n"
                        "💰 You can still stake **%g** this round. Good luck. _(Bets are final.)_"
                        % (res["stake"], who, res["frac"], res["return"], _left))
            return "❌ %s" % res
        # validate the typed team is actually in that match (win/lose need a team; a draw doesn't)
        tl = team.lower()
        team_is_home = None
        if tl and tl == m["home"].lower():
            team_is_home = True
        elif tl and tl == m["away"].lower():
            team_is_home = False
        elif tl and tl in m["home"].lower():
            team_is_home = True
        elif tl and tl in m["away"].lower():
            team_is_home = False
        elif tl:
            return "**%s** isn't in that match (**%s** v **%s**). Type one of those two teams." % (team, m["home"], m["away"])
        if team_is_home is None and result in ("win", "lose", "w", "l", "winner", "loss", "lost"):
            return "Which team? Add **team:** (one of **%s** / **%s**) for a win/lose bet — or use **result: over/under** with a **goals** line." % (m["home"], m["away"])
        team_name = (m["home"] if team_is_home else m["away"]) if team_is_home is not None else None
        # map win / draw / lose (relative to the team you typed) to the match outcome
        if result in ("w", "winner"):
            result = "win"
        if result in ("l", "loss", "lost"):
            result = "lose"
        if result == "draw":
            selection = "DRAW"
        elif result == "win":
            selection = "HOME" if team_is_home else "AWAY"
        elif result == "lose":
            selection = "AWAY" if team_is_home else "HOME"   # they lose -> the opponent wins
        else:
            return "Pick a result: **win**, **draw**, or **lose**."
        if selection == "DRAW" and m.get("stage") and m["stage"] != "GROUP_STAGE":
            return "Knockout games can't end in a draw — back **%s** or **%s** to win (whoever goes through counts)." % (m["home"], m["away"])
        o = m["odds"][selection]
        ret = wager_mod.potential_return(stake, o["num"], o["den"])
        if result == "draw":
            who = "a draw (%s v %s)" % (m["home"], m["away"])
        elif result == "lose":
            who = "%s to lose" % team_name
        else:
            who = "%s to win" % team_name
        # points + stake-left info for the preview
        prow = next((p for p in (d.get("players") or []) if p.get("name") == player), {})
        settled = prow.get("points_settled")
        if settled is None:
            settled = round((prow.get("points", 0) or 0) - (prow.get("live", 0) or 0), 1)
        wl_now = load_wagers()
        avail = wager_mod.available_points(player, settled, wl_now)
        round_max = _current_round_max_stake()
        held = wager_mod.player_deltas(wl_now).get(player, {}).get("pending_stake", 0.0)
        can_stake = max(0.0, round(min(avail, round_max - held), 1))
        if not confirm:
            warn = ""
            if stake > avail:
                warn = "\n⚠ That's more than your **%g** available points." % avail
            elif stake > can_stake:
                warn = "\n⚠ Most you can stake right now is **%g** (round cap %g, %g already riding on open bets)." % (can_stake, round_max, held)
            return ("🎲 **Bet preview** — %s v %s\n"
                    "Backing **%s** at **%s**.\n"
                    "Stake **%g** → returns **%g** if it wins (profit **%g**).\n"
                    "💰 You have **%g** points · max stake this round **%g** · you can still stake **%g**.%s\n"
                    "_Bets are final once placed — odds lock in when you confirm._\n"
                    "Run the same command again with **confirm: True** to place it."
                    % (m["home"], m["away"], who, o["frac"], stake, ret, round(ret - stake, 1),
                       avail, round_max, can_stake, warn))
        try:
            results = json.load(open("results.json"))
            teams = {t["name"]: t for t in load_teams()}
        except Exception:
            return "Couldn't load the data to place that bet."
        raw = next((x for x in results.get("matches", []) if wager_mod.match_id(x) == m["matchId"]), None)
        _M = results.get("matches", [])
        ch = wager_mod.live_strength(_comp(teams, m["home"]), m["home"], _M)
        ca = wager_mod.live_strength(_comp(teams, m["away"]), m["away"], _M)
        with _lock:
            wl = load_wagers()
            dup = _dedup_wager(wl, player, interaction_id)     # Discord retries reuse the interaction id -> no double bet
            if dup is not None:
                ok, res = True, dup
            else:
                ok, res = wager_mod.place(wl, player, raw, selection, stake, settled, ch, ca, group_mid_ts=_group_mid_ts())
                if ok:
                    if interaction_id:
                        res["nonce"] = interaction_id
                    save_wagers(wl)
        if ok:
            update_now(load_config())
            _announce_bet(player, res)
            _wl2 = load_wagers()
            _left = max(0.0, round(min(wager_mod.available_points(player, settled, _wl2),
                                       round_max - wager_mod.player_deltas(_wl2).get(player, {}).get("pending_stake", 0.0)), 1))
            return ("✅ **Bet placed!** %g on **%s** @ %s — returns **%g** if it wins.\n"
                    "💰 You can still stake **%g** this round. Good luck. _(Bets are final.)_"
                    % (res["stake"], who, res["frac"], res["return"], _left))
        return "❌ %s" % res
    if name == "help":
        return ("**WC26 bot commands**\n"
                "/leaderboard - top of the table\n"
                "/summary - current standings digest\n"
                "/groups - all 12 group tables with owners\n"
                "/odds - who's most likely to win\n"
                "/games - upcoming games + betting odds\n"
                "/linkdiscord <code> - link your account to bet (code from the tracker's Bets tab)\n"
                "/bet match:<game> result:<win/draw/lose> team:<name> stake:<n> — back a team; or result:<over/under> goals:<line> for total goals. Shows the payout; add confirm to place. Bets are final\n"
                "/claim - claim today's FREE 5 betting points when a drop is on (bet them on any game; win = winnings yours, a loss costs nothing)\n"
                "/mybets - your open and settled bets\n"
                "/allbets - everyone's open bets right now\n"
                "/points - how many points you have to bet (and your max bet)\n"
                "/mypin - DM yourself your own bet passcode\n"
                "/resetpin - reset your own passcode if forgotten/leaked\n"
                "/unlink - disconnect your Discord from betting\n"
                "/scores - live scores and recent results\n"
                "/stats - fun stats (top team, favourite, dark horse…)\n"
                "/fixtures - live and upcoming games\n"
                "/myteams <player> - that player's teams\n"
                "/players - everyone's team counts\n"
                "/team <name> - look up one team (owner, group, points)\n"
                "/notifyme <player> - DM you on that player's teams' events\n"
                "/notifyme all - DM you every game's kickoffs, goals & results\n"
                "/mute <game> - stop DMs about one specific game (kickoff, goals, full-time); /unmute to undo; /mutes to list\n"
                "/stopnotify - turn all your personal DMs off\n"
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
    if name == "scores":
        fxa = d.get("fixtures") or []
        live = [m for m in fxa if m.get("status") in ("IN_PLAY", "PAUSED", "LIVE", "SUSPENDED")]
        done = sorted([m for m in fxa if m.get("status") in ("FINISHED", "AWARDED")],
                      key=lambda m: m.get("utcDate") or "", reverse=True)
        out = []
        if live:
            out.append("**🔴 Live now**")
            for m in live:
                out.append("%s%s %s–%s %s" % (("%s' " % m["minute"]) if m.get("minute") else "",
                           m.get("home"), m.get("homeScore", 0), m.get("awayScore", 0), m.get("away")))
        if done:
            out.append("**Recent results**")
            for m in done[:10]:
                pk = ""
                if m.get("penHome") is not None and m.get("penAway") is not None:
                    pk = " _(pens %s–%s)_" % (m.get("penHome"), m.get("penAway"))
                out.append("%s %s–%s %s%s" % (m.get("home"), m.get("homeScore", 0),
                                              m.get("awayScore", 0), m.get("away"), pk))
        return "\n".join(out) if out else "No scores yet — the tournament hasn't kicked off."
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
        {"name": "scores", "description": "Live scores and recent results", "type": 1},
        {"name": "myteams", "description": "A player's teams", "type": 1,
         "options": [{"name": "player", "description": "Player name", "type": 3, "required": True}]},
        {"name": "players", "description": "Every player and how many teams are still in", "type": 1},
        {"name": "team", "description": "Look up a team's owner, group and points", "type": 1,
         "options": [{"name": "name", "description": "Team name, e.g. Brazil", "type": 3, "required": True}]},
        {"name": "notifyme", "description": "DM you when your teams play/score/go out — or 'all' for every game", "type": 1,
         "options": [{"name": "player", "description": "Your player name, or 'all' for every game", "type": 3, "required": True}]},
        {"name": "stopnotify", "description": "Turn off your DM alerts", "type": 1},
        {"name": "mute", "description": "Stop DMs about ONE specific game (kickoff, goals, full-time)", "type": 1,
         "options": [{"name": "match", "description": "Start typing a team — pick the game to mute", "type": 3, "required": True, "autocomplete": True}]},
        {"name": "unmute", "description": "Get DMs about a game you muted again", "type": 1,
         "options": [{"name": "match", "description": "Start typing a team — pick the game to unmute", "type": 3, "required": True, "autocomplete": True}]},
        {"name": "mutes", "description": "List the games you've muted DMs for", "type": 1},
        {"name": "games", "description": "Upcoming games and their betting odds", "type": 1},
        {"name": "bet", "description": "Bet your points on a match (shows the payout; add confirm to place)", "type": 1,
         "options": [
             {"name": "match", "description": "Start typing a team or match — pick the game from the list", "type": 3, "required": True, "autocomplete": True},
             {"name": "result", "description": "win / draw / lose a team — or over / under for total goals", "type": 3, "required": True,
              "choices": [{"name": "win", "value": "win"}, {"name": "draw", "value": "draw"}, {"name": "lose", "value": "lose"},
                          {"name": "over (goals)", "value": "over"}, {"name": "under (goals)", "value": "under"}]},
             {"name": "stake", "description": "Points to stake", "type": 10, "required": True},
             {"name": "team", "description": "Which team you're backing (for win/draw/lose) — leave blank for over/under", "type": 3, "required": False},
             {"name": "goals", "description": "Goals line for over/under, e.g. 2.5", "type": 10, "required": False,
              "choices": [{"name": str(L), "value": L} for L in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5)]},
             {"name": "confirm", "description": "Set true to actually place the bet", "type": 5, "required": False}]},
        {"name": "claim", "description": "Claim today's FREE 5 betting points (when a drop is on) — bet them on any game", "type": 1},
        {"name": "mybets", "description": "Your open and settled bets", "type": 1},
        {"name": "linkdiscord", "description": "Link this Discord to your player for betting (code from the website)", "type": 1,
         "options": [{"name": "code", "description": "The code shown on the tracker's Bets tab", "type": 3, "required": True}]},
        {"name": "mypin", "description": "DM yourself your own bet passcode (once linked)", "type": 1},
        {"name": "resetpin", "description": "Reset your own bet passcode if it's forgotten or leaked (once linked)", "type": 1},
        {"name": "points", "description": "How many points you have to bet (and your max bet)", "type": 1},
        {"name": "allbets", "description": "Everyone's open bets right now", "type": 1},
        {"name": "unlink", "description": "Unlink this Discord from betting (if you linked the wrong player)", "type": 1},
    ]
    for _c in cmds:                                   # Discord rejects required options after optional ones — catch it here
        _seen_optional = False
        for _o in _c.get("options") or []:
            if not _o.get("required"):
                _seen_optional = True
            elif _seen_optional:
                return False, ("Command /%s lists a required option (%s) after an optional one — "
                               "required options must come first." % (_c["name"], _o.get("name")))
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
        return False, _discord_err(e)


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
            try:
                data = update_results.fetch(out="results.tmp.json", token=token)
                if data and data.get("matches"):
                    os.replace("results.tmp.json", "results.json")        # atomic; only swap in good data
                elif os.path.exists("results.tmp.json"):
                    os.remove("results.tmp.json")
            except Exception as e:                                        # a feed blip shouldn't block the recompute
                log("results fetch failed, keeping last-good results.json:", e)
                if os.path.exists("results.tmp.json"):
                    try:
                        os.remove("results.tmp.json")
                    except Exception:
                        pass
            if not os.path.exists("results.json"):
                return False, "feed returned no matches"
            # otherwise fall through and recompute on the last-good results.json (so bets/claims still settle + show)
        else:
            _write_pretournament(cfg.get("competition", "WC"))
        wlist = None
        if wager_mod is not None:
            newly_won = []
            with _lock:                              # guard against a concurrent place_wager losing a bet
                _w = load_wagers()
                if _w or cfg.get("wagering_enabled"):   # betting on -> pass the list (even empty) so fixtures get odds;
                    wlist = _w                          # standing bets also settle even if NEW betting was switched off
                    try:
                        _res = json.load(open("results.json"))
                        _before = {w.get("id"): w.get("status") for w in wlist}
                        if wager_mod.settle_all(wlist, _res.get("matches", [])):
                            save_wagers(wlist)
                            newly_won = [w for w in wlist if w.get("status") == "won" and _before.get(w.get("id")) != "won"]
                    except Exception as e:
                        log("wager settle failed:", e)
            if newly_won:                            # network post happens outside the lock
                try:
                    _announce_wins(newly_won)        # one grouped post per finishing batch; accas only once fully won
                except Exception as e:
                    log("win announce failed:", e)
        try:                                     # track real kickoff/half-time so the tracker clock is accurate (never fatal)
            _update_match_clocks(json.load(open("results.json")).get("matches", []))
        except Exception:
            pass
        scoring_mod.compute(out="tracker_data.json", default_mode=cfg.get("scoring_mode", "hybrid"), wagers=wlist, group_mid_ts=_group_mid_ts(), composite_overrides=(_load_calibration().get("composites") or None))
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


def _odds_book_overround(decimals):
    """Sum of implied probabilities across a market's decimal prices. >1.0 = a house edge."""
    s = 0.0
    for d in decimals:
        try:
            d = float(d)
        except (TypeError, ValueError):
            return None
        if d > 1.0:
            s += 1.0 / d
    return s if s > 0 else None


def _odds_integrity_violations(td, teams):
    """READ-ONLY safety guard: every UPCOMING match's offered 1X2 + Over/Under books must carry a
    house edge (overround > 100%). Returns a list of human strings for any that don't. This should
    always be empty (the goals book filters underround lines and 1X2 carries a fixed margin) — it's a
    monitor so a money market can never silently go bettor-positive. Never raises."""
    out = []
    if wager_mod is None:
        return out
    try:
        for f in (td.get("fixtures") or []):
            if not isinstance(f, dict):
                continue
            if f.get("status") not in ("TIMED", "SCHEDULED"):
                continue
            h, a = f.get("home"), f.get("away")
            if h not in teams or a not in teams:
                continue
            ch = wager_mod.live_strength((teams.get(h) or {}).get("composite", 0), h, td.get("fixtures") or [])
            ca = wager_mod.live_strength((teams.get(a) or {}).get("composite", 0), a, td.get("fixtures") or [])
            mo = wager_mod.match_odds(ch, ca)
            b1 = _odds_book_overround([mo["HOME"]["decimal"], mo["DRAW"]["decimal"], mo["AWAY"]["decimal"]])
            if b1 is not None and b1 <= 1.0:
                out.append("%s v %s — 1X2 book %.1f%%" % (h, a, b1 * 100))
            for ln, leg in (wager_mod.goals_odds(ch, ca) or {}).items():
                bo = _odds_book_overround([leg["OVER"]["decimal"], leg["UNDER"]["decimal"]])
                if bo is not None and bo <= 1.0:
                    out.append("%s v %s — O/U %s book %.1f%%" % (h, a, ln, bo * 100))
    except Exception as e:
        log("odds integrity check error (non-fatal):", e)
    return out


def _maybe_matchday_audit(cfg):
    """AUTO, READ-ONLY: once a whole UTC matchday has finished, log a calibration + house-edge summary
    (and post it to Discord if `odds_audit_discord` is on). Idempotent via last_audited_matchday. It
    NEVER changes odds, composites or bets — it only reports, with the integrity guard as a safety net.
    Fully defensive: a bad cycle can never disturb scoring/settlement/betting."""
    try:
        if not draw_locked() or wager_mod is None:
            return
        td = _load_tracker() or {}
        fx = td.get("fixtures") or []
        if not fx:
            return
        # group finished-with-scores games by UTC date; find the latest date that's FULLY done
        by_day = {}
        for m in fx:
            d = (m.get("utcDate") or "")[:10]
            if d:
                by_day.setdefault(d, []).append(m)
        done_days = []
        for d, games in by_day.items():
            if games and all(g.get("status") in ("FINISHED", "AWARDED") for g in games):
                done_days.append(d)
        if not done_days:
            return
        day = max(done_days)
        if cfg.get("last_audited_matchday") == day:
            return                                  # already reported this matchday
        games = [g for g in by_day[day] if isinstance(g.get("homeScore"), (int, float))
                 and isinstance(g.get("awayScore"), (int, float))]
        if not games:
            return
        try:
            teams = {t["name"]: t for t in load_teams()}
        except Exception:
            teams = {}
        n = ll = fav_hits = draws = overs = goals = 0
        for g in games:
            h, a = g.get("home"), g.get("away")
            hs, as_ = int(g["homeScore"]), int(g["awayScore"])
            ch = (teams.get(h) or {}).get("composite", 0) or 0
            ca = (teams.get(a) or {}).get("composite", 0) or 0
            ph, pd, pa = wager_mod._fair_probs(ch, ca)
            lam = wager_mod.expected_goals(ch, ca)
            res = "H" if hs > as_ else ("A" if as_ > hs else "D")
            p_act = {"H": ph, "D": pd, "A": pa}[res]
            ll += -math.log(max(1e-9, p_act))
            fav = max((("H", ph), ("D", pd), ("A", pa)), key=lambda kv: kv[1])[0]
            fav_hits += (fav == res); draws += (res == "D"); overs += ((hs + as_) > 2.5); goals += hs + as_
            n += 1
        if n == 0:
            return
        violations = _odds_integrity_violations(td, teams)
        line = ("📊 Matchday %s audit — %d games · favourites %d/%d · draws %d · Over2.5 %d/%d · "
                "avg goals %.2f · 1X2 log-loss %.2f · house-edge %s"
                % (day, n, fav_hits, n, draws, overs, n, goals / n, ll / n,
                   ("OK ✓" if not violations else ("⚠ %d NEGATIVE-EDGE MARKET(S)!" % len(violations)))))
        log(line)
        if violations:
            log("  integrity violations:", "; ".join(violations[:10]))
        if cfg.get("odds_audit_discord") and (cfg.get("discord_webhook") or "").startswith("https://"):
            msg = line
            if violations:
                msg += "\n⚠ " + "; ".join(violations[:8])
            discord_send(msg)
        cfg["last_audited_matchday"] = day          # config-only write (idempotency); never touches bets/odds
        save_config(cfg)
    except Exception as e:
        log("matchday audit error (non-fatal):", e)


def _ko_ts(m):
    try:
        return calendar.timegm(time.strptime((m.get("utcDate") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def _implied_composite(target_p, opp_comp, side):
    """Bisect the composite that makes the 1X2 model give `target_p` to `side` (opponent fixed). Monotone -> exact."""
    lo, hi = 1.0, 105.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        ph, pd, pa = wager_mod._fair_probs(mid, opp_comp) if side == "home" else wager_mod._fair_probs(opp_comp, mid)
        p = ph if side == "home" else pa
        if p < target_p:
            lo = mid
        else:
            hi = mid
    return max(1.0, min(105.0, (lo + hi) / 2.0))


def _market_implied_lambda(p_over25):
    """Goal expectation implied by a market Over-2.5 probability (invert the Poisson tail). Clamped to a sane band."""
    lo, hi, tgt = 0.3, 6.0, 1.0 - p_over25            # cdf(2, lam) decreases in lam
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if wager_mod._poisson_cdf(2, mid) > tgt:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _fetch_calibration_market(cfg):
    """Pull h2h + totals from the-odds-api for calibration (median across books, with a book COUNT per game so we
    can skip thin markets). Build-only here; returns None on any failure so a bad fetch never changes odds."""
    key = cfg.get("odds_api_key")
    if not key:
        return None
    regions = cfg.get("odds_api_regions", "uk")
    url = ("https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds/"
           "?apiKey=%s&regions=%s&markets=h2h,totals&oddsFormat=decimal" % (urllib.parse.quote(key), regions))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log("calibration market fetch failed (no change):", e)
        return None
    out = {}
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
    for ev in (data or []):
        home, away = ev.get("home_team"), ev.get("away_team")
        if not home or not away:
            continue
        hL, dL, aL, oL, uL, books = [], [], [], [], [], set()
        for bk in ev.get("bookmakers", []):
            books.add(bk.get("key"))
            for mk in bk.get("markets", []):
                if mk.get("key") == "h2h":
                    for oc in mk.get("outcomes", []):
                        nm, pr = oc.get("name"), oc.get("price")
                        if nm == home: hL.append(pr)
                        elif nm == away: aL.append(pr)
                        elif (nm or "").lower() == "draw": dL.append(pr)
                elif mk.get("key") == "totals":
                    for oc in mk.get("outcomes", []):
                        if oc.get("point") == 2.5:
                            if (oc.get("name") or "").lower() == "over": oL.append(oc.get("price"))
                            elif (oc.get("name") or "").lower() == "under": uL.append(oc.get("price"))
        rec = {"books": len(books)}
        if hL and aL: rec["h2h"] = {"home": med(hL), "draw": med(dL), "away": med(aL)}
        if oL and uL: rec["totals"] = {"line": 2.5, "over": med(oL), "under": med(uL)}
        if rec.get("h2h") or rec.get("totals"):
            out["%s v %s" % (home, away)] = rec
    return out


def _maybe_auto_calibrate(cfg, market=None):
    """AUTO odds calibration toward the bookmaker market, once per finished matchday. Writes ONLY a per-instance
    calibration overlay (composites + goals base) — never the tracked teams.json, never a bet. Off unless
    `auto_calibrate` is set. Guards: market-coverage floor, clean name resolution, freeze teams in live/imminent
    games, bounded + clamped moves, finite checks, and an INTEGRITY ABORT that simulates the proposed odds and
    bails (no change) if anything would underround. Fully defensive; a bad cycle can never disturb betting."""
    try:
        if not cfg.get("auto_calibrate"):
            return                                       # master kill switch (default off)
        if wager_mod is None or not draw_locked():
            return
        td = _load_tracker() or {}
        fx = td.get("fixtures") or []
        if not fx:
            return
        by_day = {}
        for m in fx:
            d = (m.get("utcDate") or "")[:10]
            if d:
                by_day.setdefault(d, []).append(m)
        done = [d for d, g in by_day.items() if g and all(x.get("status") in ("FINISHED", "AWARDED") for x in g)]
        if not done:
            return
        day = max(done)
        if _load_calibration().get("last_calibrated_matchday") == day:
            return                                       # already calibrated for this matchday (idempotent)

        mkt = market if market is not None else _fetch_calibration_market(cfg)
        if not mkt:
            log("auto-calibrate: no market available; skipping (no change).")
            return

        teams = {t["name"]: t for t in load_teams()}     # CURRENT effective composites (overlay already applied)
        try:
            import update_results as _UR
            resolve = _UR.build_name_map("teams.json")
        except Exception:
            resolve = lambda n: n

        max_step = abs(float(cfg.get("calibration_max_step", 5.0) or 5.0))
        min_books = int(cfg.get("calibration_min_books", 3) or 3)
        freeze_min = abs(float(cfg.get("calibration_freeze_min", 30.0) or 30.0))
        goals_step = abs(float(cfg.get("calibration_goals_step", 0.1) or 0.1))

        now = time.time()
        frozen = set()                                   # don't move a team that's playing now or kicks off soon
        for m in fx:
            ko = _ko_ts(m)
            live = m.get("status") in ("IN_PLAY", "PAUSED", "LIVE")
            soon = ko is not None and 0 <= (ko - now) <= freeze_min * 60
            if live or soon:
                frozen.add(resolve(m.get("home", "")))
                frozen.add(resolve(m.get("away", "")))

        implied = {}
        glam_mkt, glam_model = [], []
        for keyk, rec in mkt.items():
            if not isinstance(rec, dict):
                continue
            try:
                _books = int(rec.get("books") or 0)
            except (TypeError, ValueError):
                _books = 0
            if _books < min_books:
                continue                                 # coverage guard: ignore thin / malformed-book markets
            if " v " not in keyk:
                continue
            hn, an = [x.strip() for x in keyk.split(" v ", 1)]
            hn, an = resolve(hn), resolve(an)
            if hn not in teams or an not in teams:
                continue                                 # never invent a strength for an unmatched name
            ch, ca = teams[hn]["composite"], teams[an]["composite"]
            h2h = rec.get("h2h") or {}
            book = _odds_book_overround([h2h.get("home"), h2h.get("draw"), h2h.get("away")])
            if book:
                try:
                    mph, mpa = (1.0 / h2h["home"]) / book, (1.0 / h2h["away"]) / book
                    if hn not in frozen:
                        implied.setdefault(hn, []).append(_implied_composite(mph, ca, "home"))
                    if an not in frozen:
                        implied.setdefault(an, []).append(_implied_composite(mpa, ch, "away"))
                except (TypeError, ZeroDivisionError, KeyError):
                    pass
            tot = rec.get("totals") or {}
            tb = _odds_book_overround([tot.get("over"), tot.get("under")])
            if tb:
                try:
                    p_over = (1.0 / tot["over"]) / tb
                    glam_mkt.append(_market_implied_lambda(p_over))
                    glam_model.append(wager_mod.expected_goals(ch, ca))
                except (TypeError, ZeroDivisionError, KeyError):
                    pass

        cal = _load_calibration()
        co = dict(cal.get("composites") or {})
        changes = []
        for name, vals in implied.items():
            cur = teams[name]["composite"]
            target = sum(vals) / len(vals)
            if target != target:                         # NaN guard
                continue
            step = max(-max_step, min(max_step, target - cur))
            new = round(max(1.0, min(105.0, cur + step)), 1)
            if abs(new - cur) >= 0.1:
                co[name] = new
                changes.append((name, round(cur, 1), new))

        gb_old = _calibrated_goals_base()
        gb_new = gb_old
        if glam_mkt and glam_model:
            diff = (sum(glam_mkt) / len(glam_mkt)) - (sum(glam_model) / len(glam_model))
            if diff == diff:
                gstep = max(-goals_step, min(goals_step, diff))
                gb_new = round(max(GOALS_BASE_MIN, min(GOALS_BASE_MAX, gb_old + gstep)), 3)

        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if not changes and abs(gb_new - gb_old) < 1e-9:
            _atomic_write_json(CALIBRATION_FILE, {**cal, "last_calibrated_matchday": day})  # mark day; nothing else moved
            return

        # INTEGRITY ABORT — simulate every upcoming market with the proposed composites + goals base; bail on any underround
        sim = {k: dict(v) for k, v in teams.items()}
        for name, _, new in changes:
            if name in sim:
                sim[name]["composite"] = new
        _gb_save = getattr(wager_mod, "GOALS_BASE", 2.6)
        try:
            wager_mod.GOALS_BASE = gb_new
            viol = _odds_integrity_violations(td, sim)
        finally:
            wager_mod.GOALS_BASE = _gb_save
        if viol:
            log("auto-calibrate ABORTED — proposed odds would underround (no change):", "; ".join(viol[:6]))
            return

        hist = (cal.get("history") or [])
        hist.append({"ts": iso, "day": day, "changes": changes, "goals_base": [gb_old, gb_new]})
        _atomic_write_json(CALIBRATION_FILE, {
            "composites": co, "goals_base": gb_new, "updated": iso,
            "last_calibrated_matchday": day, "history": hist[-50:]})
        _apply_goals_base()                              # push the new goals base into the live engine
        line = ("🎯 Auto-calibration %s — %d team(s) nudged toward market; goals base %.2f→%.2f"
                % (day, len(changes), gb_old, gb_new))
        log(line)
        if changes:
            log("  " + "; ".join("%s %.1f→%.1f" % (n, o, nw) for n, o, nw in changes[:12]))
        if cfg.get("odds_audit_discord") and (cfg.get("discord_webhook") or "").startswith("https://"):
            detail = ("\n" + "; ".join("%s %.1f→%.1f" % (n, o, nw) for n, o, nw in changes[:10])) if changes else ""
            discord_send(line + detail)
    except Exception as e:
        log("auto-calibrate error (non-fatal):", e)


def maybe_send_daily_digest(cfg):
    """Post the summary (+ today's games) to Discord and push each player their fixtures, once per day
    at/after the configured UTC hour. Idempotent: a persisted last_digest_date stops a restart double-posting."""
    if not (cfg.get("digest_enabled") and draw_locked()):
        return
    if not (cfg.get("discord_webhook") or push_enabled()):
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
    d = _load_tracker()
    lines = build_summary()
    if not lines:
        return
    if cfg.get("discord_webhook"):
        discord_send("\n".join(lines + build_day_lines(d)))
    if push_enabled() and d:
        push_day_fixtures(d)                       # each player gets their own fixtures-today push
    cfg["last_digest_date"] = today
    save_config(cfg)
    log("daily digest sent for", today)


def poller():
    while True:
        try:
            cfg = load_config()
            mins = cfg.get("poll_minutes", 10)
            if cfg.get("players") and cfg.get("token") and os.path.exists("draw_result.json"):
                try:
                    with _lock:
                        ok, err = update_now(cfg)
                    if not ok:
                        print("[poller] update failed:", err)
                except Exception as e:
                    print("[poller] update raised:", e)        # never let one bad cycle kill the polling thread
            try:
                maybe_send_daily_digest(load_config())
            except Exception as e:
                print("[digest] error:", e)
            try:
                _maybe_matchday_audit(load_config())          # read-only odds audit + house-edge guard, once per finished matchday
            except Exception as e:
                print("[odds-audit] error:", e)
            try:
                _maybe_auto_calibrate(load_config())          # auto-nudge odds toward market (off unless auto_calibrate set)
            except Exception as e:
                print("[auto-calibrate] error:", e)
            try:
                _maybe_announce_free_drop(load_config())
            except Exception as e:
                print("[freebet] error:", e)
            try:
                backup_snapshot()                 # timestamped rollback history, ~every 6h
            except Exception as e:
                print("[backup] snapshot error:", e)
        except Exception as e:
            print("[poller] loop error (continuing):", e)       # the thread keeps running no matter what
            mins = 10
        # Adaptive cadence. Games often kick off a minute or two LATE, and the feed lags flipping TIMED -> IN_PLAY,
        # so the "about to start" window runs from 15 min BEFORE the scheduled kickoff to 15 min AFTER — and while
        # we're in that window we poll every 30s so a late start (and its first goals) shows up promptly. A game
        # that's genuinely live polls every 60s; otherwise we back off to poll_minutes.
        try:
            fxs = (_load_tracker() or {}).get("fixtures") or []
            now_ts = time.time()
            live = any((m.get("status") in ("IN_PLAY", "PAUSED", "LIVE", "SUSPENDED")) for m in fxs)
            near_ko = False
            for m in fxs:
                if m.get("status") in ("TIMED", "SCHEDULED"):
                    try:
                        ko = calendar.timegm(time.strptime((m.get("utcDate") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
                    except Exception:
                        continue
                    if -15 * 60 <= (ko - now_ts) <= 15 * 60:    # within 15 min either side of the scheduled kickoff
                        near_ko = True
                        break
            if near_ko:
                time.sleep(30)        # poll hard right around kickoff — catch the real (often late) whistle fast
                continue
            if live:
                time.sleep(60)
                continue
        except Exception:
            pass
        time.sleep(max(60, (mins if isinstance(mins, (int, float)) else 10) * 60))


def _maybe_announce_free_drop(cfg):
    """Post once to Discord when a free-points drop opens (idempotent via a stored marker)."""
    if not (cfg.get("wagering_enabled") and cfg.get("discord_webhook")) or wager_mod is None:
        return
    drop = _open_free_drop()
    if not drop or cfg.get("free_bet_announced") == drop["id"]:
        return
    discord_send("🎁 **Free points!** Everyone can claim **%d free betting points** today — `/claim` here, or on the "
                 "tracker's 💷 Bets tab. Bet them on any game: win and the winnings are yours; lose and it costs you nothing. "
                 "One claim per person, today only." % wager_mod.FREE_BET_STAKE)
    cfg["free_bet_announced"] = drop["id"]
    save_config(cfg)


def _team_brief(t):
    return {"name": t["name"], "tier": t["tier"], "group": t["group"],
            "composite": t.get("composite", 0), "confederation": t.get("confederation", "?")}


def compute_assignment(mode, players, t1_cap=None, leftover="pool", seed=None):
    """Return ({player: [team dicts]}, bonus_pool list). 'fair' is ported from the wheel."""
    teams = json.load(open("teams.json"))["teams"]
    n = len(players)
    per_player = len(teams) // n
    rng = random.Random(seed)
    if mode == "fair":
        # Round 1 is strict — everyone is guaranteed a favourite. After that the draw is loose (better teams
        # just more likely). We then re-draw until BOTH hold: squads are balanced in strength (which keeps the
        # pre-tournament forecast fair) and no player is below a fair floor of champion odds. If no draw clears
        # both inside the attempt budget we keep the most balanced one found.
        J = 0.3
        ranked = sorted(teams, key=lambda t: -t.get("composite", 0))
        eq = 100.0 / n
        _ti = sum(t.get("implied_prob", 0) for t in teams)
        if _ti > 0:
            champ = {t["name"]: 100.0 * t.get("implied_prob", 0) / _ti for t in teams}   # champion odds %
        else:
            champ = {t["name"]: eq for t in teams}
        _tc = sum(t.get("composite", 0) for t in teams) or 1.0
        strength = {t["name"]: 100.0 * t.get("composite", 0) / _tc for t in teams}        # squad-strength share %
        champ_floor = 0.75 * eq                             # 15% on 5 players — hard champion-odds floor
        str_floor, str_cap = 0.92 * eq, 1.10 * eq           # squads within ~10% of an equal share -> fair forecast

        def _one_draw():
            top = ranked[:n][:]                             # the favourites: strict first band, one guaranteed per player
            rng.shuffle(top)
            rest = sorted(ranked[n:], key=lambda t: -(t.get("composite", 0) * (1 + (rng.random() * 2 - 1) * J)))
            bands = [top] + [rest[i * n:(i + 1) * n] for i in range(per_player - 1)]
            a = {p: [] for p in players}
            for b_idx, band in enumerate(bands):
                band = band[:]
                rng.shuffle(band)
                seq = players if b_idx % 2 == 0 else players[::-1]
                for i, p in enumerate(seq):
                    a[p].append(_team_brief(band[i]))
            return a

        best, best_score = None, -1e9
        for _ in range(500):
            a = _one_draw()
            cmin = min(sum(champ.get(t["name"], 0) for t in a[p]) for p in players)
            ss = [sum(strength.get(t["name"], 0) for t in a[p]) for p in players]
            smin, smax = min(ss), max(ss)
            balanced = smin >= str_floor and smax <= str_cap
            if cmin >= champ_floor and balanced:
                best = a
                break
            score = cmin - (smax - smin) - (0 if balanced else 5)   # prefer high champ floor + tight strength spread
            if score > best_score:
                best, best_score = a, score
        return best, []
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
    def _cookie(self, name):
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return None

    def _session_discord_id(self):
        return _read_session(self._cookie("wc26_sess"))

    def _session_player(self):
        did = self._session_discord_id()
        if not did:
            return None
        return (load_config().get("wager_links") or {}).get(str(did))

    def _authed_as(self, player, body):
        """A bet is authorised by EITHER a logged-in Discord session for this player OR the right passcode."""
        if player and self._session_player() == player:
            return True
        return _pin_ok(player, (body or {}).get("pin"))

    def _send(self, code, body, ctype="application/json", extra_headers=None):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
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
        if path in RECORD_PATHS:
            try:
                record_access(_client_ip(self), path, self.headers.get("User-Agent"))
            except Exception:
                pass
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
        if path == "/join":                        # stable public link -> always 302 to the CURRENT saved invite,
            inv = load_config().get("discord_invite") or ""   # so a rotated/expired discord.gg never breaks the shared URL
            if inv:
                self.send_response(302); self.send_header("Location", inv); self.end_headers(); return
            self.send_response(302); self.send_header("Location", "/tracker?join=none"); self.end_headers(); return
        if path == "/tracker": return self._file("tracker.html")
        if path == "/wheel":   return self._file("wheel.html")
        if path == "/me":      return self._file("me.html")
        if path == "/watch":   return self._file("watch.html")
        if path == "/api/whoami":                  # who is this browser logged in as (Discord), if anyone
            did = self._session_discord_id()
            if not did:
                return self._send(200, json.dumps({"logged_in": False, "oauth": _oauth_enabled()}))
            player = (load_config().get("wager_links") or {}).get(str(did))
            return self._send(200, json.dumps({"logged_in": True, "player": player, "oauth": True}))
        if path == "/api/discord_login":           # start "Log in with Discord" (302 to Discord's consent screen)
            if not _oauth_enabled():
                self.send_response(302); self.send_header("Location", "/tracker"); self.end_headers(); return
            cfg = load_config()
            redirect = (cfg.get("site_url") or ("https://" + (self.headers.get("Host") or ""))).rstrip("/") + "/api/discord_oauth_callback"
            state = secrets.token_hex(16)
            qs = urllib.parse.urlencode({"client_id": cfg.get("discord_oauth_client_id"), "redirect_uri": redirect,
                                         "response_type": "code", "scope": "identify", "state": state, "prompt": "consent"})
            self.send_response(302)
            self.send_header("Location", "https://discord.com/api/oauth2/authorize?" + qs)
            self.send_header("Set-Cookie", "wc26_ostate=%s; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax" % state)
            self.end_headers(); return
        if path == "/api/discord_oauth_callback":  # Discord sends the user back here with ?code=&state=
            if not _oauth_enabled():
                self.send_response(302); self.send_header("Location", "/tracker"); self.end_headers(); return
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            code = (q.get("code") or [""])[0]; state = (q.get("state") or [""])[0]
            if not code or not state or state != (self._cookie("wc26_ostate") or "\x00"):
                self.send_response(302); self.send_header("Location", "/tracker?login=failed"); self.end_headers(); return
            cfg = load_config()
            redirect = (cfg.get("site_url") or ("https://" + (self.headers.get("Host") or ""))).rstrip("/") + "/api/discord_oauth_callback"
            did = None
            try:                                   # exchange the code for a token, then read the user's Discord id
                data = urllib.parse.urlencode({"client_id": cfg.get("discord_oauth_client_id"),
                                               "client_secret": cfg.get("discord_oauth_client_secret"),
                                               "grant_type": "authorization_code", "code": code, "redirect_uri": redirect}).encode()
                tok = json.loads(urllib.request.urlopen(urllib.request.Request(
                    "https://discord.com/api/oauth2/token", data, {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "wc26-sweepstake (https://github.com/ElzYH/wc26-sweepstake, 1.0)"}), timeout=10).read())
                me = json.loads(urllib.request.urlopen(urllib.request.Request(
                    "https://discord.com/api/users/@me", headers={"Authorization": "Bearer " + str(tok.get("access_token")), "User-Agent": "wc26-sweepstake (https://github.com/ElzYH/wc26-sweepstake, 1.0)"}), timeout=10).read())
                did = str(me.get("id") or "")
            except Exception as e:
                log("discord oauth callback error:", e)
            if not did:
                self.send_response(302); self.send_header("Location", "/tracker?login=failed"); self.end_headers(); return
            log("discord login ok for", did)
            self.send_response(302); self.send_header("Location", "/tracker?login=ok")
            self.send_header("Set-Cookie", "wc26_sess=%s; Path=/; Max-Age=%d; HttpOnly; Secure; SameSite=Lax" % (_make_session(did), 30 * 86400))
            self.send_header("Set-Cookie", "wc26_ostate=; Path=/; Max-Age=0")
            self.end_headers(); return
        if path == "/api/live_state": return self._send(200, json.dumps(live_load()))
        if path == "/api/draw_result": return self._file("draw_result.json")
        if path == "/api/summary":
            return self._send(200, json.dumps({"ok": True, "lines": build_summary()}))
        if path == "/api/export.csv":
            d = _load_tracker() or {}
            import io
            import csv
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["Player", "Points", "Survival", "Both", "Teams alive", "Total teams"])
            for p in (d.get("players") or []):
                w.writerow([p.get("name"), p.get("points", 0), p.get("survival", 0), p.get("hybrid", 0),
                            p.get("alive_teams", 0), p.get("total_teams", 0)])
            w.writerow([])
            w.writerow(["Player", "Team", "Group", "Status", "Points", "Survival", "Goals for", "Goals against"])
            for p in (d.get("players") or []):
                for t in (p.get("teams") or []):
                    w.writerow([p.get("name"), t.get("name"), t.get("group"), t.get("status"),
                                t.get("points", 0), t.get("survival", 0), t.get("gf", 0), t.get("ga", 0)])
            return self._send(200, buf.getvalue(), "text/csv",
                              extra_headers={"Content-Disposition": "attachment; filename=wc26-sweepstake.csv"})
        if path == "/api/telegram_links":          # OPEN read: players self-subscribe, no admin key
            cfg = load_config()
            players = [(p if isinstance(p, str) else p.get("name", "")) for p in cfg.get("players", [])]
            subs = _load_subs()
            return self._send(200, json.dumps({"ok": True,
                "configured": bool(_tg_token()), "bot_username": bot_username(),
                "players": [{"name": nm, "code": "p%d" % i, "subscribed": len(subs.get(nm, []))}
                            for i, nm in enumerate(players)]}))
        if path == "/api/wagers":                  # OPEN read: list bets (optionally ?player=Name)
            who = (self.path.split("player=", 1)[1].split("&")[0] if "player=" in self.path else "").strip()
            try:
                who = urllib.parse.unquote_plus(who)
            except Exception:
                pass
            wl = load_wagers()
            if who:
                wl = [w for w in wl if w.get("player") == who]
            return self._send(200, json.dumps({"ok": True, "wagers": wl[-200:]}))
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
                "wagering_enabled": bool(cfg.get("wagering_enabled")) and wager_mod is not None,
                "wager_locked": bool(cfg.get("wager_locked")),
                "wager_pins_set": bool(_wager_pins()),
                "wager_pins_for": sorted(_wager_pins().keys()),
                "discord_oauth": _oauth_enabled(),
                "discord_guild_gate": _guild_gate_on(cfg),
                "game_channel_alerts": load_config().get("game_channel_alerts", True) is not False,
                "wager_budget": (wager_mod.STAGE_BUDGET if wager_mod is not None else None),
                "free_bet": ((lambda dr: {"open": True, "id": dr["id"], "closes": dr["closes"], "stake": wager_mod.FREE_BET_STAKE,
                                          "claimed": sorted((cfg.get("free_bet_claims", {}).get(dr["id"]) or {}).keys())}
                              if dr else {"open": False})(_open_free_drop())
                             if (cfg.get("wagering_enabled") and wager_mod is not None) else {"open": False}),
                "group_mid": (_group_mid_ts() if wager_mod is not None else None),
                "stage_caps": (_stage_schedule() if wager_mod is not None else None),
                "wager_caps": (_apply_wager_caps(cfg) or {"min_stake": wager_mod.MIN_STAKE, "max_stake": _current_round_max_stake(),
                                "base_max_stake": wager_mod.MAX_STAKE, "max_return": wager_mod.MAX_RETURN,
                                "max_pending": wager_mod.MAX_PENDING, "max_acca_legs": wager_mod.MAX_ACCA_LEGS}
                               if wager_mod is not None else None),
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
        if not _verify_ed25519(pub, sig, ts.encode() + raw):
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
            uid = ((body.get("member") or {}).get("user") or {}).get("id") or (body.get("user") or {}).get("id")
            try:
                content = discord_command(data.get("name"), opts, uid, body.get("id"))
            except Exception as e:
                log("discord command error:", e)
                content = "Something went wrong building that."
            resp = {"content": (content or "—")[:1900]}
            if data.get("name") in ("bet", "claim", "mybets", "linkdiscord", "mypin", "unlink", "points"):    # private: passcodes, codes, personal bets/points
                resp["flags"] = 64
            return self._send(200, json.dumps({"type": 4, "data": resp}))
        if body.get("type") == 4:                          # APPLICATION_COMMAND_AUTOCOMPLETE
            data = body.get("data") or {}
            focused = ""
            focused_name = ""
            for o in (data.get("options") or []):
                if o.get("focused"):
                    focused = o.get("value") or ""
                    focused_name = o.get("name") or ""
                    break
            choices = _bet_match_choices(focused) if (data.get("name") in ("bet", "mute", "unmute") and focused_name == "match") else []
            return self._send(200, json.dumps({"type": 8, "data": {"choices": choices[:25]}}))
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
                discord_send_lines(build_draw_announcement())     # round-by-round + final squads to Discord
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
                w = re.sub(r"/(slack|github)/?$", "", w)        # a /slack or /github webhook 400s our normal posts — use the plain one
                w = w.split("?")[0].rstrip("/")                 # drop query string / trailing slash
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
            if "game_channel_alerts" in body:        # admin kill switch for the communal channel feed (DMs still go out)
                cfg["game_channel_alerts"] = bool(body["game_channel_alerts"])
            if "wagering_enabled" in body:
                cfg["wagering_enabled"] = bool(body["wagering_enabled"])
            if "wager_locked" in body:
                cfg["wager_locked"] = bool(body["wager_locked"])
            if "max_return" in body:                 # admin: cap on winnings per bet; blank/0 = no limit
                v = str(body["max_return"]).strip()
                if v in ("", "0", "none", "None"):
                    cfg["max_return"] = None
                else:
                    try:
                        cfg["max_return"] = max(1.0, float(v))
                    except (TypeError, ValueError):
                        pass
            if "max_acca_legs" in body:              # admin: how many legs an accumulator may have (default 3)
                try:
                    cfg["max_acca_legs"] = max(2, min(10, int(body["max_acca_legs"])))
                except (TypeError, ValueError):
                    pass
            if "max_pending_bets" in body:           # admin: most open single bets per player (blank = default)
                v = str(body["max_pending_bets"]).strip()
                if v in ("", "0", "none", "None"):
                    cfg.pop("max_pending_bets", None)
                else:
                    try:
                        cfg["max_pending_bets"] = max(1, min(50, int(float(v))))
                    except (TypeError, ValueError):
                        pass
            if "max_active_accas" in body:           # admin: most open accumulators per player (blank = default, 0 = accas off)
                v = str(body["max_active_accas"]).strip()
                if v in ("", "none", "None"):
                    cfg.pop("max_active_accas", None)
                else:
                    try:
                        cfg["max_active_accas"] = max(0, min(20, int(float(v))))
                    except (TypeError, ValueError):
                        pass
            if "digest_hour" in body:
                try:
                    cfg["digest_hour"] = max(0, min(23, int(body["digest_hour"])))
                except (TypeError, ValueError):
                    pass
            for f in ("discord_app_id", "discord_guild_id", "discord_pubkey", "discord_bot_token"):
                if f in body:
                    val = str(body[f]).strip()[:120]
                    if f in ("discord_app_id", "discord_guild_id") and val and not val.isdigit():
                        label = "Application ID" if f == "discord_app_id" else "Server (Guild) ID"
                        hint = " (that looks like the Public Key — it has letters; the Application ID is all digits, on the General Information page)" if f == "discord_app_id" else " (right-click your server → Copy Server ID)"
                        return self._send(400, json.dumps({"ok": False, "error": "%s must be all digits%s." % (label, hint)}))
                    cfg[f] = val
            if body.get("vapid_sub"):
                cfg["vapid_sub"] = str(body["vapid_sub"]).strip()[:120]
            with _lock:
                save_config(cfg)
                _apply_wager_caps(cfg)             # admin return/acca limits take effect immediately
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
                      "results": (json.load(open("results.json")) if os.path.exists("results.json") else None),
                      "wagers": load_wagers()}
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
                          "poll_minutes", "leftover", "max_per_player", "t1_cap",
                          "max_return", "max_acca_legs", "max_pending_bets", "max_active_accas"):
                    if k in b["config"]:
                        cfg[k] = b["config"][k]
            with _lock:
                backup_draw()                       # snapshot current state before replacing it
                json.dump(b["draw_result"], open("draw_result.json", "w"), indent=2)
                if isinstance(b.get("results"), dict):
                    json.dump(b["results"], open("results.json", "w"), indent=2)
                elif not os.path.exists("results.json"):
                    _write_pretournament(cfg.get("competition", "WC"))
                if isinstance(b.get("wagers"), list):
                    save_wagers(b["wagers"])
                save_config(cfg)
                wl = load_wagers() or None             # standing bets always count, even if NEW betting is switched off
                try:                                 # rebuild the tracker from the restored data (no network needed)
                    scoring_mod.compute(out="tracker_data.json",
                                        default_mode=cfg.get("scoring_mode", "hybrid"), wagers=wl, group_mid_ts=_group_mid_ts(), composite_overrides=(_load_calibration().get("composites") or None))
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
        if path == "/api/access_log":                        # admin-only: who's been opening the site
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            return self._send(200, json.dumps({"ok": True, **access_summary()}))
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
        if path == "/api/wager_pins":              # admin-only: generate / view / reset per-player bet passcodes
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            cfg = load_config()
            players = [(p if isinstance(p, str) else p.get("name", "")) for p in cfg.get("players", [])]
            pins = cfg.get("wager_pins") if isinstance(cfg.get("wager_pins"), dict) else {}
            reset_one = str(body.get("reset_player", "")).strip()
            clear_one = str(body.get("clear_player", "")).strip()
            if clear_one:                          # wrong person claimed this name: wipe its passcode + Discord link so the RIGHT player can re-claim
                if clear_one not in players:
                    return self._send(404, json.dumps({"ok": False, "error": "Unknown player."}))
                pins.pop(clear_one, None)
                lk = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
                removed = [u for u, pl in list(lk.items()) if pl == clear_one]
                for u in removed:
                    lk.pop(u, None)
                cfg["wager_links"] = lk
                log("wager account cleared for", clear_one, "(unlinked %d Discord account[s])" % len(removed))
            elif reset_one:                        # forgot/leaked: reset JUST this player's passcode, leave everyone else alone
                if reset_one not in players:
                    return self._send(404, json.dumps({"ok": False, "error": "Unknown player."}))
                if not pins:
                    pins = {nm: _gen_pin() for nm in players if nm}
                pins[reset_one] = _gen_pin()
                log("wager pin reset for", reset_one)
            elif body.get("regenerate") or not pins:
                pins = {nm: _gen_pin() for nm in players if nm}
                log("wager pins", "regenerated" if body.get("regenerate") else "created", "for", len(pins), "players")
            else:
                for nm in players:                 # fill in any new players without clobbering existing pins
                    if nm and nm not in pins:
                        pins[nm] = _gen_pin()
                pins = {nm: pins[nm] for nm in players if nm in pins}   # drop pins for removed players
            cfg["wager_pins"] = pins
            save_config(cfg)
            links = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
            linked = {}                                # player -> True if some Discord account is linked
            for u, pl in links.items():
                linked[pl] = True
            return self._send(200, json.dumps({"ok": True, "pins": pins, "linked": linked, "reset": reset_one or None, "cleared": clear_one or None}))
        if path == "/api/wager_set_pin":           # OPEN: a player sets/changes THEIR OWN passcode (no organiser needed)
            cfg = load_config()
            if not cfg.get("wagering_enabled") or wager_mod is None:
                return self._send(400, json.dumps({"ok": False, "error": "Betting isn't switched on."}))
            if cfg.get("wager_locked"):
                return self._send(403, json.dumps({"ok": False, "error": "Betting is locked to the organiser."}))
            players = [(p if isinstance(p, str) else p.get("name", "")) for p in cfg.get("players", [])]
            player = str(body.get("player", "")).strip()
            new_pin = str(body.get("new_pin") or body.get("pin") or "").strip().upper()
            if player not in players:
                return self._send(400, json.dumps({"ok": False, "error": "Pick a valid player."}))
            if not re.fullmatch(r"[A-Z0-9]{4,24}", new_pin):
                return self._send(400, json.dumps({"ok": False, "error": "Passcode must be 4–24 letters or numbers (no spaces)."}))
            with _lock:                            # atomic: re-read inside the lock so two people can't claim the same name at once, and a concurrent config write can't clobber it
                cfg = load_config()
                pins = cfg.get("wager_pins") if isinstance(cfg.get("wager_pins"), dict) else {}
                if pins.get(player):                   # already claimed -> must prove ownership to change it
                    if not _pin_ok(player, body.get("current_pin")):
                        return self._send(403, json.dumps({"ok": False, "claimed": True, "bad_pin": True,
                            "error": "%s already has a passcode. Enter the current one to change it — or DM /resetpin on Discord, or ask the organiser to reset it." % player}))
                pins[player] = new_pin
                cfg["wager_pins"] = pins
                save_config(cfg)
            log("player self-set bet passcode for", player)
            return self._send(200, json.dumps({"ok": True, "set": True}))
        if path == "/api/wager_check_pin":         # OPEN: verify a passcode is correct (so a wrong one is never saved on the device)
            player = str(body.get("player", "")).strip()
            valid = _pin_ok(player, body.get("pin"))
            return self._send(200, json.dumps({"ok": True, "valid": bool(valid)}))
        if path in ("/api/my_alerts", "/api/game_mute", "/api/dm_master"):
            # Per-player notification controls on the website. Authorised exactly like a bet: a logged-in Discord
            # session for this player, OR their passcode. They act on every Discord account known to be this
            # player (so muting on the site silences the same DMs as /mute on Discord).
            player = str(body.get("player", "")).strip()
            if not self._authed_as(player, body):
                return self._send(403, json.dumps({"ok": False, "error": "Wrong passcode — or log in with Discord."}))
            uids = set(_uids_for_player(player))
            did = self._session_discord_id()
            if did and self._session_player() == player:
                uids.add(str(did))
            cfg = load_config()
            if path == "/api/my_alerts":
                optout = set(str(u) for u in (cfg.get("discord_dm_off") or []))
                mutesmap = cfg.get("discord_mutes") if isinstance(cfg.get("discord_mutes"), dict) else {}
                muted = sorted({str(mid) for u in uids for mid in (mutesmap.get(u) or [])})
                dm_off = bool(uids) and all(u in optout for u in uids)
                return self._send(200, json.dumps({"ok": True, "connected": bool(uids), "dm_off": dm_off, "muted": muted}))
            if not uids:
                return self._send(400, json.dumps({"ok": False, "not_connected": True,
                    "error": "Connect Discord first — tap Connect Discord on the Bets tab (or run /notifyme), then come back."}))
            if path == "/api/game_mute":
                mid = str(body.get("matchId", "")).strip()
                if not mid:
                    return self._send(400, json.dumps({"ok": False, "error": "No game specified."}))
                want = bool(body.get("muted"))
                mutes = cfg.get("discord_mutes") if isinstance(cfg.get("discord_mutes"), dict) else {}
                for u in uids:
                    lst = [str(x) for x in (mutes.get(u) or [])]
                    if want and mid not in lst:
                        lst.append(mid)
                    if not want:
                        lst = [x for x in lst if x != mid]
                    mutes[u] = lst
                cfg["discord_mutes"] = mutes
                save_config(cfg)
                muted = sorted({str(m) for u in uids for m in (mutes.get(u) or [])})
                return self._send(200, json.dumps({"ok": True, "muted": muted}))
            # /api/dm_master — turn ALL of this player's personal DMs on/off (same as /stopnotify // /notifyme)
            want_off = bool(body.get("off"))
            off = [str(x) for x in (cfg.get("discord_dm_off") or [])]
            for u in uids:
                if want_off and u not in off:
                    off.append(u)
            if not want_off:
                off = [x for x in off if x not in uids]
            cfg["discord_dm_off"] = off
            save_config(cfg)
            return self._send(200, json.dumps({"ok": True, "dm_off": want_off}))
        if path == "/api/discord_claim_player":    # first login: bind THIS logged-in Discord account to a player name (first-come)
            did = self._session_discord_id()
            if not did:
                return self._send(403, json.dumps({"ok": False, "error": "Log in with Discord first."}))
            cfg = load_config()
            players = [(p if isinstance(p, str) else p.get("name", "")) for p in cfg.get("players", [])]
            player = str(body.get("player", "")).strip()
            if player not in players:
                return self._send(400, json.dumps({"ok": False, "error": "Pick a valid player."}))
            decision = _guild_claim_check(did, cfg)        # membership check happens OUTSIDE the lock (it's a network call)
            if decision == "blocked":
                return self._send(403, json.dumps({"ok": False, "blocked": True,
                    "error": "This Discord account can't claim a name. If you think that's a mistake, contact the organiser."}))
            if decision == "not_member":
                return self._send(403, json.dumps({"ok": False, "not_member": True,
                    "error": "To claim a name you need to be in the sweepstake's Discord server. Ask the organiser for an invite, then try again."}))
            if decision == "unverified":
                return self._send(503, json.dumps({"ok": False, "retry": True,
                    "error": "Couldn't check your Discord membership just now — give it a moment and try again."}))
            with _lock:                            # atomic: re-read links inside the lock so two accounts can't grab the same name, and a concurrent write can't clobber it
                cfg = load_config()
                lk = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
                for u, pl in lk.items():               # first-come: can't grab a name another Discord account already holds
                    if pl == player and str(u) != str(did):
                        return self._send(409, json.dumps({"ok": False, "error": "%s is already claimed by another account — see the organiser if that's wrong." % player}))
                lk[str(did)] = player
                cfg["wager_links"] = lk
                save_config(cfg)
            log("discord-claim: %s -> %s" % (did, player))
            return self._send(200, json.dumps({"ok": True, "player": player}))
        if path == "/api/logout":                  # clear this browser's Discord session
            return self._send(200, json.dumps({"ok": True}), extra_headers={"Set-Cookie": "wc26_sess=; Path=/; Max-Age=0"})
        if path == "/api/logout_all":               # admin: invalidate EVERYONE's Discord login at once (rotates the session salt)
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True, "error": "Admin key required."}))
            cfg = load_config(); cfg["session_salt"] = secrets.token_hex(8); save_config(cfg)
            log("admin logged everyone out (session salt rotated)")
            return self._send(200, json.dumps({"ok": True, "logged_out_all": True}))
        if path == "/api/wager_self_unlink":       # OPEN to the player (passcode-gated): disconnect MY Discord
            player = str(body.get("player", "")).strip()
            if not self._authed_as(player, body):
                return self._send(403, json.dumps({"ok": False, "bad_pin": True, "error": "Wrong bet passcode for %s." % (player or "?")}))
            cfg = load_config()
            lk = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
            removed = [u for u, pl in list(lk.items()) if pl == player]
            for u in removed:
                lk.pop(u, None)
            cfg["wager_links"] = lk
            save_config(cfg)
            log("self-unlinked betting for", player, "(%d account[s])" % len(removed))
            return self._send(200, json.dumps({"ok": True, "removed": len(removed)}))
        if path == "/api/wager_unlink":            # admin-only: remove a Discord betting link (wrong account etc.) — by player OR by a specific account id
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            player = str(body.get("player", "")).strip()
            did = str(body.get("discord_id", "")).strip()
            with _lock:
                cfg = load_config()
                lk = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
                if did:
                    removed = [u for u in list(lk.keys()) if str(u) == did]          # one precise account
                else:
                    removed = [u for u, pl in list(lk.items()) if pl == player]       # every account on this name
                for u in removed:
                    lk.pop(u, None)
                cfg["wager_links"] = lk
                save_config(cfg)
            log("admin unlinked betting", ("id=" + did) if did else ("player=" + player), "(%d account[s])" % len(removed))
            return self._send(200, json.dumps({"ok": True, "removed": len(removed)}))
        if path == "/api/wager_block":             # admin-only: block (or unblock) a Discord account from claiming a name
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            did = str(body.get("discord_id", "")).strip()
            action = str(body.get("action", "block")).strip()
            if not did or not did.isdigit():
                return self._send(400, json.dumps({"ok": False, "error": "Enter the Discord account ID (a number) to block."}))
            with _lock:
                cfg = load_config()
                bl = [str(x) for x in (cfg.get("discord_blocklist") or []) if str(x).strip()]
                lk = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
                if action == "unblock":
                    bl = [x for x in bl if x != did]
                else:
                    if did not in bl:
                        bl.append(did)
                    if did in lk:                       # blocking also drops any name they currently hold
                        lk.pop(did, None)
                        cfg["wager_links"] = lk
                cfg["discord_blocklist"] = bl
                save_config(cfg)
            log("admin %s discord account %s" % (action, did))
            return self._send(200, json.dumps({"ok": True, "blocked": cfg.get("discord_blocklist", [])}))
        if path == "/api/wager_links_admin":       # admin-only: see who's linked to each name (spot a wrong/stranger link)
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            cfg = load_config()
            lk = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
            gate = _guild_gate_on(cfg)
            links = []
            for uid, pl in lk.items():
                member = _is_guild_member(uid, cfg) if gate else None   # True/False/None; None when we can't/needn't check
                links.append({"discord_id": str(uid), "player": pl, "member": member})
            return self._send(200, json.dumps({"ok": True, "links": links,
                                                "blocked": [str(x) for x in (cfg.get("discord_blocklist") or [])],
                                                "gate": gate}))
        if path == "/api/wager_void":               # admin-only: cancel/void open bet(s) — refunds the stake, no win/loss
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True, "error": "Admin key required."}))
            wid = str(body.get("id", "")).strip()
            player = str(body.get("player", "")).strip()
            if not wid and not player:
                return self._send(400, json.dumps({"ok": False, "error": "Pass an 'id' (one bet) or a 'player' (all their open bets)."}))
            with _lock:
                wl = load_wagers()
                targets = [w for w in wl
                           if w.get("status") == "pending" and not w.get("credit")
                           and ((wid and w.get("id") == wid) or (player and not wid and w.get("player") == player))]
                if wid and not targets:
                    return self._send(404, json.dumps({"ok": False, "error": "No open bet with that id (it may be settled already)."}))
                for w in targets:
                    w["status"] = "void"; w["settled_at"] = time.time()   # void = stake refunded, counts as neither win nor loss
                if targets:
                    save_wagers(wl)
            if targets:
                update_now(load_config())          # recompute so the refunded stake shows immediately
                log("admin voided %d bet(s)" % len(targets), ("id=" + wid) if wid else ("player=" + player))
            return self._send(200, json.dumps({"ok": True, "voided": len(targets),
                                                "stake_refunded": round(sum(w.get("stake", 0) for w in targets), 2)}))
        if path == "/api/send_pin":                # admin-only: DM a player their passcode privately via the bot
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            player = str(body.get("player", "")).strip()
            pin = _wager_pins().get(player)
            if not pin:
                return self._send(400, json.dumps({"ok": False, "error": "No passcode for %s yet — generate them first." % (player or "?")}))
            cfg = load_config()
            links = cfg.get("wager_links") if isinstance(cfg.get("wager_links"), dict) else {}
            uid = (next((u for u, pl in links.items() if pl == player), None)       # linked for betting (Connect Discord)…
                   or next((u for u, pl in _discord_subs().items() if pl == player), None))  # …or linked for alerts (/notifyme)
            if not uid:
                return self._send(400, json.dumps({"ok": False,
                    "error": "%s hasn't connected Discord yet — they need to tap Connect Discord on the Bets tab (or run /notifyme). You can also just read them their passcode." % player}))
            lk = links                                     # make sure the betting link is recorded
            lk[str(uid)] = player; cfg["wager_links"] = lk; save_config(cfg)
            ok, err = _bot_dm(uid, "🔒 Your **WC26 bet passcode** is **%s**.\nKeep it private — it's needed on the website. "
                                   "Your Discord is now linked, so on Discord just use `/games` then `/bet` (no passcode needed here)." % pin)
            return self._send(200 if ok else 400, json.dumps({"ok": ok, "error": err}))
        if path == "/api/test_notification":       # admin-only: verify Discord delivery — no bets/points are touched
            if not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            cfg = load_config()
            msg = "🔔 Test alert from the WC26 sweepstake — if you can see this, notifications are working. (A test only — no bets or points were changed.)"
            results = {}
            uid = str(body.get("discord_user_id", "")).strip()
            if uid:                                  # private: DM just this account (the bot-DM path used for win/passcode messages)
                ok, err = _bot_dm(uid, msg)
                results["bot_dm"] = "sent ✓" if ok else ("failed: %s" % (err or "no bot token / not reachable"))
            url = (cfg.get("discord_webhook") or "").strip()    # the channel that gets bet + win announcements
            if not url.startswith("https://"):
                results["discord_channel"] = "not set up"
            elif "/api/webhooks/" not in url:
                results["discord_channel"] = "that isn't a channel webhook URL — it must contain /api/webhooks/ (Discord → channel → Edit → Integrations → Webhooks → Copy Webhook URL)"
            else:
                try:
                    req = urllib.request.Request(url, data=json.dumps({"content": msg}).encode(),
                                                 headers={"Content-Type": "application/json", "User-Agent": "WC26-Sweepstake/1.0"})
                    urllib.request.urlopen(req, timeout=8)
                    results["discord_channel"] = "sent ✓ (check your Discord channel)"
                except Exception as e:
                    _m = _discord_err(e)
                    if "HTTP 400" in _m or "HTTP 404" in _m:
                        _m = "Discord rejected the webhook — it's probably deleted or mistyped. Make a fresh one (channel → Edit → Integrations → Webhooks → New Webhook → Copy URL) and paste it in."
                    results["discord_channel"] = "failed: %s" % _m
            log("test notification:", results)
            return self._send(200, json.dumps({"ok": True, "results": results}))
        if path == "/api/wager_link_code":          # OPEN (passcode-gated): mint a one-time code to link Discord
            cfg = load_config()
            if not cfg.get("wagering_enabled") or wager_mod is None:
                return self._send(400, json.dumps({"ok": False, "error": "Betting isn't switched on."}))
            player = str(body.get("player", "")).strip()
            if not self._authed_as(player, body):
                return self._send(403, json.dumps({"ok": False, "bad_pin": True, "error": "Wrong bet passcode for %s." % (player or "?")}))
            now = time.time()
            codes = cfg.get("wager_link_codes") if isinstance(cfg.get("wager_link_codes"), dict) else {}
            codes = {k: v for k, v in codes.items() if v.get("exp", 0) > now}      # prune expired
            code = _gen_pin(6)
            codes[code] = {"player": player, "exp": now + 900}                     # 15-minute single-use code
            cfg["wager_link_codes"] = codes
            save_config(cfg)
            return self._send(200, json.dumps({"ok": True, "code": code, "expires_min": 15}))
        if path == "/api/place_wager":             # OPEN: a player stakes their points on a fixture (before kickoff)
            cfg = load_config()
            if not cfg.get("wagering_enabled") or wager_mod is None:
                return self._send(400, json.dumps({"ok": False, "error": "Betting isn't switched on."}))
            _apply_wager_caps(cfg)                  # honour admin return/acca limits
            if cfg.get("wager_locked") and not key_ok(body):     # optional: lock betting behind the admin key
                return self._send(403, json.dumps({"ok": False, "need_key": True, "error": "Betting is locked to the organiser."}))
            players = [(p if isinstance(p, str) else p.get("name", "")) for p in cfg.get("players", [])]
            player = str(body.get("player", "")).strip()
            selection = str(body.get("selection", "")).strip().upper()
            match_id = str(body.get("matchId", "")).strip()
            market = str(body.get("market", "result")).strip().lower() or "result"
            line = body.get("line")
            stake = body.get("stake")
            if player not in players:
                return self._send(400, json.dumps({"ok": False, "error": "Pick a valid player."}))
            if not cfg.get("wager_locked"):          # normal mode: prove it's your account with your passcode
                if not _wager_pins():
                    return self._send(400, json.dumps({"ok": False, "error": "Betting passcodes aren't set up yet — ask the organiser."}))
                if not self._authed_as(player, body):
                    return self._send(403, json.dumps({"ok": False, "bad_pin": True,
                        "error": "Wrong bet passcode for %s." % player}))
            try:
                td = _load_tracker() or {}
                results = json.load(open("results.json"))
            except Exception:
                return self._send(400, json.dumps({"ok": False, "error": "No data yet."}))
            if wager_mod.betting_locked(td):
                return self._send(400, json.dumps({"ok": False, "error": "Betting is closed — the tournament is over."}))
            match = next((m for m in results.get("matches", []) if wager_mod.match_id(m) == match_id), None)
            if not match:
                return self._send(400, json.dumps({"ok": False, "error": "That game could not be found."}))
            teams = {}
            try:
                teams = {t["name"]: t for t in load_teams()}
            except Exception:
                pass
            if match.get("home") not in teams or match.get("away") not in teams:
                return self._send(400, json.dumps({"ok": False, "error": "You can only bet once both teams are confirmed — this game isn't set yet."}))
            try:
                _M = json.load(open("results.json")).get("matches", [])
            except Exception:
                _M = []
            ch = wager_mod.live_strength(_comp(teams, match.get("home")), match.get("home"), _M)
            ca = wager_mod.live_strength(_comp(teams, match.get("away")), match.get("away"), _M)
            prow = next((p for p in (td.get("players") or []) if p.get("name") == player), {})
            settled = prow.get("points_settled")
            if settled is None:
                settled = round((prow.get("points", 0) or 0) - (prow.get("live", 0) or 0), 1)
            nonce = str(body.get("nonce", "")).strip()[:64]
            with _lock:
                try:        # re-read the fixture under the lock: a kickoff/void that landed since we looked can't slip a bet through
                    _fm = next((m for m in json.load(open("results.json")).get("matches", [])
                                if wager_mod.match_id(m) == match_id), None)
                    if isinstance(_fm, dict):
                        match = _fm
                except Exception:
                    pass
                wlist = load_wagers()
                dup = _dedup_wager(wlist, player, nonce)
                if dup is not None:
                    ok, res = True, dup                 # idempotent: a retry of the same bet returns the original
                else:
                    ok, res = wager_mod.place(wlist, player, match, selection, stake, settled, ch, ca, group_mid_ts=_group_mid_ts(), market=market, line=line)
                    if ok:
                        if nonce:
                            res["nonce"] = nonce
                        save_wagers(wlist)
            if ok and dup is not None:
                return self._send(200, json.dumps({"ok": True, "wager": res, "duplicate": True}))
            if ok:
                update_now(load_config())          # recompute so the held stake shows immediately
                log("wager placed:", player, selection, "on", match_id)
                _announce_bet(player, res)
                return self._send(200, json.dumps({"ok": True, "wager": res}))
            return self._send(400, json.dumps({"ok": False, "error": res}))
        if path == "/api/place_free_bet":          # OPEN (passcode-gated): claim today's free betting points (no match)
            cfg = load_config()
            if not cfg.get("wagering_enabled") or wager_mod is None:
                return self._send(400, json.dumps({"ok": False, "error": "Betting isn't switched on."}))
            drop = _open_free_drop()
            if not drop:
                return self._send(400, json.dumps({"ok": False, "error": "No free points are available right now — drops land on a few match-days through the tournament."}))
            players = [(p if isinstance(p, str) else p.get("name", "")) for p in cfg.get("players", [])]
            player = str(body.get("player", "")).strip()
            if player not in players:
                return self._send(400, json.dumps({"ok": False, "error": "Pick a valid player."}))
            if cfg.get("wager_locked") and not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True, "error": "Betting is locked to the organiser."}))
            if not cfg.get("wager_locked"):
                if not _wager_pins():
                    return self._send(400, json.dumps({"ok": False, "error": "Betting passcodes aren't set up yet — ask the organiser."}))
                if not self._authed_as(player, body):
                    return self._send(403, json.dumps({"ok": False, "bad_pin": True, "error": "Wrong bet passcode for %s." % player}))
            claims = cfg.get("free_bet_claims") if isinstance(cfg.get("free_bet_claims"), dict) else {}
            taken = claims.get(drop["id"]) if isinstance(claims.get(drop["id"]), dict) else {}
            if player in taken:
                return self._send(400, json.dumps({"ok": False, "error": "You've already claimed this drop — the next one is another day."}))
            status, res = _claim_free_drop(player, drop["id"])   # atomic: re-checks inside the lock so nobody double-claims
            if status == "already":
                return self._send(400, json.dumps({"ok": False, "error": "You've already claimed this drop — the next one is another day."}))
            if status == "ok":
                update_now(load_config())
                log("free points claimed:", player, "+%g" % res["amount"], "drop", drop["id"])
                return self._send(200, json.dumps({"ok": True, "credit": res, "amount": res["amount"]}))
            return self._send(400, json.dumps({"ok": False, "error": res}))
        if path == "/api/place_acca":              # OPEN: an accumulator (one stake, combined odds)
            cfg = load_config()
            if not cfg.get("wagering_enabled") or wager_mod is None:
                return self._send(400, json.dumps({"ok": False, "error": "Betting isn't switched on."}))
            _apply_wager_caps(cfg)                  # honour admin return/acca limits
            players = [(p if isinstance(p, str) else p.get("name", "")) for p in cfg.get("players", [])]
            player = str(body.get("player", "")).strip()
            stake = body.get("stake")
            legs_in = body.get("legs") if isinstance(body.get("legs"), list) else []
            if player not in players:
                return self._send(400, json.dumps({"ok": False, "error": "Pick a valid player."}))
            if not cfg.get("wager_locked"):
                if not _wager_pins():
                    return self._send(400, json.dumps({"ok": False, "error": "Betting passcodes aren't set up yet — ask the organiser."}))
                if not self._authed_as(player, body):
                    return self._send(403, json.dumps({"ok": False, "bad_pin": True, "error": "Wrong bet passcode for %s." % player}))
            elif not key_ok(body):
                return self._send(403, json.dumps({"ok": False, "need_key": True}))
            if not (1 <= len(legs_in) <= 3):
                return self._send(400, json.dumps({"ok": False, "error": "An accumulator is 1 to 3 picks."}))
            try:
                td = _load_tracker() or {}
                results = json.load(open("results.json"))
                teams = {t["name"]: t for t in load_teams()}
            except Exception:
                return self._send(400, json.dumps({"ok": False, "error": "No data yet."}))
            if wager_mod.betting_locked(td):
                return self._send(400, json.dumps({"ok": False, "error": "Betting is closed — the tournament is over."}))
            selections = []
            for lg in legs_in:
                m = next((x for x in results.get("matches", []) if wager_mod.match_id(x) == str(lg.get("matchId", ""))), None)
                if not m:
                    return self._send(400, json.dumps({"ok": False, "error": "One of those games could not be found."}))
                if m.get("home") not in teams or m.get("away") not in teams:
                    return self._send(400, json.dumps({"ok": False, "error": "You can only bet once both teams are confirmed — one of those games isn't set yet."}))
                selections.append({"match": m, "selection": str(lg.get("selection", "")).upper(),
                                   "market": str(lg.get("market", "result")).strip().lower() or "result",
                                   "line": lg.get("line"),
                                   "comp_home": wager_mod.live_strength(_comp(teams, m.get("home")), m.get("home"), results.get("matches", [])),
                                   "comp_away": wager_mod.live_strength(_comp(teams, m.get("away")), m.get("away"), results.get("matches", []))})
            prow = next((p for p in (td.get("players") or []) if p.get("name") == player), {})
            settled = prow.get("points_settled")
            if settled is None:
                settled = round((prow.get("points", 0) or 0) - (prow.get("live", 0) or 0), 1)
            nonce = str(body.get("nonce", "")).strip()[:64]
            with _lock:
                try:        # refresh each leg's fixture under the lock so a just-kicked-off/voided leg can't slip in
                    _fresh = {wager_mod.match_id(m): m for m in json.load(open("results.json")).get("matches", []) if isinstance(m, dict)}
                    for _s in selections:
                        _fm = _fresh.get(wager_mod.match_id(_s.get("match") or {}))
                        if isinstance(_fm, dict):
                            _s["match"] = _fm
                except Exception:
                    pass
                wl = load_wagers()
                dup = _dedup_wager(wl, player, nonce)
                if dup is not None:
                    ok, res = True, dup
                else:
                    ok, res = wager_mod.place_acca(wl, player, selections, stake, settled)
                    if ok:
                        if nonce:
                            res["nonce"] = nonce
                        save_wagers(wl)
            if ok and dup is not None:
                return self._send(200, json.dumps({"ok": True, "wager": res, "duplicate": True}))
            if ok:
                update_now(load_config())
                log("acca placed:", player, len(selections), "legs")
                _announce_bet(player, res)
                return self._send(200, json.dumps({"ok": True, "wager": res}))
            return self._send(400, json.dumps({"ok": False, "error": res}))
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
                why = "the push library isn't installed on the server" if not HAVE_WEBPUSH else "the server has no push keys yet"
                return self._send(400, json.dumps({"ok": False, "error": why}))
            lst = _load_push().get(player)
            if not lst:
                return self._send(400, json.dumps({"ok": False, "error": "no devices subscribed yet"}))
            sent, errors, keep, changed = 0, [], [], False
            for e in lst:                          # a test ignores per-event choices
                keep_it, err = _webpush_one(_entry_sub(e), "WC26 Sweepstake", "✅ Push alerts are working, %s!" % player)
                if err is None:
                    sent += 1
                else:
                    errors.append(err)
                if keep_it:
                    keep.append(e)
                else:
                    changed = True
            if changed:                            # prune dead devices found by the test
                with _lock:
                    subs = _load_push(); subs[player] = keep; _save_push(subs)
            return self._send(200, json.dumps({"ok": sent > 0, "sent": sent,
                                               "failed": len(errors), "errors": errors[:3]}))
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
            # A manual "refresh now" nudge from the client. The background poller already refreshes on its
            # own schedule, so throttle the actual upstream fetch hard: if we refreshed very recently, just
            # acknowledge without hitting football-data again. This makes spamming /api/poll harmless — it
            # can't burn the upstream rate limit or pile requests on the lock and stall live updates.
            now = time.time()
            if now - _last_manual_poll[0] < MANUAL_POLL_MIN_INTERVAL:
                return self._send(200, json.dumps({"ok": True, "throttled": True}))
            _last_manual_poll[0] = now
            cfg = load_config()
            try:
                with _lock:
                    ok, err = update_now(cfg)
            except Exception as e:
                log("manual poll error:", e)
                return self._send(200, json.dumps({"ok": False, "error": "refresh failed"}))
            return self._send(200, json.dumps({"ok": bool(ok), "error": err if not ok else None}))
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
    _apply_wager_caps()                   # load admin return/acca limits at boot
    _apply_goals_base()                   # restore the calibrated goals base (if any) into the wager engine
    if HAVE_WEBPUSH:
        ensure_vapid()                    # one-time keypair so native Web Push (Path A) can work
    threading.Thread(target=poller, daemon=True).start()
    print(f"Sweepstake server on http://{HOST}:{PORT}  (Ctrl-C to stop)")
    print(f"Admin key (needed only to overwrite a finished draw): {_key}")
    print("Web Push: " + ("ENABLED" if push_enabled() else "off (pip install pywebpush to turn on)"))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
