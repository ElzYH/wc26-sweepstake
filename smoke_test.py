#!/usr/bin/env python3
"""Live smoke + security test: boots server.py in a temp dir and probes its endpoints.

Run: python3 smoke_test.py   (exit 0 = pass). No real data is touched; uses a temp dir + port.
Checks routing, the static-file whitelist (no secret leakage / traversal), POST size + JSON
guards, and admin-key gating.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
KEY = "test-admin-key-123456"          # >= 15 chars so ensure_admin_key keeps it
FAILS = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  -> " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def req(method, path, body=None, raw=None):
    url = BASE + path
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def run():
    tmp = tempfile.mkdtemp()
    # minimal data dir: teams.json (copied from repo) + config with players + key
    repo = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(repo, "teams.json")):
        shutil.copy2(os.path.join(repo, "teams.json"), os.path.join(tmp, "teams.json"))
    else:
        json.dump({"teams": [{"name": "A", "tier": 1, "tier_label": "T1", "weight": 8,
                              "group": "X", "composite": 90, "implied_prob": 0.3}]},
                  open(os.path.join(tmp, "teams.json"), "w"))
    json.dump({"players": ["Erol", "James"], "admin_key": KEY, "scoring_mode": "hybrid"},
              open(os.path.join(tmp, "config.json"), "w"))

    env = dict(os.environ, WC26_DATA=tmp, WC26_CONFIG=os.path.join(tmp, "config.json"),
               PORT=str(PORT), HOST="127.0.0.1", ADMIN_KEY=KEY)
    proc = subprocess.Popen([sys.executable, os.path.join(repo, "server.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):                # wait up to ~4s for bind
            try:
                req("GET", "/api/status"); break
            except Exception:
                time.sleep(0.1)

        st, body = req("GET", "/api/status")
        check("GET /api/status 200", st == 200, str(st))
        j = json.loads(body) if st == 200 else {}
        check("status: configured true", j.get("configured") is True, body[:120])
        check("status does NOT leak the admin key / token value", KEY not in body and '"admin_key"' not in body, body[:160])

        st, body = req("GET", "/tracker")
        check("GET /tracker serves html", st == 200 and "<" in body, str(st))

        st, _ = req("GET", "/config.json")
        check("GET /config.json blocked (404 — no secret leak)", st == 404, str(st))

        st, _ = req("GET", "/../server.py")
        check("path traversal blocked (404)", st == 404, str(st))

        st, _ = req("GET", "/api/nope")
        check("unknown GET -> 404", st == 404, str(st))

        st, body = req("POST", "/api/check_key", {"admin_key": "wrong"})
        check("wrong admin key rejected", st == 200 and json.loads(body).get("ok") is False, body[:120])
        st, body = req("POST", "/api/check_key", {"admin_key": KEY})
        check("correct admin key accepted", st == 200 and json.loads(body).get("ok") is True, body[:120])

        st, _ = req("POST", "/api/settings", raw=b"x" * 200_000)
        check("oversized POST -> 413", st == 413, str(st))

        st, _ = req("POST", "/api/settings", raw=b"{not json")
        check("malformed JSON -> 400", st == 400, str(st))

        st, body = req("POST", "/api/settings", {"competition": "WC"})   # configured + no key
        check("settings without key -> 403 (gated when configured)", st == 403, f"{st} {body[:80]}")

        st, body = req("POST", "/api/redraw", {})                        # now always key-gated
        check("redraw without key -> 403", st == 403, str(st))
        st, body = req("POST", "/api/redraw", {"admin_key": KEY})
        check("redraw with key responds ok", st == 200, str(st))

        st, body = req("GET", "/api/telegram_links")                     # dead but must not 500
        check("telegram_links doesn't 500 on string players", st == 200, str(st))

        # --- two-channel notification endpoints ---
        st, body = req("GET", "/api/status")
        j = json.loads(body)
        check("status exposes push_enabled flag", "push_enabled" in j, body[:160])
        check("status exposes discord flag", "discord" in j, body[:160])
        check("push flags consistent (enabled iff vapid_public set)",
              bool(j.get("push_enabled")) == bool(j.get("vapid_public")), body[:160])
        st, body = req("POST", "/api/discord_test", {"admin_key": "wrong"})
        check("discord_test needs admin key (403)", st == 403, str(st))
        st, body = req("POST", "/api/push_subscribe", {"player": "Nobody", "subscription": {"endpoint": "x"}})
        check("push_subscribe rejects unknown player (400)", st == 400, f"{st} {body[:80]}")
        st, body = req("POST", "/api/push_test", {"player": "Erol"})
        check("push_test 400 when push not enabled", st == 400, f"{st} {body[:80]}")
        st, body = req("POST", "/api/push_prefs", {"endpoint": "x", "prefs": {"goal": False}})
        check("push_prefs responds ok", st == 200, str(st))
        st, body = req("POST", "/api/discord_invite", {"code": "x"})
        check("discord_invite 404 when none set (no leak)", st == 404, str(st))
        st, body = req("GET", "/api/summary")
        check("GET /api/summary 200 with lines", st == 200 and isinstance(json.loads(body).get("lines"), list), str(st))
        st, body = req("POST", "/api/discord_demo", {"admin_key": "wrong"})
        check("discord_demo needs admin key (403)", st == 403, str(st))
        st, body = req("POST", "/api/discord_summary", {"admin_key": "wrong"})
        check("discord_summary needs admin key (403)", st == 403, str(st))
        st, body = req("GET", "/api/status")
        check("status exposes invite field", "invite" in json.loads(body), body[:120])
        check("status exposes bot_ready flag", "bot_ready" in json.loads(body), body[:120])
        st, body = req("POST", "/api/discord_interactions", {"type": 1})
        check("interactions reject unsigned (401)", st == 401, str(st))
        st, body = req("POST", "/api/register_commands", {"admin_key": "wrong"})
        check("register_commands needs admin key (403)", st == 403, str(st))
        st, body = req("POST", "/api/start_draw", {"admin_key": "wrong"})
        check("start_draw needs admin key (403)", st == 403, str(st))
        st, _ = req("POST", "/api/wager_pins", {"admin_key": "nope"})
        check("wager_pins needs admin key (403)", st == 403, str(st))
        st, body = req("POST", "/api/wager_pins", {"admin_key": KEY})
        pins = json.loads(body).get("pins", {})
        check("wager_pins generates a code per player", isinstance(pins, dict) and len(pins) >= 1, body[:120])
        # per-player reset: only that player's code changes; admin-gated; unknown player -> 404
        _names = list(pins.keys())
        if _names:
            _who = _names[0]
            st, _ = req("POST", "/api/wager_pins", {"reset_player": _who})
            check("per-player pin reset needs admin key (403)", st == 403, str(st))
            st, body = req("POST", "/api/wager_pins", {"admin_key": KEY, "reset_player": _who})
            newpins = json.loads(body).get("pins", {})
            check("reset changes only that player's code",
                  st == 200 and newpins.get(_who) != pins.get(_who)
                  and all(newpins.get(n) == pins.get(n) for n in _names[1:]), body[:160])
            st, _ = req("POST", "/api/wager_pins", {"admin_key": KEY, "reset_player": "NotARealPlayer"})
            check("reset of unknown player -> 404", st == 404, str(st))
            # admin Clear: wipes a player's code so the right person can re-claim (un-claim)
            st, body = req("POST", "/api/wager_pins", {"admin_key": KEY, "clear_player": _who})
            check("admin clear removes that player's code", st == 200 and _who not in json.loads(body).get("pins", {}), body[:140])
            st, _ = req("POST", "/api/wager_pins", {"clear_player": _who})
            check("clear needs admin key (403)", st == 403, str(st))
            # the "Connect Discord" route must be registered (regression guard: it once 404'd)
            st, _ = req("POST", "/api/wager_link_code", {"player": _who, "pin": "WRONGPIN"})
            check("wager_link_code route is registered (not 404)", st != 404, "got %s" % st)
            pins = newpins
        # self-service: a player sets their OWN passcode (first-come), then needs the current one to change it
        req("POST", "/api/settings", {"admin_key": KEY, "wagering_enabled": True})
        st, _ = req("POST", "/api/wager_pins", {"admin_key": KEY, "regenerate": True})   # clear to a known state, then test set
        # pick a player with a known fresh code, then OVERWRITE requires the current code
        _np = list(json.loads(req("POST", "/api/wager_pins", {"admin_key": KEY})[1]).get("pins", {}).keys())
        if _np:
            who = _np[0]
            st, body = req("POST", "/api/wager_set_pin", {"player": who, "pin": "MINE12", "current_pin": "definitely-wrong"})
            check("self-set on a claimed name without current code -> 403", st == 403, str(st))
            st, body = req("POST", "/api/wager_set_pin", {"player": who, "pin": "ab"})
            check("self-set rejects a too-short passcode (400)", st == 400, str(st))
            st, body = req("POST", "/api/wager_set_pin", {"player": "GhostPlayer", "pin": "OKPIN1"})
            check("self-set rejects an unknown player (400)", st == 400, str(st))
            _cur = json.loads(req("POST", "/api/wager_pins", {"admin_key": KEY})[1]).get("pins", {}).get(who)
            st, body = req("POST", "/api/wager_set_pin", {"player": who, "pin": "NEWONE9", "current_pin": _cur})
            check("self-set changes own code with the correct current one (200)",
                  st == 200 and json.loads(body).get("ok") is True, body[:120])
            _now = json.loads(req("POST", "/api/wager_pins", {"admin_key": KEY})[1]).get("pins", {}).get(who)
            check("self-set actually replaced the code (old gone, new in place)",
                  _now == "NEWONE9" and _now != _cur, "now=%s old=%s" % (_now, _cur))
            # a wrong current code is refused even when the player already has one
            st, _ = req("POST", "/api/wager_set_pin", {"player": who, "pin": "ANOTHER1", "current_pin": "NOPE99"})
            check("self-set with wrong current code -> 403", st == 403, str(st))
        st, body = req("GET", "/api/status")
        check("status exposes wagering flags, never the pins",
              "wager_pins_set" in json.loads(body) and not any(p in body for p in pins.values()), body[:160])
        # betting is enabled but a bet with no/!wrong passcode is refused
        req("POST", "/api/settings", {"admin_key": KEY, "wagering_enabled": True})
        st, body = req("POST", "/api/place_wager",
                       {"player": (json.loads(req("GET", "/api/status")[1]).get("players") or ["x"])[0],
                        "matchId": "nope", "selection": "HOME", "stake": 1, "pin": "WRONG"})
        check("place_wager rejects a wrong passcode (403)", st == 403, str(st))
        st, body = req("POST", "/api/place_acca",
                       {"player": (json.loads(req("GET", "/api/status")[1]).get("players") or ["x"])[0],
                        "legs": [{"matchId": "a", "selection": "HOME"}, {"matchId": "b", "selection": "HOME"}],
                        "stake": 2, "pin": "WRONG"})
        check("place_acca rejects a wrong passcode (403)", st == 403, str(st))
        st, _ = req("POST", "/api/wager_unlink", {"admin_key": "nope", "player": "x"})
        check("wager_unlink needs admin key (403)", st == 403, str(st))
        st, body = req("POST", "/api/wager_unlink", {"admin_key": KEY, "player": "x"})
        check("wager_unlink ok with admin key", st == 200 and json.loads(body).get("ok") is True, body[:120])
        # self-unlink is passcode-gated (a random can't unlink someone)
        st, _ = req("POST", "/api/wager_self_unlink", {"player": "x", "pin": "WRONG"})
        check("self-unlink rejects a wrong passcode (403)", st == 403, str(st))
        # admin test-notification: admin-gated; returns a results map; no webhook set -> 'not set up' (and nothing crashes)
        st, _ = req("POST", "/api/test_notification", {"admin_key": "nope"})
        check("test_notification needs admin key (403)", st == 403, str(st))
        st, body = req("POST", "/api/test_notification", {"admin_key": KEY})
        jr = json.loads(body)
        check("test_notification ok + reports channel status", st == 200 and jr.get("ok") and "discord_channel" in jr.get("results", {}), body[:160])
        # admin can set a winnings cap + acca legs, and status reflects them
        req("POST", "/api/settings", {"admin_key": KEY, "max_return": 120, "max_acca_legs": 4})
        st, body = req("GET", "/api/status")
        caps = (json.loads(body).get("wager_caps") or {})
        check("admin max_return applies (status shows 120)", caps.get("max_return") == 120, str(caps.get("max_return")))
        check("admin max_acca_legs applies (status shows 4)", caps.get("max_acca_legs") == 4, str(caps.get("max_acca_legs")))
        req("POST", "/api/settings", {"admin_key": KEY, "max_return": "", "max_acca_legs": 3})
        st, body = req("GET", "/api/status")
        caps = (json.loads(body).get("wager_caps") or {})
        check("blank max_return -> no cap (status null)", caps.get("max_return") is None, str(caps.get("max_return")))
        req("POST", "/api/settings", {"admin_key": KEY, "wagering_enabled": False})
        req("POST", "/api/settings", {"admin_key": KEY, "digest_enabled": True, "digest_hour": 7})
        st, body = req("GET", "/api/status")
        j = json.loads(body)
        check("digest settings persist (enabled + hour via status)",
              j.get("digest_enabled") is True and j.get("digest_hour") == 7, body[:160])
        st, body = req("GET", "/manifest.webmanifest")
        check("manifest served (200)", st == 200 and "{" in body, str(st))

        # CSV export: public, downloadable, has the standings header
        st, body = req("GET", "/api/export.csv")
        check("CSV export 200 + has header row", st == 200 and "Player,Points,Survival" in body, body[:80])

        # access log: admin-only, returns a summary, leaks no secrets; records real page views
        st, _ = req("POST", "/api/access_log", {"admin_key": "nope"})
        check("access_log needs admin key (403)", st == 403, str(st))
        req("GET", "/tracker"); req("GET", "/wheel")        # generate a couple of page views
        st, body = req("POST", "/api/access_log", {"admin_key": KEY})
        j = json.loads(body)
        check("access_log returns visitor summary", j.get("ok") and "visitors" in j and j.get("total_views", 0) >= 1, body[:160])
        check("access_log carries no secret", "admin_key" not in body and KEY not in body and "token" not in body, body[:160])

        # concurrency / stress: many parallel reads must not 500 or corrupt; the server is threaded
        import threading as _th
        results = []
        def _hit():
            try:
                results.append(req("GET", "/api/live_state")[0])
            except Exception as e:
                results.append(("ERR", str(e)))
        ths = [_th.Thread(target=_hit) for _ in range(40)]
        for t in ths: t.start()
        for t in ths: t.join()
        ok_codes = sum(1 for c in results if c == 200)
        check("40 concurrent requests, none error/500", ok_codes == 40, "200s=%d of %d: %s" % (ok_codes, len(results), set(results)))
        st, body = req("GET", "/api/status")                # server still healthy + JSON intact after the burst
        check("server healthy after burst (status still valid JSON)", st == 200 and json.loads(body).get("configured") is not None, str(st))

        leftover = [f for f in os.listdir(tmp) if f.endswith(".tmp")]
        check("no leftover .tmp files (atomic writes clean up)", not leftover, str(leftover))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        return 1
    print("All smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
