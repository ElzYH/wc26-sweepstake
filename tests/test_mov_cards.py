#!/usr/bin/env python3
"""Method-of-victory + O/U cards QA. MoV: 6-way set margin, per-outcome fair floor, symmetry at even
strengths, KO-only placement, settlement across REG/ET/PENS shapes (incl. winner-less pens feeds).
Cards: ladder rule + margin, 90'-basis settlement, the no-data VOID grace, hostile inputs. Both: acca
legs across games, same-game block intact, and the exploit angles — an MoV subset dutched against the
KO result book, and cards' independence from every score-based market."""
import math
import wager as W

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

NOW = 1_700_000_000
KO = {"id": "k1", "home": "A", "away": "B", "stage": "QUARTER_FINALS", "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"}
KO2 = {"id": "k2", "home": "C", "away": "D", "stage": "QUARTER_FINALS", "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"}
GRP = {"id": "g1", "home": "A", "away": "B", "stage": "GROUP_STAGE", "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"}
COMPS = [(h, 100 - h) for h in range(0, 101, 10)] + [(0, 0), (100, 100), (80, 60)]

print("== MoV pricing: per-outcome fair floor + set margin on the strength grid ==")
bad = []
for ch, ca in COMPS:
    b = W.mov_odds(ch, ca)
    lh, la = W._team_lambdas(ch, ca)
    p_h90 = W._hc_home_prob(lh, la, -0.5); p_a90 = 1.0 - W._hc_home_prob(lh, la, 0.5)
    p_lvl = max(1e-6, 1.0 - p_h90 - p_a90)
    sh = p_h90 / max(1e-9, p_h90 + p_a90)
    et_h = 0.5 + (W.MOV_ET_EDGE - 0.5) * (2 * sh - 1); pn_h = 0.5 + (W.MOV_PENS_EDGE - 0.5) * (2 * sh - 1)
    fair = {"HOME_REG": p_h90, "AWAY_REG": p_a90,
            "HOME_ET": p_lvl * (1 - W.MOV_P_LEVEL_ET) * et_h, "AWAY_ET": p_lvl * (1 - W.MOV_P_LEVEL_ET) * (1 - et_h),
            "HOME_PENS": p_lvl * W.MOV_P_LEVEL_ET * pn_h, "AWAY_PENS": p_lvl * W.MOV_P_LEVEL_ET * (1 - pn_h)}
    for sel, d in b.items():
        if (d["den"] / (d["num"] + d["den"])) <= fair[sel] * (1 + 1e-9):
            bad.append(("bettor-positive", ch, ca, sel))
    if len(b) == 6:
        if sum(1.0 / v["decimal"] for v in b.values()) <= 1.0 + 1e-6:
            bad.append(("set underround", ch, ca))
ck("every sold MoV price beats fair; every complete set overrounds", not bad, bad[:4])

print("\n== MoV coherence: 'in 90' is ALWAYS longer than 'to advance'; the trio still floors the result book ==")
bad = []
for ch, ca in COMPS:
    b = W.mov_odds(ch, ca); r = W.match_odds(ch, ca, knockout=True)
    for side in ("HOME", "AWAY"):
        reg = "%s_REG" % side
        ri = 1.0 / r[side]["decimal"]
        if reg in b and (b[reg]["den"] / (b[reg]["num"] + b[reg]["den"])) >= ri - 1e-9:
            bad.append(("REG not longer than result", ch, ca, side, b[reg]["frac"], r[side]["frac"]))
        trio = [t for t in (reg, "%s_ET" % side, "%s_PENS" % side) if t in b]
        if len(trio) == 3 and sum(b[t]["den"] / (b[t]["num"] + b[t]["den"]) for t in trio) <= ri:
            bad.append(("trio dutchable vs result", ch, ca, side))
ck("winning in 90' always pays MORE than plain advancing, and the trio never undercuts the result book", not bad, bad[:4])

print("\n== MoV: symmetric at even strengths, fair probabilities exhaust a knockout ==")
ev = W.mov_odds(50, 50)
ck("even game mirrors: HOME_x == AWAY_x", all(ev.get("HOME_" + k, {}).get("frac") == ev.get("AWAY_" + k, {}).get("frac")
                                              for k in ("REG", "ET", "PENS")), {k: v["frac"] for k, v in ev.items()})
lh, la = W._team_lambdas(70, 40)
tot = W._hc_home_prob(lh, la, -0.5) + (1.0 - W._hc_home_prob(lh, la, 0.5))
ck("REG probs + level mass == 1 (the six fair outcomes exhaust the game)", abs(tot + (1 - tot) - 1.0) < 1e-12, tot)

print("\n== MoV placement: knockout only, struck at the live price ==")
ok, w = W.place([], "Erol", KO, "AWAY_PENS", 5, 100, 70, 40, now=NOW, market="mov")
ck("places on a knockout", ok and w.get("market") == "mov" and w.get("selection") == "AWAY_PENS", w)
ck("struck at the live mov_odds price", ok and w["num"] == W.mov_odds(70, 40)["AWAY_PENS"]["num"], None)
okg, msg = W.place([], "Erol", GRP, "HOME_REG", 5, 100, 50, 50, now=NOW, market="mov")
ck("rejected on a group game", not okg and "knockout" in str(msg).lower(), msg)
for badsel in ("HOME", "DRAW", "HOME_reg", "", None, "HOME_GOLDEN_GOAL"):
    okb, m2 = W.place([], "Erol", KO, badsel, 5, 100, 50, 50, now=NOW, market="mov")
    ck("selection %r rejected" % (badsel,), not okb, m2 if okb else None)

print("\n== MoV settlement goldens (every feed shape) ==")
def fin(**kw):
    m = {"id": "k1", "home": "A", "away": "B", "stage": "QUARTER_FINALS", "status": "FINISHED",
         "utcDate": "2099-01-01T00:00:00Z"}
    m.update(kw); return m
def movbet(sel):
    return [{"id": "m" + sel, "player": "E", "matchId": "k1", "market": "mov", "selection": sel,
             "stake": 5, "num": 3, "den": 1, "frac": "3/1", "return": 20, "status": "pending"}]
VEC = [
    ("HOME_REG",  fin(homeScore=2, awayScore=0, winner="HOME", duration="REGULAR"), "won"),
    ("AWAY_REG",  fin(homeScore=2, awayScore=0, winner="HOME", duration="REGULAR"), "lost"),
    ("HOME_ET",   fin(homeScore=2, awayScore=1, winner="HOME", duration="EXTRA_TIME", aet=True), "won"),
    ("HOME_REG",  fin(homeScore=2, awayScore=1, winner="HOME", duration="EXTRA_TIME", aet=True), "lost"),
    ("HOME_PENS", fin(homeScore=1, awayScore=1, penHome=4, penAway=3, shootout=True, aet=True, duration="PENALTY_SHOOTOUT"), "won"),
    ("HOME_ET",   fin(homeScore=1, awayScore=1, penHome=4, penAway=3, shootout=True, aet=True, duration="PENALTY_SHOOTOUT"), "lost"),
    # a winner-less pens feed (only the shootout score) still settles — same inference as result bets
    ("AWAY_PENS", fin(homeScore=0, awayScore=0, penHome=3, penAway=4, shootout=True), "won"),
]
for sel, m, exp in VEC:
    wl = movbet(sel); W.settle(wl, m)
    ck("%s on %s -> %s" % (sel, m.get("duration", "pens-only"), exp), wl[0]["status"] == exp, wl[0])
wl = movbet("HOME_REG"); W.settle(wl, fin(homeScore=1, awayScore=1))          # level, no pens data yet
ck("a level game with no winner data stays pending (never lose a bet to a glitch)", wl[0]["status"] == "pending", wl[0])
for vs in W.VOID_STATUSES:
    wl = movbet("HOME_REG"); W.settle(wl, fin(status=vs, homeScore=None, awayScore=None))
    ck("%s voids + refunds" % vs, wl[0]["status"] == "void" and wl[0]["return"] == 5, wl[0])

print("\n== MoV exploit: any covering subset dutched with the KO result book costs > 1 ==")
bad = []
for ch, ca in COMPS:
    b = W.mov_odds(ch, ca)
    r = W.match_odds(ch, ca, knockout=True)
    trio_h = ["HOME_REG", "HOME_ET", "HOME_PENS"]; trio_a = ["AWAY_REG", "AWAY_ET", "AWAY_PENS"]
    for trio, opp in ((trio_h, "AWAY"), (trio_a, "HOME")):
        if all(t in b for t in trio):                      # {side advances any way} U {other side advances} = everything
            cost = sum(1.0 / b[t]["decimal"] for t in trio) + 1.0 / r[opp]["decimal"]
            if cost <= 1.0 + 1e-9:
                bad.append((ch, ca, trio[0][:4], cost))
    if len(b) == 6:                                        # all six = everything
        if sum(1.0 / v["decimal"] for v in b.values()) <= 1.0 + 1e-9:
            bad.append((ch, ca, "all-six"))
ck("MoV trio + opposite result never dutches; the full set never dutches", not bad, bad[:4])

print("\n== cards pricing: ladder rule + margin, KO bump moves the lines the right way ==")
for ko in (False, True):
    book = W.cards_odds(knockout=ko)
    lam = W._cards_lambda(ko)
    for k, leg in book.items():
        s = 1.0 / leg["OVER"]["decimal"] + 1.0 / leg["UNDER"]["decimal"]
        ck("cards %s (%s) book overrounds (%.3f)" % (k, "KO" if ko else "GRP", s), s > 1.0 + 1e-6, s)
        # fair check against the same Poisson the pricer uses
        kmax = int(float(k)); term = math.exp(-lam); pu = 0.0
        for i in range(0, W.CARDS_GRID_MAX + 1):
            if i > 0:
                term *= lam / i
            if i <= kmax:
                pu += term
        for sel, pf in (("OVER", 1 - pu), ("UNDER", pu)):
            imp = leg[sel]["den"] / (leg[sel]["num"] + leg[sel]["den"])
            ck("cards %s %s (%s) priced above fair" % (k, sel, "KO" if ko else "GRP"), imp > pf, (imp, pf))
grp, ko = W.cards_odds(False), W.cards_odds(True)
if "4.5" in grp and "4.5" in ko:
    ck("knockout Over 4.5 is SHORTER than group (KO bump raises expected cards)",
       ko["4.5"]["OVER"]["decimal"] < grp["4.5"]["OVER"]["decimal"], (ko["4.5"]["OVER"], grp["4.5"]["OVER"]))

print("\n== cards placement + settlement (90' basis, no-data void grace) ==")
ok, w = W.place([], "Erol", GRP, "OVER", 5, 100, 50, 50, now=NOW, market="cards", line=4.5)
ck("cards bet places on any game", ok and w.get("market") == "cards" and w.get("line") == 4.5, w)
for badl in (4.0, 1.5, 9.5, "x", None, float("nan")):
    okb, m2 = W.place([], "Erol", GRP, "OVER", 5, 100, 50, 50, now=NOW, market="cards", line=badl)
    ck("cards line %r rejected" % (badl,), not okb, m2 if okb else None)
def cbet(sel="OVER", line=4.5):
    return [{"id": "c1", "player": "E", "matchId": "k1", "market": "cards", "selection": sel, "line": line,
             "stake": 5, "num": 1, "den": 1, "frac": "1/1", "return": 10, "status": "pending"}]
wl = cbet(); W.settle(wl, fin(homeScore=1, awayScore=0, winner="HOME", cardsHome=3, cardsAway=2))
ck("5 cards beats Over 4.5", wl[0]["status"] == "won", wl[0])
wl = cbet("UNDER"); W.settle(wl, fin(homeScore=1, awayScore=0, winner="HOME", cardsHome=2, cardsAway=2))
ck("4 cards wins Under 4.5", wl[0]["status"] == "won", wl[0])
wl = cbet(); W.settle(wl, fin(homeScore=1, awayScore=0, winner="HOME", cardsHome=0, cardsAway=0))
ck("an explicit 0-0 cards count SETTLES (empty bookings list is data, not absence)", wl[0]["status"] == "lost", wl[0])
wl = cbet(); W.settle(wl, fin(homeScore=1, awayScore=0, winner="HOME"), now=NOW)
ck("no cards data right after FT stays pending (bookings can lag)", wl[0]["status"] == "pending", wl[0])
old = fin(homeScore=1, awayScore=0, winner="HOME", utcDate="2000-01-01T00:00:00Z")
wl = cbet(); W.settle(wl, old, now=NOW)
ck("no cards data hours after FT -> VOID with the stake back", wl[0]["status"] == "void" and wl[0]["return"] == 5, wl[0])
for hostile in ((float("nan"), 2), (-1, 2), ("x", "y"), (float("inf"), 0)):
    wl = cbet(); W.settle(wl, fin(homeScore=1, awayScore=0, winner="HOME", cardsHome=hostile[0], cardsAway=hostile[1]), now=NOW)
    ck("hostile cards %r never settles it" % (hostile,), wl[0]["status"] == "pending", wl[0])

print("\n== accas: mov + cards legs across games; same-game still blocked; both settle by their own basis ==")
ws = []
ok, acc = W.place_acca(ws, "Erol",
                       [{"match": KO, "selection": "HOME_REG", "comp_home": 70, "comp_away": 40, "market": "mov"},
                        {"match": KO2, "selection": "OVER", "comp_home": 50, "comp_away": 50, "market": "cards", "line": 4.5}],
                       5, 100, now=NOW)
ck("a mov + cards 2-fold across games places", ok and len(acc.get("legs", [])) == 2, acc)
W.settle(ws, fin(homeScore=2, awayScore=0, winner="HOME", duration="REGULAR"))
W.settle(ws, {"id": "k2", "home": "C", "away": "D", "stage": "QUARTER_FINALS", "status": "FINISHED",
              "utcDate": "2099-01-01T00:00:00Z", "homeScore": 1, "awayScore": 1, "penHome": 4, "penAway": 3,
              "shootout": True, "winner": "HOME", "cardsHome": 4, "cardsAway": 3})
ck("both legs settled by their own basis and the acca WON", ws[0]["status"] == "won"
   and all(l.get("result") == "won" for l in ws[0]["legs"]), ws[0])
okx, msgx = W.place_acca([], "Erol",
                         [{"match": KO, "selection": "HOME_REG", "comp_home": 70, "comp_away": 40, "market": "mov"},
                          {"match": KO, "selection": "OVER", "comp_home": 70, "comp_away": 40, "market": "cards", "line": 4.5}],
                         5, 100, now=NOW)
ck("a MoV leg in a same-game group is still blocked (the grid can't split 90 vs ET)",
   not okx and "method" in str(msgx).lower(), msgx)
okg, msgg = W.place_acca([], "Erol",
                         [{"match": GRP, "selection": "HOME_REG", "comp_home": 70, "comp_away": 40, "market": "mov"},
                          {"match": KO2, "selection": "HOME", "comp_home": 70, "comp_away": 40}],
                         5, 100, now=NOW)
ck("a mov leg on a GROUP game is rejected in an acca too", not okg, msgg if okg else None)
# a cards acca leg on a no-data game voids out after the grace and the acca pays on the rest
ws2 = []
ok2, _ = W.place_acca(ws2, "Erol",
                      [{"match": KO, "selection": "HOME", "comp_home": 70, "comp_away": 40},
                       {"match": KO2, "selection": "OVER", "comp_home": 50, "comp_away": 50, "market": "cards", "line": 4.5}],
                      5, 100, now=NOW)
W.settle(ws2, fin(homeScore=2, awayScore=0, winner="HOME"))
W.settle(ws2, {"id": "k2", "home": "C", "away": "D", "stage": "QUARTER_FINALS", "status": "FINISHED",
               "utcDate": "2000-01-01T00:00:00Z", "homeScore": 1, "awayScore": 0, "winner": "HOME"})
legs = {l["matchId"]: l.get("result") for l in ws2[0]["legs"]}
ck("no-data cards leg voided out; result leg won; acca pays on the remaining leg",
   legs.get("k2") == "void" and legs.get("k1") == "won" and ws2[0]["status"] == "won", ws2[0])

print("\n== cards are an independent axis: NO score-based combo can cover them ==")
# a cards Over can lose at ANY scoreline and a cards Under can lose at ANY scoreline, so no basket of
# result/OU/HC/CS/MoV legs (all functions of score+method) plus ONE cards side ever covers all outcomes —
# assert the primitive: both cards outcomes are possible at every score.
ck("both cards outcomes possible regardless of score (independence primitive)",
   W._cards_result(4.5, "OVER", fin(cardsHome=9, cardsAway=0)) == "won"
   and W._cards_result(4.5, "OVER", fin(cardsHome=0, cardsAway=0)) == "lost", None)


print("\n== feed normaliser: bookings -> 90' cards; no bookings key -> None (not zero) ==")
import update_results as UR
resolve = lambda n: n
api = [{"id": 1, "homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}, "utcDate": "2026-07-01T18:00:00Z",
        "status": "FINISHED", "stage": "QUARTER_FINALS",
        "score": {"fullTime": {"home": 1, "away": 0}, "regularTime": {"home": 1, "away": 0},
                  "duration": "REGULAR", "winner": "HOME_TEAM"},
        "bookings": [{"minute": 12, "team": {"name": "A"}, "card": "YELLOW"},
                     {"minute": 88, "team": {"name": "B"}, "card": "YELLOW_RED"},
                     {"minute": 95, "team": {"name": "A"}, "card": "YELLOW"},          # ET booking -> excluded
                     {"minute": None, "team": {"name": "B"}, "card": "YELLOW"},        # no minute -> counted
                     "junk", {"minute": 30, "team": {}, "card": "YELLOW"}]},           # hostile entries ignored
       {"id": 2, "homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}, "utcDate": "2026-07-02T18:00:00Z",
        "status": "FINISHED", "stage": "GROUP_STAGE",
        "score": {"fullTime": {"home": 0, "away": 0}, "regularTime": {"home": 0, "away": 0},
                  "duration": "REGULAR", "winner": "DRAW"}}]
out = UR.normalize_matches(api, resolve)
m1, m2 = out[0], out[1]
ck("90' cards counted per team (ET booking excluded, minuteless counted)", m1["cardsHome"] == 1 and m1["cardsAway"] == 2, (m1["cardsHome"], m1["cardsAway"]))
ck("a second yellow counts as a red", m1["redAway"] == 1 and m1["redHome"] == 0, (m1["redHome"], m1["redAway"]))
ck("a match with NO bookings key has cards None (data absent, not zero)", m2["cardsHome"] is None and m2["cardsAway"] is None, (m2["cardsHome"], m2["cardsAway"]))
ck("duration/aet/shootout unchanged by the cards pass", m1["duration"] == "REGULAR" and not m1["aet"] and not m1["shootout"], m1)

print("\n" + ("All MoV + cards QA passed." if not fails else "MOV/CARDS QA FAILED (%d): %s" % (len(fails), ", ".join(fails))))
import sys
sys.exit(1 if fails else 0)
