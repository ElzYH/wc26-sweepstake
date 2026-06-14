#!/usr/bin/env python3
"""
Live match-clock QA. Drives _update_match_clocks through a full match lifecycle with controlled time and
checks the elapsed seconds the tracker will show: anchors correctly, excludes half-time, ticks accurately,
and anchors the clock at the server-detected kickoff (back-dating by the feed minute when one is available, otherwise starting at 0:00), excluding half-time and re-locking to the broadcast minute if it drifts.
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

print("\n== extra time: clock counts on through the ET breaks (90 -> 105 -> 120) ==")
mid = "et1"
S._update_match_clocks(match("IN_PLAY", 0, mid=mid), now=0)               # kickoff
S._update_match_clocks(match("PAUSED", 45, mid=mid), now=2700)            # HT
S._update_match_clocks(match("IN_PLAY", 46, mid=mid), now=3600)           # 2nd half (HT=900 banked)
S._update_match_clocks(match("PAUSED", 90, mid=mid), now=6300)            # pre-ET break
S._update_match_clocks(match("IN_PLAY", 91, mid=mid), now=6600)           # ET 1st half (break=300 banked)
ck("ET kicks off at 90:00 (breaks excluded)", abs((elapsed(mid, 6600) or 0) - 5400) < 1, elapsed(mid, 6600))
S._update_match_clocks(match("PAUSED", 105, mid=mid), now=7500)           # ET half-time
S._update_match_clocks(match("IN_PLAY", 106, mid=mid), now=7560)          # ET 2nd half (ET-HT=60 banked)
ck("ET 2nd half resumes at 105:00", abs((elapsed(mid, 7560) or 0) - 6300) < 1, elapsed(mid, 7560))
ck("ET ends at 120:00", abs((elapsed(mid, 8460) or 0) - 7200) < 1, elapsed(mid, 8460))

print("\n== drift guard: if a poll misses half-time, the clock re-locks to the feed minute ==")
rs = "rs1"
S._update_match_clocks(match("IN_PLAY", 0, mid=rs), now=0)                # kickoff; ko=0
# 60 real minutes pass and we NEVER saw the PAUSED half-time; without correction the clock would read ~60:00.
# The feed says it's the 46th minute (second half just underway) -> we should re-lock to ~46:00.
S._update_match_clocks(match("IN_PLAY", 46, mid=rs), now=3600)
_e = elapsed(rs, 3600)
ck("clock re-locks to the feed minute after missing HT", _e is not None and abs(_e - 2760) < 5, _e)
# and it stays smooth afterwards (no second jump when the feed advances normally)
S._update_match_clocks(match("IN_PLAY", 47, mid=rs), now=3660)
_e2 = elapsed(rs, 3660)
ck("clock stays in sync the next poll", _e2 is not None and abs(_e2 - 2820) < 5, _e2)

print("\n== mid-match first detection (e.g. just deployed): back-date by the feed minute ==")
S._update_match_clocks(match("IN_PLAY", 52, mid="m2"), now=5000)
ck("elapsed reads 52:00 immediately", abs((elapsed("m2", 5000) or 0) - 3120) < 1, elapsed("m2", 5000))
ck("and 53:00 a minute later", abs((elapsed("m2", 5060) or 0) - 3180) < 1, elapsed("m2", 5060))

print("\n== no broadcast minute -> anchor at the server-detected kickoff (clock starts at 0:00) ==")
S._update_match_clocks(match("IN_PLAY", None, mid="m3"), now=6000)
ck("match with no feed minute IS anchored at kickoff (ko=now)", S._load_match_clocks().get("m3") is not None)
ck("no-minute clock reads ~0:00 at kickoff", abs((elapsed("m3", 6000) or 0) - 0) < 1, elapsed("m3", 6000))
ck("no-minute clock ticks on from kickoff", abs((elapsed("m3", 6063) or 0) - 63) < 1, elapsed("m3", 6063))
# ...and if a real broadcast minute later disagrees by >2 min, it re-locks to the feed minute
S._update_match_clocks(match("IN_PLAY", 10, mid="m3"), now=6063)
ck("re-locks to the feed minute when one arrives (10' -> ~600s)", abs((elapsed("m3", 6063) or 0) - 600) < 5, elapsed("m3", 6063))

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

print("\n== clock hardening: a missed half-time (no PAUSED + no feed minute) can NEVER overshoot — the 72'-when-50' bug ==")
def live_sec_via_scoring(clock_rec, fx_extra):
    json.dump({"m1": clock_rec}, open(os.path.join(t, "match_clocks.json"), "w"))
    base = {"id": "m1", "home": "Spain", "away": "France", "status": "IN_PLAY",
            "homeScore": 1, "awayScore": 0, "stage": "GROUP_STAGE",
            "utcDate": "2026-06-11T18:00:00Z", "group": "A"}
    base.update(fx_extra)
    json.dump({"matches": [base]}, open(os.path.join(t, "results.json"), "w"))
    td2 = scoring.compute(teams_path=os.path.join(t, "teams.json"), draw_path=os.path.join(t, "draw_result.json"),
                          results_path=os.path.join(t, "results.json"), out=os.path.join(t, "td.json"),
                          clocks_path=os.path.join(t, "match_clocks.json"))
    return td2["fixtures"][0].get("liveSec")
now2 = _time.time()
# wall-clock says 72 min since kickoff, but half-time was never banked and there's no feed minute -> MUST cap, not show 72
ls = live_sec_via_scoring({"ko": now2 - 72 * 60, "htp": 0, "ps": None}, {"minute": None})
ck("missed-HT + no-minute clock capped at the 1st-half ceiling (<=47:00), NEVER 72:00", ls is not None and ls <= 47 * 60 + 2 and ls < 60 * 60, ls)
# once a real half-time IS banked, the 2nd-half clock is accurate (~50:00) and not capped away
ls = live_sec_via_scoring({"ko": now2 - 65 * 60, "htp": 15 * 60, "ps": None}, {"minute": None})
ck("2nd half with a banked HT reads ~50:00 (accurate)", ls is not None and abs(ls - 50 * 60) <= 2, ls)
# a real feed minute is trusted into extra time
ls = live_sec_via_scoring({"ko": now2 - 105 * 60, "htp": 0, "ps": None}, {"minute": 105})
ck("a real feed minute is trusted into extra time (~105:00)", ls is not None and abs(ls - 105 * 60) <= 60, ls)

print("\n== clock hardening: stop at a penalty shootout (clock freezes, UI shows PENS) ==")
ls = live_sec_via_scoring({"ko": now2 - 125 * 60, "htp": 0, "ps": None}, {"minute": None, "shootout": True})
ck("a shootout does NOT tick a match clock (no liveSec)", ls is None, ls)
ls = live_sec_via_scoring({"ko": now2 - 125 * 60, "htp": 0, "ps": None}, {"minute": None, "penHome": 3, "penAway": 2})
ck("a pens score present also stops the clock", ls is None, ls)

print("\n== clock hardening: corrupt/garbage clock data can never crash or run the clock away ==")
for bad in (float("nan"), -9999, 9e9):
    ls = live_sec_via_scoring({"ko": now2 - 50 * 60, "htp": bad, "ps": None}, {"minute": None})
    ck("corrupt htp=%r -> clock stays sane (<=47:00)" % (bad,), ls is None or ls <= 47 * 60 + 2, ls)
ls = live_sec_via_scoring({"ko": now2 + 600, "htp": 0, "ps": None}, {"minute": None})   # kickoff in the FUTURE (clock skew)
ck("kickoff in the future -> no negative clock (clock simply absent)", ls is None, ls)
for badko in (float("nan"), float("inf"), float("-inf")):
    ls = live_sec_via_scoring({"ko": badko, "htp": 0, "ps": None}, {"minute": None})
    ck("corrupt ko=%r -> no clock (never crashes)" % (badko,), ls is None, ls)
ls = live_sec_via_scoring("not-a-dict", {"minute": None})   # corrupt clock record type
ck("a non-dict clock record -> no clock (never crashes)", ls is None, ls)
# restore the canonical fixture for the sections below
json.dump({"m1": {"ko": nowc - 3120, "htp": 0, "ps": None}}, open(os.path.join(t, "match_clocks.json"), "w"))
json.dump({"matches": [{"id": "m1", "home": "Spain", "away": "France", "status": "IN_PLAY",
                        "homeScore": 1, "awayScore": 0, "stage": "GROUP_STAGE",
                        "utcDate": "2026-06-11T18:00:00Z", "group": "A", "minute": 52}]},
          open(os.path.join(t, "results.json"), "w"))
td = scoring.compute(teams_path=os.path.join(t, "teams.json"), draw_path=os.path.join(t, "draw_result.json"),
                     results_path=os.path.join(t, "results.json"), out=os.path.join(t, "td.json"),
                     clocks_path=os.path.join(t, "match_clocks.json"))
fx = td["fixtures"][0]

print("\n== live-bet 'winning/level/losing' data contract: a bet's matchId matches the live fixture + score ==")
import wager
_m = {"id": "m1", "home": "Spain", "away": "France", "stage": "GROUP_STAGE", "utcDate": "2026-06-11T18:00:00Z"}
_bet_mid = wager.match_id(_m)                                  # the id a bet stores at placement
ck("bet matchId == live fixture matchId", _bet_mid == fx.get("matchId"), (_bet_mid, fx.get("matchId")))
ck("fixture carries the live score (home)", fx.get("homeScore") == 1, fx.get("homeScore"))
ck("fixture carries the live score (away)", fx.get("awayScore") == 0, fx.get("awayScore"))
ck("fixture is flagged live", fx.get("status") == "IN_PLAY", fx.get("status"))
# so a HOME bet on this fixture resolves to 'winning' (1-0) on the frontend, an AWAY bet to 'losing', a DRAW to 'losing'
_hs, _as = fx.get("homeScore"), fx.get("awayScore")
ck("=> HOME bet would read winning", _hs > _as)
ck("=> DRAW bet would read losing", not (_hs == _as))

shutil.rmtree(t, ignore_errors=True)
if FAILS:
    print("\nCLOCK QA FAILED (%d):" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nAll clock QA passed.")
