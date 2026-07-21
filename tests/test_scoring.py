#!/usr/bin/env python3
"""Unit tests for scoring.py — points, survival, fair, and the over-time history replay.

Run: python3 test_scoring.py   (exit code 0 = all pass, non-zero = failure)
Self-contained: writes tiny fixtures to a temp dir, no network, no real data touched.
"""
import json
import os
import sys
import tempfile
import time

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

    # ---- normaliser: extra time + penalty shootout (v4 fullTime INCLUDES shootout goals) ----
    import update_results
    ident = lambda n: n
    raw = [
        # EC1996-style: 1-1 after 90 & ET, home win 6-5 on pens. v4 stores fullTime as 7-6.
        {"id": 1, "stage": "SEMI_FINALS", "homeTeam": {"name": "H"}, "awayTeam": {"name": "A"},
         "status": "FINISHED", "score": {"winner": "HOME_TEAM", "duration": "PENALTY_SHOOTOUT",
             "fullTime": {"home": 7, "away": 6}, "regularTime": {"home": 1, "away": 1},
             "extraTime": {"home": 0, "away": 0}, "penalties": {"home": 6, "away": 5}}},
        # extra-time winner, no pens: 1-1 then 2-1 in ET → fullTime 2-1.
        {"id": 2, "stage": "QUARTER_FINALS", "homeTeam": {"name": "H"}, "awayTeam": {"name": "A"},
         "status": "FINISHED", "score": {"winner": "HOME_TEAM", "duration": "EXTRA_TIME",
             "fullTime": {"home": 2, "away": 1}, "regularTime": {"home": 1, "away": 1},
             "extraTime": {"home": 1, "away": 0}}},
        # ordinary 90-minute game.
        {"id": 3, "stage": "GROUP_STAGE", "homeTeam": {"name": "H"}, "awayTeam": {"name": "A"},
         "status": "FINISHED", "score": {"winner": "AWAY_TEAM", "duration": "REGULAR",
             "fullTime": {"home": 0, "away": 2}}},
    ]
    nm = {x["id"]: x for x in update_results.normalize_matches(raw, ident)}
    check("shootout: on-field goals exclude pens (1-1 not 7-6)",
          nm[1]["homeScore"] == 1 and nm[1]["awayScore"] == 1, nm[1])
    check("shootout: shootout score + a.e.t. flags carried",
          nm[1]["shootout"] and nm[1]["aet"] and nm[1]["penHome"] == 6 and nm[1]["penAway"] == 5, nm[1])
    check("shootout: winner still HOME", nm[1]["winner"] == "HOME", nm[1])
    check("extra time: on-field goals include ET (2-1), aet set, no shootout",
          nm[2]["homeScore"] == 2 and nm[2]["awayScore"] == 1 and nm[2]["aet"] and not nm[2]["shootout"], nm[2])
    check("regular game: plain fullTime, no aet/shootout",
          nm[3]["homeScore"] == 0 and nm[3]["awayScore"] == 2 and not nm[3]["aet"] and not nm[3]["shootout"], nm[3])

    # ---- free tier: minimal payload (delayed scores, NO minute, NO regular/extra/penalty breakdown) ----
    free_raw = [
        # live game on free tier: score present (delayed), no minute, no breakdown, no duration
        {"id": 11, "stage": "GROUP_STAGE", "homeTeam": {"name": "H"}, "awayTeam": {"name": "A"},
         "status": "IN_PLAY", "score": {"winner": None, "fullTime": {"home": 1, "away": 0}}},
        # finished game on free tier: just fullTime + winner, nothing else
        {"id": 12, "stage": "GROUP_STAGE", "homeTeam": {"name": "H"}, "awayTeam": {"name": "A"},
         "status": "FINISHED", "score": {"winner": "AWAY_TEAM", "fullTime": {"home": 0, "away": 1}}},
        # scheduled game on free tier: no score object at all
        {"id": 13, "stage": "GROUP_STAGE", "homeTeam": {"name": "H"}, "awayTeam": {"name": "A"},
         "status": "TIMED"},
    ]
    fm = {x["id"]: x for x in update_results.normalize_matches(free_raw, ident)}
    check("free tier live: score reads from fullTime, minute is None, no aet/shootout",
          fm[11]["homeScore"] == 1 and fm[11]["awayScore"] == 0 and fm[11]["minute"] is None
          and not fm[11]["aet"] and not fm[11]["shootout"], fm[11])
    check("free tier finished: fullTime score + winner resolve with no breakdown",
          fm[12]["homeScore"] == 0 and fm[12]["awayScore"] == 1 and fm[12]["winner"] == "AWAY", fm[12])
    check("free tier scheduled: no score object doesn't crash",
          fm[13]["homeScore"] is None and fm[13]["awayScore"] is None and fm[13]["status"] == "TIMED", fm[13])

    # ---- group-stage scoring ----
    fx = _fixtures(tmp, [
        _m("A", "B", 2, 0, group="X", date="2026-06-11T18:00:00Z"),   # A: 2 goals + win(3) + clean sheet(1) = 6 ; B: 0
        _m("C", "D", 1, 1, group="Y", date="2026-06-12T18:00:00Z"),   # C,D: 1 goal + draw(1) = 2 each
    ])
    d = scoring.compute(teams_path=fx["teams.json"], draw_path=fx["draw_result.json"],
                        results_path=fx["results.json"], out=None)
    pts = {p["name"]: p["points"] for p in d["players"]}
    surv = {p["name"]: p["survival"] for p in d["players"]}
    check("A scores goals+win+clean-sheet (P1 = 6 + 2 = 8)", pts.get("P1") == 8, f"got {pts.get('P1')}")
    check("draw + loss tally (P2 = 0 + 2 = 2)", pts.get("P2") == 2, f"got {pts.get('P2')}")
    check("group stage gives no survival points", surv.get("P1") == 0 and surv.get("P2") == 0, f"{surv}")

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
    # Stage progression is rewarded ONCE, via Survival — POINTS is goals/wins/clean sheets only (stage_bonus is {} now).
    # A: group win 1 goal + win(3) + cs(1) = 5, semi 1 goal + win(3) + cs(1) = 5, plus any points stage bonus.
    # Winning the SEMI means A has REACHED the FINAL, so any stage bonus / survival value is the FINAL's.
    expected = 5 + 5 + sb.get("FINAL", 0)
    check("points = match points only (stage reward is via Survival, not stacked)", a["points"] == expected,
          f"got {a['points']} expected {expected}")
    check("survival value reflects the FINAL (won the semi = reached the final)", a["survival"] == scoring.SURVIVAL_VALUE["FINAL"],
          f"got {a['survival']}")

    # --- regression: when betting is ON, upcoming fixtures must get odds even before anyone has bet ---
    # (a None wager list = betting off -> no odds; an empty list = betting on, no bets yet -> odds present)
    fut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3 * 86400))
    fx3 = _fixtures(tempfile.mkdtemp(), [_m("A", "B", None, None, status="TIMED", date=fut)])
    d_off = scoring.compute(teams_path=fx3["teams.json"], draw_path=fx3["draw_result.json"],
                            results_path=fx3["results.json"], out=None, wagers=None)
    d_on = scoring.compute(teams_path=fx3["teams.json"], draw_path=fx3["draw_result.json"],
                           results_path=fx3["results.json"], out=None, wagers=[])
    off_odds = [f for f in (d_off.get("fixtures") or []) if f.get("odds")]
    on_odds = [f for f in (d_on.get("fixtures") or []) if f.get("odds")]
    check("betting OFF (wagers=None) -> no odds on fixtures", len(off_odds) == 0, str(len(off_odds)))
    check("betting ON (wagers=[]) -> upcoming fixtures get odds (so the Bets tab isn't empty)",
          len(on_odds) >= 1, str(len(on_odds)))

    # --- regression: the "No bets" leaderboard exists, mirrors Points when no bets are placed,
    #     and is never moved by betting being switched on. ---
    nb_off = {r["name"]: r["score"] for r in (d_off["leaderboards"].get("points_no_bets") or [])}
    nb_on = {r["name"]: r["score"] for r in (d_on["leaderboards"].get("points_no_bets") or [])}
    pts_off = {r["name"]: r["score"] for r in d_off["leaderboards"]["points"]}
    check("'points_no_bets' leaderboard is present", len(nb_off) == len(pts_off) and len(nb_off) > 0,
          f"nobets={len(nb_off)} points={len(pts_off)}")
    check("No-bets == Points when betting is off", nb_off == pts_off, f"{nb_off} vs {pts_off}")
    check("No-bets board unchanged whether betting is on or off", nb_off == nb_on, f"{nb_off} vs {nb_on}")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        return 1
    print("All scoring tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
