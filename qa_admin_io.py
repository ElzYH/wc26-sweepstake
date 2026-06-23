#!/usr/bin/env python3
"""Admin & I/O QA across three areas:
  (1) settings caps validation — junk/extreme values are clamped to safe bounds, never crash, never
      produce a negative/zero max-return;
  (2) export/import round-trip — data survives a round-trip, secrets (token/admin_key) are NEVER exported
      or injectable via import, a malformed bundle is rejected, and a corrupt draw inside a bundle can't
      crash the rebuild;
  (3) hostile/oversized payloads on the admin write endpoints — huge strings, deep nesting, wrong types
      all get a clean status (never a 500) and the server stays alive.
Part A is unit-level (fast); Parts B & C boot the real server."""
import os, sys, json, time, shutil, tempfile, subprocess, socket
import urllib.request, urllib.error

REPO = os.path.dirname(os.path.abspath(__file__))
KEY = "QA_ADMIN_KEY_1234567"
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

# ============================================================== PART A — CAPS VALIDATION (unit)
print("\n== A. Settings caps validation (clamp junk/extreme, no negative max-return) ==")
TMPA = tempfile.mkdtemp(prefix="wc26_capsA_")
for fn in os.listdir(REPO):
    if fn.endswith(".py"):
        try: shutil.copy(os.path.join(REPO, fn), TMPA)
        except Exception: pass
json.dump({"configured": True}, open(os.path.join(TMPA, "config.json"), "w"))
os.environ["WC26_CONFIG"] = os.path.join(TMPA, "config.json")
_cwd = os.getcwd(); os.chdir(TMPA); sys.path.insert(0, TMPA)
import server as S
import wager as wm

def apply(cfg):
    S._apply_wager_caps(cfg)
    return dict(MAX_PENDING=wm.MAX_PENDING, MAX_ACTIVE_ACCAS=wm.MAX_ACTIVE_ACCAS,
                MAX_RETURN=wm.MAX_RETURN, MAX_ACCA_LEGS=wm.MAX_ACCA_LEGS)

c = apply({"max_pending_bets": -5})
ck("negative max_pending clamps to >= 1", c["MAX_PENDING"] >= 1, c["MAX_PENDING"])
c = apply({"max_pending_bets": 9999})
ck("huge max_pending clamps to <= 50", c["MAX_PENDING"] <= 50, c["MAX_PENDING"])
c = apply({"max_pending_bets": "abc"})
ck("junk max_pending falls back to the default (8)", c["MAX_PENDING"] == wm.MAX_PENDING and c["MAX_PENDING"] == 8, c["MAX_PENDING"])
c = apply({"max_pending_bets": ""})
ck("blank max_pending -> default", c["MAX_PENDING"] == 8, c["MAX_PENDING"])
c = apply({"max_active_accas": -3})
ck("negative max_active_accas clamps to >= 0", c["MAX_ACTIVE_ACCAS"] >= 0, c["MAX_ACTIVE_ACCAS"])
c = apply({"max_active_accas": 999})
ck("huge max_active_accas clamps to <= 20", c["MAX_ACTIVE_ACCAS"] <= 20, c["MAX_ACTIVE_ACCAS"])
c = apply({"max_active_accas": 0})
ck("max_active_accas 0 is allowed (accas off)", c["MAX_ACTIVE_ACCAS"] == 0, c["MAX_ACTIVE_ACCAS"])
c = apply({"max_acca_legs": 99})
ck("max_acca_legs clamps to <= 10", c["MAX_ACCA_LEGS"] <= 10, c["MAX_ACCA_LEGS"])
c = apply({"max_acca_legs": 1})
ck("max_acca_legs clamps to >= 2", c["MAX_ACCA_LEGS"] >= 2, c["MAX_ACCA_LEGS"])
c = apply({"max_return": -5})
ck("NEGATIVE max_return is clamped to >= 1 (no negative returns)", c["MAX_RETURN"] is None or c["MAX_RETURN"] >= 1.0, c["MAX_RETURN"])
c = apply({"max_return": 0.5})
ck("sub-1 max_return is clamped to >= 1", c["MAX_RETURN"] is None or c["MAX_RETURN"] >= 1.0, c["MAX_RETURN"])
c = apply({"max_return": 0})
ck("max_return 0 means unlimited (None)", c["MAX_RETURN"] is None, c["MAX_RETURN"])
c = apply({"max_return": "none"})
ck("max_return 'none' means unlimited (None)", c["MAX_RETURN"] is None, c["MAX_RETURN"])
c = apply({"max_return": "abc"})
ck("junk max_return -> unlimited (None), no crash", c["MAX_RETURN"] is None, c["MAX_RETURN"])
c = apply({"max_return": 50})
ck("a valid max_return is kept", c["MAX_RETURN"] == 50.0, c["MAX_RETURN"])
# prove the negative-return bug is gone end to end: cap=-5 should NOT make a winning bet pay negative
apply({"max_return": -5})
w = []
ok, res = wm.place(w, "Erol", {"id": "m1", "home": "Brazil", "away": "Serbia", "stage": "GROUP_STAGE",
                               "status": "TIMED", "utcDate": "2099-06-15T18:00:00Z"},
                   "HOME", 5, settled_points=999, comp_home=80, comp_away=50, now=1_700_000_000)
ck("with max_return=-5 applied, a placed bet's return is still positive", (not ok) or res["return"] > 0, res if ok else "rejected")
apply({"max_return": 0})   # restore unlimited for later
os.chdir(_cwd)

# ============================================================== server harness for B & C
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class Server:
    def __init__(self, with_data=True):
        self.tmp = tempfile.mkdtemp(prefix="wc26_io_")
        self.port = free_port(); self.base = "http://127.0.0.1:%d" % self.port
        shutil.copy2(os.path.join(REPO, "teams.json"), os.path.join(self.tmp, "teams.json"))
        if with_data:
            nm = [t["name"] for t in json.load(open(os.path.join(REPO, "teams.json")))["teams"]]
            json.dump({"players": [{"name": "Erol", "teams": [{"name": nm[0], "tier": 1, "group": "A"}]},
                                   {"name": "James", "teams": [{"name": nm[1], "tier": 1, "group": "A"}]}]},
                      open(os.path.join(self.tmp, "draw_result.json"), "w"))
            json.dump({"matches": [{"id": "g1", "home": nm[0], "away": nm[1], "status": "TIMED",
                                    "stage": "GROUP_STAGE", "utcDate": "2099-06-15T18:00:00Z",
                                    "homeScore": None, "awayScore": None, "winner": None}]},
                      open(os.path.join(self.tmp, "results.json"), "w"))
        json.dump({"configured": True, "wagering_enabled": True, "players": ["Erol", "James"],
                   "admin_key": KEY, "token": "dummy-token", "draw_locked": True,
                   "max_pending_bets": 6, "scoring_mode": "points"},
                  open(os.path.join(self.tmp, "config.json"), "w"))
        env = dict(os.environ, WC26_DATA=self.tmp, WC26_CONFIG=os.path.join(self.tmp, "config.json"),
                   PORT=str(self.port), HOST="127.0.0.1", ADMIN_KEY=KEY)
        self.proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                if self.req("GET", "/api/status")[0] == 200: break
            except Exception: time.sleep(0.1)
    def req(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=8) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
    def cfg(self):
        return json.load(open(os.path.join(self.tmp, "config.json")))
    def stop(self):
        self.proc.terminate()
        try: self.proc.wait(timeout=5)
        except Exception: self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

# ============================================================== PART B — EXPORT / IMPORT ROUND-TRIP + WHITELIST
print("\n== B. Export/import round-trip + secret whitelist ==")
S1 = Server()
try:
    st, b = S1.req("POST", "/api/export", {"admin_key": KEY})
    ck("export returns 200 with the admin key", st == 200, st)
    bundle = json.loads(b)
    ck("export bundle has draw_result + results + wagers + config", all(k in bundle for k in ("draw_result", "results", "wagers", "config")), list(bundle.keys()))
    ck("export NEVER includes the admin_key", "admin_key" not in bundle.get("config", {}), bundle.get("config", {}).keys())
    ck("export NEVER includes the upstream token", "token" not in bundle.get("config", {}), bundle.get("config", {}).keys())
    st, b = S1.req("POST", "/api/export", {})
    ck("export without the key is refused (403) when draw is locked", st == 403, st)
    # round-trip: import the exported bundle back, draw should be preserved
    st, b = S1.req("POST", "/api/import", {"admin_key": KEY, "bundle": bundle})
    ck("re-importing the exported bundle succeeds (round-trip)", json.loads(b).get("ok") is True, b[:160])
    # whitelist on import: a bundle trying to inject admin_key / token must NOT overwrite them
    evil = json.loads(json.dumps(bundle))
    evil["config"]["admin_key"] = "HACKED"; evil["config"]["token"] = "STOLEN"
    evil["config"]["discord_bot_token"] = "PWNED"
    st, b = S1.req("POST", "/api/import", {"admin_key": KEY, "bundle": evil})
    ck("import succeeds but ignores injected secrets", json.loads(b).get("ok") is True, b[:120])
    after = S1.cfg()
    ck("injected admin_key was NOT applied (real key intact)", after.get("admin_key") == KEY, "admin_key changed!")
    ck("injected token was NOT applied", after.get("token") != "STOLEN", after.get("token"))
    ck("injected discord_bot_token was NOT applied", after.get("discord_bot_token") != "PWNED", after.get("discord_bot_token"))
    # admin key still works after the evil import (we weren't locked out)
    st, b = S1.req("POST", "/api/export", {"admin_key": KEY})
    ck("the real admin key still works after a malicious import", st == 200, st)
    # malformed bundles
    st, b = S1.req("POST", "/api/import", {"admin_key": KEY, "bundle": {"nonsense": True}})
    ck("a bundle with no draw_result is rejected (400)", st == 400, st)
    st, b = S1.req("POST", "/api/import", {"admin_key": KEY, "bundle": "notadict"})
    ck("a non-dict bundle is rejected cleanly", st == 400, st)
    # a corrupt DRAW inside an otherwise-valid bundle must not crash the rebuild
    corrupt = {"draw_result": {"players": [{"name": "Erol"},  # no teams key
                                           {"name": "James", "teams": ["junk", None, {"noname": 1},
                                                                        {"name": "Brazil"}]}]},
               "results": {"matches": []}, "wagers": []}
    st, b = S1.req("POST", "/api/import", {"admin_key": KEY, "bundle": corrupt})
    ck("importing a corrupt draw doesn't 500 the endpoint", st in (200, 400), st)
    ck("server still healthy after a corrupt-draw import (no frozen tracker)", S1.req("GET", "/api/status")[0] == 200, "status")
finally:
    S1.stop()

# ============================================================== PART C — HOSTILE / OVERSIZED PAYLOADS
print("\n== C. Hostile / oversized payloads on admin write endpoints ==")
S2 = Server()
try:
    BIG = "A" * 200000
    cases = [
        ("/api/settings", {"admin_key": KEY, "competition": BIG}),
        ("/api/settings", {"admin_key": KEY, "max_pending_bets": BIG}),
        ("/api/settings", {"admin_key": KEY, "max_return": ["not", "a", "number"]}),
        ("/api/settings", {"admin_key": KEY, "max_active_accas": {"nested": "bomb"}}),
        ("/api/settings", {"admin_key": KEY, "digest_hour": 9999}),
        ("/api/settings", {"admin_key": KEY, "max_acca_legs": -100}),
        ("/api/import", {"admin_key": KEY, "bundle": {"draw_result": {"players": BIG}}}),
        ("/api/import", {"admin_key": KEY, "bundle": {"draw_result": {"players": [{"name": BIG, "teams": []}]}}}),
        ("/api/place_wager", {"player": BIG, "matchId": BIG, "selection": "HOME", "stake": "NaN", "pin": "x"}),
        ("/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": 12345, "stake": {"x": 1}, "pin": "ABCD"}),
    ]
    for path, body in cases:
        st, b = S2.req("POST", path, body)
        ck("no 500 on hostile %s (%s)" % (path, list(body.keys())[-1]), st != 500, (st, b[:80]))
    # raw junk bodies
    for raw in [b"", b"{", b"not json at all", b"[]", b"null", b'{"admin_key":"' + KEY.encode() + b'"', ("{\"x\":\"" + "z" * 100000 + "\"}").encode()]:
        st, b = S2.req("POST", "/api/settings", raw=raw)
        ck("no 500 on raw junk settings body (%d bytes)" % len(raw), st != 500, (st, b[:60]))
    ck("server still answers /api/status after the hostile barrage", S2.req("GET", "/api/status")[0] == 200, "status")
    ck("config.json is still valid JSON after the barrage (not corrupted)", isinstance(S2.cfg(), dict), "cfg")
    ck("admin key still works after the barrage", S2.req("POST", "/api/export", {"admin_key": KEY})[0] == 200, "export")
finally:
    S2.stop()

shutil.rmtree(TMPA, ignore_errors=True)
if FAILS:
    print("\nADMIN/IO QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll admin/IO QA passed.")
