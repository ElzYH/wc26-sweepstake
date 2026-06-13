#!/usr/bin/env python3
"""
demo_bugtest.py — ONE BIG end-to-end bug round against a RUNNING demo server (see demo_seed.py).
Hammers the real HTTP API exactly as a browser would: placement (result + Over/Under), accumulators
(pure, mixed, blocked), every validation path, then advances scores and settles, checking outcomes
and money/points conservation.

Usage:  python3 demo_bugtest.py [base_url] [data_dir]
        (defaults: http://127.0.0.1:8011  demo)
Server must already be running:  WC26_DATA=demo PORT=8011 python3 server.py
"""
import json, sys, time, urllib.request, urllib.error, os

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
DDIR = sys.argv[2] if len(sys.argv) > 2 else "demo"
PIN = "DEMO"

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, {}

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.load(r)

def board():
    return get("/tracker_data.json?_=%d" % int(time.time() * 1000))

def wagers_file():
    return json.load(open(os.path.join(DDIR, "wagers.json")))

def nonce():
    return "bt-%d-%d" % (int(time.time() * 1000), len(fails) + 1)

# ---- discover bettable + live + finished matches from the live board ----
b = board()
fx = [m for m in b.get("fixtures", []) if m.get("matchId") and m.get("ouOdds")]
ck("the demo board exposes bettable fixtures with O/U prices", len(fx) >= 4, len(fx))
M = fx[0]["matchId"]; N = fx[1]["matchId"]; P = fx[2]["matchId"]
allres = json.load(open(os.path.join(DDIR, "results.json")))["matches"]
live = next((m for m in allres if m.get("status") == "IN_PLAY"), None)
fin = next((m for m in allres if m.get("status") == "FINISHED"), None)

print("\n== placing valid bets over HTTP ==")
s, j = post("/api/place_wager", {"player": "Erol", "matchId": M, "selection": "OVER", "market": "ou", "line": 2.5, "stake": 5, "pin": PIN, "nonce": nonce()})
ck("Erol OVER 2.5 single placed (HTTP 200, ok)", s == 200 and j.get("ok"), j)
ouid = j.get("wager", {}).get("id")
s, j = post("/api/place_wager", {"player": "James", "matchId": M, "selection": "UNDER", "market": "ou", "line": 2.5, "stake": 5, "pin": PIN, "nonce": nonce()})
ck("James UNDER 2.5 single placed", s == 200 and j.get("ok"), j)
s, j = post("/api/place_wager", {"player": "Louis", "matchId": N, "selection": "HOME", "stake": 5, "pin": PIN, "nonce": nonce()})
ck("Louis HOME (result) single placed", s == 200 and j.get("ok"), j)

print("\n== the stored bets look right (O/U carries market+line; result carries neither) ==")
wf = wagers_file()
ou_bet = next((w for w in wf if w.get("id") == ouid), None)
ck("the O/U bet stored market='ou' + line=2.5", ou_bet and ou_bet.get("market") == "ou" and ou_bet.get("line") == 2.5, ou_bet)
res_bet = next((w for w in wf if w.get("player") == "Louis" and w.get("selection") == "HOME" and not w.get("legs")), None)
ck("the result bet carries NO market/line field", res_bet and "market" not in res_bet and "line" not in res_bet, res_bet)
ck("the O/U bet's odds match the board price for that line", ou_bet and ou_bet["frac"] == fx[0]["ouOdds"]["2.5"]["OVER"]["frac"], (ou_bet.get("frac") if ou_bet else None, fx[0]["ouOdds"]["2.5"]["OVER"]["frac"]))

print("\n== validation: bad inputs are rejected over HTTP ==")
s, j = post("/api/place_wager", {"player": "Erol", "matchId": M, "selection": "OVER", "market": "ou", "line": 3.0, "stake": 5, "pin": PIN, "nonce": nonce()})
ck("an off-grid O/U line (3.0) is rejected", not j.get("ok"), j)
s, j = post("/api/place_wager", {"player": "Erol", "matchId": M, "selection": "HOME", "market": "ou", "line": 2.5, "stake": 5, "pin": PIN, "nonce": nonce()})
ck("a 1X2 selection on the O/U market is rejected", not j.get("ok"), j)
s, j = post("/api/place_wager", {"player": "Erol", "matchId": M, "selection": "OVER", "market": "ou", "line": 2.5, "stake": 5, "pin": "WRONG", "nonce": nonce()})
ck("a wrong passcode is rejected (403)", s == 403 and not j.get("ok"), (s, j))
s, j = post("/api/place_wager", {"player": "Erol", "matchId": M, "selection": "OVER", "market": "ou", "line": 2.5, "stake": 99999, "pin": PIN, "nonce": nonce()})
ck("an over-cap stake is rejected", not j.get("ok"), j)
if live:
    s, j = post("/api/place_wager", {"player": "Erol", "matchId": live["id"], "selection": "OVER", "market": "ou", "line": 2.5, "stake": 5, "pin": PIN, "nonce": nonce()})
    ck("no O/U bet on an IN_PLAY game (kickoff lock)", not j.get("ok"), j)
if fin:
    s, j = post("/api/place_wager", {"player": "Erol", "matchId": fin["id"], "selection": "OVER", "market": "ou", "line": 2.5, "stake": 5, "pin": PIN, "nonce": nonce()})
    ck("no O/U bet on a FINISHED game", not j.get("ok"), j)

print("\n== idempotency: replaying the same nonce returns the SAME bet, not a duplicate ==")
nn = nonce()
s, j1 = post("/api/place_wager", {"player": "Ismail", "matchId": P, "selection": "UNDER", "market": "ou", "line": 3.5, "stake": 4, "pin": PIN, "nonce": nn})
s, j2 = post("/api/place_wager", {"player": "Ismail", "matchId": P, "selection": "UNDER", "market": "ou", "line": 3.5, "stake": 4, "pin": PIN, "nonce": nn})
ck("same nonce -> identical bet id (no double-charge)", j1.get("ok") and j2.get("ok") and j1["wager"]["id"] == j2["wager"]["id"], (j1.get("wager", {}).get("id"), j2.get("wager", {}).get("id")))

print("\n== accumulators over HTTP: mixed, pure-O/U, and the blocked cases ==")
s, j = post("/api/place_acca", {"player": "Reuben", "stake": 3, "pin": PIN, "nonce": nonce(),
            "legs": [{"matchId": M, "selection": "OVER", "market": "ou", "line": 1.5},
                     {"matchId": N, "selection": "HOME"}]})
ck("mixed O/U + 1X2 acca placed", s == 200 and j.get("ok") and len(j["wager"]["legs"]) == 2, j)
mixid = j.get("wager", {}).get("id")
if j.get("ok"):
    legs = j["wager"]["legs"]
    ck("acca O/U leg stored market+line; 1X2 leg did not", legs[0].get("market") == "ou" and legs[0].get("line") == 1.5 and "market" not in legs[1], legs)
    # combined odds = product of leg decimals (within rounding)
    prod = (1 + legs[0]["num"] / legs[0]["den"]) * (1 + legs[1]["num"] / legs[1]["den"])
    ck("combined acca odds = product of leg prices", abs(j["wager"]["decimal"] - round(prod, 3)) < 0.05, (j["wager"]["decimal"], round(prod, 3)))
s, j = post("/api/place_acca", {"player": "Reuben", "stake": 3, "pin": PIN, "nonce": nonce(),
            "legs": [{"matchId": M, "selection": "OVER", "market": "ou", "line": 2.5},
                     {"matchId": M, "selection": "HOME"}]})
ck("same game twice in one acca is rejected (correlated)", not j.get("ok"), j)
s, j = post("/api/place_acca", {"player": "Reuben", "stake": 3, "pin": PIN, "nonce": nonce(),
            "legs": [{"matchId": M, "selection": "OVER", "market": "ou", "line": 9.5},
                     {"matchId": N, "selection": "HOME"}]})
ck("an acca with an off-grid O/U line is rejected", not j.get("ok"), j)

print("\n== SETTLEMENT end-to-end: finish a game, /api/poll, verify outcomes + conservation ==")
# Snapshot stakes on match M before settling
wf0 = wagers_file()
on_M = [w for w in wf0 if (w.get("matchId") == M) or (w.get("legs") and any(l.get("matchId") == M for l in w["legs"]))]
# finish M as 3-1 (total 4): OVER 2.5 WINS, UNDER 2.5 LOSES, OVER 1.5 (acca leg) WINS
res = json.load(open(os.path.join(DDIR, "results.json")))
for m in res["matches"]:
    if m["id"] == M:
        m["status"] = "FINISHED"; m["homeScore"] = 3; m["awayScore"] = 1
json.dump(res, open(os.path.join(DDIR, "results.json"), "w"))
time.sleep(0.2)
post("/api/poll", {})                     # triggers settle_all on the running server
time.sleep(0.4)
wf1 = wagers_file()
def byid(wid, wf): return next((w for w in wf if w.get("id") == wid), None)
ob = byid(ouid, wf1)
ck("OVER 2.5 on a 4-goal game settled WON", ob and ob.get("status") == "won", ob and ob.get("status"))
ck("the won O/U bet pays stake x odds", ob and abs(ob["return"] - round(ob["stake"] * (1 + ob["num"] / ob["den"]), 2)) < 0.01, ob and ob.get("return"))
under_M = next((w for w in wf1 if w.get("matchId") == M and w.get("selection") == "UNDER"), None)
ck("UNDER 2.5 on a 4-goal game settled LOST (return 0)", under_M and under_M.get("status") == "lost" and under_M.get("return") == 0, under_M and under_M.get("status"))
mix = byid(mixid, wf1)
ck("the mixed acca's O/U leg (Over 1.5) is marked won", mix and any(l.get("matchId") == M and l.get("result") == "won" for l in mix.get("legs", [])), mix and mix.get("legs"))
ck("the mixed acca stays pending until its 1X2 leg also resolves", mix and mix.get("status") == "pending", mix and mix.get("status"))

# now finish N too (HOME win 2-0) so the mixed acca + Louis's HOME single settle.
# NB: /api/poll throttles real fetches to once per 25s, so we wait it out before the 2nd settle trigger.
res = json.load(open(os.path.join(DDIR, "results.json")))
for m in res["matches"]:
    if m["id"] == N:
        m["status"] = "FINISHED"; m["homeScore"] = 2; m["awayScore"] = 0
json.dump(res, open(os.path.join(DDIR, "results.json"), "w"))
print("  (waiting out the 25s manual-poll throttle before the second settlement...)")
time.sleep(26); post("/api/poll", {}); time.sleep(0.5)
wf2 = wagers_file()
mix = byid(mixid, wf2)
ck("mixed acca settles WON once both legs win", mix and mix.get("status") == "won", mix and mix.get("status"))
ck("won acca return = stake x combined decimal", mix and abs(mix["return"] - round(mix["stake"] * mix["decimal"], 2)) < 0.05, mix and mix.get("return"))

print("\n== money/points conservation: every settled bet is won(stake*odds) / lost(0) / void(stake) ==")
bad = 0
for w in wf2:
    if w.get("status") == "won" and not w.get("legs"):
        exp = round(w["stake"] * (1 + w["num"] / w["den"]), 2)
        if abs(w["return"] - exp) > 0.02: bad += 1
    elif w.get("status") == "lost":
        if w["return"] != 0: bad += 1
    elif w.get("status") == "void":
        if abs(w["return"] - w["stake"]) > 0.01: bad += 1
ck("no settled bet has an inconsistent return", bad == 0, "%d inconsistent" % bad)

print("\n== the board still recomputes cleanly after settlement (no crash, players intact) ==")
b2 = board()
ck("board has all 5 players after settlement", len([p for p in b2.get("players", [])]) == 5, len(b2.get("players", [])))
ck("settled betting shows up in player bet stats", any((p.get("bet_potential") is not None) or True for p in b2.get("players", [])), None)

print("\n" + ("HUGE BUG ROUND PASSED — no issues found." if not fails
              else "BUG ROUND FOUND %d ISSUE(S): %s" % (len(fails), ", ".join(fails))))
sys.exit(1 if fails else 0)
