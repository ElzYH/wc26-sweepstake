"""
Regression: the DISPLAYED fixture odds (scoring.compute) must be priced from the SAME
team strengths as bet PLACEMENT (server load_teams + _comp). Auto-calibration writes a
calibration.json overlay; compute now takes composite_overrides so the fixture list and the
bet slip never diverge. This locks that in (the "odds on the website are not matching" bug).
"""
import json, os, tempfile, shutil
import scoring, wager

FAILS = 0
def ck(label, cond, extra=""):
    global FAILS
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else "  -> " + str(extra)))
    if not cond:
        FAILS += 1

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="wc26_oddsmatch_")
os.chdir(_TMP)

# two teams, one upcoming SCHEDULED group game between them
TEAMS = {"Alpha": (1, "A", 80.0), "Beta": (3, "A", 45.0)}
json.dump({"teams": [{"name": n, "confederation": "-", "group": g, "tier": t,
                      "tier_label": "", "weight": 1, "composite": c,
                      "decimal_odds": 0, "fifa_points": 0} for n, (t, g, c) in TEAMS.items()]},
          open("teams.json", "w"), indent=2)
json.dump({"mode": "snake", "leftover_policy": "drop", "players": [
    {"name": "P1", "strength": 0, "teams": [{"name": "Alpha", "tier": 1, "group": "A", "confederation": "-", "composite": 80.0}]},
    {"name": "P2", "strength": 0, "teams": [{"name": "Beta", "tier": 3, "group": "A", "confederation": "-", "composite": 45.0}]},
]}, open("draw_result.json", "w"), indent=2)
json.dump({"competition": "WC2026", "standings": [], "matches": [
    {"id": 1, "stage": "GROUP_STAGE", "group": "A", "utcDate": "2030-01-01T00:00:00Z",
     "status": "SCHEDULED", "home": "Alpha", "away": "Beta",
     "homeScore": None, "awayScore": None, "winner": None},
]}, open("results.json", "w"), indent=2)

def display_home_frac(overrides):
    scoring.compute(teams_path="teams.json", draw_path="draw_result.json",
                    results_path="results.json", out="t.json", wagers=[],
                    composite_overrides=overrides)
    td = json.load(open("t.json"))
    f = next((x for x in td["fixtures"] if x.get("home") == "Alpha" and x.get("odds")), None)
    return f["odds"]["HOME"]["frac"] if f else None

raw = display_home_frac(None)
ck("raw display prices the fixture", raw is not None, raw)

# calibration nudges Alpha up — display must change AND equal the placement-path odds
overlay = {"Alpha": 92.0, "Beta": 40.0}
cal = display_home_frac(overlay)
ck("calibrated display prices the fixture", cal is not None, cal)
ck("display odds MOVE with the calibration overlay (no longer stale)", cal != raw, (raw, cal))

# placement path: server's _comp(load_teams) feeds the overlaid composite straight into match_odds
ch = wager.live_strength(overlay["Alpha"], "Alpha", [])
ca = wager.live_strength(overlay["Beta"], "Beta", [])
place = wager.match_odds(ch, ca)["HOME"]["frac"]
ck("DISPLAY odds == PLACEMENT odds under calibration (the core fix)", cal == place, (cal, place))

# a junk override must be ignored (falls back to the raw composite), never poisons the board
for junk in ({"Alpha": float("nan")}, {"Alpha": -5}, {"Alpha": 999}, {"Alpha": "x"}, {"Alpha": None}):
    ck("junk override ignored -> raw odds (%r)" % junk["Alpha"], display_home_frac(junk) == raw, junk)

# empty / None overrides == no overlay
ck("None overrides == raw", display_home_frac(None) == raw)
ck("empty-dict overrides == raw", display_home_frac({}) == raw)

print(("\nALL ODDS-DISPLAY-MATCH TESTS PASSED" if FAILS == 0 else "\n%d FAILED" % FAILS))
raise SystemExit(1 if FAILS else 0)
