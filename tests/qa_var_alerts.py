#!/usr/bin/env python3
"""Disallowed-goal (VAR) alert QA — a live score that ROSE then drops back must notify everyone exactly
once. Locks in: the alert fires on a live reversion (owner push + DM, channel line, all-games feed);
a feed flap replaying the same drop is silent (persistent at-most-once guard, which also covers a
service restart re-comparing snapshots); a LATER distinct chalk-off still gets its own single alert;
a revert landing on the full-time tick itself fires; a correction to an already-FINISHED game stays
silent; a 1-0 -> 0-1 swap fires the away goal AND the home disallowed together; and plain goal /
kickoff alerts are untouched."""
import os, sys, json, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        FAILS.append(name)

t = tempfile.mkdtemp(prefix="wc26_var_")
os.environ["WC26_DATA"] = t
os.environ["WC26_CONFIG"] = os.path.join(t, "config.json")
json.dump({"configured": True, "discord_webhook": "https://example/webhook", "site_url": "https://example"},
          open(os.environ["WC26_CONFIG"], "w"))
import server as S

PUSH, DM, CHAN, DMALL = [], [], [], []
S.discord_send = lambda text: None
S.push_player = lambda player, etype, title, body: PUSH.append((player, etype, title, body))
S._bot_dm_player = lambda player, text, match_id=None: (DM.append((player, text)) or 1)
S._channel_event = lambda text, mid=None: CHAN.append(text)
S._dm_all_games = lambda text, exclude_players=None: DMALL.append((text, list(exclude_players or [])))
S.discord_mention = lambda who, msg: None

def clear_caps():
    PUSH.clear(); DM.clear(); CHAN.clear(); DMALL.clear()

def clear_guard():
    try:
        os.remove(os.path.join(t, "alerts_sent.json"))
    except OSError:
        pass

def tracker(mp, status, hs, as_, redH=0, redA=0, lineups=None, scorers=None):
    fx = {"home": "Spain", "away": "France", "status": status,
          "homeOwner": "Erol", "awayOwner": "James", "redHome": redH, "redAway": redA,
          "homeScore": hs, "awayScore": as_, "stage": "GROUP_STAGE", "group": "A"}
    if lineups:
        fx["homeLineup"] = [{"name": "Keeper", "position": "Goalkeeper", "shirtNumber": 1}]
        fx["awayLineup"] = [{"name": "Gardien", "position": "Goalkeeper", "shirtNumber": 1}]
    if scorers:
        fx["scorers"] = scorers
    return {"stats": {"matches_played": mp},
            "leaderboards": {"points": [{"name": "Erol"}, {"name": "James"}], "survival": [{"name": "Erol"}]},
            "players": [{"name": "Erol"}, {"name": "James"}],
            "fixtures": [fx]}

def transition(old_td, new_td):
    json.dump(old_td, open(os.path.join(t, "tracker_data.json"), "w"))
    old = S._load_tracker()
    json.dump(new_td, open(os.path.join(t, "tracker_data.json"), "w"))
    clear_caps()
    S.notify_changes(old)

def var_chan():
    return [c for c in CHAN if "disallowed" in c.lower()]

print("== a live 1-0 -> 0-0 reversion fires the full fan-out, once ==")
clear_guard()
transition(tracker(0, "IN_PLAY", 1, 0), tracker(0, "IN_PLAY", 0, 0))
ck("channel got exactly one disallowed line", len(var_chan()) == 1, CHAN)
ck("the line names the chalked team + the corrected score", var_chan() and "Spain" in var_chan()[0] and "0–0" in var_chan()[0], var_chan())
ck("the owner of the chalked goal got a push (goal class)", any(p == ("Erol",) + p[1:] and p[1] == "goal" and "disallowed" in p[2].lower() for p in PUSH), PUSH)
ck("the owner got a personal DM", any(pl == "Erol" and "disallowed" in tx.lower() for pl, tx in DM), DM)
ck("the all-games feed got it, owner excluded", any("disallowed" in tx.lower() and "Erol" in ex for tx, ex in DMALL), DMALL)
ck("no goal-scored alert fired for the drop", not any("scored" in c for c in CHAN), CHAN)

print("\n== the same drop replayed (feed flap / restart re-compare) is silent ==")
transition(tracker(0, "IN_PLAY", 1, 0), tracker(0, "IN_PLAY", 0, 0))   # guard file NOT cleared
ck("no second alert for the identical reversion", len(var_chan()) == 0, CHAN)

print("\n== a LATER, different chalk-off still gets its own single alert ==")
transition(tracker(0, "IN_PLAY", 2, 1), tracker(0, "IN_PLAY", 1, 1))
ck("distinct reversion (2-1 -> 1-1) fires once", len(var_chan()) == 1, CHAN)
transition(tracker(0, "IN_PLAY", 2, 1), tracker(0, "IN_PLAY", 1, 1))
ck("...and never again", len(var_chan()) == 0, CHAN)

print("\n== a revert landing on the full-time tick itself still fires ==")
clear_guard()
transition(tracker(0, "IN_PLAY", 1, 0), tracker(1, "FINISHED", 0, 0))
ck("IN_PLAY 1-0 -> FINISHED 0-0 fires the disallowed alert", len(var_chan()) == 1, CHAN)

print("\n== a correction to an ALREADY-finished game is results territory: silent ==")
clear_guard()
transition(tracker(1, "FINISHED", 1, 0), tracker(1, "FINISHED", 0, 0))
ck("FINISHED -> FINISHED score change fires nothing", len(var_chan()) == 0, CHAN)

print("\n== a 1-0 -> 0-1 live swap fires the away goal AND the home disallowed ==")
clear_guard()
transition(tracker(0, "IN_PLAY", 1, 0), tracker(0, "IN_PLAY", 0, 1))
ck("France's goal alert fired", any("France" in c and "scored" in c for c in CHAN), CHAN)
ck("Spain's disallowed alert fired", len(var_chan()) == 1 and "Spain" in var_chan()[0], CHAN)

print("\n== plain goal + kickoff alerts are untouched (regression) ==")
clear_guard()
transition(tracker(0, "TIMED", None, None), tracker(0, "IN_PLAY", 0, 0))
ck("kickoff still fires, no disallowed noise", any("Kicked off" in c for c in CHAN) and not var_chan(), CHAN)
transition(tracker(0, "IN_PLAY", 0, 0), tracker(0, "IN_PLAY", 1, 0))
ck("a normal goal still fires, no disallowed noise", any("scored" in c for c in CHAN) and not var_chan(), CHAN)
transition(tracker(0, "IN_PLAY", None, None), tracker(0, "IN_PLAY", 0, 0))
ck("None -> 0-0 is not a reversion", not var_chan(), CHAN)


print("\n== red cards (deep-data feed): a rising 90' red count alerts everyone once ==")
clear_guard()
def red_chan():   return [c for c in CHAN if "Red card" in c or "🟥" in c]
def red_pushes(): return [p for p in PUSH if "Red card" in (p[2] or "")]
transition(tracker(0, "IN_PLAY", 0, 0, redH=0), tracker(0, "IN_PLAY", 0, 0, redH=1))
ck("the sent-off team's owner gets the push", any(p[0] == "Erol" for p in red_pushes()), PUSH)
ck("the channel gets the red-card line naming team + owner", len(red_chan()) == 1 and "Spain" in red_chan()[0] and "Erol" in red_chan()[0], CHAN)
ck("the all-games feed carries it, excluding the owner", any("🟥" in tx and "Erol" in ex for tx, ex in DMALL), DMALL)
transition(tracker(0, "IN_PLAY", 0, 0, redH=1), tracker(0, "IN_PLAY", 0, 0, redH=1))
ck("a feed flap on the same count is silent", not red_chan() and not red_pushes(), CHAN)
transition(tracker(0, "IN_PLAY", 0, 0, redH=1), tracker(0, "IN_PLAY", 0, 0, redH=1, redA=1))
ck("the OTHER team's later red fires its own alert (James)", any(p[0] == "James" for p in red_pushes()) and "France" in (red_chan() or [""])[0], (PUSH, CHAN))
transition(tracker(1, "FINISHED", 1, 0, redH=1), tracker(1, "FINISHED", 1, 0, redH=2))
ck("a post-FT bookings correction stays silent (not live drama)", not red_chan() and not red_pushes(), CHAN)
transition(tracker(0, "IN_PLAY", 0, 0, redH=1), tracker(0, "IN_PLAY", 1, 0, redH=1))
ck("a goal with an unchanged red count fires no red alert (regression)", not red_chan(), CHAN)


print("\n== line-ups released: owners pushed + channel line, once; post-FT backfill silent ==")
clear_guard()
def lu_chan(): return [c for c in CHAN if "Line-ups" in c]
transition(tracker(1, "TIMED", None, None), tracker(1, "TIMED", None, None, lineups=True))   # mp=1: past the pre-tournament silence gate
ck("both owners get the line-ups push", {p[0] for p in PUSH if "Line-ups" in (p[2] or "")} == {"Erol", "James"}, PUSH)
ck("the channel announces the XIs once", len(lu_chan()) == 1 and "Spain" in lu_chan()[0], CHAN)
transition(tracker(1, "TIMED", None, None, lineups=True), tracker(1, "TIMED", None, None, lineups=True))
ck("a re-poll with the same XIs is silent", not lu_chan() and not any("Line-ups" in (p[2] or "") for p in PUSH), CHAN)
transition(tracker(1, "FINISHED", 1, 0), tracker(1, "FINISHED", 1, 0, lineups=True))
ck("a post-FT line-up backfill stays silent (history, not news)", not lu_chan(), CHAN)

print("\n== goal alerts name the scorer when the feed carries one ==")
clear_guard()
transition(tracker(0, "IN_PLAY", 0, 0), tracker(0, "IN_PLAY", 1, 0,
           scorers=[{"minute": 23, "team": "HOME", "player": "El Nueve"}]))
ck("the channel goal line names the scorer", any("El Nueve" in c and "scored" in c for c in CHAN), CHAN)
clear_guard()
transition(tracker(0, "IN_PLAY", 0, 0), tracker(0, "IN_PLAY", 1, 0))
ck("no scorers in the feed -> the plain goal line still fires (free tier)", any("Spain" in c and "scored" in c for c in CHAN), CHAN)

print()
if FAILS:
    print("FAILED: %d -> %s" % (len(FAILS), FAILS))
    raise SystemExit(1)
print("ALL DISALLOWED-GOAL ALERT CHECKS PASSED")
