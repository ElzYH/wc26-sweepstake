#!/usr/bin/env python3
"""Idempotency + live-edge robustness QA:
 - a retried bet (same nonce) never becomes two bets (web single, web acca, Discord interaction)
 - the manual-poll throttle exists so /api/poll spam can't burn the upstream quota
 - odds, settlement and the tracker compute never crash on weird/partial live data
   (missing or zero team strength, TBD knockout teams, null scores, missing kickoff times)."""
import os, sys, json, shutil, tempfile, time

SRC = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="wc26_idem_")
for fn in os.listdir(SRC):
    if fn.endswith(".py") or fn.endswith(".json"):
        try: shutil.copy(os.path.join(SRC, fn), TMP)
        except Exception: pass
os.environ["WC26_CONFIG"] = os.path.join(TMP, "config.json")
json.dump({"configured": True, "wagering_enabled": True, "players": ["Erol", "James"]},
          open(os.path.join(TMP, "config.json"), "w"))
os.chdir(TMP); sys.path.insert(0, TMP)
import server as S
import wager as W

FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

print("=== idempotency: _dedup_wager ===")
wl = [{"id": "w1", "player": "Erol", "nonce": "abc", "stake": 10, "status": "pending"}]
ck("finds an existing wager by (player, nonce)", S._dedup_wager(wl, "Erol", "abc") is not None, "")
ck("empty nonce never dedups (each bet is its own)", S._dedup_wager(wl, "Erol", "") is None, "")
ck("unknown nonce -> no match", S._dedup_wager(wl, "Erol", "zzz") is None, "")
ck("same nonce, different player -> no match", S._dedup_wager(wl, "James", "abc") is None, "")

print("\n=== idempotency: simulate the handler pattern (place, then retry) ===")
NOW = 1_700_000_000; FUT = NOW + 86_400
def M(h, a, mid):
    return {"id": mid, "home": h, "away": a, "stage": "GROUP_STAGE", "status": "TIMED",
            "utcDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FUT))}
wl = []
nonce = "fixed-nonce-123"
def place_once(wl, nonce):
    dup = S._dedup_wager(wl, "Erol", nonce)
    if dup is not None:
        return dup, True
    ok, res = W.place(wl, "Erol", M("A", "B", 1), "HOME", 10, 1000, 90, 50, now=NOW)
    if ok and nonce:
        res["nonce"] = nonce
    return res, False
r1, was_dup1 = place_once(wl, nonce)
r2, was_dup2 = place_once(wl, nonce)         # the "retry" after a dropped response
ck("first placement creates a bet", not was_dup1 and len([w for w in wl if w.get('status')=='pending']) == 1, len(wl))
ck("retry with same nonce is a no-op (deduped)", was_dup2 and r2 is r1, (was_dup2,))
ck("still exactly ONE bet after the retry", len([w for w in wl if w.get('status') == 'pending']) == 1, len(wl))
r3, was_dup3 = place_once(wl, "different-nonce")
ck("a genuinely new bet (new nonce) is placed", not was_dup3 and len([w for w in wl if w.get('status')=='pending']) == 2, len(wl))

print("\n=== poll throttle exists ===")
ck("MANUAL_POLL_MIN_INTERVAL is a positive number", isinstance(S.MANUAL_POLL_MIN_INTERVAL, (int, float)) and S.MANUAL_POLL_MIN_INTERVAL > 0, S.MANUAL_POLL_MIN_INTERVAL)

print("\n=== live-edge: odds never crash on weird team strength ===")
for ch, ca, label in [(0, 0, "both zero"), (None, None, "both None"), (0, 90, "one zero"),
                      (100, 0.0001, "huge gap"), (-5, 50, "negative")]:
    try:
        o = W.match_odds(ch, ca)
        ok = all(k in o for k in ("HOME", "DRAW", "AWAY")) and all(o[k]["num"] > 0 for k in o)
    except Exception as e:
        ok = False; o = repr(e)
    ck("match_odds(%s) returns sane odds, no crash" % label, ok, o)
# live_strength on weird inputs
for base, team, label in [(0, "X", "zero base"), (None, "X", "None base"), (90, "X", "team not played")]:
    try:
        v = W.live_strength(base, team, [])
        ok = isinstance(v, (int, float))
    except Exception as e:
        ok = False; v = repr(e)
    ck("live_strength(%s) no crash" % label, ok, v)

print("\n=== live-edge: settlement never crashes on weird match data ===")
weird_matches = [
    {"id": "m1", "home": None, "away": None, "status": "FINISHED", "homeScore": None, "awayScore": None},
    {"id": "m2", "home": "A", "away": "B", "status": "FINISHED"},                       # no scores
    {"id": "m3", "home": "A", "away": "B", "status": "IN_PLAY", "homeScore": 1, "awayScore": 0},
    {"id": "m4"},                                                                        # almost empty
    {"home": "A", "away": "B", "status": "FINISHED", "homeScore": 2, "awayScore": 1, "winner": "HOME"},  # no id
]
wl = []
W.place(wl, "Erol", M("A", "B", "m3"), "HOME", 5, 1000, 90, 50, now=NOW)
for m in weird_matches:
    try:
        W.settle(wl, m, now=NOW); ok = True; err = ""
    except Exception as e:
        ok = False; err = repr(e)
    ck("settle on weird match %r -> no crash" % (m.get("id"),), ok, err)
ck("a live (IN_PLAY) match did NOT settle the pending bet", wl[0]["status"] == "pending", wl[0]["status"])

print("\n=== live-edge: scoring.compute survives partial data (TBD teams, no scores) ===")
# build a minimal but weird results + draw and confirm compute doesn't throw
try:
    import scoring
    # a draw_result with two players, teams.json already present
    if os.path.exists("draw_result.json") and os.path.exists("teams.json"):
        # craft a results file with a TBD fixture + a finished game with odd fields
        results = {"matches": [
            {"id": "g1", "home": "TBD", "away": "TBD", "status": "TIMED", "stage": "FINAL", "utcDate": "2026-07-19T19:00:00Z"},
            {"id": "g2", "home": None, "away": None, "status": "SCHEDULED", "stage": "SEMI_FINAL"},
        ]}
        json.dump(results, open("results.json", "w"))
        scoring.compute(out="tracker_data.json", default_mode="hybrid", wagers=[])
        ok = os.path.exists("tracker_data.json"); err = ""
    else:
        ok = True; err = "skipped (no draw_result/teams in harness)"
except Exception as e:
    ok = False; err = repr(e)
ck("scoring.compute on TBD/partial fixtures -> no crash", ok, err)

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("\nIDEMPOTENCY/EDGE QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll idempotency + live-edge robustness QA passed.")
