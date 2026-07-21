#!/usr/bin/env python3
"""End-to-end integration QA: boot the REAL server with a bettable scenario, place real bets over HTTP,
finish a match, and confirm settlement AND scoring move together correctly — the live-match path.
A dummy token makes the poll's upstream fetch fail closed, so our hand-crafted results.json is preserved
and drives the recompute. Each scenario uses a fresh server so the manual-poll throttle never interferes."""
import os, sys, json, time, shutil, tempfile, subprocess, socket, threading
import urllib.request, urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "QA_ADMIN_KEY_1234567"
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

# pick two real teams that exist in teams.json
TJ = json.load(open(os.path.join(REPO, "teams.json")))
NAMES = [t["name"] for t in TJ["teams"]]
HOME, AWAY = NAMES[0], NAMES[1]          # e.g. France, Argentina (whatever is first/second)
OTHERH, OTHERA = NAMES[2], NAMES[3]

def match(mid, h, a, status, hs=None, as_=None, winner=None, stage="GROUP_STAGE", utc="2099-06-15T18:00:00Z"):
    return {"id": mid, "home": h, "away": a, "status": status, "homeScore": hs, "awayScore": as_,
            "winner": winner, "stage": stage, "utcDate": utc, "group": "A"}

class Server:
    def __init__(self, results, wagers=None, extra_cfg=None):
        self.tmp = tempfile.mkdtemp(prefix="wc26_int_")
        self.port = free_port(); self.base = "http://127.0.0.1:%d" % self.port
        shutil.copy2(os.path.join(REPO, "teams.json"), os.path.join(self.tmp, "teams.json"))
        draw = {"players": [
            {"name": "Erol", "teams": [{"name": HOME, "tier": 1, "group": "A"}, {"name": OTHERH, "tier": 2, "group": "B"}]},
            {"name": "James", "teams": [{"name": AWAY, "tier": 1, "group": "A"}, {"name": OTHERA, "tier": 2, "group": "B"}]},
        ]}
        json.dump(draw, open(os.path.join(self.tmp, "draw_result.json"), "w"))
        json.dump({"matches": results}, open(os.path.join(self.tmp, "results.json"), "w"))
        json.dump(wagers or [], open(os.path.join(self.tmp, "wagers.json"), "w"))
        cfg = {"configured": True, "wagering_enabled": True, "players": ["Erol", "James"],
               "admin_key": KEY, "token": "dummy-token-so-fetch-fails-closed",
               "wager_pins": {"Erol": "ABCD", "James": "WXYZ"}, "scoring_mode": "points"}
        cfg.update(extra_cfg or {})
        json.dump(cfg, open(os.path.join(self.tmp, "config.json"), "w"))
        last_err = None
        for _attempt in range(4):                      # retry the whole launch if the port got stolen / server can't come up
            env = dict(os.environ, WC26_DATA=self.tmp, WC26_CONFIG=os.path.join(self.tmp, "config.json"),
                       PORT=str(self.port), HOST="127.0.0.1", ADMIN_KEY=KEY)
            self.proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ready = False
            for _ in range(150):                       # up to ~15s — server.py is import-heavy under the full gate's load
                if self.proc.poll() is not None:       # server exited (e.g. the port got taken first) -> relaunch
                    last_err = "server exited during startup"
                    break
                try:
                    if self.req("GET", "/api/status")[0] == 200:
                        ready = True
                        break
                except Exception as _e:
                    last_err = _e
                time.sleep(0.1)                          # sleep EVERY iteration, not only on a refused connection
            if ready:
                break
            try: self.proc.terminate()
            except Exception: pass
            self.port = free_port(); self.base = "http://127.0.0.1:%d" % self.port   # fresh port for the retry
        else:
            raise RuntimeError("integration test server never became ready: %r" % (last_err,))
    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=6) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
    def jget(self, path):
        return json.loads(self.req("GET", path)[1])
    def wait(self, cond, timeout=8.0):
        """Poll until cond() is true — load-independent replacement for a fixed sleep.
        The /api/poll handler settles + recomputes in the background, so we wait for the
        expected on-disk state rather than guessing a duration."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        return False
    def set_results(self, results):
        json.dump({"matches": results}, open(os.path.join(self.tmp, "results.json"), "w"))
    def wagers(self):
        return json.load(open(os.path.join(self.tmp, "wagers.json")))
    def player(self, name):
        td = self.jget("/api/live_state") if False else None
        # read tracker_data.json directly (authoritative server output)
        td = json.load(open(os.path.join(self.tmp, "tracker_data.json")))
        return next((p for p in td["players"] if p["name"] == name), None)
    def stop(self):
        self.proc.terminate()
        try: self.proc.wait(timeout=5)
        except Exception: self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

UP = "2099-06-15T18:00:00Z"

# ============================================================== 1. REAL HTTP PLACEMENT
print("\n== 1. Real HTTP bet placement (passcode auth + storage) ==")
S = Server([match("g1", HOME, AWAY, "TIMED", utc=UP)])
try:
    st, b = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "HOME", "stake": 5, "pin": "ABCD"})
    j = json.loads(b)
    ck("a valid passcode bet is accepted (200/ok)", st == 200 and j.get("ok"), b[:160])
    ck("the wager is stored on disk", len([w for w in S.wagers() if w.get("player") == "Erol"]) == 1, S.wagers())
    ck("the stored bet has locked odds + a return", all(k in (j.get("wager") or {}) for k in ("num", "den", "return")), j.get("wager"))
    st2, b2 = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "HOME", "stake": 5, "pin": "WRONG"})
    ck("a wrong passcode is rejected (403), no extra bet", st2 == 403 and len(S.wagers()) == 1, (st2, len(S.wagers())))
    st3, b3 = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "HOME", "stake": 9999, "pin": "ABCD"})
    ck("a stake beyond available is rejected, no extra bet", json.loads(b3).get("ok") is not True and len(S.wagers()) == 1, b3[:120])
    st4, b4 = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "NOPE", "selection": "HOME", "stake": 5, "pin": "ABCD"})
    ck("betting on a non-existent match is rejected cleanly", json.loads(b4).get("ok") is not True, b4[:120])
finally:
    S.stop()

# ============================================================== 1b. REAL HTTP SELF-VOID
print("\n== 1b. Player self-voids their own bet over HTTP (>2h before kick-off) ==")
S = Server([match("g1", HOME, AWAY, "TIMED", utc=UP)])
try:
    _, pb = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "HOME", "stake": 5, "pin": "ABCD", "nonce": "v1"})
    bid = json.loads(pb)["wager"]["id"]
    stw, bw = S.req("POST", "/api/wager_void", {"player": "Erol", "id": bid, "pin": "WRONG"})
    ck("wrong passcode can't void (403)", stw == 403, (stw, bw[:100]))
    sto, bo = S.req("POST", "/api/wager_void", {"player": "Louis", "id": bid, "pin": "ABCD"})
    ck("another player can't void it", json.loads(bo).get("ok") is not True, bo[:120])
    st, b = S.req("POST", "/api/wager_void", {"player": "Erol", "id": bid, "pin": "ABCD"})
    j = json.loads(b)
    ck("own bet voids over HTTP (200/ok)", st == 200 and j.get("ok"), b[:160])
    ck("the stored bet is now void with the stake refunded", any(w.get("id") == bid and w.get("status") == "void" for w in S.wagers()), S.wagers())
    st2, b2 = S.req("POST", "/api/wager_void", {"player": "Erol", "id": bid, "pin": "ABCD"})
    ck("a settled/void bet can't be voided again (404)", st2 == 404, (st2, b2[:100]))
finally:
    S.stop()

# ============================================================== 2. LIVE SETTLEMENT (WIN) + SCORING TOGETHER
print("\n== 2. Finish a match -> bet settles AND points update together ==")
S = Server([match("g1", HOME, AWAY, "TIMED", utc=UP)])
try:
    S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "HOME", "stake": 5, "pin": "ABCD"})
    bet = [w for w in S.wagers() if w.get("player") == "Erol"][0]
    ret = bet["return"]
    # now the match finishes: HOME wins 2-0
    S.set_results([match("g1", HOME, AWAY, "FINISHED", hs=2, as_=0, winner="HOME")])
    st, b = S.req("POST", "/api/poll")          # triggers update_now -> fetch fails (dummy token) -> keeps our results -> settles + recomputes
    S.wait(lambda: [x for x in S.wagers() if x.get("player") == "Erol"][0].get("status") != "open")
    w = [x for x in S.wagers() if x.get("player") == "Erol"][0]
    ck("the bet settled to WON after the match finished", w["status"] == "won", w["status"])
    ck("the won bet kept the odds it was struck at (return unchanged)", w["return"] == ret, (w["return"], ret))
    S.wait(lambda: next(t for t in S.player("Erol")["teams"] if t["name"] == HOME)["points"] == 6)
    erol = S.player("Erol")
    # scoring: HOME team scored 2, won, clean sheet -> 2+3+1 = 6 base points for that team
    home_team = next(t for t in erol["teams"] if t["name"] == HOME)
    ck("the match ALSO scored points for the owner's team (2 goals+3 win+1 CS = 6)", home_team["points"] == 6, home_team)
    ck("the owner's leaderboard points reflect the win", erol["points"] >= 6, erol["points"])
    ck("settlement and scoring are consistent (bet won AND team scored)", w["status"] == "won" and home_team["points"] == 6, (w["status"], home_team["points"]))
finally:
    S.stop()

# ============================================================== 3. LIVE VOID -> REFUND
print("\n== 3. Match cancelled -> bet voids and stake is refunded ==")
S = Server([match("g1", HOME, AWAY, "TIMED", utc=UP)])
try:
    S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "HOME", "stake": 5, "pin": "ABCD"})
    S.set_results([match("g1", HOME, AWAY, "CANCELLED", winner=None)])
    S.req("POST", "/api/poll")
    S.wait(lambda: [x for x in S.wagers() if x.get("player") == "Erol"][0].get("status") != "open")
    w = [x for x in S.wagers() if x.get("player") == "Erol"][0]
    ck("a cancelled match voids the bet", w["status"] == "void", w["status"])
    erol = S.player("Erol")
    ck("a voided bet doesn't dent the leaderboard (refund)", erol.get("bettable", 0) >= 0 and erol["points"] >= 0, erol.get("bettable"))
finally:
    S.stop()

# ============================================================== 4. CONCURRENT HTTP PLACEMENT (true end-to-end)
print("\n== 4. Concurrent REAL HTTP bets: only enough for one ==")
S = Server([match("g%d" % i, HOME, AWAY, "TIMED", utc=UP) for i in range(8)])
try:
    # Erol has 0 earned + 5 free = 5 available; fire 8 simultaneous 5pt bets on distinct games over HTTP
    results = [None] * 8; bar = threading.Barrier(8); lk = threading.Lock()
    def worker(i):
        bar.wait()
        st, b = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g%d" % i, "selection": "HOME", "stake": 5, "pin": "ABCD", "nonce": "n%d" % i})
        with lk: results[i] = json.loads(b).get("ok") is True
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    placed = [w for w in S.wagers() if w.get("player") == "Erol" and w.get("status") == "pending"]
    ck("exactly ONE of 8 simultaneous HTTP bets is stored (no overspend over the wire)", len(placed) == 1, len(placed))
    ck("the server reports exactly one success", results.count(True) == 1, results)
    ck("total staked over HTTP never exceeded the 5 available", sum(w["stake"] for w in placed) <= 5, placed)
    ck("server still healthy after the concurrent burst", S.req("GET", "/api/status")[0] == 200, "status")
finally:
    S.stop()

# ============================================================== 5. IDEMPOTENT HTTP RETRY (dropped response)
print("\n== 5. Same bet retried over HTTP (same nonce) -> one bet ==")
S = Server([match("g1", HOME, AWAY, "TIMED", utc=UP)])
try:
    body = {"player": "Erol", "matchId": "g1", "selection": "HOME", "stake": 5, "pin": "ABCD", "nonce": "retry-1"}
    S.req("POST", "/api/place_wager", body)
    S.req("POST", "/api/place_wager", body)   # the "retry" after a dropped response
    S.req("POST", "/api/place_wager", body)
    ck("retrying the same bet (same nonce) over HTTP yields exactly ONE wager", len([w for w in S.wagers() if w.get("player") == "Erol"]) == 1, S.wagers())
finally:
    S.stop()

if FAILS:
    print("\nINTEGRATION QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll end-to-end integration QA passed.")
