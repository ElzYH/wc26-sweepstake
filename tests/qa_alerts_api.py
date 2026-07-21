#!/usr/bin/env python3
"""HTTP QA for the website notification controls: /api/my_alerts, /api/game_mute, /api/dm_master
(player-authed via passcode or Discord session) and the admin /api/settings game_channel_alerts kill switch.
Starts a real server against a minimal config and exercises auth, connected-vs-not, mute/unmute, master DM
toggle and admin gating. Dev-only (not wired into check.sh, which would make it a startup-race flake)."""
import os, sys, json, tempfile, subprocess, time, urllib.request, urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = tempfile.mkdtemp(prefix="wc26_alertsapi_")
cfg = {"configured": True,
       "players": [{"name": "Erol"}, {"name": "James"}, {"name": "Louis"}],
       "wager_pins": {"Erol": "ABC1", "James": "XYZ2"},   # Louis has no passcode
       "wager_links": {"999": "Erol"},                    # only Erol is linked to a Discord account
       "wagering_enabled": True,
       "admin_key": "ADMINK"}
json.dump(cfg, open(os.path.join(d, "config.json"), "w"))

PORT = "8731"
BASE = "http://127.0.0.1:%s" % PORT
env = dict(os.environ, WC26_DATA=d, WC26_CONFIG=os.path.join(d, "config.json"), PORT=PORT, HOST="127.0.0.1")
proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], env=env,
                        stdout=open(os.path.join(d, "srv.log"), "w"), stderr=subprocess.STDOUT)
time.sleep(2.5)
# the server generates its own admin_key on first start (printed to its log) — read it back for the admin calls
ADMIN_KEY = json.load(open(os.path.join(d, "config.json"))).get("admin_key") or "ADMINK"

def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}

def get(path):
    with urllib.request.urlopen(urllib.request.Request(BASE + path), timeout=6) as r:
        return json.loads(r.read())

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

try:
    print("== auth ==")
    code, j = post("/api/my_alerts", {"player": "Erol", "pin": "ABC1"})
    ck("correct passcode authenticates + shows connected", code == 200 and j.get("ok") and j.get("connected") is True, (code, j))
    code, j = post("/api/my_alerts", {"player": "Erol", "pin": "WRONG"})
    ck("wrong passcode rejected (403)", code == 403, (code, j))
    code, j = post("/api/my_alerts", {"player": "James", "pin": "XYZ2"})
    ck("authed but not Discord-linked -> connected:false", code == 200 and j.get("ok") and j.get("connected") is False, (code, j))

    print("\n== per-game mute (Erol, connected) ==")
    code, j = post("/api/game_mute", {"player": "Erol", "pin": "ABC1", "matchId": "Brazil|Serbia|2026-06-18T03:00", "muted": True})
    ck("mute a game (matchId with | : is fine over the wire)", code == 200 and "Brazil|Serbia|2026-06-18T03:00" in (j.get("muted") or []), (code, j))
    code, j = post("/api/my_alerts", {"player": "Erol", "pin": "ABC1"})
    ck("mute persists across calls", "Brazil|Serbia|2026-06-18T03:00" in (j.get("muted") or []), j)
    code, j = post("/api/game_mute", {"player": "Erol", "pin": "ABC1", "matchId": "Brazil|Serbia|2026-06-18T03:00", "muted": False})
    ck("unmute a game", code == 200 and "Brazil|Serbia|2026-06-18T03:00" not in (j.get("muted") or []), (code, j))

    print("\n== mute requires a Discord connection ==")
    code, j = post("/api/game_mute", {"player": "James", "pin": "XYZ2", "matchId": "M1", "muted": True})
    ck("can't mute when not connected (400 + not_connected)", code == 400 and j.get("not_connected"), (code, j))

    print("\n== master DM on/off ==")
    code, j = post("/api/dm_master", {"player": "Erol", "pin": "ABC1", "off": True})
    ck("master DMs off", code == 200 and j.get("dm_off") is True, (code, j))
    code, j = post("/api/my_alerts", {"player": "Erol", "pin": "ABC1"})
    ck("dm_off reflected in my_alerts", j.get("dm_off") is True, j)
    code, j = post("/api/dm_master", {"player": "Erol", "pin": "ABC1", "off": False})
    ck("master DMs back on", code == 200 and j.get("dm_off") is False, (code, j))

    print("\n== admin kill switch via /api/settings ==")
    code, j = post("/api/settings", {"game_channel_alerts": False, "admin_key": ADMIN_KEY})
    ck("admin turns channel feed off", code == 200 and j.get("ok"), (code, j))
    ck("status reflects game_channel_alerts:false", get("/api/status").get("game_channel_alerts") is False, get("/api/status").get("game_channel_alerts"))
    code, j = post("/api/settings", {"game_channel_alerts": True, "admin_key": ADMIN_KEY})
    ck("admin turns it back on", code == 200, (code, j))
    code, j = post("/api/settings", {"game_channel_alerts": False, "admin_key": "NOPE"})
    ck("wrong admin key rejected (403)", code == 403, (code, j))
    ck("status still on after a rejected change", get("/api/status").get("game_channel_alerts") is True, get("/api/status").get("game_channel_alerts"))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

import shutil
shutil.rmtree(d, ignore_errors=True)
print("\n" + ("ALERTS API QA PASSED" if not fails else "ALERTS API QA FAILED (%d): %s" % (len(fails), ", ".join(fails))))
sys.exit(1 if fails else 0)
