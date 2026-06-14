#!/usr/bin/env python3
"""QA + bug-hunt for the odds-audit tool (audit_match_odds.py) and the server-side house-edge
integrity guard + auto matchday audit. Pure-logic + defensive-edge coverage; never hits the network.
Exits non-zero on any failure (so check.sh catches it)."""
import os, sys, json, tempfile, shutil, math

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
_T = tempfile.mkdtemp(prefix="qa_odds_")
os.environ["WC26_DATA"] = _T
shutil.copy(os.path.join(REPO, "teams.json"), os.path.join(_T, "teams.json"))

import audit_match_odds as A
import server as S
import wager as W

FAILS = []
def ck(name, cond, extra=None):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -> %r" % (extra,)))
    if not cond:
        FAILS.append(name)

teams = {t["name"]: t for t in json.load(open(os.path.join(_T, "teams.json"))).get("teams", [])}

print("== book_overround: sums implied probs; guards junk/zero/empty ==")
ck("a normal 2-way book overrounds (>1)", A.book_overround([2.05, 1.80]) > 1.0, A.book_overround([2.05, 1.80]))
ck("a fair 2-way book is ~1.0", abs(A.book_overround([2.0, 2.0]) - 1.0) < 1e-9, A.book_overround([2.0, 2.0]))
ck("an underround book is <1", A.book_overround([2.10, 2.10]) < 1.0, A.book_overround([2.10, 2.10]))
ck("decimals <=1 are ignored (can't divide by a sub-1 price)", A.book_overround([1.0, 0.0, 2.0]) == 0.5, A.book_overround([1.0, 0.0, 2.0]))
ck("non-numeric -> None (never raises)", A.book_overround(["x", 2.0]) is None, A.book_overround(["x", 2.0]))
ck("empty -> None", A.book_overround([]) is None, A.book_overround([]))

print("\n== market_lookup: exact, reversed order, fuzzy spelling, and a clean miss ==")
mkt = {"Australia v Turkey": {"h2h": {"home": 2.1}}, "USA v Paraguay": {"h2h": {"home": 1.9}}}
ck("exact key matches", A.market_lookup(mkt, "Australia", "Turkey") is not None)
ck("close spelling matches (Turkiye)", A.market_lookup(mkt, "Australia", "Turkiye") is not None)
ck("a genuine miss returns None", A.market_lookup(mkt, "Spain", "Japan") is None)
ck("None market -> None (no crash)", A.market_lookup(None, "A", "B") is None)

print("\n== result_letter ==")
ck("home win -> H", A.result_letter(2, 0) == "H")
ck("draw -> D", A.result_letter(1, 1) == "D")
ck("away win -> A", A.result_letter(0, 1) == "A")

print("\n== SERVER house-edge integrity guard ==")
S.wager_mod = W
up = {"fixtures": [{"home": "Brazil", "away": "Morocco", "status": "TIMED", "utcDate": "2030-01-01T20:00:00Z"},
                   {"home": "USA", "away": "Paraguay", "status": "SCHEDULED", "utcDate": "2030-01-01T20:00:00Z"}]}
ck("healthy upcoming fixtures -> NO violations (every real market overrounds)", S._odds_integrity_violations(up, teams) == [])
# tamper the goals book so a line underrounds -> must be flagged
_orig = W.goals_odds
W.goals_odds = lambda ch, ca, lines=None: {"2.5": {"OVER": {"decimal": 2.10}, "UNDER": {"decimal": 2.10}}}  # 95.2%
viol = S._odds_integrity_violations(up, teams)
W.goals_odds = _orig
ck("a NEGATIVE-EDGE market is flagged by the guard", len(viol) >= 1 and "O/U" in viol[0], viol)
ck("guard ignores teams not in teams.json (no crash)",
   S._odds_integrity_violations({"fixtures": [{"home": "Nowhere", "away": "Elsewhere", "status": "TIMED"}]}, teams) == [])
ck("guard ignores already-live/finished games (only prices what's bettable)",
   S._odds_integrity_violations({"fixtures": [{"home": "Brazil", "away": "Morocco", "status": "IN_PLAY"}]}, teams) == [])
ck("guard never raises on garbage fixtures", isinstance(S._odds_integrity_violations({"fixtures": [None, 7, {"home": "Brazil"}]}, teams), list))

print("\n== SERVER auto matchday audit: detection, idempotency, guards, resilience ==")
def setup(fixtures):
    json.dump({"fixtures": fixtures, "players": []}, open(os.path.join(_T, "tracker_data.json"), "w"))
    json.dump({"players": [{"name": "Erol", "teams": []}]}, open(os.path.join(_T, "draw_result.json"), "w"))
    S.save_config({"discord_webhook": "", "odds_audit_discord": False})

# a fully-finished matchday is detected and recorded once
setup([{"id": "g1", "home": "Australia", "away": "Turkey", "homeScore": 2, "awayScore": 0, "status": "FINISHED", "utcDate": "2026-06-14T15:00:00Z"},
       {"id": "g2", "home": "Brazil", "away": "Morocco", "homeScore": 1, "awayScore": 1, "status": "FINISHED", "utcDate": "2026-06-14T18:00:00Z"}])
S._maybe_matchday_audit(S.load_config())
ck("a finished matchday is recorded", S.load_config().get("last_audited_matchday") == "2026-06-14", S.load_config().get("last_audited_matchday"))
before = S.load_config().get("last_audited_matchday")
S._maybe_matchday_audit(S.load_config())
ck("re-running does NOT re-audit the same matchday (idempotent)", S.load_config().get("last_audited_matchday") == before)

# a day that is NOT fully finished must be ignored
S.save_config({"discord_webhook": "", "odds_audit_discord": False})
setup([{"id": "g1", "home": "Spain", "away": "Japan", "homeScore": 1, "awayScore": 0, "status": "FINISHED", "utcDate": "2026-06-20T15:00:00Z"},
       {"id": "g2", "home": "France", "away": "Mexico", "status": "TIMED", "utcDate": "2026-06-20T18:00:00Z"}])
S._maybe_matchday_audit(S.load_config())
ck("an unfinished matchday is NOT audited", S.load_config().get("last_audited_matchday") is None, S.load_config().get("last_audited_matchday"))

# never writes betting data, and never raises on broken input
setup([])
S._maybe_matchday_audit(S.load_config())
ck("empty fixtures -> no-op, no crash", S.load_config().get("last_audited_matchday") is None)
json.dump({"fixtures": [{"home": "X", "homeScore": "oops", "awayScore": None, "status": "FINISHED", "utcDate": "2026-06-21T00:00:00Z"}]},
          open(os.path.join(_T, "tracker_data.json"), "w"))
try:
    S._maybe_matchday_audit(S.load_config()); _crashed = False
except Exception:
    _crashed = True
ck("garbage scores -> never raises", not _crashed)

print("\n== the audit must be READ-ONLY w.r.t. betting: it touches no wagers/odds, only config idempotency ==")
ck("no wagers.json was created by the audit", not os.path.exists(os.path.join(_T, "wagers.json")))

print("\n== _comp: unmatched team prices NEUTRAL (not 0 -> would make its opponent ~98%) ==")
ck("a known team returns its real composite", abs(S._comp(teams, "France") - teams["France"]["composite"]) < 1e-9)
ck("an UNMATCHED name returns the neutral default, never 0", S._comp(teams, "Quuxland") == S.NEUTRAL_COMPOSITE)
ck("a None/garbage record returns neutral (no crash)", S._comp({"X": None}, "X") == S.NEUTRAL_COMPOSITE)
ck("a 0/negative composite is treated as neutral", S._comp({"X": {"composite": 0}}, "X") == S.NEUTRAL_COMPOSITE)
ck("neutral default is mid-table, not a phantom minnow (10..60)", 10 <= S.NEUTRAL_COMPOSITE <= 60)

print("\n== team-name resolver: the Bosnia hyphen form now maps to canonical (was mispriced as 0) ==")
import update_results as U
r = U.build_name_map(os.path.join(_T, "teams.json"))
ck("'Bosnia-Herzegovina' resolves to the canonical team", r("Bosnia-Herzegovina") == "Bosnia & Herzegovina", r("Bosnia-Herzegovina"))
ck("'Bosnia and Herzegovina' still resolves", r("Bosnia and Herzegovina") == "Bosnia & Herzegovina")
ck("a real canonical name maps to itself", r("Brazil") == "Brazil")

print("\n== calibrator: market inversion is monotone, exact and clamped ==")
import calibrate_odds as C
_pe = W._fair_probs(40.0, 40.0)[0]                       # home prob at equal strength (~0.35; draw eats 30%)
ck("inverting the equal-strength prob recovers ~equal composite", abs(C.implied_composite(_pe, 40.0, "home") - 40.0) < 1.5,
   C.implied_composite(_pe, 40.0, "home"))
hi = C.implied_composite(0.70, 40.0, "home"); lo = C.implied_composite(0.30, 40.0, "home")
ck("a higher target prob implies a higher composite (monotone)", hi > lo, (hi, lo))
ck("implied composite is clamped to the search band", C.C_MIN <= C.implied_composite(0.999, 40.0, "home") <= C.C_MAX)
# bounded-step property: build a fake market that wants a huge jump, confirm the move is capped
def step(cur, target, cap):
    return round(max(C.C_MIN, min(C.C_MAX, cur + max(-cap, min(cap, target - cur)))), 1)
ck("a big gap is capped at max-step up", step(30.0, 90.0, 5.0) == 35.0)
ck("a big gap is capped at max-step down", step(60.0, 5.0, 5.0) == 55.0)
ck("a small gap moves only the gap", step(40.0, 42.0, 5.0) == 42.0)
ck("the move is clamped to the composite band", step(104.0, 200.0, 5.0) <= C.C_MAX)

shutil.rmtree(_T, ignore_errors=True)
if FAILS:
    print("\nODDS-AUDIT QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll odds-audit QA passed.")
