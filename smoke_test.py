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

        st, body = req("POST", "/api/redraw", {})                        # not locked, no key needed
        check("redraw responds ok", st == 200, str(st))

        st, body = req("GET", "/api/telegram_links")                     # dead but must not 500
        check("telegram_links doesn't 500 on string players", st == 200, str(st))
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
