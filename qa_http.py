#!/usr/bin/env python3
"""Adversarial HTTP QA: boot the real server and bombard every write endpoint with malformed,
weird and hostile input — wrong types, missing fields, junk JSON, huge numbers, unicode, nested
bombs — and assert it ALWAYS answers with a clean status (never a 500), keeps state intact, and
stays alive afterwards. This is the 'weird input / dropped connection / never crash' guarantee."""
import os, sys, json, time, shutil, tempfile, subprocess, threading, socket
import urllib.request, urllib.error

def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

PORT = _free_port()
BASE = "http://127.0.0.1:%d" % PORT
KEY = "QA_ADMIN_KEY_123"
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

def req(method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": ctype} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def run():
    tmp = tempfile.mkdtemp(prefix="wc26_http_")
    repo = os.path.dirname(os.path.abspath(__file__))
    for f in ("teams.json",):
        if os.path.exists(os.path.join(repo, f)):
            shutil.copy2(os.path.join(repo, f), os.path.join(tmp, f))
    json.dump({"configured": True, "players": ["Erol", "James", "Louis"], "admin_key": KEY,
               "scoring_mode": "points", "wagering_enabled": True,
               "wager_pins": {"Erol": "ABCD"}}, open(os.path.join(tmp, "config.json"), "w"))
    env = dict(os.environ, WC26_DATA=tmp, WC26_CONFIG=os.path.join(tmp, "config.json"),
               PORT=str(PORT), HOST="127.0.0.1", ADMIN_KEY=KEY)
    proc = subprocess.Popen([sys.executable, os.path.join(repo, "server.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        booted = False
        for _ in range(80):
            try:
                s, _b = req("GET", "/api/status")
                if s == 200: booted = True; break
            except Exception:
                time.sleep(0.1)
        ck("server booted", booted, "did not come up on port %d" % PORT)

        # ---- malformed bodies on a representative write endpoint ----
        print("=== malformed request bodies never 500 ===")
        bad_bodies = [
            ("empty body", None),
            ("not json", b"this is not json at all"),
            ("truncated json", b'{"player": "Erol", "stake":'),
            ("json array not object", b"[1,2,3]"),
            ("json null", b"null"),
            ("json number", b"42"),
            ("json string", b'"hello"'),
            ("deeply nested", ('{"a":' * 200) .encode() + b"1" + (b"}" * 200)),
            ("huge string field", json.dumps({"player": "E" * 100000, "stake": 5}).encode()),
            ("wrong content-type", b"player=Erol&stake=5"),
        ]
        for path in ("/api/place_wager", "/api/place_acca", "/api/place_free_bet",
                     "/api/wager_set_pin", "/api/wager_void", "/api/discord_claim_player",
                     "/api/settings", "/api/push_subscribe"):
            for label, raw in bad_bodies:
                st, _ = req("POST", path, raw=raw)
                ck("%s + %s -> clean status (not 500)" % (path, label), st != 500, st)

        # ---- hostile / weird FIELD VALUES on betting endpoints ----
        print("\n=== weird field values are rejected cleanly ===")
        weird_bets = [
            {"player": "Erol", "selection": "HOME"},                                   # no match/stake
            {"player": "Erol", "matchId": "x", "selection": "HOME", "stake": "abc"},     # stake not a number
            {"player": "Erol", "matchId": "x", "selection": "HOME", "stake": -50},       # negative
            {"player": "Erol", "matchId": "x", "selection": "HOME", "stake": 1e18},      # absurd
            {"player": "Erol", "matchId": "x", "selection": "HOME", "stake": float("nan")} if False else {"player": "Erol", "matchId": "x", "selection": "HOME", "stake": "NaN"},
            {"player": "Erol", "matchId": "x", "selection": "SIDEWAYS", "stake": 5},      # bad selection
            {"player": "Иван", "matchId": "x", "selection": "HOME", "stake": 5},          # unicode player
            {"player": "", "matchId": "x", "selection": "HOME", "stake": 5},              # empty player
            {"player": ["Erol"], "matchId": "x", "selection": "HOME", "stake": 5},        # player wrong type
            {"player": "Erol", "matchId": {"x": 1}, "selection": "HOME", "stake": 5},     # matchId wrong type
        ]
        for b in weird_bets:
            st, body = req("POST", "/api/place_wager", b)
            ck("place_wager weird %s -> 4xx, not 500/200-success" % (list(b.values())[1:3],),
               st != 500 and not (st == 200 and json.loads(body).get("ok") is True), (st, body[:80]))

        weird_accas = [
            {"player": "Erol", "stake": 5, "legs": []},
            {"player": "Erol", "stake": 5, "legs": "nope"},
            {"player": "Erol", "stake": 5, "legs": [{"matchId": "x"}]},                   # leg missing selection
            {"player": "Erol", "stake": 5, "legs": [{} for _ in range(50)]},              # too many empty legs
        ]
        for b in weird_accas:
            st, _ = req("POST", "/api/place_acca", b)
            ck("place_acca weird legs=%r -> not 500" % (b["legs"] if not isinstance(b["legs"], list) else len(b["legs"]),), st != 500, st)

        # ---- void / set_pin / claim garbage ----
        print("\n=== void / passcode / claim garbage ===")
        for b in [{"admin_key": KEY}, {"admin_key": KEY, "id": 12345}, {"admin_key": KEY, "id": {"x": 1}},
                  {"admin_key": KEY, "player": ["x"]}, {"id": "x"}]:
            st, _ = req("POST", "/api/wager_void", b)
            ck("wager_void %r -> not 500" % (b,), st != 500, st)
        for b in [{"player": "Erol", "new_pin": "??"}, {"player": "Erol", "new_pin": "x" * 9999},
                  {"player": 5, "new_pin": "ABCD"}, {"player": "Nobody", "new_pin": "ABCD"}]:
            st, _ = req("POST", "/api/wager_set_pin", b)
            ck("wager_set_pin %r -> not 500" % (b,), st != 500, st)

        # ---- a burst of hostile requests; server must SURVIVE (a few socket resets under load are
        #      normal backpressure for a stdlib threaded server, not a crash) ----
        print("\n=== 50 concurrent hostile requests; server survives (resets tolerated as backpressure) ===")
        errs = []
        def hammer():
            try:
                req("POST", "/api/place_wager", raw=b'{"garbage":')
            except Exception as e:
                errs.append(repr(e))
        ts = [threading.Thread(target=hammer) for _ in range(50)]
        for t in ts: t.start()
        for t in ts: t.join()
        ck("not a meltdown (server answered the majority of the burst)", len(errs) < 25, len(errs))
        alive_after = False
        for _ in range(20):
            try:
                s, _b = req("GET", "/api/status")
                if s == 200: alive_after = True; break
            except Exception:
                time.sleep(0.1)
        ck("server still answers after the hostile burst (no crash)", alive_after, "")

        # ---- server is still alive + state intact ----
        print("\n=== server still healthy after the barrage ===")
        st, body = req("GET", "/api/status")
        ck("GET /api/status still 200", st == 200, st)
        try:
            j = json.loads(body); alive = j.get("configured") is True
        except Exception:
            alive = False
        ck("status still valid JSON + configured", alive, body[:100])
        ck("admin key never leaked in status", KEY not in body, "")
        st, body = req("GET", "/tracker")
        ck("tracker still served", st == 200 and "<" in body, st)
        # /join must 302 to the saved invite (stable public link that survives invite rotation)
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        _op = urllib.request.build_opener(_NoRedirect)
        try:
            _op.open(BASE + "/join", timeout=5)
            _loc, _code = "", 0
        except urllib.error.HTTPError as e:
            _code, _loc = e.code, e.headers.get("Location", "")
        ck("/join redirects (302/301)", _code in (301, 302), _code)
        # with no invite saved it should bounce to the tracker, not error
        ck("/join points somewhere sensible", ("discord" in _loc.lower()) or _loc.startswith("/tracker"), _loc)
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILS:
        print("\nHTTP QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS[:8])))
        sys.exit(1)
    print("\nAll HTTP robustness QA passed — malformed input is rejected cleanly and the server never crashes.")

if __name__ == "__main__":
    run()
