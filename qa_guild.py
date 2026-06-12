#!/usr/bin/env python3
"""Tests for the Discord guild-membership gate: only people in the organiser's Discord server can
claim a player name. The real Discord call can't run here (no token/network), so we test the parsing,
the on/off logic, and the claim decision with the membership check stubbed to each outcome."""
import os, sys, json, shutil, tempfile

SRC = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="wc26_guild_")
for fn in os.listdir(SRC):
    if fn.endswith(".py") or fn.endswith(".json"):
        try: shutil.copy(os.path.join(SRC, fn), TMP)
        except Exception: pass
os.environ["WC26_CONFIG"] = os.path.join(TMP, "config.json")
json.dump({"configured": True, "players": ["Erol", "James"]}, open(os.path.join(TMP, "config.json"), "w"))
os.chdir(TMP); sys.path.insert(0, TMP)
import server as S

FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

print("=== _member_from_status maps Discord's reply correctly ===")
ck("200 -> member (True)", S._member_from_status(200) is True, "")
ck("404 -> definitely not a member (False)", S._member_from_status(404) is False, "")
for code in (401, 403, 429, 500, 502, 0):
    ck("%s -> unknown (None, fail closed)" % code, S._member_from_status(code) is None, code)

print("\n=== _guild_gate_on: off unless fully configured ===")
ck("off when nothing configured", S._guild_gate_on({}) is False, "")
ck("off with only a bot token", S._guild_gate_on({"discord_bot_token": "x"}) is False, "")
ck("off with only a guild id", S._guild_gate_on({"discord_guild_id": "123"}) is False, "")
ck("ON when token + guild set (default)", S._guild_gate_on({"discord_bot_token": "x", "discord_guild_id": "123"}) is True, "")
ck("OFF when explicitly disabled even if configured",
   S._guild_gate_on({"discord_bot_token": "x", "discord_guild_id": "123", "discord_guild_gate": False}) is False, "")

print("\n=== _guild_claim_check: the actual allow/deny decision ===")
GATE_OFF = {"players": ["Erol"]}
GATE_ON = {"players": ["Erol"], "discord_bot_token": "x", "discord_guild_id": "123"}
# gate OFF -> always allowed (so a site not wired for it behaves exactly as before)
ck("gate off -> 'ok' regardless of membership", S._guild_claim_check("u1", GATE_OFF) == "ok", "")

orig = S._is_guild_member
try:
    S._is_guild_member = lambda uid, cfg=None: True
    ck("gate on + member -> 'ok'", S._guild_claim_check("u1", GATE_ON) == "ok", "")
    S._is_guild_member = lambda uid, cfg=None: False
    ck("gate on + NOT a member -> 'not_member' (refused)", S._guild_claim_check("u1", GATE_ON) == "not_member", "")
    S._is_guild_member = lambda uid, cfg=None: None
    ck("gate on + can't verify -> 'unverified' (fail closed, retry)", S._guild_claim_check("u1", GATE_ON) == "unverified", "")
finally:
    S._is_guild_member = orig

print("\n=== _is_guild_member returns None when not configured (no crash, no call) ===")
ck("no token/guild -> None without any network call", S._is_guild_member("u1", {}) is None, "")
ck("missing user id -> None", S._is_guild_member("", {"discord_bot_token": "x", "discord_guild_id": "1"}) is None, "")

print("\n=== blocklist: a blocked account can never claim ===")
ck("_is_blocked false when not on the list", S._is_blocked("u9", {"discord_blocklist": ["u1"]}) is False, "")
ck("_is_blocked true when on the list", S._is_blocked("u1", {"discord_blocklist": ["u1"]}) is True, "")
ck("_is_blocked matches numeric ids stored as ints too", S._is_blocked("123", {"discord_blocklist": [123]}) is True, "")
# decision: blocked beats everything, even a confirmed member
orig = S._is_guild_member
try:
    S._is_guild_member = lambda uid, cfg=None: True
    ck("blocked account refused even if it IS a guild member", S._guild_claim_check("u1", {"discord_blocklist": ["u1"], "discord_bot_token": "x", "discord_guild_id": "1"}) == "blocked", "")
    ck("a non-blocked member is still ok", S._guild_claim_check("u2", {"discord_blocklist": ["u1"], "discord_bot_token": "x", "discord_guild_id": "1"}) == "ok", "")
finally:
    S._is_guild_member = orig
ck("blocked refused even when the gate is OFF", S._guild_claim_check("u1", {"discord_blocklist": ["u1"]}) == "blocked", "")

print("\n=== HTTP: the new admin controls exist, are gated, and behave ===")
import urllib.request, urllib.error, subprocess, socket, time as _t
def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
PORT = _free_port(); BASE = "http://127.0.0.1:%d" % PORT; KEY = "QA_ADMIN_KEY_1234567"
json.dump({"configured": True, "players": ["Erol", "James"], "admin_key": KEY,
           "wager_links": {"111": "Erol", "999": "James"}, "wagering_enabled": True},
          open(os.path.join(TMP, "config.json"), "w"))
def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp: return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()
proc = subprocess.Popen([sys.executable, os.path.join(TMP, "server.py")],
                        env=dict(os.environ, WC26_DATA=TMP, WC26_CONFIG=os.path.join(TMP, "config.json"), PORT=str(PORT), HOST="127.0.0.1", ADMIN_KEY=KEY),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(80):
        try:
            s, _b = req("GET", "/api/status")
            if s == 200: break
        except Exception: _t.sleep(0.1)
    st, _ = req("POST", "/api/wager_links_admin", {})
    ck("links_admin needs the admin key (403)", st == 403, st)
    st, _ = req("POST", "/api/wager_block", {})
    ck("wager_block needs the admin key (403)", st == 403, st)
    st, b = req("POST", "/api/wager_links_admin", {"admin_key": KEY})
    j = json.loads(b)
    ck("links_admin lists the two current links", st == 200 and len(j.get("links", [])) == 2, b[:120])
    st, b = req("POST", "/api/wager_block", {"admin_key": KEY, "discord_id": "111"})
    ck("blocking id 111 succeeds and lists it", st == 200 and "111" in json.loads(b).get("blocked", []), b[:120])
    st, b = req("POST", "/api/wager_links_admin", {"admin_key": KEY})
    j = json.loads(b)
    ck("blocking 111 also unlinked it (only James remains)", [L["player"] for L in j["links"]] == ["James"], j.get("links"))
    st, b = req("POST", "/api/wager_block", {"admin_key": KEY, "discord_id": "abc"})
    ck("block rejects a non-numeric id (400)", st == 400, b[:80])
    st, b = req("POST", "/api/wager_unlink", {"admin_key": KEY, "discord_id": "999"})
    ck("unlink by specific discord_id removes exactly that account", st == 200 and json.loads(b).get("removed") == 1, b[:80])
    st, b = req("POST", "/api/wager_block", {"admin_key": KEY, "discord_id": "111", "action": "unblock"})
    ck("unblock removes it from the list", st == 200 and "111" not in json.loads(b).get("blocked", []), b[:120])
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()

print("\n=== admin-configurable bet/acca caps apply to the engine ===")
import wager as Wm
_dp, _da = S._WAGER_DEFAULTS["MAX_PENDING"], S._WAGER_DEFAULTS["MAX_ACTIVE_ACCAS"]
c = S.load_config(); c["max_pending_bets"] = 3; c["max_active_accas"] = 1; S.save_config(c)
S._apply_wager_caps(S.load_config())
ck("max_pending_bets=3 -> engine MAX_PENDING is 3", Wm.MAX_PENDING == 3, Wm.MAX_PENDING)
ck("max_active_accas=1 -> engine MAX_ACTIVE_ACCAS is 1", Wm.MAX_ACTIVE_ACCAS == 1, Wm.MAX_ACTIVE_ACCAS)
caps = S._apply_wager_caps(S.load_config())
ck("status caps reflect the new max_pending", caps.get("max_pending") == 3, caps.get("max_pending"))
ck("status caps reflect the new max_active_accas", caps.get("max_active_accas") == 1, caps.get("max_active_accas"))
c = S.load_config(); c["max_pending_bets"] = 9999; S.save_config(c); S._apply_wager_caps(S.load_config())
ck("absurd max_pending is clamped (<=50)", Wm.MAX_PENDING == 50, Wm.MAX_PENDING)
c = S.load_config(); c.pop("max_pending_bets", None); c.pop("max_active_accas", None); S.save_config(c)
S._apply_wager_caps(S.load_config())
ck("clearing max_pending_bets restores the default", Wm.MAX_PENDING == _dp, (Wm.MAX_PENDING, _dp))
ck("clearing max_active_accas restores the default", Wm.MAX_ACTIVE_ACCAS == _da, (Wm.MAX_ACTIVE_ACCAS, _da))

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("\nGUILD-GATE QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll guild-gate QA passed — only Discord members can claim, gate is safe-off until configured, and it fails closed.")
