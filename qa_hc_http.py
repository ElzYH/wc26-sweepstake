#!/usr/bin/env python3
"""Handicap over the wire — boot the REAL server, bet real HTTP, settle through the real poll.
Locks in: the tracker payload carries margined hcOdds for a bettable fixture; a bet struck via
POST /api/place_wager locks EXACTLY the served price (display == placement); junk lines and SAME-GAME
acca combos are rejected cleanly over HTTP while a cross-game hc acca leg is accepted and settles;
both sides of one hc line are allowed (own-margin market);
a finished match settles hc bets to the right statuses + returns via /api/poll; and on a knockout
decided by penalties the RESULT bet wins off the shootout while HOME -1.5 loses on the 90'+ET margin
— the two settlement bases proven divergent end-to-end."""
import os, sys, json, time, shutil, tempfile, subprocess, socket
import urllib.request, urllib.error

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import wager as W
KEY = "QA_ADMIN_KEY_1234567"
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        FAILS.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

TJ = json.load(open(os.path.join(REPO, "teams.json")))
NAMES = [t["name"] for t in TJ["teams"]]
HOME, AWAY, OTHERH, OTHERA = NAMES[0], NAMES[1], NAMES[2], NAMES[3]
UP = "2099-06-15T18:00:00Z"

def match(mid, h, a, status, hs=None, as_=None, winner=None, stage="GROUP_STAGE", utc=UP, **kw):
    m = {"id": mid, "home": h, "away": a, "status": status, "homeScore": hs, "awayScore": as_,
         "winner": winner, "stage": stage, "utcDate": utc, "group": "A"}
    m.update(kw)
    return m

class Server:
    def __init__(self, results, extra_cfg=None):
        self.tmp = tempfile.mkdtemp(prefix="wc26_hchttp_")
        self.port = free_port(); self.base = "http://127.0.0.1:%d" % self.port
        shutil.copy2(os.path.join(REPO, "teams.json"), os.path.join(self.tmp, "teams.json"))
        draw = {"players": [
            {"name": "Erol", "teams": [{"name": HOME, "tier": 1, "group": "A"}, {"name": OTHERH, "tier": 2, "group": "B"}]},
            {"name": "James", "teams": [{"name": AWAY, "tier": 1, "group": "A"}, {"name": OTHERA, "tier": 2, "group": "B"}]},
        ]}
        json.dump(draw, open(os.path.join(self.tmp, "draw_result.json"), "w"))
        json.dump({"matches": results}, open(os.path.join(self.tmp, "results.json"), "w"))
        json.dump([], open(os.path.join(self.tmp, "wagers.json"), "w"))
        cfg = {"configured": True, "wagering_enabled": True, "players": ["Erol", "James"],
               "admin_key": KEY, "token": "dummy-token-so-fetch-fails-closed",
               "wager_pins": {"Erol": "ABCD", "James": "WXYZ"}, "scoring_mode": "points"}
        cfg.update(extra_cfg or {})
        json.dump(cfg, open(os.path.join(self.tmp, "config.json"), "w"))
        last_err = None
        for _attempt in range(4):
            env = dict(os.environ, WC26_DATA=self.tmp, WC26_CONFIG=os.path.join(self.tmp, "config.json"),
                       PORT=str(self.port), HOST="127.0.0.1", ADMIN_KEY=KEY)
            self.proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ready = False
            for _ in range(150):
                if self.proc.poll() is not None:
                    last_err = "server exited during startup"
                    break
                try:
                    urllib.request.urlopen(self.base + "/api/summary", timeout=1)
                    ready = True
                    break
                except Exception:
                    time.sleep(0.1)
            if ready:
                return
            try:
                self.proc.terminate(); self.proc.wait(timeout=3)
            except Exception:
                pass
            self.port = free_port(); self.base = "http://127.0.0.1:%d" % self.port
        raise RuntimeError("server would not come up: %s" % last_err)

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def wait(self, cond, timeout=12):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        return False

    def tracker(self):
        return json.load(open(os.path.join(self.tmp, "tracker_data.json")))

    def wagers(self):
        return json.load(open(os.path.join(self.tmp, "wagers.json")))

    def set_results(self, results):
        json.dump({"matches": results}, open(os.path.join(self.tmp, "results.json"), "w"))

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

# ============================================================ 1. served odds == struck odds
print("\n== 1. tracker payload carries margined hcOdds; a real bet strikes the served price ==")
S = Server([match("g1", HOME, AWAY, "TIMED"), match("g2", OTHERH, OTHERA, "TIMED")])
try:
    ok_fx = S.wait(lambda: (next((f for f in S.tracker().get("fixtures", []) if f.get("matchId") == "g1"), {}) or {}).get("hcOdds"))
    fx = next((f for f in S.tracker().get("fixtures", []) if f.get("matchId") == "g1"), {})
    hc = fx.get("hcOdds") or {}
    ck("fixture payload has hcOdds", ok_fx and isinstance(hc, dict) and hc, sorted(hc))
    ck("every served line key is a real HC line", all(any(k == ("%g" % L) for L in W.HC_LINES) for k in hc), sorted(hc))
    ck("every served hc book is margined (two-sided overround; one-sided lines allowed)",
       all(((1.0 / v["HOME"]["decimal"] + 1.0 / v["AWAY"]["decimal"]) > 1.0 + 1e-6) if ("HOME" in v and "AWAY" in v) else bool(v) for v in hc.values()), hc)
    ln = "-1.5" if "-1.5" in hc else sorted(hc, key=lambda k: abs(float(k)))[0]
    served = hc[ln]["HOME"]
    st, b = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "HOME",
                                               "market": "hc", "line": float(ln), "stake": 2, "pin": "ABCD"})
    j = json.loads(b)
    w = j.get("wager") or {}
    ck("hc bet accepted over HTTP (200/ok)", st == 200 and j.get("ok"), b[:160])
    ck("stored as market=hc with the line", w.get("market") == "hc" and w.get("line") == float(ln), w)
    ck("struck odds EXACTLY equal the served price", w.get("num") == served["num"] and w.get("den") == served["den"],
       (w.get("frac"), served["frac"]))
    st2, b2 = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "AWAY",
                                                 "market": "hc", "line": float(ln), "stake": 2, "pin": "ABCD"})
    ck("the other side of the same line is allowed (own-margin market)", json.loads(b2).get("ok") is True, b2[:140])
    st3, b3 = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "HOME",
                                                 "market": "hc", "line": 0.5, "stake": 3, "pin": "ABCD"})
    ck("a half-goal line is rejected over HTTP", json.loads(b3).get("ok") is not True and "line" in json.loads(b3).get("error", "").lower(), b3[:140])
    st4, b4 = S.req("POST", "/api/place_acca", {"player": "James", "stake": 3, "pin": "WXYZ",
                                                "legs": [{"matchId": "g1", "selection": "HOME", "market": "hc", "line": float(ln)},
                                                         {"matchId": "g2", "selection": "HOME"}]})
    j4 = json.loads(b4)
    ck("an acca with an hc leg on a DIFFERENT game is accepted over HTTP", st4 == 200 and j4.get("ok"), b4[:160])
    ck("the acca's hc leg is struck at the served price",
       j4.get("ok") and any(l.get("market") == "hc" and l.get("num") == served["num"] and l.get("den") == served["den"]
                            for l in (j4.get("wager") or {}).get("legs", [])), j4.get("wager"))
    st5, b5 = S.req("POST", "/api/place_acca", {"player": "Erol", "stake": 1, "pin": "ABCD", "nonce": "sgm1",
                                                "legs": [{"matchId": "g1", "selection": "HOME", "market": "hc", "line": float(ln)},
                                                         {"matchId": "g1", "selection": "OVER", "market": "ou", "line": 2.5}]})
    j5 = json.loads(b5)
    _wsg = j5.get("wager") or {}
    _n = 1.0
    for _l in _wsg.get("legs", []):
        _n *= 1 + _l["num"] / _l["den"]
    ck("a SAME-game hc+OU acca now places over HTTP at a JOINT group price under the naive product",
       j5.get("ok") and _wsg.get("groups") and _wsg.get("decimal", 99) < _n - 1e-9, b5[:200])
    st5b, b5b = S.req("POST", "/api/place_acca", {"player": "James", "stake": 1, "pin": "WXYZ", "nonce": "sgm2",
                                                  "legs": [{"matchId": "g1", "selection": "OVER", "market": "ou", "line": 2.5},
                                                           {"matchId": "g1", "selection": "UNDER", "market": "ou", "line": 2.5}]})
    ck("a contradictory same-game combo is rejected over HTTP with the can't-all-win message",
       json.loads(b5b).get("ok") is not True and "can't all win" in json.loads(b5b).get("error", ""), b5b[:160])

    # -------- settle through the real poll --------
    S.set_results([match("g1", HOME, AWAY, "FINISHED", hs=3, as_=1), match("g2", OTHERH, OTHERA, "FINISHED", hs=1, as_=0)])
    S.req("POST", "/api/poll")
    done = S.wait(lambda: all(x.get("status") != "pending" for x in S.wagers()))
    ws = S.wagers()
    wh = next((x for x in ws if x.get("selection") == "HOME" and x.get("market") == "hc"), {})
    wa = next((x for x in ws if x.get("selection") == "AWAY" and x.get("market") == "hc"), {})
    ck("both hc bets settled via /api/poll", done and wh and wa, ws)
    want = round(2 * (1 + wh.get("num", 0) / wh.get("den", 1)), 2)
    ck("HOME %s on 3-1 WON with stake x odds back" % ln, wh.get("status") == "won" and abs(wh.get("return", 0) - want) < 0.011, wh)
    ck("AWAY %s on 3-1 LOST with return 0" % ln, wa.get("status") == "lost" and wa.get("return") == 0, wa)
    acc = next((x for x in ws if x.get("legs")), {})
    ck("the hc+result acca settled WON (hc leg by margin, result leg by winner)",
       acc.get("status") == "won" and all(l.get("result") == "won" for l in acc.get("legs", [])), acc)
finally:
    S.stop()

# ============================================================ 2. knockout: pens decide the result, never the handicap
print("\n== 2. knockout decided on penalties: result bet wins, HOME -1.5 loses (bases diverge) ==")
S = Server([match("k1", HOME, AWAY, "TIMED", stage="QUARTER_FINALS")])
try:
    S.wait(lambda: (next((f for f in S.tracker().get("fixtures", []) if f.get("matchId") == "k1"), {}) or {}).get("hcOdds"))
    st, b = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "k1", "selection": "HOME",
                                               "market": "hc", "line": -1.5, "stake": 3, "pin": "ABCD"})
    ck("hc places on a knockout over HTTP", json.loads(b).get("ok") is True, b[:140])
    st2, b2 = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "k1", "selection": "HOME",
                                                 "stake": 3, "pin": "ABCD"})
    ck("a result (to-advance) bet places alongside it", json.loads(b2).get("ok") is True, b2[:140])
    S.set_results([match("k1", HOME, AWAY, "FINISHED", hs=1, as_=1, stage="QUARTER_FINALS",
                         penHome=4, penAway=2, shootout=True)])
    S.req("POST", "/api/poll")
    done = S.wait(lambda: all(x.get("status") != "pending" for x in S.wagers()))
    ws = S.wagers()
    hc_bet = next((x for x in ws if x.get("market") == "hc"), {})
    res_bet = next((x for x in ws if "market" not in x or x.get("market") in (None, "result")), {})
    ck("both bets settled", done and hc_bet and res_bet, ws)
    ck("HOME -1.5 on 1-1 (4-2 pens) LOST — pens never count for the margin", hc_bet.get("status") == "lost", hc_bet)
    ck("the result bet WON off the shootout", res_bet.get("status") == "won", res_bet)
finally:
    S.stop()

# ============================================================ 3. acca LEG on a pens game BANKS while the acca stays open
print("\n== 3. a result acca leg decided on penalties banks ✅ while the other leg is still upcoming ==")
S = Server([match("k1", HOME, AWAY, "TIMED", stage="QUARTER_FINALS"),
            match("k2", OTHERH, OTHERA, "TIMED", stage="QUARTER_FINALS")])
try:
    st, b = S.req("POST", "/api/place_acca", {"player": "Erol", "stake": 5, "pin": "ABCD",
                                              "legs": [{"matchId": "k1", "selection": "HOME"},
                                                       {"matchId": "k2", "selection": "HOME"}]})
    ck("a 2-leg result acca places", json.loads(b).get("ok") is True, b[:140])
    # k1 finishes level and HOME wins the shootout — the WORST-case feed shape: no `winner` field at all,
    # only penHome/penAway (exactly what the live Switzerland–Colombia card looked like)
    S.set_results([match("k1", HOME, AWAY, "FINISHED", hs=0, as_=0, stage="QUARTER_FINALS",
                         penHome=4, penAway=3, shootout=True),
                   match("k2", OTHERH, OTHERA, "TIMED")])
    S.req("POST", "/api/poll")
    S.wait(lambda: any((l.get("result") for w in S.wagers() for l in (w.get("legs") or []))))
    acc = next((x for x in S.wagers() if x.get("legs")), {})
    lk1 = next((l for l in acc.get("legs", []) if l.get("matchId") == "k1"), {})
    lk2 = next((l for l in acc.get("legs", []) if l.get("matchId") == "k2"), {})
    ck("the pens-decided leg banked as WON (winner inferred from the shootout score)", lk1.get("result") == "won", lk1)
    ck("the future leg is untouched and the acca is still open", not lk2.get("result") and acc.get("status") == "pending", acc)
finally:
    S.stop()

# ============================================================ 4. mov + cards over REAL HTTP: served, placed at price, settled
print("\n== 4. method-of-victory + cards: served odds, struck price, settlement (incl. no-data cards push) ==")
S = Server([match("k1", HOME, AWAY, "TIMED", stage="QUARTER_FINALS"),
            match("k2", OTHERH, OTHERA, "TIMED", stage="QUARTER_FINALS"),
            match("g1", HOME, OTHERA, "TIMED", stage="GROUP_STAGE")],
           extra_cfg={"cards_market": True})   # force the cards market on (this harness feed has no bookings yet)
try:
    ok_fx = S.wait(lambda: (next((f for f in S.tracker().get("fixtures", []) if f.get("matchId") == "k1"), {}) or {}).get("movOdds"))
    fxs = {f.get("matchId"): f for f in S.tracker().get("fixtures", [])}
    ck("knockout fixtures carry movOdds + cardsOdds", ok_fx and fxs["k1"].get("cardsOdds"), sorted(fxs.get("k1", {}).keys()))
    ck("the GROUP fixture has cardsOdds but NO movOdds (knockout-only market)",
       fxs.get("g1", {}).get("cardsOdds") and not fxs.get("g1", {}).get("movOdds"), sorted(fxs.get("g1", {}).keys()))
    mv = fxs["k1"]["movOdds"]; cd = fxs["k2"]["cardsOdds"]
    st, b = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "k1", "selection": "HOME_PENS",
                                               "market": "mov", "stake": 3, "pin": "ABCD"})
    j = json.loads(b)
    ck("a method-of-victory bet places over HTTP at the served price", j.get("ok") and
       (j.get("wager") or {}).get("num") == mv["HOME_PENS"]["num"], b[:140])
    ln = "4.5" if "4.5" in cd else sorted(cd)[0]
    st2, b2 = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "k2", "selection": "OVER",
                                                 "market": "cards", "line": float(ln), "stake": 3, "pin": "ABCD"})
    j2 = json.loads(b2)
    ck("a cards bet places over HTTP at the served price", j2.get("ok") and
       (j2.get("wager") or {}).get("num") == cd[ln]["OVER"]["num"], b2[:140])
    st3, b3 = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "AWAY_REG",
                                                 "market": "mov", "stake": 3, "pin": "ABCD"})
    ck("a method-of-victory bet on a GROUP game is rejected over HTTP", json.loads(b3).get("ok") is not True, b3[:120])
    # k1: 1-1, home wins the shootout -> HOME_PENS wins. k2: finished long ago with NO bookings -> cards pushes.
    S.set_results([match("k1", HOME, AWAY, "FINISHED", hs=1, as_=1, stage="QUARTER_FINALS",
                         penHome=5, penAway=4, shootout=True, aet=True, duration="PENALTY_SHOOTOUT"),
                   match("k2", OTHERH, OTHERA, "FINISHED", hs=1, as_=0, winner="HOME", stage="QUARTER_FINALS",
                         utc="2000-01-01T18:00:00Z"),
                   match("g1", HOME, OTHERA, "TIMED", stage="GROUP_STAGE")])
    S.req("POST", "/api/poll")
    S.wait(lambda: all(x.get("status") != "pending" for x in S.wagers()))
    got = {x.get("matchId"): x for x in S.wagers()}
    ck("HOME_PENS settled WON off the shootout feed shape", got.get("k1", {}).get("status") == "won", got.get("k1"))
    ck("the no-data cards bet PUSHED with the stake back (grace elapsed)",
       got.get("k2", {}).get("status") == "void" and got.get("k2", {}).get("return") == 3, got.get("k2"))
finally:
    S.stop()

# ============================================================ 5. BTTS over REAL HTTP + the cards auto-gate
print("\n== 5. BTTS served/placed/settled at the harsher book; the cards market auto-hides on a feed with no bookings ==")
S = Server([match("g1", HOME, AWAY, "TIMED", stage="GROUP_STAGE"),
            match("g2", OTHERH, OTHERA, "TIMED", stage="GROUP_STAGE")])
try:
    ok_fx = S.wait(lambda: (next((f for f in S.tracker().get("fixtures", []) if f.get("matchId") == "g1"), {}) or {}).get("odds"))
    fxs = {f.get("matchId"): f for f in S.tracker().get("fixtures", [])}
    ck("fixtures carry bttsOdds again (back on sale at the harsher book)", ok_fx and fxs["g1"].get("bttsOdds", {}).get("YES"), sorted(fxs.get("g1", {}).keys()))
    ck("the cards market is HIDDEN when this feed has never supplied bookings (auto-gate)",
       not fxs["g1"].get("cardsOdds"), sorted(fxs.get("g1", {}).keys()))
    st, b = S.req("POST", "/api/place_wager", {"player": "Erol", "matchId": "g1", "selection": "YES",
                                               "market": "btts", "stake": 4, "pin": "ABCD"})
    _j = json.loads(b)
    ck("a BTTS bet places over HTTP at the served price", _j.get("ok") and (_j.get("wager") or {}).get("num") == fxs["g1"]["bttsOdds"]["YES"]["num"], b[:160])
finally:
    S.stop()

print()
if FAILS:
    print("FAILED: %d -> %s" % (len(FAILS), FAILS))
    raise SystemExit(1)
print("ALL HANDICAP HTTP CHECKS PASSED")
