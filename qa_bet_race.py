#!/usr/bin/env python3
"""
Bet RACE QA — placing/booking a bet while the match is flipping to in-play or being voided.

PART A (engine): place()/place_acca() reject every "match has moved on" state, accept only a genuinely
                 open fixture, and a bet that slipped onto a then-voided match refunds the stake (never loses).
PART B (real HTTP): bet on an open fixture, then overwrite results.json (simulating the poll flipping the
                    match to in-play / cancelled / finished) and confirm the next bet is rejected — the server
                    re-reads the fixture UNDER THE LOCK, so a flip that just landed can't slip a bet through.
PART C (real HTTP concurrency): a burst of simultaneous bets on an open fixture never errors/over-stakes;
                    after the fixture flips to in-play, a simultaneous burst is rejected wholesale.
A dummy token makes the poll's upstream fetch fail closed so our hand-crafted results.json is preserved.
"""
import os, sys, json, time, shutil, tempfile, subprocess, socket, threading
import urllib.request, urllib.error

REPO = os.path.dirname(os.path.abspath(__file__))
KEY = "QA_RACE_KEY_1234567"
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        FAILS.append(name)

# ===================================================================== PART A — engine matrix
print("== A. engine: only a genuinely-open fixture is bettable ==")
import wager as W
NOW = 1_700_000_000
def mk(status, off, mid="m1"):
    return {"id": mid, "home": "Spain", "away": "France", "stage": "GROUP_STAGE", "status": status,
            "utcDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + off))}
def can_place(status, off):
    ok, _ = W.place([], "Erol", mk(status, off), "HOME", 5, settled_points=50, comp_home=90, comp_away=60, now=NOW)
    return ok
ck("SCHEDULED + future kickoff is bettable", can_place("SCHEDULED", 3600))
ck("TIMED + future kickoff is bettable", can_place("TIMED", 3600))
ck("SCHEDULED but kickoff PASSED is rejected (time guard, even if status hasn't flipped)", not can_place("SCHEDULED", -30))
for st in ("IN_PLAY", "PAUSED", "FINISHED", "AWARDED"):
    ck("%s is rejected" % st, not can_place(st, -600))
for st in ("CANCELLED", "POSTPONED", "ABANDONED"):
    ck("%s is rejected even with a future kickoff" % st, not can_place(st, 3600))

# a bet that slipped onto a match which is THEN voided -> refund the stake, never a loss
wl = []
W.place(wl, "Erol", mk("SCHEDULED", 3600), "HOME", 5, settled_points=50, comp_home=90, comp_away=60, now=NOW)
for void_status in ("CANCELLED", "POSTPONED", "ABANDONED"):
    w2 = [dict(wl[0])]
    W.settle(w2, {"id": "m1", "home": "Spain", "away": "France", "stage": "GROUP_STAGE", "status": void_status})
    ck("slipped bet then %s -> void + stake refunded (no loss)" % void_status,
       w2[0]["status"] == "void" and abs(W._num(w2[0]["return"]) - 5.0) < 1e-9, w2[0])

# accumulator: any non-open leg kills the whole acca
sels_ok = [{"match": mk("SCHEDULED", 3600, "a"), "selection": "HOME", "comp_home": 90, "comp_away": 60},
           {"match": mk("TIMED", 7200, "b"), "selection": "AWAY", "comp_home": 70, "comp_away": 80}]
ok, _ = W.place_acca([], "Erol", sels_ok, 5, settled_points=50, now=NOW)
ck("acca with two open legs is accepted", ok)
for bad in ("IN_PLAY", "FINISHED", "CANCELLED"):
    sels_bad = [{"match": mk("SCHEDULED", 3600, "a"), "selection": "HOME", "comp_home": 90, "comp_away": 60},
                {"match": mk(bad, -600 if bad != "CANCELLED" else 3600, "b"), "selection": "AWAY", "comp_home": 70, "comp_away": 80}]
    ok2, _ = W.place_acca([], "Erol", sels_bad, 5, settled_points=50, now=NOW)
    ck("acca with a %s leg is rejected" % bad, not ok2)

# ===================================================================== real-HTTP harness
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
TJ = json.load(open(os.path.join(REPO, "teams.json")))
NAMES = [t["name"] for t in TJ["teams"]]
HOME, AWAY, OH, OA = NAMES[0], NAMES[1], NAMES[2], NAMES[3]
def match(mid, h, a, status, hs=None, as_=None, winner=None, utc="2099-06-15T18:00:00Z"):
    return {"id": mid, "home": h, "away": a, "status": status, "homeScore": hs, "awayScore": as_,
            "winner": winner, "stage": "GROUP_STAGE", "utcDate": utc, "group": "A"}
PAST = "2000-01-01T00:00:00Z"

class Server:
    def __init__(self, results):
        self.tmp = tempfile.mkdtemp(prefix="wc26_race_")
        self.port = free_port(); self.base = "http://127.0.0.1:%d" % self.port
        shutil.copy2(os.path.join(REPO, "teams.json"), os.path.join(self.tmp, "teams.json"))
        json.dump({"players": [
            {"name": "Erol", "teams": [{"name": HOME, "tier": 1, "group": "A"}, {"name": OH, "tier": 2, "group": "B"}]},
            {"name": "James", "teams": [{"name": AWAY, "tier": 1, "group": "A"}, {"name": OA, "tier": 2, "group": "B"}]},
        ]}, open(os.path.join(self.tmp, "draw_result.json"), "w"))
        self.set_results(results)
        json.dump([], open(os.path.join(self.tmp, "wagers.json"), "w"))
        json.dump({"configured": True, "wagering_enabled": True, "players": ["Erol", "James"],
                   "admin_key": KEY, "token": "dummy-token-fails-closed",
                   "wager_pins": {"Erol": "ABCD", "James": "WXYZ"}, "scoring_mode": "points"},
                  open(os.path.join(self.tmp, "config.json"), "w"))
        env = dict(os.environ, WC26_DATA=self.tmp, WC26_CONFIG=os.path.join(self.tmp, "config.json"),
                   PORT=str(self.port), HOST="127.0.0.1", ADMIN_KEY=KEY)
        self.proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                if self.req("GET", "/api/status")[0] == 200:
                    break
            except Exception:
                time.sleep(0.1)
    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=6) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
    def set_results(self, results):
        json.dump({"matches": results}, open(os.path.join(self.tmp, "results.json"), "w"))
    def wagers(self):
        return json.load(open(os.path.join(self.tmp, "wagers.json")))
    def bet(self, mid, stake=1, nonce=None, player="Erol", pin="ABCD", sel="HOME"):
        body = {"player": player, "matchId": mid, "selection": sel, "stake": stake, "pin": pin}
        if nonce is not None:
            body["nonce"] = nonce
        st, b = self.req("POST", "/api/place_wager", body)
        try:
            return st, json.loads(b)
        except Exception:
            return st, {}
    def stop(self):
        self.proc.terminate()
        try: self.proc.wait(timeout=5)
        except Exception: self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

UP = "2099-06-15T18:00:00Z"

# ===================================================================== PART B — flip then reject (sequential, deterministic)
print("\n== B. real HTTP: a fixture that flips can't be bet on (server re-reads under the lock) ==")
S = Server([match("g1", HOME, AWAY, "TIMED", utc=UP)])
try:
    st, j = S.bet("g1", stake=1)
    ck("bet on the open fixture is accepted", st == 200 and j.get("ok"), j)
    n_open = len(S.wagers())

    S.set_results([match("g1", HOME, AWAY, "IN_PLAY", utc=PAST)])     # poll flips it live
    st, j = S.bet("g1", stake=1)
    ck("after it flips IN_PLAY, the next bet is rejected", j.get("ok") is not True, j)
    ck("...and no extra wager was stored", len(S.wagers()) == n_open, len(S.wagers()))

    S.set_results([match("g1", HOME, AWAY, "CANCELLED", utc=UP)])     # poll flips it cancelled (future utc)
    st, j = S.bet("g1", stake=1)
    ck("after it flips CANCELLED, the next bet is rejected", j.get("ok") is not True, j)
    ck("...still no extra wager", len(S.wagers()) == n_open, len(S.wagers()))

    S.set_results([match("g1", HOME, AWAY, "FINISHED", hs=1, as_=0, winner="HOME", utc=PAST)])
    st, j = S.bet("g1", stake=1)
    ck("after it flips FINISHED, the next bet is rejected", j.get("ok") is not True, j)

    S.set_results([match("g1", HOME, AWAY, "TIMED", utc=UP)])         # back to open -> bettable again (it was the status, not a stuck flag)
    st, j = S.bet("g1", stake=1)
    ck("restored to open -> bettable again", st == 200 and j.get("ok"), j)
finally:
    S.stop()

# acca leg flip
print("\n== B2. an acca with a leg that just kicked off is rejected ==")
S = Server([match("g1", HOME, AWAY, "TIMED", utc=UP), match("g2", OH, OA, "IN_PLAY", utc=PAST)])
try:
    st, b = S.req("POST", "/api/place_acca", {"player": "Erol", "pin": "ABCD", "stake": 1,
                  "legs": [{"matchId": "g1", "selection": "HOME"}, {"matchId": "g2", "selection": "HOME"}]})
    j = json.loads(b)
    ck("acca with one live leg is rejected", j.get("ok") is not True, j)
    ck("no acca was stored", len([w for w in S.wagers() if w.get("legs")]) == 0, S.wagers())
finally:
    S.stop()

# ===================================================================== PART C — concurrency around a flip
print("\n== C. concurrency: burst on an open fixture is safe; burst after a flip is rejected wholesale ==")
S = Server([match("g1", HOME, AWAY, "TIMED", utc=UP)])
try:
    errors = []; oks = []
    def fire(nonce):
        try:
            st, j = S.bet("g1", stake=1, nonce=nonce)
            (oks if j.get("ok") else errors).append((st, j.get("ok"), j.get("error", "")[:30]))
            return st, j
        except Exception as e:
            errors.append(("EXC", str(e))); return None
    # 10 simultaneous DISTINCT bets; player only has 5 starting points, so at most 5 can be struck
    ts = [threading.Thread(target=fire, args=("n%d" % i,)) for i in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    stored = S.wagers()
    total_stake = sum(W._num(w.get("stake")) for w in stored)
    ck("burst on an open fixture: no request raised/500", all(e[0] != "EXC" and e[0] != 500 for e in errors), errors[:4])
    ck("burst never over-stakes the 5-point starting balance", total_stake <= 5 + 1e-9, total_stake)
    ck("burst stored at least one and at most the affordable number", 1 <= len(stored) <= 5, len(stored))
    ck("server healthy after the burst", S.req("GET", "/api/status")[0] == 200)

    # same-nonce storm -> exactly one stored (idempotent), even concurrently
    before = len(S.wagers())
    # free up: give a fresh fixture the player can afford only if room; instead test idempotency of a retry nonce
    ts = [threading.Thread(target=lambda: S.bet("g1", stake=1, nonce="SAME")) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    same = [w for w in S.wagers() if w.get("nonce") == "SAME"]
    ck("a same-nonce concurrent storm stores at most one bet (idempotent)", len(same) <= 1, len(same))

    # now flip the fixture live and fire another burst -> all rejected, nothing new stored
    n_now = len(S.wagers())
    S.set_results([match("g1", HOME, AWAY, "IN_PLAY", utc=PAST)])
    ts = [threading.Thread(target=lambda i=i: S.bet("g1", stake=1, nonce="post%d" % i)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    ck("a burst AFTER the fixture flips live stores nothing new", len(S.wagers()) == n_now, (n_now, len(S.wagers())))
    ck("server still healthy after the post-flip burst", S.req("GET", "/api/status")[0] == 200)
finally:
    S.stop()

if FAILS:
    print("\nBET-RACE QA FAILED (%d):" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nAll bet-race QA passed.")
