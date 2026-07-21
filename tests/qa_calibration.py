#!/usr/bin/env python3
"""Heavy QA + fuzz for the auto-calibration system (server.py): the calibration overlay loader,
the goals knob, every guard in _maybe_auto_calibrate, the integrity abort, and a 1000-case fuzz of
random/garbage markets. Asserts the live betting surface can never be left underround, out-of-band,
or crashed by calibration. Exits non-zero on any failure."""
import os, sys, json, tempfile, shutil, random, math

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
_T = tempfile.mkdtemp(prefix="qa_cal_")
os.environ["WC26_DATA"] = _T
shutil.copy(os.path.join(REPO, "teams.json"), os.path.join(_T, "teams.json"))

import server as S
import wager as W
S.wager_mod = W
CAL = os.path.join(_T, "calibration.json")

FAILS = []
def ck(name, cond, extra=None):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -> %r" % (extra,)))
    if not cond:
        FAILS.append(name)

def write(path, obj):
    json.dump(obj, open(os.path.join(_T, path), "w"))

def reset_cal():
    try: os.remove(CAL)
    except OSError: pass

BASE = {t["name"]: t["composite"] for t in json.load(open(os.path.join(_T, "teams.json")))["teams"]}
SAMPLE = "Brazil"

print("== load_teams: overlay applies clean values, ignores every kind of junk ==")
reset_cal()
ck("no calibration file -> base composites", {t["name"]: t["composite"] for t in S.load_teams()}[SAMPLE] == BASE[SAMPLE])
write("calibration.json", {"composites": {SAMPLE: 50.0}})
ck("a clean override is applied", {t["name"]: t["composite"] for t in S.load_teams()}[SAMPLE] == 50.0)
for bad in [float("nan"), float("inf"), -5, 0, 9999, "x", None, [1]]:
    write("calibration.json", {"composites": {SAMPLE: bad}})
    got = {t["name"]: t["composite"] for t in S.load_teams()}[SAMPLE]
    ck("junk override %r ignored -> base kept" % (bad,), abs(got - BASE[SAMPLE]) < 1e-9, got)
write("calibration.json", {"composites": {"NoSuchTeam": 50.0}})
ck("override for unknown team is harmless", {t["name"]: t["composite"] for t in S.load_teams()}[SAMPLE] == BASE[SAMPLE])
write("calibration.json", {"garbage": True})
ck("malformed calibration -> base, no crash", {t["name"]: t["composite"] for t in S.load_teams()}[SAMPLE] == BASE[SAMPLE])
reset_cal()

print("\n== goals knob: applied only when finite + in-band ==")
_def = W.GOALS_BASE
reset_cal(); ck("no override -> wager default", abs(S._calibrated_goals_base() - _def) < 1e-9)
write("calibration.json", {"goals_base": 2.5}); ck("in-band override returned", abs(S._calibrated_goals_base() - 2.5) < 1e-9)
for bad in [1.0, 5.0, float("nan"), "x", None]:
    write("calibration.json", {"goals_base": bad})
    ck("out-of-band/junk goals_base %r -> default" % (bad,), abs(S._calibrated_goals_base() - _def) < 1e-9)
write("calibration.json", {"goals_base": 2.4}); S._apply_goals_base()
ck("_apply_goals_base pushes a valid value into wager", abs(W.GOALS_BASE - 2.4) < 1e-9)
W.GOALS_BASE = _def; reset_cal(); S._apply_goals_base()

print("\n== inversion maths: monotone, clamped, round-trip ==")
pe = W._fair_probs(40.0, 40.0)[0]
ck("equal-strength prob inverts to ~equal composite", abs(S._implied_composite(pe, 40.0, "home") - 40.0) < 1.5)
ck("higher target -> higher composite", S._implied_composite(0.7, 40, "home") > S._implied_composite(0.3, 40, "home"))
ck("implied composite clamped to band", 1.0 <= S._implied_composite(0.999, 40, "home") <= 105.0)
lam = S._market_implied_lambda(0.5)
ck("implied lambda round-trips through the Poisson tail", abs((1 - W._poisson_cdf(2, lam)) - 0.5) < 0.02, lam)

# ---- harness for the auto-calibrator ----
UP = "2030-01-01T20:00:00Z"          # far-future -> always "upcoming", never frozen by the soon-window
def setup(extra_fixtures=None, finished_day="2026-06-14"):
    fx = [
        {"id": "g1", "home": "Australia", "away": "Turkey", "homeScore": 2, "awayScore": 0, "status": "FINISHED", "utcDate": finished_day + "T15:00:00Z"},
        {"id": "g2", "home": "Brazil", "away": "Morocco", "homeScore": 1, "awayScore": 1, "status": "FINISHED", "utcDate": finished_day + "T18:00:00Z"},
        {"id": "u1", "home": "Spain", "away": "Japan", "status": "TIMED", "utcDate": UP},
        {"id": "u2", "home": "Argentina", "away": "Croatia", "status": "TIMED", "utcDate": UP},
    ] + (extra_fixtures or [])
    write("tracker_data.json", {"fixtures": fx, "players": []})
    write("draw_result.json", {"players": [{"name": "Erol", "teams": []}]})

def cfg(**kw):
    base = {"auto_calibrate": True, "discord_webhook": "", "odds_audit_discord": False}
    base.update(kw); S.save_config(base); return S.load_config()

def MK(**games):
    """games: name -> (h_dec, d_dec, a_dec, books[, over, under])"""
    out = {}
    for k, v in games.items():
        h, d, a = v[0], v[1], v[2]; books = v[3]
        rec = {"books": books, "h2h": {"home": h, "draw": d, "away": a}}
        if len(v) >= 6:
            rec["totals"] = {"line": 2.5, "over": v[4], "under": v[5]}
        out[k.replace("__", " ")] = rec
    return out

print("\n== auto-calibrator: master switch + matchday + market gating ==")
reset_cal(); setup()
S._maybe_auto_calibrate(cfg(auto_calibrate=False), market=MK(**{"Spain v Japan": (1.8, 3.4, 4.5, 8)}))
ck("OFF by default -> no calibration file written", not os.path.exists(CAL))
S._maybe_auto_calibrate(cfg(auto_calibrate=True), market=None)
ck("ON but no market -> no-op (no file)", not os.path.exists(CAL))
# unfinished matchday -> skip
reset_cal(); setup(extra_fixtures=[{"id": "x", "home": "Egypt", "away": "Ghana", "status": "FINISHED", "homeScore": 1, "awayScore": 0, "utcDate": "2026-06-15T15:00:00Z"},
                                   {"id": "y", "home": "Iran", "away": "Wales", "status": "TIMED", "utcDate": "2026-06-15T18:00:00Z"}])
S._maybe_auto_calibrate(cfg(), market=MK(**{"Spain v Japan": (1.8, 3.4, 4.5, 8)}))
got = S._load_calibration().get("last_calibrated_matchday")
ck("only a FULLY finished matchday calibrates (latest=2026-06-14)", got in (None, "2026-06-14"), got)

print("\n== auto-calibrator: applies, is bounded, clamped, idempotent ==")
reset_cal(); setup()
# market strongly disagrees with the model on Spain/Japan + Argentina/Croatia -> wants big moves; must be capped
S._maybe_auto_calibrate(cfg(calibration_max_step=5.0),
                        market=MK(**{"Spain v Japan": (1.30, 5.0, 9.0, 9),
                                     "Argentina v Croatia": (1.05, 11.0, 21.0, 9)}))
cal = S._load_calibration()
ck("a calibration was written", os.path.exists(CAL) and cal.get("last_calibrated_matchday") == "2026-06-14")
ck("history recorded", isinstance(cal.get("history"), list) and len(cal["history"]) == 1)
moved = cal.get("composites") or {}
ck("at least one team moved", len(moved) >= 1, moved)
for nm, nv in moved.items():
    ck("%s move capped at <=5.0" % nm, abs(nv - BASE[nm]) <= 5.0 + 1e-6, (BASE[nm], nv))
    ck("%s stays in band" % nm, 1.0 <= nv <= 105.0)
before = dict(moved)
S._maybe_auto_calibrate(cfg(calibration_max_step=5.0),
                        market=MK(**{"Spain v Japan": (1.30, 5.0, 9.0, 9)}))
ck("idempotent: same matchday does not move again", (S._load_calibration().get("composites") or {}) == before)

print("\n== auto-calibrator: coverage floor + live/imminent freeze ==")
reset_cal(); setup()
S._maybe_auto_calibrate(cfg(calibration_min_books=5),
                        market=MK(**{"Spain v Japan": (1.30, 5.0, 9.0, 2)}))   # only 2 books < 5
ck("thin market (books < min) is ignored", (S._load_calibration().get("composites") or {}) == {})
# freeze: Spain kicks off in 5 min -> must not move
reset_cal()
import time as _t
soon = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() + 300))
setup(extra_fixtures=[])
fx = json.load(open(os.path.join(_T, "tracker_data.json")))
for m in fx["fixtures"]:
    if m["id"] == "u1":
        m["utcDate"] = soon
write("tracker_data.json", fx)
S._maybe_auto_calibrate(cfg(), market=MK(**{"Spain v Japan": (1.30, 5.0, 9.0, 9)}))
moved = S._load_calibration().get("composites") or {}
ck("a team kicking off soon is FROZEN (not nudged)", "Spain" not in moved and "Japan" not in moved, moved)

print("\n== auto-calibrator: INTEGRITY ABORT leaves odds untouched ==")
reset_cal(); setup()
_orig = S._odds_integrity_violations
S._odds_integrity_violations = lambda td, teams: ["FORCED v VIOLATION — 1X2 book 99.0%"]
try:
    S._maybe_auto_calibrate(cfg(), market=MK(**{"Spain v Japan": (1.30, 5.0, 9.0, 9)}))
finally:
    S._odds_integrity_violations = _orig
ck("on integrity violation, NO composites are written", not os.path.exists(CAL) or not (S._load_calibration().get("composites")))

print("\n== auto-calibrator never writes betting data ==")
ck("no wagers.json created by calibration", not os.path.exists(os.path.join(_T, "wagers.json")))

print("\n== applied calibration keeps every offered market overround (house edge preserved) ==")
reset_cal(); setup()
S._maybe_auto_calibrate(cfg(), market=MK(**{"Spain v Japan": (1.8, 3.4, 4.5, 9, 1.9, 1.95),
                                            "Argentina v Croatia": (2.2, 3.2, 3.3, 9, 2.0, 1.85)}))
teams_after = {t["name"]: t for t in S.load_teams()}
td_after = json.load(open(os.path.join(_T, "tracker_data.json")))
S._apply_goals_base()
ck("post-calibration: no upcoming market underrounds", S._odds_integrity_violations(td_after, teams_after) == [])
W.GOALS_BASE = _def

print("\n== FUZZ: 1000 random/garbage markets — never crash, never underround, always in-band ==")
random.seed(1234)
names = list(BASE.keys())
junk_prices = [None, "x", 0, -1, 1.0, float("nan"), float("inf"), 1.01, 100.0, {}, []]
crashed = underround = oob = 0
for i in range(1000):
    reset_cal(); setup()
    games = {}
    for _ in range(random.randint(0, 6)):
        h, a = random.sample(names, 2)
        def price():
            return random.choice(junk_prices) if random.random() < 0.25 else round(random.uniform(1.05, 25.0), 2)
        rec = {"books": random.choice([0, 1, 2, 3, 5, 9, 999, -1, "x"])}
        if random.random() < 0.9:
            rec["h2h"] = {"home": price(), "draw": price(), "away": price()}
        if random.random() < 0.5:
            rec["totals"] = {"line": 2.5, "over": price(), "under": price()}
        # occasionally a totally malformed record
        if random.random() < 0.1:
            rec = random.choice([None, 42, "nope", {"h2h": "bad"}, {"books": 9}])
        games["%s v %s" % (h, a)] = rec
    try:
        S._maybe_auto_calibrate(cfg(calibration_max_step=random.choice([1, 5, 50]),
                                    calibration_min_books=random.choice([1, 3]),
                                    calibration_goals_step=random.choice([0.05, 0.2])),
                                market=games)
    except Exception as e:
        crashed += 1
        if crashed <= 3:
            print("    FUZZ CRASH:", repr(e))
        continue
    co = S._load_calibration().get("composites") or {}
    for nm, nv in co.items():
        if not (isinstance(nv, (int, float)) and 1.0 <= nv <= 105.0):
            oob += 1
    teams_after = {t["name"]: t for t in S.load_teams()}
    S._apply_goals_base()
    if S._odds_integrity_violations(td_after, teams_after) != []:
        underround += 1
    W.GOALS_BASE = _def
ck("FUZZ: zero crashes over 1000 random markets", crashed == 0, crashed)
ck("FUZZ: every written composite stayed in band", oob == 0, oob)
ck("FUZZ: no calibration ever left a market underround", underround == 0, underround)

shutil.rmtree(_T, ignore_errors=True)
if FAILS:
    print("\nCALIBRATION QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll calibration QA passed.")
