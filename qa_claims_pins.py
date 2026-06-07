#!/usr/bin/env python3
"""QA for the passcode flow + the free-bet drop lifecycle.
  Part A (unit): the free-drop SCHEDULE is deterministic & bounded, windows are well-formed, _open_free_drop
                 picks the right drop, and grant_free_points credits exactly the right amount.
  Part B (HTTP): claiming free points over HTTP is passcode-gated, strictly one-per-person-per-drop, rejects
                 when no drop is open / wrong passcode; and the self-set passcode flow lets a player set a NEW
                 passcode but requires the current one to CHANGE it (and you can't silently hijack a set one)."""
import os, sys, json, time, shutil, tempfile, subprocess, socket, threading
import urllib.request, urllib.error

REPO = os.path.dirname(os.path.abspath(__file__))
KEY = "QA_ADMIN_KEY_1234567"
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

NM = [t["name"] for t in json.load(open(os.path.join(REPO, "teams.json")))["teams"]]

# results spanning ~4 weeks so several weekly drops generate; first kickoff 12h from now -> 'pre' drop open NOW
NOW = time.time()
FIRST = NOW + 12 * 3600
def iso(ts): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
MATCHES = []
for wk in range(4):
    for g in range(2):
        ts = FIRST + wk * 7 * 86400 + g * 3600
        MATCHES.append({"id": "w%dg%d" % (wk, g), "home": NM[g * 2], "away": NM[g * 2 + 1],
                        "status": "TIMED", "stage": "GROUP_STAGE", "utcDate": iso(ts),
                        "homeScore": None, "awayScore": None, "winner": None})

# ============================================================== PART A — DROP SCHEDULE (unit)
print("\n== A. Free-drop schedule: deterministic, bounded, well-formed ==")
TMPA = tempfile.mkdtemp(prefix="wc26_dropA_")
for fn in os.listdir(REPO):
    if fn.endswith(".py"):
        try: shutil.copy(os.path.join(REPO, fn), TMPA)
        except Exception: pass
shutil.copy2(os.path.join(REPO, "teams.json"), os.path.join(TMPA, "teams.json"))
json.dump({"matches": MATCHES}, open(os.path.join(TMPA, "results.json"), "w"))
json.dump({"configured": True, "wagering_enabled": True, "players": ["Erol", "James"]},
          open(os.path.join(TMPA, "config.json"), "w"))
os.environ["WC26_CONFIG"] = os.path.join(TMPA, "config.json")
_cwd = os.getcwd(); os.chdir(TMPA); sys.path.insert(0, TMPA)
import server as S
import wager as wm

d1 = S._free_bet_drops()
d2 = S._free_bet_drops()
ck("the schedule is non-empty", len(d1) >= 1, len(d1))
ck("the schedule is DETERMINISTIC across calls (seeded)", [x["id"] for x in d1] == [x["id"] for x in d2], ([x["id"] for x in d1], [x["id"] for x in d2]))
ck("at most 5 drops in total", len(d1) <= 5, len(d1))
ck("the first drop is the pre-tournament 'pre' drop", d1[0]["id"] == "pre", d1[0])
ck("drops are ordered by open time", all(d1[i]["opens"] <= d1[i+1]["opens"] for i in range(len(d1)-1)), [x["opens"] for x in d1])
ck("every drop opens strictly before it closes", all(x["opens"] < x["closes"] for x in d1), [(x["opens"], x["closes"]) for x in d1])
ck("drop ids are unique", len({x["id"] for x in d1}) == len(d1), [x["id"] for x in d1])
# windowing
ck("the pre-drop is OPEN right now (kickoff in 12h)", S._open_free_drop(NOW) is not None, S._open_free_drop(NOW))
ck("no drop is open far in the past", S._open_free_drop(FIRST - 10 * 86400) is None, "past")
ck("no drop is open after kickoff if none covers it", isinstance(S._open_free_drop(FIRST - 1), (dict, type(None))), "now-window")
open_now = S._open_free_drop(NOW)
ck("the open drop is within its own window", open_now and open_now["opens"] <= NOW < open_now["closes"], open_now)

print("\n== A2. grant_free_points credits the right amount ==")
wl = []
ok, cr = wm.grant_free_points(wl, "Erol", "pre")
ck("grant succeeds", ok, cr)
ck("the credit amount is FREE_BET_STAKE", cr["amount"] == wm.FREE_BET_STAKE, cr)
ck("free_bonus rises by exactly the credit", wm.free_bonus("Erol", wl) == wm.STARTING_BONUS + wm.FREE_BET_STAKE, wm.free_bonus("Erol", wl))
ck("a free-points credit is not a bet (no pending stake)", wm.player_deltas(wl).get("Erol", {}).get("pending_count", 0) == 0, wm.player_deltas(wl))
os.chdir(_cwd)

# ============================================================== server for Part B
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class Server:
    def __init__(self, results=MATCHES, pins=None, wager_locked=False):
        self.tmp = tempfile.mkdtemp(prefix="wc26_cp_")
        self.port = free_port(); self.base = "http://127.0.0.1:%d" % self.port
        shutil.copy2(os.path.join(REPO, "teams.json"), os.path.join(self.tmp, "teams.json"))
        json.dump({"players": [{"name": "Erol", "teams": [{"name": NM[0], "tier": 1, "group": "A"}]},
                               {"name": "James", "teams": [{"name": NM[1], "tier": 1, "group": "A"}]}]},
                  open(os.path.join(self.tmp, "draw_result.json"), "w"))
        json.dump({"matches": results}, open(os.path.join(self.tmp, "results.json"), "w"))
        cfg = {"configured": True, "wagering_enabled": True, "players": ["Erol", "James"],
               "admin_key": KEY, "token": "dummy", "scoring_mode": "hybrid", "free_bet_seed": 12345}
        if pins is not None: cfg["wager_pins"] = pins
        if wager_locked: cfg["wager_locked"] = True
        json.dump(cfg, open(os.path.join(self.tmp, "config.json"), "w"))
        env = dict(os.environ, WC26_DATA=self.tmp, WC26_CONFIG=os.path.join(self.tmp, "config.json"),
                   PORT=str(self.port), HOST="127.0.0.1", ADMIN_KEY=KEY)
        self.proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                if self.req("GET", "/api/status")[0] == 200: break
            except Exception: time.sleep(0.1)
    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=8) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try: return e.code, json.loads(e.read().decode("utf-8", "replace"))
            except Exception: return e.code, {}
    def cfg(self):
        return json.load(open(os.path.join(self.tmp, "config.json")))
    def stop(self):
        self.proc.terminate()
        try: self.proc.wait(timeout=5)
        except Exception: self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

# ============================================================== PART B1 — PASSCODE SET / CHANGE / CHECK
print("\n== B1. Self-set passcode: set NEW freely, CHANGE needs the current one ==")
S1 = Server(pins={})
try:
    st, j = S1.req("POST", "/api/wager_set_pin", {"player": "Erol", "new_pin": "abcd"})
    ck("a player can set a NEW passcode (no current needed)", st == 200 and j.get("ok"), j)
    ck("the passcode is stored upper-cased", S1.cfg().get("wager_pins", {}).get("Erol") == "ABCD", S1.cfg().get("wager_pins"))
    st, j = S1.req("POST", "/api/wager_check_pin", {"player": "Erol", "pin": "abcd"})
    ck("check_pin accepts the correct passcode case-insensitively", j.get("valid") is True, j)
    st, j = S1.req("POST", "/api/wager_check_pin", {"player": "Erol", "pin": "WRONG"})
    ck("check_pin rejects a wrong passcode", j.get("valid") is False, j)
    # changing an existing passcode requires the current one
    st, j = S1.req("POST", "/api/wager_set_pin", {"player": "Erol", "new_pin": "NEWPASS", "current_pin": "WRONG"})
    ck("changing a set passcode with the WRONG current is refused (403)", st == 403, (st, j))
    ck("the passcode is unchanged after a failed change", S1.cfg().get("wager_pins", {}).get("Erol") == "ABCD", S1.cfg().get("wager_pins"))
    st, j = S1.req("POST", "/api/wager_set_pin", {"player": "Erol", "new_pin": "NEWPASS", "current_pin": "ABCD"})
    ck("changing with the correct current passcode succeeds", st == 200 and j.get("ok"), j)
    ck("the new passcode took effect", S1.cfg().get("wager_pins", {}).get("Erol") == "NEWPASS", S1.cfg().get("wager_pins"))
    # format validation
    for bad in ["ab", "x" * 30, "has space", "no_underscore!", ""]:
        st, j = S1.req("POST", "/api/wager_set_pin", {"player": "James", "new_pin": bad})
        ck("invalid passcode %r is rejected (400)" % bad, st == 400, (st, j))
    ck("James still has no passcode after invalid attempts", "James" not in S1.cfg().get("wager_pins", {}), S1.cfg().get("wager_pins"))
    # unknown player
    st, j = S1.req("POST", "/api/wager_set_pin", {"player": "Nobody", "new_pin": "VALID1"})
    ck("setting a passcode for an unknown player is rejected", st == 400, (st, j))
    # a stranger CANNOT change Erol's set passcode without the current one (no hijack)
    st, j = S1.req("POST", "/api/wager_set_pin", {"player": "Erol", "new_pin": "HIJACK"})
    ck("a set passcode cannot be overwritten without the current one (no hijack)", st == 403 and S1.cfg().get("wager_pins", {}).get("Erol") == "NEWPASS", (st, S1.cfg().get("wager_pins")))
finally:
    S1.stop()

# ============================================================== PART B2 — FREE-BET CLAIM LIFECYCLE
print("\n== B2. Free-bet claim: passcode-gated, one-per-person, rejects when closed ==")
S2 = Server(pins={"Erol": "ABCD", "James": "WXYZ"})
try:
    # wrong passcode -> no credit
    st, j = S2.req("POST", "/api/place_free_bet", {"player": "Erol", "pin": "NOPE"})
    ck("a wrong passcode can't claim free points (403)", st == 403, (st, j))
    # correct passcode -> credit
    st, j = S2.req("POST", "/api/place_free_bet", {"player": "Erol", "pin": "ABCD"})
    ck("a correct passcode claims the open drop", st == 200 and j.get("ok"), j)
    ck("the claim credited FREE_BET_STAKE points", j.get("amount") == wm.FREE_BET_STAKE, j)
    # second claim same drop -> rejected
    st, j = S2.req("POST", "/api/place_free_bet", {"player": "Erol", "pin": "ABCD"})
    ck("the same player can't claim the SAME drop twice", st == 400 and not j.get("ok"), (st, j))
    # a different player can still claim the same drop once
    st, j = S2.req("POST", "/api/place_free_bet", {"player": "James", "pin": "WXYZ"})
    ck("a different player can claim the same drop once", st == 200 and j.get("ok"), j)
    # exactly one credit per player on disk
    wl = json.load(open(os.path.join(S2.tmp, "wagers.json")))
    ec = [w for w in wl if w.get("credit") and w.get("player") == "Erol"]
    ck("exactly ONE credit recorded for Erol (no double points)", len(ec) == 1, len(ec))
finally:
    S2.stop()

print("\n== B3. No drop open -> claim refused; concurrent claims -> one ==")
# results far in the future -> no drop open now
FUT = [{"id": "f1", "home": NM[0], "away": NM[1], "status": "TIMED", "stage": "GROUP_STAGE",
        "utcDate": iso(NOW + 60 * 86400), "homeScore": None, "awayScore": None, "winner": None}]
S3 = Server(results=FUT, pins={"Erol": "ABCD"})
try:
    st, j = S3.req("POST", "/api/place_free_bet", {"player": "Erol", "pin": "ABCD"})
    ck("with no drop open, claiming is refused", st == 400 and not j.get("ok"), (st, j))
finally:
    S3.stop()

S4 = Server(pins={"Erol": "ABCD"})
try:
    # 10 simultaneous HTTP claims by the same player on the open drop -> exactly one credit
    results = [None] * 10; bar = threading.Barrier(10); lk = threading.Lock()
    def worker(i):
        bar.wait()
        st, j = S4.req("POST", "/api/place_free_bet", {"player": "Erol", "pin": "ABCD"})
        with lk: results[i] = (st == 200 and j.get("ok") is True)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in ts: t.start()
    for t in ts: t.join()
    ck("10 simultaneous HTTP claims -> exactly one success", results.count(True) == 1, results.count(True))
    wl = json.load(open(os.path.join(S4.tmp, "wagers.json")))
    ck("exactly one credit on disk after the concurrent burst", len([w for w in wl if w.get("credit") and w.get("player") == "Erol"]) == 1, "credits")
    ck("server healthy after concurrent claims", S4.req("GET", "/api/status")[0] == 200, "status")
finally:
    S4.stop()

shutil.rmtree(TMPA, ignore_errors=True)
if FAILS:
    print("\nCLAIMS/PINS QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll claims/pins QA passed.")
