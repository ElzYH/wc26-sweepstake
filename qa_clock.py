#!/usr/bin/env python3
"""
Live match-clock QA. Drives _update_match_clocks through a full match lifecycle with controlled time and
checks the elapsed seconds the tracker will show: anchors correctly, excludes half-time, ticks accurately,
and only anchors when the feed gives a minute (never guesses).
"""
import os, sys, json, tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
FAILS = []
def ck(name, cond, got=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -> %s" % (got,)))
    if not cond:
        FAILS.append(name)

t = tempfile.mkdtemp(prefix="wc26_clock_")
os.environ["WC26_DATA"] = t
os.environ["WC26_CONFIG"] = os.path.join(t, "config.json")
json.dump({"configured": True}, open(os.environ["WC26_CONFIG"], "w"))
import server as S

def match(status, minute=None, mid="m1"):
    return [{"id": mid, "home": "Spain", "away": "France", "status": status, "minute": minute}]

def elapsed(mid, now):
    rec = S._load_match_clocks().get(mid)
    if not rec or rec.get("ko") is None:
        return None
    el = now - rec["ko"] - (rec.get("htp") or 0.0)
    if rec.get("ps"):
        el -= max(0.0, now - rec["ps"])
    return el

print("== kickoff anchored at minute 0, then ticks ==")
S._update_match_clocks(match("SCHEDULED"), now=1000)
ck("scheduled match is not tracked", S._load_match_clocks().get("m1") is None)
S._update_match_clocks(match("IN_PLAY", 0), now=1000)
_e0 = elapsed("m1", 1000)
ck("elapsed ~0:00 right after kickoff", _e0 is not None and abs(_e0 - 0) < 1, _e0)
ck("elapsed = 45:00 after 45 real minutes", abs((elapsed("m1", 1000 + 45 * 60) or 0) - 2700) < 1, elapsed("m1", 1000 + 2700))

print("\n== half-time freezes, second half resumes from 45:00 (HT excluded) ==")
S._update_match_clocks(match("PAUSED", 45), now=1000 + 2700)          # HT begins at 45:00
ck("pause start recorded", S._load_match_clocks()["m1"].get("ps") is not None)
S._update_match_clocks(match("IN_PLAY", 46), now=1000 + 2700 + 900)   # 15-min HT, second half kicks off
ck("half-time (900s) banked into htp", abs(S._load_match_clocks()["m1"]["htp"] - 900) < 1, S._load_match_clocks()["m1"]["htp"])
ck("elapsed resumes at 45:00 (not 60:00) after HT", abs((elapsed("m1", 1000 + 3600) or 0) - 2700) < 1, elapsed("m1", 1000 + 3600))
ck("elapsed = 90:00 at full real time", abs((elapsed("m1", 1000 + 2700 + 900 + 2700) or 0) - 5400) < 1, elapsed("m1", 1000 + 6300))

print("\n== mid-match first detection (e.g. just deployed): back-date by the feed minute ==")
S._update_match_clocks(match("IN_PLAY", 52, mid="m2"), now=5000)
ck("elapsed reads 52:00 immediately", abs((elapsed("m2", 5000) or 0) - 3120) < 1, elapsed("m2", 5000))
ck("and 53:00 a minute later", abs((elapsed("m2", 5060) or 0) - 3180) < 1, elapsed("m2", 5060))

print("\n== no broadcast minute -> never guess (stays untracked, frontend shows LIVE) ==")
S._update_match_clocks(match("IN_PLAY", None, mid="m3"), now=6000)
ck("match with no feed minute is not anchored", S._load_match_clocks().get("m3") is None)
# ...but once a minute appears, it anchors
S._update_match_clocks(match("IN_PLAY", 10, mid="m3"), now=6000)
ck("anchors as soon as a minute is available", abs((elapsed("m3", 6000) or 0) - 600) < 1, elapsed("m3", 6000))

print("\n== scoring attaches liveSec/liveHT to fixtures from the clocks file ==")
# write a clocks file and a tiny results.json, run scoring, read the fixture back
shutil_dir = t
import shutil
shutil.copy(os.path.join(REPO, "teams.json"), os.path.join(t, "teams.json"))
json.dump({"players": [{"name": "Erol", "teams": [{"name": "Spain", "tier": 1, "group": "A"}]}]},
          open(os.path.join(t, "draw_result.json"), "w"))
import time as _time
nowc = _time.time()
json.dump({"m1": {"ko": nowc - 3120, "htp": 0, "ps": None}}, open(os.path.join(t, "match_clocks.json"), "w"))
json.dump({"matches": [{"id": "m1", "home": "Spain", "away": "France", "status": "IN_PLAY",
                        "homeScore": 1, "awayScore": 0, "stage": "GROUP_STAGE",
                        "utcDate": "2026-06-11T18:00:00Z", "group": "A", "minute": 52}]},
          open(os.path.join(t, "results.json"), "w"))
import scoring
td = scoring.compute(teams_path=os.path.join(t, "teams.json"), draw_path=os.path.join(t, "draw_result.json"),
                     results_path=os.path.join(t, "results.json"), out=os.path.join(t, "td.json"),
                     clocks_path=os.path.join(t, "match_clocks.json"))
fx = td["fixtures"][0]
ck("fixture carries liveSec ~52:00 (3120s)", abs(fx.get("liveSec", 0) - 3120) <= 2, fx.get("liveSec"))

shutil.rmtree(t, ignore_errors=True)
if FAILS:
    print("\nCLOCK QA FAILED (%d):" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nAll clock QA passed.")
