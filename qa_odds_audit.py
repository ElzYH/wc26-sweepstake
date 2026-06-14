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

shutil.rmtree(_T, ignore_errors=True)
if FAILS:
    print("\nODDS-AUDIT QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll odds-audit QA passed.")
