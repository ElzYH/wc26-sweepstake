#!/usr/bin/env python3
"""
FIFA 2026 group tiebreaker + free-points claim-window QA.

Two behaviours locked in here:
  1. scoring._order_group_table applies the 2026 World Cup order — head-to-head among level teams FIRST
     (points, then GD, then goals), only then overall GD/goals, then a deterministic ranking fallback.
     This is the rule change that replaced overall-goal-difference-first from 2022 and earlier.
  2. _free_bet_drops makes each match-day drop claimable for the WHOLE UTC day, and an unclaimed drop rolls
     forward (grace days) instead of slamming shut at the first kickoff — so a missed drop isn't simply lost.
"""
import os, sys, json, tempfile, time

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        FAILS.append(name)

import scoring as SC

def order(rows, gm):
    return [r["team"] for r in SC._order_group_table(rows, gm)]

# ---------- 1. head-to-head beats overall goal difference (the core 2026 change) ----------
# A and B both 6 pts. B has the better OVERALL goal difference (+5 vs +2), but A beat B head-to-head.
rows = [
    {"team": "A", "points": 6, "goalsFor": 4, "goalsAgainst": 2, "goalDifference": 2, "composite": 50},
    {"team": "B", "points": 6, "goalsFor": 7, "goalsAgainst": 2, "goalDifference": 5, "composite": 50},
    {"team": "C", "points": 0, "goalsFor": 0, "goalsAgainst": 7, "goalDifference": -7, "composite": 40},
]
gm = [("A", "B", 1, 0), ("A", "C", 3, 2), ("B", "C", 6, 0)]
ck("head-to-head result outranks overall goal difference (2026 rule, not 2022)", order(rows, gm)[:2] == ["A", "B"], order(rows, gm))

# ---------- 2. head-to-head GOALS as the separator when H2H points & GD are level ----------
# A & B both 4 pts, drew head-to-head 2-2 (H2H pts level, H2H GD level 0) -> H2H goals: irrelevant here since the
# single H2H game is a draw with equal goals, so it falls through to overall GD. Make overall GD decide: A +1, B 0.
rows2 = [
    {"team": "A", "points": 4, "goalsFor": 5, "goalsAgainst": 4, "goalDifference": 1, "composite": 50},
    {"team": "B", "points": 4, "goalsFor": 4, "goalsAgainst": 4, "goalDifference": 0, "composite": 60},
    {"team": "C", "points": 4, "goalsFor": 3, "goalsAgainst": 4, "goalDifference": -1, "composite": 55},
]
# H2H mini-table among A,B,C: A 2-2 B, B 1-0 C, C 1-0 A  -> pts: A3 B4 C3 ; so B first by H2H pts.
gm2 = [("A", "B", 2, 2), ("B", "C", 1, 0), ("C", "A", 1, 0)]
o2 = order(rows2, gm2)
ck("head-to-head mini-table points decide a 3-way level group (B tops on H2H pts)", o2[0] == "B", o2)
# A and C both have 3 H2H pts; H2H GD A:(2-2)+(0-1)=-1, C:(0-1)+(1-0)? wait recompute below in assertion
# A H2H: vs B 2-2 (gd0), vs C 0-1 (gd-1) => H2H gd -1 ; C H2H: vs B 0-1 (gd-1), vs A 1-0 (gd+1) => 0. C > A.
ck("remaining H2H tie (A vs C) broken by head-to-head goal difference (C above A)", o2 == ["B", "C", "A"], o2)

# ---------- 3. full 3-way head-to-head deadlock -> overall goal difference ----------
rows3 = [
    {"team": "A", "points": 6, "goalsFor": 5, "goalsAgainst": 3, "goalDifference": 2, "composite": 50},
    {"team": "B", "points": 6, "goalsFor": 6, "goalsAgainst": 3, "goalDifference": 3, "composite": 50},
    {"team": "C", "points": 6, "goalsFor": 7, "goalsAgainst": 3, "goalDifference": 4, "composite": 50},
    {"team": "D", "points": 0, "goalsFor": 1, "goalsAgainst": 13, "goalDifference": -12, "composite": 40},
]
gm3 = [("A", "B", 1, 0), ("B", "C", 1, 0), ("C", "A", 1, 0), ("A", "D", 3, 1), ("B", "D", 4, 1), ("C", "D", 5, 1)]
ck("rock-paper-scissors H2H deadlock falls back to overall GD (C>B>A)", order(rows3, gm3) == ["C", "B", "A", "D"], order(rows3, gm3))

# ---------- 4. positions renumber 1..N, every team kept ----------
res = SC._order_group_table(rows3, gm3)
ck("positions renumber 1..N with no team dropped", [r["position"] for r in res] == [1, 2, 3, 4] and {r["team"] for r in res} == {"A", "B", "C", "D"}, res)

# ---------- 5. no head-to-head data yet (early group stage) -> overall GD/goals, never crashes ----------
rows4 = [
    {"team": "A", "points": 3, "goalsFor": 3, "goalsAgainst": 1, "goalDifference": 2, "composite": 50},
    {"team": "B", "points": 3, "goalsFor": 2, "goalsAgainst": 1, "goalDifference": 1, "composite": 60},
]
ck("no H2H played yet -> orders on overall GD without error", order(rows4, []) == ["A", "B"], order(rows4, []))

# ================= third-place race (best-N qualification) =================
gt = [
    {"group": "A", "table": [{"team": "A1", "points": 9}, {"team": "A2", "points": 6},
        {"team": "A3", "points": 4, "goalDifference": 1, "goalsFor": 5, "goalsAgainst": 4, "playedGames": 3, "composite": 40, "owner": "Erol"}]},
    {"group": "B", "table": [{"team": "B1", "points": 9}, {"team": "B2", "points": 6},
        {"team": "B3", "points": 4, "goalDifference": 2, "goalsFor": 6, "goalsAgainst": 4, "playedGames": 3, "composite": 40, "owner": "Lou"}]},
    {"group": "C", "table": [{"team": "C1", "points": 9}, {"team": "C2", "points": 6},
        {"team": "C3", "points": 3, "goalDifference": 0, "goalsFor": 3, "goalsAgainst": 3, "playedGames": 3, "composite": 40, "owner": "Ismail"}]},
    {"group": "D", "table": [{"team": "D1", "points": 9}, {"team": "D2", "points": 6},
        {"team": "D3", "points": 4, "goalDifference": -1, "goalsFor": 2, "goalsAgainst": 3, "playedGames": 3, "composite": 40, "owner": "James"}]},
]
race = SC._third_place_table(gt)
ord3 = [r["team"] for r in race["table"]]
# B3(4,+2,6) > A3(4,+1,5) > D3(4,-1,2) > C3(3pts): points first, then GD, then goals; no head-to-head
ck("third-place teams ranked points -> GD -> goals (no head-to-head)", ord3 == ["B3", "A3", "D3", "C3"], ord3)
ck("ranks are 1..N", [r["rank"] for r in race["table"]] == [1, 2, 3, 4], race["table"])
ck("qualifying flag matches the slot cut-off", [r["qualifying"] for r in race["table"]] == [r["rank"] <= race["slots"] for r in race["table"]], race)
ck("started flag true once any third-placed team has played", race["started"] is True, race["started"])

# ================= mid-group mathematical elimination =================
FIN="FINISHED"
def _st(group, rows): return [{"group":group,"table":rows}]
def _m(group,h,a,hg,ag,status=FIN): return {"group":group,"stage":"GROUP_STAGE","home":h,"away":a,"homeScore":hg,"awayScore":ag,"status":status}

# Haiti-like: lost both games (0 pts, one to play), lost the head-to-head to the 3rd-placed team -> can't reach 3rd
hrows=[{"team":"Australia","points":6},{"team":"Paraguay","points":4},{"team":"Scotland","points":3},{"team":"Haiti","points":0}]
hgms=[_m("D","Australia","Haiti",2,0), _m("D","Scotland","Haiti",1,0), _m("D","Australia","Paraguay",1,1),
      _m("D","Paraguay","Scotland",2,1), _m("D","Australia","Scotland",2,0), _m("D","Paraguay","Haiti",None,None,"TIMED")]
helim=SC._eliminated_teams(_st("D",hrows),hgms)
ck("team that can't reach 3rd (lost H2H, max 3 pts) is flagged eliminated", "Haiti" in helim, sorted(helim))
ck("elimination never over-fires (only Haiti out here)", helim=={"Haiti"}, sorted(helim))

# soundness: teams level on points with games left and live paths are NOT eliminated
srows=[{"team":"A","points":6},{"team":"B","points":3},{"team":"C","points":3},{"team":"D","points":3}]
sgms=[_m("E","A","B",1,0),_m("E","A","C",1,0),_m("E","C","D",0,0),
      _m("E","B","C",None,None,"TIMED"),_m("E","A","D",None,None,"TIMED"),_m("E","B","D",None,None,"TIMED")]
ck("no team with a remaining path to 3rd is ever eliminated (soundness)", SC._eliminated_teams(_st("E",srows),sgms)==set())

# early stage: one game played -> nobody out
erows=[{"team":"W","points":3},{"team":"X","points":0},{"team":"Y","points":1},{"team":"Z","points":1}]
egms=[_m("F","W","X",1,0),_m("F","Y","Z",0,0),_m("F","W","Y",None,None,"TIMED"),
      _m("F","X","Z",None,None,"TIMED"),_m("F","W","Z",None,None,"TIMED"),_m("F","X","Y",None,None,"TIMED")]
ck("early group stage: no team eliminated yet", SC._eliminated_teams(_st("F",erows),egms)==set())

# ================= free-points claim window =================
t = tempfile.mkdtemp(prefix="wc26_tb_")
os.environ["WC26_DATA"] = t
os.environ["WC26_CONFIG"] = os.path.join(t, "config.json")
json.dump({"configured": True, "wagering_enabled": True}, open(os.environ["WC26_CONFIG"], "w"))
# a results.json with one match-day of group games (first kickoff at 16:00 UTC)
DAY0 = "2026-06-20"
ko = time.strftime("%Y-%m-%dT16:00:00Z", time.gmtime(time.time()))   # not used directly; build explicit below
results = {"competition": "WC", "matches": [
    {"utcDate": DAY0 + "T16:00:00Z", "stage": "GROUP_STAGE", "group": "A", "home": "X", "away": "Y", "status": "TIMED"},
    {"utcDate": DAY0 + "T19:00:00Z", "stage": "GROUP_STAGE", "group": "A", "home": "Z", "away": "W", "status": "TIMED"},
], "standings": []}
json.dump(results, open(os.path.join(t, "results.json"), "w"))
import server as S

def _utc(s):
    import calendar, time as _t
    return calendar.timegm(_t.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))

midnight = _utc(DAY0 + "T00:00:00Z")
first_ko = _utc(DAY0 + "T16:00:00Z")
drops = {d["id"]: d for d in S._free_bet_drops()}
ck("a match-day drop exists for the fixture day", DAY0 in drops, list(drops))
if DAY0 in drops:
    d = drops[DAY0]
    # whole-day: still open AFTER the first kickoff (old code closed exactly at first kickoff)
    ck("drop is still OPEN one hour after the first kickoff (whole-day window, not closed at KO)",
       d["opens"] <= first_ko + 3600 < d["closes"], (d["opens"], first_ko, d["closes"]))
    # grace rollover: unclaimed -> open well past end of the match-day
    ck("unclaimed drop rolls past end of day (grace) so it isn't simply lost",
       d["closes"] >= midnight + 2 * 86400, d["closes"])

# once SOMEONE claims, the grace extension drops away (window no longer rolls forward)
cfg = S.load_config(); cfg["free_bet_claims"] = {DAY0: {"Erol": "x"}}; S.save_config(cfg)
d2 = {x["id"]: x for x in S._free_bet_drops()}.get(DAY0)
if d2:
    ck("after a claim, the window no longer carries the grace roll-forward",
       d2["closes"] <= midnight + 86400, d2["closes"])

print()
if FAILS:
    print("FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All tiebreak + claim-window tests passed.")
