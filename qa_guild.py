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

print("\n=== status exposes the gate flag (off by default in this unconfigured test) ===")
# the gate is off here (no token/guild in the test config), so existing claim flow is unchanged
ck("gate is off for an unconfigured server", S._guild_gate_on(S.load_config()) is False, "")

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("\nGUILD-GATE QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll guild-gate QA passed — only Discord members can claim, gate is safe-off until configured, and it fails closed.")
