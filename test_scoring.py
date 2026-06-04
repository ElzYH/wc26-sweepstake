#!/usr/bin/env python3
"""Unit tests for scoring.py — points, survival, hybrid, fair, and the over-time history replay.

Run: python3 test_scoring.py   (exit code 0 = all pass, non-zero = failure)
Self-contained: writes tiny fixtures to a temp dir, no network, no real data touched.
"""
import json
import os
import sys
import tempfile

import scoring

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  -> " + detail) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def _fixtures(tmp, matches):
    teams = {"teams": [
        {"name": "A", "tier": 1, "tier_label": "T1", "weight": 8, "group": "X", "composite": 90, "implied_prob": 0.30},
        {"name": "B", "tier": 2, "tier_label": "T2", "weight": 4, "group": "X", "composite": 60, "implied_prob": 0.12},
        {"name": "C", "tier": 1, "tier_label": "T1", "weight": 8, "group": "Y", "composite": 88, "implied_prob": 0.25},
        {"name": "D", "tier": 3, "tier_label": "T3", "weight": 2, "group": "Y", "composite": 40, "implied_prob": 0.05},
    ]}
    draw = {"players": [
        {"name": "P1", "teams": [{"name": "A", "tier": 1, "group": "X"}, {"name": "C", "tier": 1, "group": "Y"}]},
        {"name": "P2", "teams": [{"name": "B", "tier": 2, "group": "X"}, {"name": "D", "tier": 3, "group": "Y"}]},
    ]}
    standings = [
        {"group": "X", "table": [{"team": "A", "played": 1, "points": 3}, {"team": "B", "played": 1, "points": 0}]},
        {"group": "Y", "table": [{"team": "C", "played": 1, "points": 1}, {"team": "D", "played": 1, "points": 1}]},
    ]
    results = {"competition": "WC", "matches": matches, "standings": standings}
    p = {}
    for nm, obj in (("teams.json", teams), ("draw_result.json", draw), ("results.json", results)):
        p[nm] = os.path.join(tmp, nm)
        json.dump(obj, open(p[nm], "w"))
    return p


def _m(home, away, hs, as_, stage="GROUP_STAGE", group="X", status="FINISHED", date="2026-06-12T18:00:00Z"):
    return {"utcDate": date, "stage": stage, "group": group, "status": status,
            "home": home, "away": away, "homeScore": hs, "awayScore": as_, "winner": None}


def run():
    tmp = tempfile.mkdtemp()

    # ---- group-stage scoring ----
    fx = _fixtures(tmp, [
        _m("A", "B", 2, 0, group="X", date="2026-06-11T18:00:00Z"),   # A: 2 goals + win(3) + clean sheet(1) = 6 ; B: 0
        _m("C", "D", 1, 1, group="Y", date="2026-06-12T18:00:00Z"),   # C,D: 1 goal + draw(1) = 2 each
    ])
    d = scoring.compute(teams_path=fx["teams.json"], draw_path=fx["draw_result.json"],
                        results_path=fx["results.json"], out=None)
    pts = {p["name"]: p["points"] for p in d["players"]}
    surv = {p["name"]: p["survival"] for p in d["players"]}
    hyb = {p["name"]: p["hybrid"] for p in d["players"]}
    check("A scores goals+win+clean-sheet (P1 = 6 + 2 = 8)", pts.get("P1") == 8, f"got {pts.get('P1')}")
    check("draw + loss tally (P2 = 0 + 2 = 2)", pts.get("P2") == 2, f"got {pts.get('P2')}")
    check("group stage gives no survival points", surv.get("P1") == 0 and surv.get("P2") == 0, f"{surv}")
    check("hybrid == points + survival", all(hyb[k] == pts[k] + surv[k] for k in pts), f"{hyb}")

    lb = d["leaderboards"]
    check("points leaderboard ordered (P1 top)", lb["points"][0]["name"] == "P1", str(lb["points"]))
    check("fair residuals sum to ~0", abs(sum(p["fair"] for p in d["players"])) <= 1,
          str([p["fair"] for p in d["players"]]))

    # ---- history replay ----
    hist = d["history"]
    check("history has one point per finished match", len(hist) == 2, f"got {len(hist)}")
    if len(hist) == 2:
        p1 = [h["p"]["P1"]["pts"] for h in hist]
        check("P1 points non-decreasing over time", p1[0] <= p1[1], str(p1))
        check("final history point matches final total", hist[-1]["p"]["P1"]["pts"] == pts["P1"],
              f"{hist[-1]['p']['P1']['pts']} vs {pts['P1']}")

    # ---- knockout stage bonus is furthest-only (not cumulative) ----
    fx2 = _fixtures(tmp, [
        _m("A", "B", 1, 0, group="X", date="2026-06-11T18:00:00Z"),
        _m("A", "C", 1, 0, stage="SEMI_FINALS", group=None, date="2026-07-01T18:00:00Z"),
    ])
    d2 = scoring.compute(teams_path=fx2["teams.json"], draw_path=fx2["draw_result.json"],
                         results_path=fx2["results.json"], out=None)
    a = next(t for pl in d2["players"] for t in pl["teams"] if t["name"] == "A")
    sb = scoring.SCORING["stage_bonus"]
    # A: group win 1 goal + win(3) + cs(1) = 5, semi 1 goal + win(3) + cs(1) = 5, plus furthest bonus (SEMI only)
    expected = 5 + 5 + sb["SEMI_FINALS"]
    check("furthest-stage bonus only (SEMI, not stacked)", a["points"] == expected,
          f"got {a['points']} expected {expected}")
    check("survival value reflects SEMI", a["survival"] == scoring.SURVIVAL_VALUE["SEMI_FINALS"],
          f"got {a['survival']}")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        return 1
    print("All scoring tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
