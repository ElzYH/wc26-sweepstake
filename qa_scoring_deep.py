#!/usr/bin/env python3
"""Deep scoring QA — ~100 checks on the points/survival engine (the heart of the sweepstake):
point math (goals/wins/draws/clean sheets/stage bonuses/champion), live in-play accrual, correct
ownership/allocation, survival values, and a hard adversarial pass to guarantee NO infinite points,
no negative points, no points lost or sent to the wrong player, and no crash on junk match data."""
import os, sys, json, math, shutil, tempfile

SRC = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="wc26_score_")
os.chdir(TMP); sys.path.insert(0, SRC)
import scoring

FAILS = []
def ck(name, cond, extra=""):
    if not cond:
        FAILS.append(name); print("  FAIL " + name + ("" if extra == "" else "  -> %r" % (extra,)))
    else:
        print("  PASS " + name)

# ---- scenario builder -------------------------------------------------------
TEAMS = ["Brazil", "Serbia", "Spain", "Japan", "France", "Ghana", "Argentina", "Mexico"]
def teams_json():
    return {"teams": [{"name": n, "composite": 60 + i, "implied_prob": 0.1,
                       "tier": (i % 4) + 1, "group": "ABCD"[i % 4]} for i, n in enumerate(TEAMS)]}

def draw_json(assign):
    """assign: {player: [team,...]}"""
    tinfo = {t["name"]: t for t in teams_json()["teams"]}
    return {"players": [{"name": pl, "teams": [{"name": t, "tier": tinfo[t]["tier"], "group": tinfo[t]["group"]} for t in ts]}
                        for pl, ts in assign.items()]}

def run(assign, matches, mode="points", wagers=None):
    json.dump(teams_json(), open("teams.json", "w"))
    json.dump(draw_json(assign), open("draw_result.json", "w"))
    json.dump({"matches": matches}, open("results.json", "w"))
    scoring.compute(out="td.json", default_mode=mode, wagers=wagers)
    return json.load(open("td.json"))

def M(mid, h, a, hs, as_, status="FINISHED", stage="GROUP_STAGE", winner=None, **kw):
    if winner is None and hs is not None and as_ is not None:
        winner = "HOME" if hs > as_ else ("AWAY" if as_ > hs else None)
    m = {"id": mid, "home": h, "away": a, "homeScore": hs, "awayScore": as_,
         "status": status, "stage": stage, "winner": winner, "utcDate": "2026-06-15T18:00:00Z"}
    m.update(kw); return m

def player(td, name):
    return next((p for p in td["players"] if p["name"] == name), None)
def team_in(td, pname, tname):
    p = player(td, pname); return next((t for t in p["teams"] if t["name"] == tname), None)

A2 = {"Erol": ["Brazil", "Spain"], "James": ["Serbia", "Japan"]}

# ============================================================== POINT MATH
print("\n== POINT MATH (single results) ==")
td = run(A2, [M("m1", "Brazil", "Serbia", 2, 1)])
ck("winner gets goals(2)+win(3)+0CS = 5", team_in(td, "Erol", "Brazil")["points"] == 5, team_in(td, "Erol", "Brazil"))
ck("loser gets goals(1) only = 1", team_in(td, "James", "Serbia")["points"] == 1, team_in(td, "James", "Serbia"))
td = run(A2, [M("m1", "Brazil", "Serbia", 3, 0)])
ck("3-0 winner: 3 goals+3 win+1 clean sheet = 7", team_in(td, "Erol", "Brazil")["points"] == 7, team_in(td, "Erol", "Brazil"))
ck("0-conceded loser scores 0", team_in(td, "James", "Serbia")["points"] == 0, team_in(td, "James", "Serbia"))
td = run(A2, [M("m1", "Brazil", "Serbia", 0, 0)])
ck("0-0 draw: each 0 goals+1 draw+1 CS = 2", team_in(td, "Erol", "Brazil")["points"] == 2 and team_in(td, "James", "Serbia")["points"] == 2, td)
td = run(A2, [M("m1", "Brazil", "Serbia", 1, 1)])
ck("1-1 draw: each 1 goal+1 draw+0 CS = 2", team_in(td, "Erol", "Brazil")["points"] == 2, team_in(td, "Erol", "Brazil"))
td = run(A2, [M("m1", "Brazil", "Serbia", 5, 4)])
ck("high-scoring win 5-4: 5+3 = 8 (no CS)", team_in(td, "Erol", "Brazil")["points"] == 8, team_in(td, "Erol", "Brazil"))
ck("clean sheet only when conceded 0 (loser 4 conceded -> no CS): 4 goals", team_in(td, "James", "Serbia")["points"] == 4, team_in(td, "James", "Serbia"))
# record string
ck("win/draw/loss record reads 1-0-0 for the winner", team_in(td, "Erol", "Brazil")["record"] == "1-0-0", team_in(td, "Erol", "Brazil")["record"])

print("\n== POINT MATH (accumulation across games) ==")
td = run(A2, [M("m1", "Brazil", "Serbia", 2, 0), M("m2", "Brazil", "Japan", 1, 1)])
# Brazil: game1 2+3+1CS=6 ; game2 1+1draw=2 -> 8
ck("points accumulate across two games (6+2=8)", team_in(td, "Erol", "Brazil")["points"] == 8, team_in(td, "Erol", "Brazil"))
ck("player total = sum of their teams", player(td, "Erol")["points"] == sum(t["points"] for t in player(td, "Erol")["teams"]), player(td, "Erol"))

# ============================================================== OWNERSHIP / ALLOCATION
print("\n== OWNERSHIP & ALLOCATION ==")
td = run(A2, [M("m1", "Brazil", "Serbia", 2, 1)])
ck("points go to the team's OWNER (Erol), not the opponent's owner", player(td, "Erol")["points"] == 5 and player(td, "James")["points"] == 1, td)
ck("a player's total equals the sum over only THEIR teams", player(td, "James")["points"] == team_in(td, "James", "Serbia")["points"] + team_in(td, "James", "Japan")["points"], player(td, "James"))
# isolation: a game between two of Erol's own teams credits both to Erol, nobody else
td2 = run({"Erol": ["Brazil", "Spain"], "James": ["Serbia", "Japan"]}, [M("m1", "Brazil", "Spain", 1, 0)])
ck("intra-owner game credits both teams to the same owner", player(td2, "Erol")["points"] == (1 + 3 + 1) + 0, player(td2, "Erol"))
ck("the other player is unaffected (0)", player(td2, "James")["points"] == 0, player(td2, "James"))
# orphan team (in results but owned by nobody) -> its points simply aren't allocated, no crash
td3 = run({"Erol": ["Brazil"], "James": ["Serbia"]}, [M("m1", "France", "Ghana", 3, 0)])
ck("a game between unowned teams doesn't allocate to anyone (no crash)", player(td3, "Erol")["points"] == 0 and player(td3, "James")["points"] == 0, td3)
# conservation: sum of player points == sum of points over owned teams only
td4 = run(A2, [M("m1", "Brazil", "Serbia", 2, 1), M("m2", "Spain", "Japan", 0, 0)])
owned_pts = sum(t["points"] for p in td4["players"] for t in p["teams"])
ck("conservation: total player points == total owned-team points", sum(p["points"] for p in td4["players"]) == owned_pts, (sum(p["points"] for p in td4["players"]), owned_pts))

# ============================================================== LIVE / IN-PLAY
print("\n== LIVE / IN-PLAY POINTS ==")
td = run(A2, [M("m1", "Brazil", "Serbia", 1, 0, status="IN_PLAY", winner=None)])
ck("in-play match accrues provisional points (1 goal+? ) into total", team_in(td, "Erol", "Brazil")["points"] >= 1, team_in(td, "Erol", "Brazil"))
ck("in-play points show in the team's 'live' field", team_in(td, "Erol", "Brazil")["live"] >= 1, team_in(td, "Erol", "Brazil"))
ck("player 'live' aggregates in-play points", player(td, "Erol")["live"] >= 1, player(td, "Erol"))
td = run(A2, [M("m1", "Brazil", "Serbia", 2, 1, status="FINISHED")])
ck("a FINISHED match contributes 0 live points", team_in(td, "Erol", "Brazil")["live"] == 0, team_in(td, "Erol", "Brazil"))
td = run(A2, [M("m1", "Brazil", "Serbia", 1, 0, status="PAUSED", winner=None)])
ck("a PAUSED (half-time) match still accrues live", team_in(td, "Erol", "Brazil")["live"] >= 1, team_in(td, "Erol", "Brazil"))
# a live scoreline that would be a 'win' shows the provisional win points but status stays alive
td = run(A2, [M("m1", "Brazil", "Serbia", 3, 0, status="IN_PLAY", winner=None)])
ck("live 3-0 shows provisional 3+3+1=7", team_in(td, "Erol", "Brazil")["live"] == 7, team_in(td, "Erol", "Brazil"))

# ============================================================== STAGE BONUS / SURVIVAL / CHAMPION
print("\n== STAGE BONUS, SURVIVAL, CHAMPION ==")
KO = [M("k1", "Brazil", "Serbia", 1, 0, stage="LAST_16"),
      M("k2", "Brazil", "Spain", 2, 1, stage="QUARTER_FINALS"),
      M("k3", "Brazil", "Japan", 1, 0, stage="SEMI_FINALS"),
      M("k4", "Brazil", "France", 2, 0, stage="FINAL")]
td = run({"Erol": ["Brazil"], "James": ["France"]}, KO)
b = team_in(td, "Erol", "Brazil")
ck("furthest stage bonus is WINNER, NOT stacked across rounds", b["points"] == (1+3+1) + (2+3) + (1+3+1) + (2+3+1) + scoring.SCORING["stage_bonus"]["WINNER"], b)
ck("champion team status is alive at WINNER stage", b["status"] == "alive" and b["stage"] == "WINNER", b)
ck("survival for champion is WINNER value (135)", b["survival"] == 135, b)
# a beaten finalist: furthest FINAL bonus(30), survival FINAL(85)
f = team_in(td, "James", "France")
ck("losing finalist gets FINAL points bonus, not WINNER", f["points"] == scoring.SCORING["stage_bonus"]["FINAL"], f)
ck("losing finalist survival is FINAL (85), not WINNER", f["survival"] == 85, f)
# 3rd place: winner gets bronze POINTS bonus but NO survival
td = run({"Erol": ["Brazil"], "James": ["France"]},
         [M("tp", "Brazil", "France", 2, 1, stage="THIRD_PLACE")])
ck("3rd-place winner gets THIRD_PLACE points bonus", team_in(td, "Erol", "Brazil")["points"] == (2+3) + scoring.SCORING["stage_bonus"]["THIRD_PLACE"], team_in(td, "Erol", "Brazil"))
ck("3rd-place gives NO survival value", team_in(td, "Erol", "Brazil")["survival"] == 0, team_in(td, "Erol", "Brazil"))
# penalties: a KO drawn on the pitch but won on pens counts as a win + advances
td = run({"Erol": ["Brazil"], "James": ["Serbia"]},
         [M("k1", "Brazil", "Serbia", 1, 1, stage="QUARTER_FINALS", winner="HOME", shootout=True)])
ck("penalty win in KO counts as a win (1 goal+3 win)", team_in(td, "Erol", "Brazil")["points"] >= 1+3, team_in(td, "Erol", "Brazil"))
ck("penalty loser is OUT at that stage", team_in(td, "James", "Serbia")["status"] == "out", team_in(td, "James", "Serbia"))

# ============================================================== ADVERSARIAL SCORES  (infinity / junk / negative)
print("\n== ADVERSARIAL SCORES (no infinity, no crash, no negatives) ==")
def safe_run(matches, label):
    try:
        td = run(A2, matches); return td, None
    except Exception as e:
        return None, repr(e)
# string score
td, err = safe_run([M("m1", "Brazil", "Serbia", "2", "1")], "string")
ck("string score doesn't crash the rebuild", err is None, err)
if td: ck("string score still produces finite points", math.isfinite(player(td, "Erol")["points"]), player(td, "Erol"))
# infinite score -> must NOT yield infinite points
td, err = safe_run([M("m1", "Brazil", "Serbia", float("inf"), 0)], "inf")
ck("infinite score doesn't crash", err is None, err)
if td:
    ck("INFINITE score does NOT create infinite points", math.isfinite(player(td, "Erol")["points"]), player(td, "Erol"))
    ck("infinite score points stay within a sane bound", player(td, "Erol")["points"] < 1e6, player(td, "Erol"))
# NaN score
td, err = safe_run([M("m1", "Brazil", "Serbia", float("nan"), 0)], "nan")
ck("NaN score doesn't crash", err is None, err)
if td: ck("NaN score yields finite points", math.isfinite(player(td, "Erol")["points"]), player(td, "Erol"))
# negative score
td, err = safe_run([M("m1", "Brazil", "Serbia", -5, 0)], "neg")
ck("negative score doesn't crash", err is None, err)
if td: ck("negative score does NOT make negative points", player(td, "Erol")["points"] >= 0, player(td, "Erol"))
# float score
td, err = safe_run([M("m1", "Brazil", "Serbia", 2.7, 1.2)], "float")
ck("fractional score doesn't crash", err is None, err)
if td: ck("fractional score handled (finite, non-negative)", math.isfinite(player(td, "Erol")["points"]) and player(td, "Erol")["points"] >= 0, player(td, "Erol"))
# huge but finite
td, err = safe_run([M("m1", "Brazil", "Serbia", 10**9, 0)], "huge")
ck("huge finite score doesn't crash", err is None, err)
if td: ck("huge score stays finite", math.isfinite(player(td, "Erol")["points"]), player(td, "Erol"))
# None scores (scheduled)
td, err = safe_run([M("m1", "Brazil", "Serbia", None, None, status="TIMED", winner=None)], "none")
ck("missing scores (scheduled) -> 0 points, no crash", err is None and player(td, "Erol")["points"] == 0, err or player(td, "Erol"))

# ============================================================== DUPLICATE / WEIRD MATCH DATA
print("\n== DUPLICATE & MALFORMED MATCHES ==")
# the SAME match appearing twice must not double-count points
dup = [M("m1", "Brazil", "Serbia", 2, 0), M("m1", "Brazil", "Serbia", 2, 0)]
td, err = safe_run(dup, "dup")
ck("duplicate match doesn't crash", err is None, err)
if td:
    ck("duplicate match is NOT double-counted (still 6, not 12)", team_in(td, "Erol", "Brazil")["points"] == 6, team_in(td, "Erol", "Brazil"))
# TBD / null-team knockout fixtures
td, err = safe_run([M("k1", None, None, None, None, status="TIMED", stage="FINAL", winner=None)], "tbd")
ck("TBD knockout fixture doesn't crash and allocates nothing", err is None and player(td, "Erol")["points"] == 0, err or td)
# a match dict missing keys entirely (sanitizer should backfill)
td, err = safe_run([{"id": "x", "homeScore": 2, "awayScore": 1}], "partial")
ck("a match missing most keys doesn't crash the rebuild", err is None, err)
# non-dict junk in the matches array
td, err = safe_run(["junk", None, 123, M("m1", "Brazil", "Serbia", 1, 0)], "nondict")
ck("non-dict entries in matches are ignored, real one still scores", err is None and team_in(td, "Erol", "Brazil")["points"] == (1+3+1), err or team_in(td, "Erol", "Brazil"))

# ============================================================== GLOBAL INVARIANTS
print("\n== GLOBAL INVARIANTS (no infinity / no negative anywhere) ==")
big = [M("m%d" % i, TEAMS[i % 8], TEAMS[(i + 1) % 8], i % 6, (i + 2) % 5, stage="GROUP_STAGE") for i in range(20)]
big += [M("kf", "Brazil", "Spain", 2, 1, stage="FINAL", winner="HOME")]
td = run({"Erol": ["Brazil", "Spain", "France"], "James": ["Serbia", "Japan", "Ghana"]}, big)
allp = td["players"]
ck("every player's points are finite", all(math.isfinite(p["points"]) for p in allp), [p["points"] for p in allp])
ck("every player's points are >= 0", all(p["points"] >= 0 for p in allp), [p["points"] for p in allp])
ck("every player's survival is finite & >= 0", all(math.isfinite(p["survival"]) and p["survival"] >= 0 for p in allp), [p["survival"] for p in allp])
ck("every team's points finite & >= 0", all(math.isfinite(t["points"]) and t["points"] >= 0 for p in allp for t in p["teams"]), "team pts")
ck("every team's live points finite & >= 0", all(math.isfinite(t["live"]) and t["live"] >= 0 for p in allp for t in p["teams"]), "live")

# ============================================================== MODES
print("\n== MODES (points / survival) ==")
td = run(A2, [M("m1", "Brazil", "Serbia", 2, 1), M("m2", "Spain", "Japan", 1, 0, stage="LAST_16")])
ck("a leaderboard exists for the active mode", "leaderboards" in td or "players" in td, list(td.keys()))

# ============================================================== EMPTY / DEGENERATE
print("\n== EMPTY / DEGENERATE ==")
td, err = safe_run([], "empty")
ck("no matches at all -> everyone on 0, no crash", err is None and all(p["points"] == 0 for p in td["players"]), err or td)

print("\n== LIVE CHURN (score changes during a match; VAR removals; settle on full-time) ==")
import wager as _W
def live_points(td, pname, tname):
    t = team_in(td, pname, tname); return (t["live"], t["points"])
# walk Brazil's match through: IN_PLAY 0-0 -> 1-0 -> (VAR) 0-0 -> 1-0 -> FINISHED 2-1
seq = [
    ("IN_PLAY", 0, 0), ("IN_PLAY", 1, 0), ("IN_PLAY", 0, 0),  # a goal given then chalked off
    ("IN_PLAY", 1, 0), ("PAUSED", 1, 0),
]
prev_live = None
for st, hs, as_ in seq:
    td = run(A2, [M("m1", "Brazil", "Serbia", hs, as_, status=st, winner=None)])
    lv, pts = live_points(td, "Erol", "Brazil")
    ck("live churn %s %d-%d: live points finite & >=0" % (st, hs, as_), math.isfinite(lv) and lv >= 0, lv)
    ck("live churn %s %d-%d: total finite & >=0" % (st, hs, as_), math.isfinite(pts) and pts >= 0, pts)
    if hs == 0:
        ck("VAR removal (0-0) drops live back to a clean-sheet baseline (<=2)", lv <= 2, lv)
    if hs == 1:
        ck("a live 1-0 shows provisional goal+win+CS = 5", lv == 5, lv)
# now FINISHED 2-1: live must clear, final points booked
td = run(A2, [M("m1", "Brazil", "Serbia", 2, 1, status="FINISHED", winner="HOME")])
lv, pts = live_points(td, "Erol", "Brazil")
ck("on full-time, live points clear to 0", lv == 0, lv)
ck("on full-time, final points booked (2 goals+3 win = 5, no CS conceded 1)", pts == 5, pts)
# a bet on this match: pending through live, settles at full-time, no double-count under repeated settle
w = []
_W.place(w, "Erol", {"id": "m1", "home": "Brazil", "away": "Serbia", "stage": "GROUP_STAGE",
                     "status": "TIMED", "utcDate": "2099-01-01T00:00:00Z"}, "HOME", 5,
         settled_points=999, comp_home=80, comp_away=50, now=1_700_000_000)
_W.settle(w, M("m1", "Brazil", "Serbia", 1, 0, status="IN_PLAY", winner=None))
ck("a bet stays pending while the match is live", w[0]["status"] == "pending", w[0]["status"])
_W.settle(w, M("m1", "Brazil", "Serbia", 2, 1, status="FINISHED", winner="HOME"))
ck("the bet settles to won at full-time", w[0]["status"] == "won", w[0]["status"])
r = w[0]["return"]
_W.settle(w, M("m1", "Brazil", "Serbia", 2, 1, status="FINISHED", winner="HOME"))
ck("re-settling after full-time doesn't change it (idempotent under churn)", w[0]["return"] == r, w[0])
# a score correction AFTER settlement must not retro-change the booked bet
_W.settle(w, M("m1", "Brazil", "Serbia", 0, 3, status="FINISHED", winner="AWAY"))
ck("a post-settlement score correction does NOT flip a settled bet", w[0]["status"] == "won", w[0]["status"])

# ---- bet_potential: "+N if your bets land" must equal the REAL score delta when they do ----
print("\n== bet_potential (potential betting points on the leaderboard) ==")
import scoring as _SCO, tempfile as _tfp
_bd = _tfp.mkdtemp()
json.dump({"teams": [{"name": "BP1", "composite": 50, "group": "A"}, {"name": "BP2", "composite": 50, "group": "A"}]}, open(os.path.join(_bd, "teams.json"), "w"))
json.dump({"players": [{"name": "PA", "teams": [{"name": "BP1"}]}, {"name": "PB", "teams": [{"name": "BP2"}]}]}, open(os.path.join(_bd, "draw.json"), "w"))
json.dump({"matches": [{"home": "BP1", "away": "BP2", "homeScore": 2, "awayScore": 0, "status": "FINISHED", "stage": "GROUP_STAGE", "utcDate": "2026-06-10T17:00:00Z", "matchId": 1}]}, open(os.path.join(_bd, "results.json"), "w"))
def _bp_run(_wl):
    _o = os.path.join(_bd, "out.json")
    _SCO.compute(os.path.join(_bd, "teams.json"), os.path.join(_bd, "draw.json"), os.path.join(_bd, "results.json"), _o, "points", wagers=_wl)
    _j = json.load(open(_o))
    return {p["name"]: p for p in _j["players"]}, {r["name"]: r for r in _j["leaderboards"]["points"]}
_BW = [{"id": "a", "player": "PA", "stake": 10, "return": 35, "status": "pending", "matchId": 9},
       {"id": "b", "player": "PA", "stake": 5, "return": 20, "status": "pending", "free": True, "matchId": 9},
       {"id": "c", "player": "PB", "stake": 3, "return": 6, "status": "lost", "matchId": 8}]
_pl, _lb = _bp_run(_BW)
_pl2, _ = _bp_run([dict(w, status=("won" if w["status"] == "pending" else w["status"])) for w in _BW])
_delta = round(_pl2["PA"]["points"] - _pl["PA"]["points"], 1)
ck("potential equals the real score delta when every open bet wins (cushion-aware)", _pl["PA"]["bet_potential"] == _delta, (_pl["PA"]["bet_potential"], _delta))
ck("a player with no open bets shows zero potential", _pl["PB"]["bet_potential"] == 0, _pl["PB"]["bet_potential"])
ck("the potential is surfaced on the leaderboard rows", _lb["PA"].get("bet_potential") == _pl["PA"]["bet_potential"], _lb["PA"])
ck("potential is display-only: the actual score is unchanged by having open bets", _pl["PA"]["points"] < _pl2["PA"]["points"], (_pl["PA"]["points"], _pl2["PA"]["points"]))
shutil.rmtree(_bd, ignore_errors=True)

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("\nDEEP SCORING QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll deep scoring QA passed.")
