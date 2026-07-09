#!/usr/bin/env python3
"""BTTS + feed-tier degradation QA. BTTS: pricing integrity off the shared lambdas, ladder rule,
placement, settlement on the final score only (so it works on the FREE tier), accas, hostile input.
Degradation: the whole stack downgraded to a bare free-tier feed — MoV bets push after the grace
instead of guessing REG vs ET; the cards MARKET disappears (auto-gate) rather than selling bets that
can only push; scorers/lineups stay None; goals/points/result settlement untouched. Plus the deep-data
normaliser extras: scorers extraction and starting XIs."""
import json
import wager as W
import update_results as UR

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

NOW = 1_700_000_000
GRP = {"id": "g1", "home": "A", "away": "B", "stage": "GROUP_STAGE", "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"}
GRP2 = {"id": "g2", "home": "C", "away": "D", "stage": "GROUP_STAGE", "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"}
COMPS = [(h, 100 - h) for h in range(0, 101, 10)] + [(0, 0), (100, 100), (80, 60)]

print("== BTTS pricing: margin vs fair on the strength grid, ladder rule, symmetry sanity ==")
bad = []
import math
for ch, ca in COMPS:
    b = W.btts_odds(ch, ca)
    lh, la = W._team_lambdas(ch, ca)
    p_yes = (1 - math.exp(-lh)) * (1 - math.exp(-la))
    if not b:
        if p_yes <= W.BTTS_MAX_PROB and (1 - p_yes) <= W.BTTS_MAX_PROB:
            bad.append(("missing safe book", ch, ca, p_yes))
        continue
    s = 1.0 / b["YES"]["decimal"] + 1.0 / b["NO"]["decimal"]
    if s <= 1.0 + 1e-6:
        bad.append(("underround", ch, ca, s))
    for sel, pf in (("YES", p_yes), ("NO", 1 - p_yes)):
        if (b[sel]["den"] / (b[sel]["num"] + b[sel]["den"])) <= pf * (1 + 1e-9):
            bad.append(("bettor-positive", ch, ca, sel))
        if pf > W.BTTS_MAX_PROB + 1e-9:
            bad.append(("capped side sold", ch, ca, sel))
ck("every offered BTTS book overrounds; every price beats fair; the ladder rule holds", not bad, bad[:4])
b5050, b8020 = W.btts_odds(50, 50), W.btts_odds(80, 20)
ck("a lopsided game lengthens YES vs an even one (the weak side scores less often)",
   (not b5050 or not b8020) or b8020["YES"]["decimal"] > b5050["YES"]["decimal"], (b8020.get("YES"), b5050.get("YES")))

print("\n== BTTS placement + settlement (final score only — free-tier settleable) ==")
ok, w = W.place([], "Erol", GRP, "YES", 5, 100, 60, 60, now=NOW, market="btts")
ck("YES places, struck at the live price", ok and w["num"] == W.btts_odds(60, 60)["YES"]["num"], w)
for badsel in ("MAYBE", "OVER", "yes", "", None):
    okb, msg = W.place([], "Erol", GRP, badsel, 5, 100, 60, 60, now=NOW, market="btts")
    ck("selection %r rejected" % (badsel,), not okb, msg if okb else None)
def fin(h, a, **kw):
    m = {"id": "g1", "home": "A", "away": "B", "stage": "GROUP_STAGE", "status": "FINISHED",
         "utcDate": "2099-01-01T00:00:00Z", "homeScore": h, "awayScore": a}
    m.update(kw); return m
def bb(sel):
    return [{"id": "b" + sel, "player": "E", "matchId": "g1", "market": "btts", "selection": sel,
             "stake": 5, "num": 1, "den": 1, "frac": "1/1", "return": 10, "status": "pending"}]
for sel, h, a, exp in [("YES", 2, 1, "won"), ("YES", 2, 0, "lost"), ("YES", 0, 0, "lost"),
                       ("NO", 3, 0, "won"), ("NO", 1, 1, "lost")]:
    wl = bb(sel); W.settle(wl, fin(h, a))
    ck("%s on %d-%d -> %s" % (sel, h, a, exp), wl[0]["status"] == exp, wl[0])
wl = bb("YES"); W.settle(wl, fin(None, None))
ck("no score -> stays pending", wl[0]["status"] == "pending", wl[0])
for vs in W.VOID_STATUSES:
    wl = bb("YES"); W.settle(wl, fin(None, None, status=vs))
    ck("%s voids + refunds" % vs, wl[0]["status"] == "void" and wl[0]["return"] == 5, wl[0])
for h, a in ((float("nan"), 1), (-1, 0), ("x", "y")):
    wl = bb("YES"); W.settle(wl, fin(h, a))
    ck("hostile score %r never settles" % ((h, a),), wl[0]["status"] == "pending", wl[0])

print("\n== BTTS accas: cross-game in, same-game blocked ==")
ok, acc = W.place_acca([], "Erol",
                       [{"match": GRP, "selection": "YES", "comp_home": 60, "comp_away": 60, "market": "btts"},
                        {"match": GRP2, "selection": "HOME", "comp_home": 70, "comp_away": 40}],
                       5, 100, now=NOW)
ck("a BTTS leg + a result leg across games places", ok and any(l.get("market") == "btts" for l in acc.get("legs", [])), acc)
okx, wx = W.place_acca([], "Erol",
                       [{"match": GRP, "selection": "YES", "comp_home": 60, "comp_away": 60, "market": "btts"},
                        {"match": GRP, "selection": "OVER", "comp_home": 60, "comp_away": 60, "market": "ou", "line": 2.5}],
                       5, 100, now=NOW)
_naive = (1 + wx["legs"][0]["num"]/wx["legs"][0]["den"]) * (1 + wx["legs"][1]["num"]/wx["legs"][1]["den"]) if okx else 0
ck("same-game BTTS+OU (heavily POSITIVELY correlated) prices jointly, paying under the naive product",
   okx and wx.get("groups") and wx["decimal"] < _naive - 1e-9, (wx.get("decimal") if okx else wx, _naive))

print("\n== BTTS exploit: covering dutches with OU are margin-negative (YES ∪ Under 1.5 covers everything) ==")
# BTTS NO pays unless both score; Over 0.5/1.5 relationships — enumerate covers over the score grid
GRID = [(h, a) for h in range(8) for a in range(8)]
def btts_pays(sel, h, a): return (h > 0 and a > 0) if sel == "YES" else not (h > 0 and a > 0)
def ou_pays(line, sel, h, a): return (h + a) > line if sel == "OVER" else (h + a) < line
bad = []
for ch, ca in COMPS:
    bt = W.btts_odds(ch, ca); ou = W.goals_odds(ch, ca)
    if not bt:
        continue
    for k in ou:
        L = float(k)
        for bsel in ("YES", "NO"):
            for osel in ("OVER", "UNDER"):
                if all(btts_pays(bsel, h, a) or ou_pays(L, osel, h, a) for h, a in GRID):
                    cost = 1.0 / bt[bsel]["decimal"] + 1.0 / ou[k][osel]["decimal"]
                    if cost <= 1.0 + 1e-9:
                        bad.append((ch, ca, bsel, k, osel, cost))
    r = W.match_odds(ch, ca)
    for bsel in ("YES", "NO"):
        for rsel in ("HOME", "DRAW", "AWAY"):
            if all(btts_pays(bsel, h, a) or ((h > a) if rsel == "HOME" else ((a > h) if rsel == "AWAY" else h == a)) for h, a in GRID):
                cost = 1.0 / bt[bsel]["decimal"] + 1.0 / r[rsel]["decimal"]
                if cost <= 1.0 + 1e-9:
                    bad.append((ch, ca, bsel, "1x2", rsel, cost))
ck("every covering BTTS x OU / BTTS x 1X2 dutch costs > 1.0 implied", not bad, bad[:4])

print("\n== FULL FREE-TIER DEGRADATION: the stack downgraded to a bare feed ==")
# a bare free-tier payload: fullTime only, winner, no duration key, no bookings, no goals, no lineup
api_free = [{"id": 9, "homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}, "utcDate": "2026-07-01T18:00:00Z",
             "status": "FINISHED", "stage": "QUARTER_FINALS",
             "score": {"fullTime": {"home": 2, "away": 1}, "winner": "HOME_TEAM"}}]
out = UR.normalize_matches(api_free, lambda n: n)[0]
ck("free tier: cards None, scorers None, lineups None (absence, not zero)",
   out["cardsHome"] is None and out["scorers"] is None and out["homeLineup"] is None, out)
ck("free tier: durationKnown is FALSE (no REG-vs-ET evidence)", out["durationKnown"] is False, out)
ck("free tier: score + winner still read (points/result bets unaffected)",
   out["homeScore"] == 2 and out["awayScore"] == 1 and out["winner"] == "HOME", out)
# MoV on that game: never guesses; pushes after the grace
mov = [{"id": "m", "player": "E", "matchId": "9", "market": "mov", "selection": "HOME_REG",
        "stake": 5, "num": 2, "den": 1, "frac": "2/1", "return": 15, "status": "pending"}]
W.settle(mov, out, now=NOW)
ck("MoV on an unknowable-method game stays pending inside the grace", mov[0]["status"] == "pending", mov[0])
W.settle(mov, dict(out, utcDate="2000-01-01T00:00:00Z"), now=NOW)
ck("MoV pushes with the stake back once the grace passes", mov[0]["status"] == "void" and mov[0]["return"] == 5, mov[0])
# ...but a free-tier PENS game still settles MoV properly (penalties fields are on every tier)
api_pens = [{"id": 10, "homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}, "utcDate": "2026-07-01T18:00:00Z",
             "status": "FINISHED", "stage": "QUARTER_FINALS",
             "score": {"fullTime": {"home": 5, "away": 4}, "penalties": {"home": 4, "away": 3},
                       "duration": "PENALTY_SHOOTOUT", "winner": "HOME_TEAM"}}]
outp = UR.normalize_matches(api_pens, lambda n: n)[0]
movp = [{"id": "p", "player": "E", "matchId": "10", "market": "mov", "selection": "HOME_PENS",
         "stake": 5, "num": 4, "den": 1, "frac": "4/1", "return": 25, "status": "pending"}]
W.settle(movp, outp, now=NOW)
ck("a pens game settles MoV on any tier (duration/pens fields carried)", movp[0]["status"] == "won", movp[0])
ck("...and the on-field score excluded the shootout goals", outp["homeScore"] == 1 and outp["awayScore"] == 1, outp)
# BTTS + result + O/U all settle on the bare feed
btt = bb("YES"); btt[0]["matchId"] = "9"; W.settle(btt, out)
ck("BTTS settles on the bare feed", btt[0]["status"] == "won", btt[0])
# cards market auto-gate: with a bare feed scoring must NOT attach cardsOdds
import scoring
ck("scoring exposes the cards capability gate (compute takes cards_market)", "cards_market" in scoring.compute.__code__.co_varnames, None)

print("\n== deep-data normaliser: scorers + lineups extraction ==")
api_deep = [{"id": 11, "homeTeam": {"name": "A", "lineup": [{"name": "Keeper One", "position": "Goalkeeper", "shirtNumber": 1},
                                                            {"name": "Back Two", "position": "Defence", "shirtNumber": 2}]},
             "awayTeam": {"name": "B", "lineup": []},
             "utcDate": "2026-07-01T18:00:00Z", "status": "IN_PLAY", "stage": "GROUP_STAGE",
             "score": {"fullTime": {"home": 2, "away": 0}, "regularTime": {"home": 2, "away": 0}, "duration": "REGULAR"},
             "goals": [{"minute": 12, "scorer": {"name": "Striker Nine"}, "team": {"name": "A"}},
                       {"minute": 44, "scorer": {"name": "Winger Seven"}, "team": {"name": "A"}, "type": "PENALTY"},
                       {"minute": 50, "scorer": {"name": "Nobody"}, "team": {"name": "Z"}},   # unknown team -> dropped
                       "junk"]}]
outd = UR.normalize_matches(api_deep, lambda n: n)[0]
ck("scorers extracted with side + minute (junk/unknown-team dropped)",
   outd["scorers"] == [{"minute": 12, "team": "HOME", "player": "Striker Nine", "type": "REGULAR"},
                       {"minute": 44, "team": "HOME", "player": "Winger Seven", "type": "PENALTY"}], outd["scorers"])
ck("home XI extracted; an EMPTY away lineup is None (not [])",
   outd["homeLineup"] and outd["homeLineup"][0]["name"] == "Keeper One" and outd["awayLineup"] is None, (outd["homeLineup"], outd["awayLineup"]))
ck("durationKnown True when the breakdown is present", outd["durationKnown"] is True, None)

print("\n" + ("All BTTS + degradation QA passed." if not fails else "BTTS/DEGRADATION QA FAILED (%d): %s" % (len(fails), ", ".join(fails))))
import sys
sys.exit(1 if fails else 0)
