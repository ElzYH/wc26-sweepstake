#!/usr/bin/env python3
"""
Notification regression QA — the LIVE bug where per-match Discord/push alerts went silent on opening day.

Root cause: notify_changes() returned early whenever matches_played == 0, but on opening day NOTHING has
finished yet while the first games are live, so every kickoff/goal/full-time alert was suppressed (the
daily digest is a separate path, which is why it still worked). These checks lock in that per-match alerts
fire as soon as a game is live — and that the leaderboard pings stay quiet until a result is settled.
"""
import os, sys, json, tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        FAILS.append(name)

t = tempfile.mkdtemp(prefix="wc26_notify_")
os.environ["WC26_DATA"] = t
os.environ["WC26_CONFIG"] = os.path.join(t, "config.json")
json.dump({"configured": True, "discord_webhook": "https://example/webhook", "site_url": "https://example"},
          open(os.environ["WC26_CONFIG"], "w"))
import server as S

DISC, PUSH, MENT, DM, DMALL = [], [], [], [], []
S.discord_send = lambda text: DISC.append(text)
S.push_player = lambda player, etype, title, body: PUSH.append((player, etype, title))
S.discord_mention = lambda who, msg: MENT.append((who, msg))
S._bot_dm_player = lambda player, text, match_id=None: (DM.append((player, text)) or 1)   # personal DM path
S._dm_all_games = lambda text: DMALL.append(text)                          # all-games opt-in feed

def reset():
    DISC.clear(); PUSH.clear(); MENT.clear(); DM.clear(); DMALL.clear()

def tracker(matches_played, status, hs, as_, leader="Erol", hybrid=None):
    return {"stats": {"matches_played": matches_played},
            "leaderboards": {"hybrid": hybrid or [{"name": leader}, {"name": "James"}],
                             "points": [{"name": leader}], "survival": [{"name": leader}]},
            "players": [{"name": "Erol"}, {"name": "James"}],
            "fixtures": [{"home": "Spain", "away": "France", "status": status,
                          "homeOwner": "Erol", "awayOwner": "James",
                          "homeScore": hs, "awayScore": as_, "stage": "GROUP_STAGE", "group": "A"}]}

def transition(old_td, new_td):
    json.dump(old_td, open(os.path.join(t, "tracker_data.json"), "w"))
    old = S._load_tracker()
    json.dump(new_td, open(os.path.join(t, "tracker_data.json"), "w"))
    reset()
    S.notify_changes(old)

print("== opening day (matches_played == 0): live alerts must STILL fire ==")
transition(tracker(0, "TIMED", None, None), tracker(0, "IN_PLAY", 0, 0))
ck("kickoff posts to the webhook on opening day", any("Kicked off" in x for x in DISC), DISC)
ck("kickoff pushes to both owners", {p[0] for p in PUSH} == {"Erol", "James"}, PUSH)
ck("kickoff DMs both owners personally", {p[0] for p in DM} == {"Erol", "James"}, DM)
ck("kickoff feeds the all-games subscribers", any("Kicked off" in x for x in DMALL), DMALL)

transition(tracker(0, "IN_PLAY", 0, 0), tracker(0, "IN_PLAY", 1, 0))
ck("a goal DMs the scoring team's owner (not an @mention)", any("scored" in m[1] for m in DM), DM)
ck("a goal no longer @mentions in the channel", not any("scored" in m[1] for m in MENT), MENT)
ck("a goal feeds the all-games subscribers", any("scored" in x for x in DMALL), DMALL)
ck("a goal pushes the owner", any(p[1] == "goal" for p in PUSH), PUSH)

transition(tracker(0, "IN_PLAY", 1, 0), tracker(1, "FINISHED", 1, 0))
ck("full-time posts to the webhook", any("Full-time" in x for x in DISC), DISC)

print("\n== channel feed (switch ON): EVERY game event posts to the channel — solo, head-to-head, knockout ==")
def solo_tracker(status, hs, as_, stage="GROUP_STAGE"):
    # James owns the home team; the away team is unowned (pool) -> NOT head-to-head, but the channel still gets it
    return {"stats": {"matches_played": 0, "teams_remaining": 30, "goals": 0},
            "leaderboards": {"hybrid": [{"name": "James"}], "points": [{"name": "James"}], "survival": [{"name": "James"}]},
            "players": [{"name": "Erol"}, {"name": "James"}],
            "fixtures": [{"home": "Brazil", "away": "Panama", "status": status,
                          "homeOwner": "James", "awayOwner": "—",
                          "homeScore": hs, "awayScore": as_, "stage": stage, "group": "A"}]}
# solo kickoff -> channel post + owner DM + push + all-games feed
transition(solo_tracker("TIMED", None, None), solo_tracker("IN_PLAY", 0, 0))
ck("a solo game's kickoff posts to the channel (switch on means post game events)", any("Kicked off" in x for x in DISC), DISC)
ck("a solo game's kickoff still DMs the owner", any(p[0] == "James" for p in DM), DM)
ck("a solo game's kickoff still pushes the owner", any(p[0] == "James" for p in PUSH), PUSH)
ck("a solo game's kickoff still feeds the all-games feed", any("Kicked off" in x for x in DMALL), DMALL)
# solo goal -> channel post + owner DM
transition(solo_tracker("IN_PLAY", 0, 0), solo_tracker("IN_PLAY", 1, 0))
ck("a solo goal posts to the channel (switch on)", any("scored" in x for x in DISC), DISC)
ck("a solo goal still DMs the owner", any("scored" in m[1] for m in DM), DM)
# head-to-head (Spain/Erol vs France/James) kickoff -> posts to channel
transition(tracker(0, "TIMED", None, None), tracker(0, "IN_PLAY", 0, 0))
ck("a head-to-head kickoff posts to the channel", any("Kicked off" in x for x in DISC), DISC)
# knockout kickoff -> posts to channel
transition(solo_tracker("TIMED", None, None, stage="LAST_16"), solo_tracker("IN_PLAY", 0, 0, stage="LAST_16"))
ck("a knockout game posts to the channel too", any("Kicked off" in x for x in DISC), DISC)

print("\n== pre-tournament (nothing live, nothing finished): stay SILENT ==")
transition(tracker(0, "TIMED", None, None), tracker(0, "TIMED", None, None))
ck("no spurious webhook posts before anything is live", DISC == [], DISC)
ck("no spurious pushes before anything is live", PUSH == [], PUSH)

print("\n== leaderboard pings stay quiet during live-only play (no settled result yet) ==")
# leader changes in the live (matches_played==0) snapshot -> must NOT fire 'New leader' (avoids live-shuffle spam)
old = tracker(0, "IN_PLAY", 0, 0, hybrid=[{"name": "Erol"}, {"name": "James"}])
new = tracker(0, "IN_PLAY", 1, 0, hybrid=[{"name": "James"}, {"name": "Erol"}])   # live shuffle
transition(old, new)
ck("no 'New leader' ping from a live-only shuffle", not any("New leader" in x for x in DISC), DISC)
# but once a result is settled (matches_played>0), a genuine leader change DOES fire
old = tracker(1, "FINISHED", 1, 0, hybrid=[{"name": "Erol"}, {"name": "James"}])
new = tracker(2, "FINISHED", 1, 0, hybrid=[{"name": "James"}, {"name": "Erol"}])
transition(old, new)
ck("a settled leader change DOES post 'New leader'", any("New leader" in x for x in DISC), DISC)

print("\n== daily digest: live standings instead of 'hasn't kicked off', and no mirrored/duplicate fixtures ==")
import time as _t
_today = _t.strftime("%Y-%m-%d", _t.gmtime())
def _digest_tracker(matches_played, fixtures):
    return {"stats": {"matches_played": matches_played, "teams_remaining": 30, "goals": 3, "goals_per_match": 1.5},
            "leaderboards": {"hybrid": [{"name": "James", "score": 6}, {"name": "Erol", "score": 4}],
                             "points": [{"name": "James", "score": 6}], "survival": [{"name": "James", "score": 1}]},
            "players": [{"name": "Erol", "teams": []}, {"name": "James", "teams": []}],
            "fixtures": fixtures}

# Bug A: nothing finished yet but a game is live -> must NOT say 'hasn't kicked off'
live_fx = [{"utcDate": _today + "T19:00:00Z", "status": "IN_PLAY", "home": "Mexico", "away": "South Africa",
            "homeOwner": "James", "awayOwner": "James", "homeScore": 1, "awayScore": 0, "stage": "GROUP_STAGE"}]
json.dump(_digest_tracker(0, live_fx), open(os.path.join(t, "tracker_data.json"), "w"))
_summary = "\n".join(S.build_summary())
ck("digest doesn't claim 'hasn't kicked off' while a game is live", "hasn't kicked off" not in _summary, _summary[:80])
ck("digest shows a live-standings line instead", "Games in progress" in _summary or "James" in _summary, _summary[:80])

# Bug A inverse: genuinely pre-tournament (nothing live, nothing finished) -> DOES say 'hasn't kicked off'
pre_fx = [{"utcDate": _today + "T19:00:00Z", "status": "TIMED", "home": "Mexico", "away": "South Africa",
           "homeOwner": "James", "awayOwner": "James", "homeScore": None, "awayScore": None, "stage": "GROUP_STAGE"}]
json.dump(_digest_tracker(0, pre_fx), open(os.path.join(t, "tracker_data.json"), "w"))
ck("digest still says 'hasn't kicked off' before anything starts", "hasn't kicked off" in "\n".join(S.build_summary()))

# Bug B: a player owning BOTH teams sees the fixture once, not mirrored twice
json.dump(_digest_tracker(0, live_fx), open(os.path.join(t, "tracker_data.json"), "w"))
_day = S._day_by_player(S._load_tracker())
ck("owner of both teams gets the fixture exactly once", len(_day.get("James", [])) == 1, _day.get("James"))
ck("the single entry isn't mirrored", _day.get("James", [""])[0].startswith("Mexico vs South Africa"), _day.get("James"))

# Bug B: two different owners each get their own perspective (once each)
two_fx = [{"utcDate": _today + "T19:00:00Z", "status": "TIMED", "home": "Mexico", "away": "Brazil",
           "homeOwner": "James", "awayOwner": "Erol", "homeScore": None, "awayScore": None, "stage": "GROUP_STAGE"}]
json.dump(_digest_tracker(0, two_fx), open(os.path.join(t, "tracker_data.json"), "w"))
_day2 = S._day_by_player(S._load_tracker())
ck("each distinct owner gets one perspective", len(_day2.get("James", [])) == 1 and len(_day2.get("Erol", [])) == 1, _day2)
ck("James sees his team first", _day2.get("James", [""])[0].startswith("Mexico vs Brazil"), _day2.get("James"))
ck("Erol sees his team first", _day2.get("Erol", [""])[0].startswith("Brazil vs Mexico"), _day2.get("Erol"))

print("\n== /notifyme routing: personal vs all-games, welcome DM, stopnotify clears both ==")
DMRAW = []
S._bot_dm = lambda uid, text: (DMRAW.append((str(uid), text)) or (True, None))
json.dump({"players": [{"name": "Erol", "teams": []}, {"name": "James", "teams": []}],
           "stats": {}, "leaderboards": {}, "fixtures": []},
          open(os.path.join(t, "tracker_data.json"), "w"))
S.discord_command("notifyme", {"player": "Erol"}, uid="111")
_cfg = S.load_config()
ck("/notifyme <player> stores a personal sub", _cfg.get("discord_subs", {}).get("111") == "Erol", _cfg.get("discord_subs"))
ck("/notifyme <player> sends a welcome DM", any(u == "111" for u, _m in DMRAW), DMRAW)
DMRAW.clear()
S.discord_command("notifyme", {"player": "all"}, uid="222")
_cfg = S.load_config()
ck("/notifyme all opts into the all-games list", "222" in [str(x) for x in _cfg.get("discord_dm_all", [])], _cfg.get("discord_dm_all"))
ck("/notifyme all sends a welcome DM", any(u == "222" for u, _m in DMRAW), DMRAW)
S.discord_command("notifyme", {"player": "all"}, uid="111")        # 111 now has personal + all-games
S.discord_command("stopnotify", {}, uid="111")
_cfg = S.load_config()
ck("/stopnotify clears the personal sub", "111" not in _cfg.get("discord_subs", {}), _cfg.get("discord_subs"))
ck("/stopnotify clears the all-games opt-in", "111" not in [str(x) for x in _cfg.get("discord_dm_all", [])], _cfg.get("discord_dm_all"))

print("\n== admin kill switch: game_channel_alerts=false silences the channel feed but DMs still fire ==")
def _set_flag(v):
    c = S.load_config(); c["game_channel_alerts"] = v; S.save_config(c)
_set_flag(False)
transition(tracker(0, "TIMED", None, None), tracker(0, "IN_PLAY", 0, 0))   # a kickoff
ck("kickoff does NOT post to the channel when the feed is off", not any("Kicked off" in x for x in DISC), DISC)
ck("...but owners are still DM'd personally", any("Spain" in d[1] or "France" in d[1] for d in DM), DM)
_set_flag(True)
transition(tracker(0, "TIMED", None, None), tracker(0, "IN_PLAY", 0, 0))
ck("kickoff DOES post to the channel when the feed is on", any("Kicked off" in x for x in DISC), DISC)
# a goal with the feed off: no channel post, owner still DM'd
_set_flag(False)
transition(tracker(1, "IN_PLAY", 0, 0), tracker(1, "IN_PLAY", 1, 0))
ck("a goal does NOT post to the channel when the feed is off", not any("scored" in x for x in DISC), DISC)
ck("...but the scorer's owner is still DM'd", any("scored" in d[1] for d in DM), DM)
_set_flag(True)

import shutil
shutil.rmtree(t, ignore_errors=True)
if FAILS:
    print("\nNOTIFY QA FAILED (%d):" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nAll notification QA passed.")
