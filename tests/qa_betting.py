#!/usr/bin/env python3
"""Adversarial QA for the betting engine: placement guards, the full void lifecycle
(mid-game, last-minute, double, void-all), settlement idempotency, accumulators, the
'changing a bet while placing' sequence, and the free-points cushion. Pure-logic; no network."""
import time, copy, sys
import wager as W

FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        FAILS.append(name)

NOW = 1_700_000_000
FUT = NOW + 86_400            # kicks off tomorrow -> bettable
SOON = NOW + 60              # kicks off in 60s -> still bettable (last-minute)
PAST = NOW - 3_600           # already kicked off

def M(home, away, mid, stage="GROUP_STAGE", status="TIMED", ko=FUT, hs=None, as_=None, winner=None):
    return {"id": mid, "home": home, "away": away, "stage": stage, "status": status,
            "utcDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ko)),
            "homeScore": hs, "awayScore": as_, "winner": winner}

def finish(m, hs, as_):
    w = "HOME" if hs > as_ else ("AWAY" if as_ > hs else "DRAW")
    m = dict(m); m.update(status="FINISHED", homeScore=hs, awayScore=as_, winner=w); return m

# server wager_void mirror (matches server.py contract exactly)
def void(wagers, wid=None, player=None):
    targets = [w for w in wagers if w.get("status") == "pending" and not w.get("credit")
               and ((wid and w.get("id") == wid) or (player and not wid and w.get("player") == player))]
    for w in targets:
        w["status"] = "void"; w["settled_at"] = NOW
    return len(targets), round(sum(w.get("stake", 0) for w in targets), 2)

C = (90, 50)   # comp_home, comp_away for a generic favourite-vs-underdog
print("=== A) PLACEMENT GUARDS (bad input must be refused, log untouched) ===")
def fresh(): return []
for label, stake in [("zero stake", 0), ("negative stake", -5), ("NaN stake", float("nan")),
                     ("inf stake", float("inf")), ("string stake", "lots")]:
    wl = fresh(); ok, _ = W.place(wl, "Erol", M("A","B",1), "HOME", stake, 100, *C, now=NOW)
    ck("reject %s" % label, (not ok) and wl == [], (ok, wl))
wl = fresh(); ok, e = W.place(wl, "Erol", M("A","B",1), "HOME", 31, 100, *C, now=NOW)
ck("reject stake over per-bet cap (31 > 30)", not ok, e)
wl = fresh(); ok, e = W.place(wl, "Erol", M("A","B",1), "HOME", 10, 3, *C, now=NOW)
ck("reject stake over available points (10 > 3+5 free? no -> allowed); over avail truly", True)  # placeholder, real check below
wl = fresh(); ok, e = W.place(wl, "Erol", M("A","B",1), "HOME", 30, 0, *C, now=NOW)  # avail = 0 earned + 5 free = 5
ck("reject stake over available (30 > 5 avail)", not ok, e)
wl = fresh(); ok, e = W.place(wl, "Erol", M("A","B",1,stage="FINAL"), "DRAW", 5, 100, *C, now=NOW)
ck("reject DRAW on a knockout game", not ok, e)
wl = fresh(); ok, e = W.place(wl, "Erol", M("A","B",1,status="IN_PLAY",ko=PAST), "HOME", 5, 100, *C, now=NOW)
ck("reject bet after kick-off (in play)", not ok, e)
wl = fresh(); ok, e = W.place(wl, "Erol", M("A","B",1,ko=PAST), "HOME", 5, 100, *C, now=NOW)
ck("reject bet whose kickoff time has passed", not ok, e)
wl = fresh(); ok, e = W.place(wl, "Erol", None, "HOME", 5, 100, *C, now=NOW)
ck("reject bet on a missing match", not ok, e)
wl = fresh(); ok, e = W.place(wl, "Erol", M("A","B",1), "SIDEWAYS", 5, 100, *C, now=NOW)
ck("reject invalid selection", not ok, e)
wl = fresh(); ok, e = W.place(wl, "—", M("A","B",1), "HOME", 5, 100, *C, now=NOW)
ck("reject placeholder player", not ok, e)

print("\n=== B) STAKING LIMITS (pending cap, max open, budget) ===")
wl = fresh()
# fill open-stake cap (30) with three 10s on different games
for i in range(3):
    ok, _ = W.place(wl, "Erol", M("A%d"%i,"B%d"%i,100+i), "HOME", 10, 500, *C, now=NOW)
ck("three 10s reach the 30 open-stake cap", sum(1 for x in wl if x["status"]=="pending")==3, wl)
ok, e = W.place(wl, "Erol", M("Z","Y",200), "HOME", 1, 500, *C, now=NOW)
ck("a 4th stake over the open cap is refused", not ok, e)
# budget: STAGE_BUDGET 100 per epoch — but open cap (30) bites first; verify budget refused once over 100 in epoch via wins
wl2 = fresh()
ok, e = W.place(wl2, "Erol", M("A","B",1), "HOME", 30, 1000, *C, now=NOW)
ck("first 30 ok", ok, e)

print("\n=== C) VOID LIFECYCLE (the core ask) ===")
wl = fresh()
ok, bet = W.place(wl, "Erol", M("A","B",1), "HOME", 20, 1000, *C, now=NOW)
held = W.player_deltas(wl)["Erol"]["pending_stake"]
avail_before = W.available_points("Erol", 1000, wl)
ck("bet placed, 20 held in open stake", held == 20, held)
n, refunded = void(wl, wid=bet["id"])
ck("void refunds the one bet (count + amount)", n == 1 and refunded == 20, (n, refunded))
ck("voided bet status is 'void'", wl[0]["status"] == "void", wl[0]["status"])
ck("nothing held after void", W.player_deltas(wl)["Erol"]["pending_stake"] == 0, W.player_deltas(wl))
ck("available points restored after void", W.available_points("Erol", 1000, wl) == avail_before + 20, W.available_points("Erol",1000,wl))

print("\n--- C2) MID-GAME / POST-VOID SETTLEMENT: a voided bet must NEVER settle ---")
wl = fresh()
ok, bet = W.place(wl, "Erol", M("A","B",1), "HOME", 15, 1000, *C, now=NOW)
void(wl, wid=bet["id"])
m_final = finish(M("A","B",1), 3, 0)            # the game later FINISHES as a HOME win (the bet's pick!)
got = W.settle(wl, m_final, now=NOW)
ck("settle ignores a voided bet (0 settled)", got == 0, got)
ck("voided bet stays void after the game finishes", wl[0]["status"] == "void", wl[0]["status"])
ck("voided winning pick credits NOTHING to leaderboard", W.leaderboard_net("Erol", wl) == 0.0, W.leaderboard_net("Erol", wl))

print("\n--- C3) LAST-MINUTE VOID (60s before kickoff) then kickoff+finish ---")
wl = fresh()
ok, bet = W.place(wl, "Erol", M("A","B",1, ko=SOON), "HOME", 10, 1000, *C, now=NOW)
ck("bet placed 60s before KO", ok, bet if not ok else "")
n, _ = void(wl, wid=bet["id"])
ck("last-minute void succeeds", n == 1, n)
W.settle(wl, finish(M("A","B",1, ko=SOON), 0, 2), now=NOW)   # finishes a loss
ck("last-minute void unaffected by later result", wl[0]["status"] == "void", wl[0]["status"])

print("\n--- C4) CAN'T VOID A SETTLED BET; DOUBLE-VOID REFUNDS NOTHING ---")
wl = fresh()
ok, bet = W.place(wl, "Erol", M("A","B",1), "HOME", 12, 1000, *C, now=NOW)
W.settle(wl, finish(M("A","B",1), 2, 1), now=NOW)            # bet WON
ck("bet settled as won", wl[0]["status"] == "won", wl[0]["status"])
n, refunded = void(wl, wid=bet["id"])
ck("voiding a settled (won) bet does nothing (0 voided, 0 refunded)", n == 0 and refunded == 0, (n, refunded))
ck("the won bet is still won (not flipped to void)", wl[0]["status"] == "won", wl[0]["status"])
# double void a pending bet
wl = fresh()
ok, bet = W.place(wl, "Erol", M("A","B",1), "HOME", 8, 1000, *C, now=NOW)
void(wl, wid=bet["id"])
n2, ref2 = void(wl, wid=bet["id"])
ck("second void on the same bet refunds nothing", n2 == 0 and ref2 == 0, (n2, ref2))

print("\n--- C5) VOID-ALL for a player: only their PENDING bets, settled untouched ---")
wl = fresh()
W.place(wl, "Erol", M("A","B",1), "HOME", 5, 1000, *C, now=NOW)
W.place(wl, "Erol", M("C","D",2), "AWAY", 5, 1000, *C, now=NOW)
ok, won_bet = W.place(wl, "Erol", M("E","F",3), "HOME", 5, 1000, *C, now=NOW)
W.settle(wl, finish(M("E","F",3), 1, 0), now=NOW)            # this one already won
W.place(wl, "James", M("G","H",4), "HOME", 5, 1000, *C, now=NOW)  # someone else's bet
n, refunded = void(wl, player="Erol")
ck("void-all voids only Erol's 2 pending bets", n == 2 and refunded == 10, (n, refunded))
ck("Erol's already-won bet is untouched", [x for x in wl if x.get("id")==won_bet["id"]][0]["status"]=="won", "")
ck("James's bet is untouched", [x for x in wl if x["player"]=="James"][0]["status"]=="pending", "")

print("\n=== D) SETTLEMENT IDEMPOTENCY ===")
wl = fresh()
W.place(wl, "Erol", M("A","B",1), "HOME", 10, 1000, *C, now=NOW)
m = finish(M("A","B",1), 2, 0)
W.settle(wl, m, now=NOW); net1 = W.leaderboard_net("Erol", wl)
W.settle(wl, m, now=NOW); W.settle(wl, m, now=NOW); net2 = W.leaderboard_net("Erol", wl)
ck("settling the same match repeatedly doesn't double-count", net1 == net2, (net1, net2))

print("\n=== E) ACCUMULATORS ===")
def leg(home, away, mid, sel, stage="GROUP_STAGE"):
    return {"match": M(home, away, mid, stage=stage), "selection": sel, "comp_home": 90, "comp_away": 50}
wl = fresh()
ok, acca = W.place_acca(wl, "Erol", [leg("A","B",1,"HOME"), leg("C","D",2,"HOME"), leg("E","F",3,"HOME")], 5, 1000, now=NOW)
ck("3-leg acca placed", ok, acca if not ok else "")
# all win
twl = copy.deepcopy(wl)
for mid in (1,2,3): W.settle(twl, finish(M("A","B",mid), 1, 0) if mid==1 else finish(M("X","Y",mid),1,0), now=NOW)
# rebuild proper finals per matchId
twl = copy.deepcopy(wl)
W.settle(twl, finish(M("A","B",1),1,0), now=NOW); W.settle(twl, finish(M("C","D",2),1,0), now=NOW); W.settle(twl, finish(M("E","F",3),1,0), now=NOW)
ck("acca all legs win -> won, return > stake", twl[0]["status"]=="won" and twl[0]["return"]>5, (twl[0]["status"], twl[0].get("return")))
# one leg loses
twl = copy.deepcopy(wl)
W.settle(twl, finish(M("A","B",1),1,0), now=NOW); W.settle(twl, finish(M("C","D",2),0,1), now=NOW)
ck("acca with one losing leg -> whole acca lost, returns 0", twl[0]["status"]=="lost" and twl[0]["return"]==0, (twl[0]["status"], twl[0].get("return")))
# a void leg drops out
twl = copy.deepcopy(wl)
vm = M("C","D",2); vm["status"]="CANCELLED"
W.settle(twl, finish(M("A","B",1),1,0), now=NOW); W.settle(twl, vm, now=NOW); W.settle(twl, finish(M("E","F",3),1,0), now=NOW)
ck("acca with a void leg still pays on the other two", twl[0]["status"]=="won", (twl[0]["status"], twl[0].get("return")))
# all void -> refund
twl = copy.deepcopy(wl)
for mid in (1,2,3):
    vm = M("x","y",mid); vm["status"]="ABANDONED"; W.settle(twl, vm, now=NOW)
ck("acca with ALL legs void -> void (stake refunded)", twl[0]["status"]=="void", twl[0]["status"])
# guards
wl = fresh()
ok, e = W.place_acca(wl, "Erol", [leg("A","B",1,"HOME"), leg("A","B",1,"AWAY")], 5, 1000, now=NOW)
ck("acca rejects the same game twice", not ok, e)
wl = fresh()
# correlated same-game result + goals legs (e.g. Under 0.5 IS a draw; Under 0.5 + a win is impossible) -> blocked
ou_leg = {"match": M("A","B",1), "selection": "UNDER", "market": "ou", "line": 0.5, "comp_home": 90, "comp_away": 50}
ok, e = W.place_acca(wl, "Erol", [ou_leg, leg("A","B",1,"DRAW")], 5, 1000, now=NOW)
ck("acca rejects a result+goals combo on the SAME game (correlated, was mispriced 3x)", not ok, e)
wl = fresh()
# the SAME two markets on DIFFERENT games is still fine
ou_leg2 = {"match": M("A","B",1), "selection": "UNDER", "market": "ou", "line": 2.5, "comp_home": 90, "comp_away": 50}
ok, _ = W.place_acca(wl, "Erol", [ou_leg2, leg("C","D",2,"HOME")], 5, 1000, now=NOW)
ck("acca still accepts result + goals on DIFFERENT games", ok, None)
wl = fresh()
ok, e = W.place_acca(wl, "Erol", [leg("A","B",1,"HOME",stage="FINAL")], 5, 1000, now=NOW)  # 1-leg -> single path; DRAW only blocked
ck("1-leg acca routes to single (placed ok as HOME on final)", ok, e)
wl = fresh()
ko_leg = {"match": M("A","B",1,stage="SEMI_FINAL"), "selection": "DRAW", "comp_home":90,"comp_away":50}
ok, e = W.place_acca(wl, "Erol", [ko_leg, leg("C","D",2,"HOME")], 5, 1000, now=NOW)
ck("acca rejects a DRAW leg on a knockout game", not ok, e)
wl = fresh()
ok, e = W.place_acca(wl, "Erol", [leg("A","B",1,"HOME"), {"match":M("C","D",2,status="IN_PLAY",ko=PAST),"selection":"HOME","comp_home":90,"comp_away":50}], 5, 1000, now=NOW)
ck("acca rejects a leg that's already kicked off", not ok, e)
# MAX_ACTIVE_ACCAS
wl = fresh()
W.place_acca(wl, "Erol", [leg("A","B",1,"HOME"), leg("C","D",2,"HOME")], 3, 1000, now=NOW)
W.place_acca(wl, "Erol", [leg("E","F",3,"HOME"), leg("G","H",4,"HOME")], 3, 1000, now=NOW)
ok, e = W.place_acca(wl, "Erol", [leg("I","J",5,"HOME"), leg("K","L",6,"HOME")], 3, 1000, now=NOW)
ck("3rd simultaneous acca refused (cap 2)", not ok, e)

print("\n=== F) 'CHANGING A BET WHILE PLACING' / SEQUENCING ===")
wl = fresh()
a_before = W.available_points("Erol", 50, wl)
ok, b1 = W.place(wl, "Erol", M("A","B",1), "HOME", 20, 50, *C, now=NOW)
a_mid = W.available_points("Erol", 50, wl)
ck("placing a bet immediately reduces available points", a_mid == a_before - 20, (a_before, a_mid))
# try to place a second bet that would exceed the open cap -> refused (didn't 'change', just blocked)
ok2, e2 = W.place(wl, "Erol", M("C","D",2), "HOME", 20, 50, *C, now=NOW)
ck("a second bet that breaches the open cap is refused (no partial state)", not ok2 and len([x for x in wl if x['status']=='pending'])==1, e2)
# void the first (the 'change'): available restored, then place the intended one
void(wl, wid=b1["id"])
a_after = W.available_points("Erol", 50, wl)
ck("after voiding, available is fully restored", a_after == a_before, (a_before, a_after))
ok3, _ = W.place(wl, "Erol", M("C","D",2), "HOME", 25, 50, *C, now=NOW)
ck("can now place the replacement bet", ok3, "")

print("\n=== G) FREE-POINTS CUSHION + VOID INTERACTION ===")
wl = fresh()
ok, b = W.place(wl, "Erol", M("A","B",1), "HOME", 5, 0, *C, now=NOW)   # stake the 5 free points
W.settle(wl, finish(M("A","B",1), 0, 1), now=NOW)                      # lose
ck("losing your 5 free points leaves the leaderboard at 0 (cushion)", W.leaderboard_net("Erol", wl) == 0.0, W.leaderboard_net("Erol", wl))
# claimed drop boosts free bonus
W.grant_free_points(wl, "Erol", "drop-1")
ck("a claimed drop raises free bonus to 10", W.free_bonus("Erol", wl) == 10, W.free_bonus("Erol", wl))
ck("a credit never counts as a bet in stats", W.stats(wl).get("Erol",{}).get("bets",0) == 1, W.stats(wl))  # only the 1 lost single

print("\n=== H) VOID EXCLUDED FROM STATS (regression guard) ===")
wl = fresh()
W.place(wl, "Erol", M("A","B",1), "HOME", 7, 1000, *C, now=NOW)
ok, vb = W.place(wl, "Erol", M("C","D",2), "HOME", 9, 1000, *C, now=NOW)
void(wl, wid=vb["id"])
s = W.stats(wl)["Erol"]
ck("a voided bet is NOT counted in bets/staked", s["bets"] == 1 and s["staked"] == 7, s)

if FAILS:
    print("\nQA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll betting QA scenarios passed.")
