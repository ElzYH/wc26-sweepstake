#!/usr/bin/env python3
"""Disallowed-goal (VAR) alert QA — a live score that ROSE then drops back must notify everyone exactly
once. Locks in: the alert fires on a live reversion (owner push + DM, channel line, all-games feed);
a feed flap replaying the same drop is silent (persistent at-most-once guard, which also covers a
service restart re-comparing snapshots); a LATER distinct chalk-off still gets its own single alert;
a revert landing on the full-time tick itself fires; a correction to an already-FINISHED game stays
silent; a 1-0 -> 0-1 swap fires the away goal AND the home disallowed together; and plain goal /
kickoff alerts are untouched."""
import os, sys, json, tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
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

def tracker(mp, status, hs, as_):
    return {"stats": {"matches_played": mp},
            "leaderboards": {"points": [{"name": "Erol"}, {"name": "James"}], "survival": [{"name": "Erol"}]},
            "players": [{"name": "Erol"}, {"name": "James"}],
            "fixtures": [{"home": "Spain", "away": "France", "status": status,
                          "homeOwner": "Erol", "awayOwner": "James",
                          "homeScore": hs, "awayScore": as_, "stage": "GROUP_STAGE", "group": "A"}]}

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

print()
if FAILS:
    print("FAILED: %d -> %s" % (len(FAILS), FAILS))
    raise SystemExit(1)
print("ALL DISALLOWED-GOAL ALERT CHECKS PASSED")
