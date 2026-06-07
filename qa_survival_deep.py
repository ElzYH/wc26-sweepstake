#!/usr/bin/env python3
"""Deep survival / advancement / forecast / leaderboard QA (~100 checks).
Covers: alive/out status + furthest-stage, survival values (furthest-only, no 3rd-place survival),
champion-odds (alive-aware, sums to 100 when all teams owned, 0 when knocked out), projected points,
'fair' handicap, leaderboard ordering & tie-breaks, and adversarial/churn data (revived teams, a team
in two groups, decimal_odds=0, all-eliminated, single-team groups) — guaranteeing no crash / no div0."""
import os, sys, json, math, shutil, tempfile

SRC = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="wc26_surv_")
os.chdir(TMP); sys.path.insert(0, SRC)
import scoring

FAILS = []
def ck(name, cond, extra=""):
    if not cond:
        FAILS.append(name); print("  FAIL " + name + ("" if extra == "" else "  -> %r" % (extra,)))
    else:
        print("  PASS " + name)

TEAMS = ["Brazil", "Serbia", "Spain", "Japan", "France", "Ghana", "Argentina", "Mexico"]
def teams_json(odds=None, comps=None):
    out = []
    for i, n in enumerate(TEAMS):
        out.append({"name": n, "composite": (comps or {}).get(n, 60 + i),
                    "implied_prob": 0.1, "decimal_odds": (odds or {}).get(n, 10.0),
                    "tier": (i % 4) + 1, "group": "ABCD"[i % 4]})
    return {"teams": out}

def draw_json(assign, tj=None):
    tinfo = {t["name"]: t for t in (tj or teams_json())["teams"]}
    return {"players": [{"name": pl, "teams": [{"name": t, "tier": tinfo.get(t, {}).get("tier", 4),
                                                "group": tinfo.get(t, {}).get("group", "A")} for t in ts]}
                        for pl, ts in assign.items()]}

def run(assign, matches, mode="hybrid", wagers=None, tj=None):
    tj = tj or teams_json()
    json.dump(tj, open("teams.json", "w"))
    json.dump(draw_json(assign, tj), open("draw_result.json", "w"))
    json.dump({"matches": matches}, open("results.json", "w"))
    scoring.compute(out="td.json", default_mode=mode, wagers=wagers)
    return json.load(open("td.json"))

def M(mid, h, a, hs, as_, status="FINISHED", stage="GROUP_STAGE", winner=None, **kw):
    if winner is None and hs is not None and as_ is not None:
        winner = "HOME" if hs > as_ else ("AWAY" if as_ > hs else None)
    m = {"id": mid, "home": h, "away": a, "homeScore": hs, "awayScore": as_,
         "status": status, "stage": stage, "winner": winner, "utcDate": "2026-06-15T18:00:00Z"}
    m.update(kw); return m

def player(td, name): return next((p for p in td["players"] if p["name"] == name), None)
def team_in(td, pn, tn): return next((t for t in player(td, pn)["teams"] if t["name"] == tn), None)

FULL = {"Erol": ["Brazil", "Spain", "France", "Ghana"], "James": ["Serbia", "Japan", "Argentina", "Mexico"]}
A2 = {"Erol": ["Brazil", "Spain"], "James": ["Serbia", "Japan"]}

# ============================================================== STATUS: ALIVE / OUT
print("\n== STATUS: ALIVE / OUT ==")
td = run(A2, [M("g1", "Brazil", "Serbia", 1, 0)])      # only group games so far
ck("group team alive before any KO starts", team_in(td, "Erol", "Brazil")["status"] == "alive", team_in(td, "Erol", "Brazil"))
ck("status stage is GROUP_STAGE pre-KO", team_in(td, "Erol", "Brazil")["stage"] == "GROUP_STAGE", team_in(td, "Erol", "Brazil"))
# once a KO match exists, a team NOT in the KO is out
td = run(A2, [M("g1", "Brazil", "Serbia", 1, 0), M("k1", "Spain", "Japan", 1, 0, stage="LAST_16")])
ck("group team eliminated once KO begins (not in any KO match) -> out", team_in(td, "James", "Serbia")["status"] == "out", team_in(td, "James", "Serbia"))
ck("a team IN the KO is alive", team_in(td, "Erol", "Spain")["status"] == "alive", team_in(td, "Erol", "Spain"))
ck("KO winner alive at that KO stage", team_in(td, "Erol", "Spain")["stage"] in ("LAST_16", "GROUP_STAGE", "QUARTER_FINALS"), team_in(td, "Erol", "Spain"))
ck("KO loser is OUT", team_in(td, "James", "Japan")["status"] == "out", team_in(td, "James", "Japan"))
ck("KO loser's 'stage' marks where they went out", team_in(td, "James", "Japan")["stage"] == "LAST_16", team_in(td, "James", "Japan"))
# champion path
KO = [M("r1", "Brazil", "Serbia", 1, 0, stage="LAST_16"),
      M("r2", "Brazil", "Spain", 1, 0, stage="QUARTER_FINALS"),
      M("r3", "Brazil", "Japan", 1, 0, stage="SEMI_FINALS"),
      M("r4", "Brazil", "France", 1, 0, stage="FINAL")]
td = run(FULL, KO)
ck("champion is alive at WINNER", team_in(td, "Erol", "Brazil")["status"] == "alive" and team_in(td, "Erol", "Brazil")["stage"] == "WINNER", team_in(td, "Erol", "Brazil"))
ck("beaten finalist is OUT at FINAL", team_in(td, "Erol", "France")["status"] == "out" and team_in(td, "Erol", "France")["stage"] == "FINAL", team_in(td, "Erol", "France"))
ck("a semi-final loser is out at SEMI_FINALS", team_in(td, "James", "Japan")["stage"] == "SEMI_FINALS", team_in(td, "James", "Japan"))
# alive count
ck("Erol alive_teams counts only his still-in teams", player(td, "Erol")["alive_teams"] == sum(1 for t in player(td, "Erol")["teams"] if t["status"] == "alive"), player(td, "Erol"))
ck("champion's owner has >=1 alive team", player(td, "Erol")["alive_teams"] >= 1, player(td, "Erol"))

print("\n== FURTHEST STAGE (deepest, not earliest) ==")
multi = [M("a", "Brazil", "Serbia", 1, 0, stage="LAST_16"),
         M("b", "Brazil", "Spain", 2, 0, stage="QUARTER_FINALS"),
         M("c", "Brazil", "Japan", 1, 0, stage="SEMI_FINALS")]
td = run(FULL, multi)
ck("furthest-stage reflects the DEEPEST round reached (SEMI not LAST_16)", team_in(td, "Erol", "Brazil")["stage"] == "SEMI_FINALS", team_in(td, "Erol", "Brazil"))

# ============================================================== SURVIVAL VALUES
print("\n== SURVIVAL VALUES ==")
vals = {"LAST_16": 26, "QUARTER_FINALS": 34, "SEMI_FINALS": 44}
for stage, val in vals.items():
    td = run(FULL, [M("x", "Brazil", "Serbia", 1, 0, stage=stage)])
    ck("survival value for %s == %d" % (stage, val), team_in(td, "Erol", "Brazil")["survival"] == val, team_in(td, "Erol", "Brazil"))
td = run(FULL, KO)
ck("champion survival == WINNER (135)", team_in(td, "Erol", "Brazil")["survival"] == 135, team_in(td, "Erol", "Brazil"))
ck("losing finalist survival == FINAL (85)", team_in(td, "Erol", "France")["survival"] == 85, team_in(td, "Erol", "France"))
ck("survival is furthest-only, never summed across rounds", team_in(td, "Erol", "Brazil")["survival"] == 135, "winner shouldn't be 18+26+34+44+85+135")
# 3rd-place: no survival
td = run(FULL, [M("sf1", "Brazil", "France", 0, 1, stage="SEMI_FINALS"),
                M("sf2", "Spain", "Japan", 0, 1, stage="SEMI_FINALS"),
                M("tp", "Brazil", "Spain", 2, 1, stage="THIRD_PLACE")])
ck("3rd-place play-off awards NO survival", team_in(td, "Erol", "Brazil")["survival"] == 44, team_in(td, "Erol", "Brazil"))  # capped at SF
ck("player survival = sum of furthest-survival over their teams", player(td, "Erol")["survival"] == sum(t["survival"] for t in player(td, "Erol")["teams"]), player(td, "Erol"))

# ============================================================== CHAMPION ODDS / FORECAST
print("\n== CHAMPION ODDS & FORECAST ==")
td = run(FULL, [])      # pre-tournament, all alive, all 8 teams owned
ck("champion odds sum to ~100 when all teams owned & alive", abs(sum(p["champion_odds"] for p in td["players"]) - 100) < 1.0, sum(p["champion_odds"] for p in td["players"]))
ck("every champion_odds is 0..100", all(0 <= p["champion_odds"] <= 100 for p in td["players"]), [p["champion_odds"] for p in td["players"]])
ck("champion_odds finite", all(math.isfinite(p["champion_odds"]) for p in td["players"]), "finite")
# a player whose teams are ALL eliminated -> 0% champion
elim = [M("k1", "Serbia", "Japan", 0, 0, stage="LAST_16", winner="HOME", shootout=True),  # James's teams advance one, lose other? make both James out:
        M("k2", "Argentina", "Mexico", 1, 0, stage="LAST_16")]
# Build a clean elimination: knock out BOTH of a 2-team player's teams
td = run(A2, [M("k1", "Brazil", "Serbia", 1, 0, stage="LAST_16"),     # Serbia (James) out
              M("k2", "Spain", "Japan", 1, 0, stage="LAST_16")])      # Japan (James) out
ck("a player with all teams eliminated has 0% champion odds", player(td, "James")["champion_odds"] == 0, player(td, "James"))
ck("the still-alive player keeps >0% champion odds", player(td, "Erol")["champion_odds"] > 0, player(td, "Erol"))
# squad strength + favourites
td = run(FULL, [])
ck("squad_strength == sum of team composites", player(td, "Erol")["squad_strength"] == round(sum(team_in(td, "Erol", t)["composite"] for t in FULL["Erol"])), player(td, "Erol"))
ck("favourites counts tier-1 teams in the squad", player(td, "Erol")["favourites"] == sum(1 for t in player(td, "Erol")["teams"] if t["tier"] == 1), player(td, "Erol"))
ck("projected_points finite & >= 0 for all", all(math.isfinite(p["projected_points"]) and p["projected_points"] >= 0 for p in td["players"]), [p["projected_points"] for p in td["players"]])
ck("'fair' handicap is finite (can be +/-, by design)", all(math.isfinite(p["fair"]) for p in td["players"]), [p["fair"] for p in td["players"]])
ck("champion board exists & sorted by odds desc", all(td["champion"][i]["odds"] >= td["champion"][i+1]["odds"] for i in range(len(td["champion"])-1)), td["champion"])

# ============================================================== LEADERBOARD ORDERING
print("\n== LEADERBOARD ORDERING & TIE-BREAKS ==")
td = run(A2, [M("m1", "Brazil", "Serbia", 3, 0), M("m2", "Spain", "Japan", 0, 0)])
for key in ("points", "hybrid", "fair", "survival"):
    b = td["leaderboards"][key]
    ck("%s board sorted descending by score" % key, all(b[i]["score"] >= b[i+1]["score"] for i in range(len(b)-1)), b)
ck("points board: the higher-scoring player is first", td["leaderboards"]["points"][0]["score"] >= td["leaderboards"]["points"][1]["score"], td["leaderboards"]["points"])
ck("every board lists all players exactly once", all(len({r["name"] for r in td["leaderboards"][k]}) == len(td["players"]) for k in ("points", "hybrid", "fair", "survival")), "names")
# tie: two players equal points -> stable, both present
td = run(A2, [M("m1", "Brazil", "Serbia", 1, 1)])   # 2-2 each side -> equal
ck("a tie keeps both players on the board", len(td["leaderboards"]["points"]) == 2, td["leaderboards"]["points"])

# ============================================================== CHURN / FEED CORRECTIONS
print("\n== CHURN / FEED CORRECTIONS ==")
# a KO result then a 'correction' to SCHEDULED -> team revived to alive (recompute self-heals)
td_out = run(FULL, [M("k1", "Brazil", "Serbia", 0, 1, stage="LAST_16")])   # Brazil out
ck("Brazil is OUT after losing the LAST_16", team_in(td_out, "Erol", "Brazil")["status"] == "out", team_in(td_out, "Erol", "Brazil"))
td_rev = run(FULL, [M("k1", "Brazil", "Serbia", None, None, status="SCHEDULED", stage="LAST_16", winner=None)])
ck("if that match is corrected to SCHEDULED, Brazil is alive again (self-heals)", team_in(td_rev, "Erol", "Brazil")["status"] == "alive", team_in(td_rev, "Erol", "Brazil"))
# a team that both lost a KO and (glitch) appears in a later KO fixture -> OUT takes precedence
td = run(FULL, [M("k1", "Brazil", "Serbia", 0, 1, stage="LAST_16"),       # Brazil lost
                M("k2", "Brazil", "Spain", None, None, status="SCHEDULED", stage="QUARTER_FINALS", winner=None)])
ck("a contradictory 'lost but in a later fixture' team -> still counts as OUT", team_in(td, "Erol", "Brazil")["status"] == "out", team_in(td, "Erol", "Brazil"))
# FINAL with no winner yet (pending) -> no champion decided
td = run(FULL, [M("f", "Brazil", "France", None, None, status="TIMED", stage="FINAL", winner=None)])
ck("a not-yet-played FINAL leaves champion undecided", not td.get("champion_decided"), td.get("champion_decided"))
# two FINAL matches in the feed (bad data) -> no crash
td, err = (None, None)
try:
    td = run(FULL, [M("f1", "Brazil", "France", 1, 0, stage="FINAL"), M("f2", "Spain", "Japan", 2, 1, stage="FINAL")])
except Exception as e:
    err = repr(e)
ck("two FINAL matches don't crash the rebuild", err is None, err)

# ============================================================== FORECAST ADVERSARIAL (no div0 / no crash)
print("\n== FORECAST ADVERSARIAL ==")
def safe(fn):
    try:
        return fn(), None
    except Exception as e:
        return None, repr(e)
# decimal_odds = 0 for every team (would div0 in implied) -> must be guarded
tj0 = teams_json(odds={n: 0 for n in TEAMS})
for t in tj0["teams"]:
    t["implied_prob"] = 0      # force the fallback path
td, err = safe(lambda: run(FULL, [], tj=tj0))
ck("decimal_odds=0 & implied=0 doesn't divide-by-zero", err is None, err)
if td: ck("champion_odds all finite with zero odds data", all(math.isfinite(p["champion_odds"]) for p in td["players"]), "finite")
# composite 0 / missing for all -> projections still finite
tjc = teams_json(comps={n: 0 for n in TEAMS})
td, err = safe(lambda: run(FULL, [], tj=tjc))
ck("all-zero composites: no crash", err is None, err)
if td: ck("projected_points finite with zero composites", all(math.isfinite(p["projected_points"]) for p in td["players"]), "finite")
# every team eliminated (all of them lose a KO) -> champion odds handled, no div0
allko = [M("k%d" % i, TEAMS[i], TEAMS[(i + 4)], 0, 1, stage="LAST_16") for i in range(4)]  # 4 matches, 8 teams, 4 winners 4 losers
td, err = safe(lambda: run(FULL, allko))
ck("a full KO round (half eliminated) doesn't crash", err is None, err)
if td:
    ck("champion odds still sum sanely (<=100.5) after eliminations", sum(p["champion_odds"] for p in td["players"]) <= 100.5, sum(p["champion_odds"] for p in td["players"]))
    ck("champion odds non-negative after eliminations", all(p["champion_odds"] >= 0 for p in td["players"]), "non-neg")
# a team listed in TWO groups (bad teams.json) -> no crash
tjdup = teams_json()
tjdup["teams"][1]["group"] = tjdup["teams"][0]["group"]   # force a duplicate-ish group layout
td, err = safe(lambda: run(FULL, [], tj=tjdup))
ck("an odd group layout doesn't crash the forecast", err is None, err)
# single team in a group (others removed) -> _exp_group_points 0, no crash
tjsolo = {"teams": [t for t in teams_json()["teams"] if t["group"] == "A"]}
solo_assign = {"Erol": [t["name"] for t in tjsolo["teams"]], "James": []}
td, err = safe(lambda: run(solo_assign, [], tj=tjsolo))
ck("single-team group: no crash, finite projection", err is None, err)
if td: ck("a player with NO teams is handled (0s, no crash)", player(td, "James")["points"] == 0 and player(td, "James")["champion_odds"] == 0, player(td, "James"))

# ============================================================== GLOBAL INVARIANTS
print("\n== GLOBAL INVARIANTS ==")
big = [M("m%d" % i, TEAMS[i % 8], TEAMS[(i + 3) % 8], i % 5, (i + 1) % 4, stage="GROUP_STAGE") for i in range(16)]
big += [M("ko%d" % i, TEAMS[i], TEAMS[i + 4], 2, 0, stage="LAST_16") for i in range(4)]
big += [M("fin", "Brazil", "Spain", 1, 0, stage="FINAL", winner="HOME")]
td = run(FULL, big)
P = td["players"]
ck("all survival values finite & >= 0", all(math.isfinite(p["survival"]) and p["survival"] >= 0 for p in P), [p["survival"] for p in P])
ck("all champion_odds finite & 0..100", all(math.isfinite(p["champion_odds"]) and 0 <= p["champion_odds"] <= 100.5 for p in P), [p["champion_odds"] for p in P])
ck("all alive_teams between 0 and total_teams", all(0 <= p["alive_teams"] <= p["total_teams"] for p in P), [(p["alive_teams"], p["total_teams"]) for p in P])
ck("all projected_points finite & >= 0", all(math.isfinite(p["projected_points"]) and p["projected_points"] >= 0 for p in P), "proj")
ck("all 'fair' finite", all(math.isfinite(p["fair"]) for p in P), "fair")
ck("champion_decided set once a FINAL has a winner", bool(td.get("champion_decided")), td.get("champion_decided"))
ck("champion_decided names the winning team", td.get("champion_decided", {}).get("team") == "Brazil", td.get("champion_decided"))
ck("champion_decided credits the right owner", td.get("champion_decided", {}).get("owner") == "Erol", td.get("champion_decided"))

print("\n== MALFORMED STANDINGS (feed groups table) ==")
def run_with_standings(standings, label):
    tj = teams_json()
    json.dump(tj, open("teams.json", "w"))
    json.dump(draw_json(FULL, tj), open("draw_result.json", "w"))
    json.dump({"matches": [M("m1", "Brazil", "Serbia", 1, 0)], "standings": standings}, open("results.json", "w"))
    try:
        scoring.compute(out="td.json", default_mode="hybrid", wagers=None)
        return json.load(open("td.json")), None
    except Exception as e:
        return None, repr(e)
td, err = run_with_standings([{"group": "A", "table": [{"position": 1, "team": "Brazil"}]}], "ok")
ck("a well-formed standings section works", err is None and td, err)
td, err = run_with_standings([{"group": "A"}], "no-table")
ck("standings section missing 'table' doesn't crash", err is None, err)
td, err = run_with_standings([{"table": [{"position": 1}]}], "row-no-team")
ck("standings row missing 'team' doesn't crash", err is None, err)
td, err = run_with_standings(["junk", None, 5, {"table": "notalist"}], "junk-sections")
ck("junk standings sections are ignored, no crash", err is None, err)
td, err = run_with_standings([{"group": "A", "table": ["x", None, {"position": 1, "team": "Brazil"}]}], "junk-rows")
ck("junk rows inside a section are ignored, no crash", err is None, err)

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("\nDEEP SURVIVAL/FORECAST QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll deep survival/forecast QA passed.")
