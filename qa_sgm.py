#!/usr/bin/env python3
"""Same-game multi (SGM) exploit QA. The sold price of EVERY same-game combo must beat the TRUE joint
probability — computed here by an INDEPENDENT brute-force reference (its own Poisson grid, its own leg
predicates) so a shared bug can't self-certify. Fuzzes 1500 random 2-3 pick same-game combos across the
strength grid and all combinable markets; proves the classic correlation farms (hc+Over, win+BTTS,
win+Under) are priced above fair; contradictions rejected; degenerate subsets never punter-positive;
placement/settlement pay by the group price with void-group semantics; legacy accas untouched."""
import math, random
import wager as W

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

NOW = 1_700_000_000
GMAX = 20   # deliberately BIGGER than the engine's grid — truncation must be conservative, not generous

def ref_joint(group, ch, ca, knockout=False):
    """Independent reference joint probability (own grid, own predicates, own pens edge)."""
    lh, la = W._team_lambdas(ch, ca)
    p_h90 = W._hc_home_prob(lh, la, -0.5); p_a90 = 1.0 - W._hc_home_prob(lh, la, 0.5)
    strong = p_h90 / max(1e-9, p_h90 + p_a90)
    pn_home = 0.5 + (W.MOV_PENS_EDGE - 0.5) * (2 * strong - 1)
    def pays(mk, sel, ln, h, a):
        if mk == "ou":   return (h + a) > ln if sel == "OVER" else (h + a) < ln
        if mk == "hc":   return ((h + ln) > a) == (sel == "HOME")
        if mk == "cs":   return sel == "%d-%d" % (h, a)
        if mk == "btts": return ((h > 0 and a > 0)) == (sel == "YES")
        return None
    score = [(g["market"], g["selection"], g.get("line")) for g in group if g["market"] in ("ou", "hc", "cs", "btts")]
    winners = [g["selection"] for g in group if g["market"] == "result" and g["selection"] in ("HOME", "AWAY")]
    draws = [g for g in group if g["market"] == "result" and g["selection"] == "DRAW"]
    cards = [(float(g["line"]), g["selection"]) for g in group if g["market"] == "cards"]
    if len(set(winners)) > 1:
        return 0.0
    side = winners[0] if winners else None
    ph = [math.exp(-lh)]; pa = [math.exp(-la)]
    for k in range(1, GMAX + 1):
        ph.append(ph[-1] * lh / k); pa.append(pa[-1] * la / k)
    p = 0.0
    for h in range(GMAX + 1):
        for a in range(GMAX + 1):
            if any(pays(mk, sel, ln, h, a) is not True for mk, sel, ln in score):
                continue
            if draws and h != a:
                continue
            w = 1.0
            if side is not None:
                if draws:
                    return 0.0                      # a draw AND a winner: impossible
                if h == a:
                    if not knockout:
                        continue
                    w = pn_home if side == "HOME" else 1 - pn_home
                elif (h > a) != (side == "HOME"):
                    continue
            p += ph[h] * pa[a] * w
    if cards:
        lam = W._cards_lambda(knockout)
        pc, term = 0.0, math.exp(-lam)
        for k in range(0, 40):
            if k > 0:
                term *= lam / k
            if all(((k > ln) if sel == "OVER" else (k < ln)) for ln, sel in cards):
                pc += term
        p *= pc
    return p

def implied(d):
    return d["den"] / (d["num"] + d["den"])

COMPS = [(c1, c2) for c1 in range(0, 101, 20) for c2 in range(0, 101, 20)] + [(80, 60), (60, 80), (50, 50)]

print("== the classic correlation farms are priced ABOVE fair (the exact exploits SGM must kill) ==")
for name, group, ch, ca, ko in [
    ("favourite -1.5 + Over 2.5",   [{"market": "hc", "selection": "HOME", "line": -1.5}, {"market": "ou", "selection": "OVER", "line": 2.5}], 80, 20, False),
    ("favourite win + BTTS No",     [{"market": "result", "selection": "HOME"}, {"market": "btts", "selection": "NO"}], 80, 20, False),
    ("favourite win + Under 2.5",   [{"market": "result", "selection": "HOME"}, {"market": "ou", "selection": "UNDER", "line": 2.5}], 80, 20, False),
    ("underdog +1.5 + Under 2.5",   [{"market": "hc", "selection": "AWAY", "line": -1.5}, {"market": "ou", "selection": "UNDER", "line": 2.5}], 80, 20, False),
    ("KO advance + fails to cover", [{"market": "result", "selection": "HOME"}, {"market": "hc", "selection": "AWAY", "line": -1.5}], 60, 40, True),
    ("win + BTTS Yes + Over 2.5",   [{"market": "result", "selection": "HOME"}, {"market": "btts", "selection": "YES"}, {"market": "ou", "selection": "OVER", "line": 2.5}], 60, 40, False),
]:
    d, err = W.sgm_group_price(group, ch, ca, knockout=ko)
    fair = ref_joint(group, ch, ca, knockout=ko)
    if d is None:
        ck("%s: refused only because it's unsellable, never mispriced" % name, fair <= 1e-9 or fair > W.SGM_MAX_PROB, (err, fair))
    else:
        ck("%s: sold %.4f implied > %.4f fair (edge %.1f%%)" % (name, implied(d), fair, (implied(d) / max(fair, 1e-9) - 1) * 100),
           implied(d) >= fair * (1 + W.SGM_MIN_MARGIN) - 1e-9, (d["frac"], fair))

print("\n== fuzz: 1500 random same-game combos — sold implied ALWAYS beats the independent fair joint ==")
rng = random.Random(2026)
bad = 0; priced = 0; refused = 0
def rand_pick():
    mk = rng.choice(("result", "ou", "hc", "cs", "btts", "cards"))
    if mk == "result": return {"market": mk, "selection": rng.choice(("HOME", "DRAW", "AWAY"))}
    if mk == "ou":     return {"market": mk, "selection": rng.choice(("OVER", "UNDER")), "line": rng.choice(W.OU_LINES)}
    if mk == "hc":     return {"market": mk, "selection": rng.choice(("HOME", "AWAY")), "line": rng.choice(W.HC_LINES)}
    if mk == "cs":     return {"market": mk, "selection": "%d-%d" % (rng.randint(0, 4), rng.randint(0, 4))}
    if mk == "btts":   return {"market": mk, "selection": rng.choice(("YES", "NO"))}
    return {"market": "cards", "selection": rng.choice(("OVER", "UNDER")), "line": rng.choice(W.CARDS_LINES)}
for _ in range(1500):
    ch, ca = rng.choice(COMPS); ko = rng.random() < 0.5
    group = [rand_pick() for _ in range(rng.randint(2, 3))]
    if ko:
        group = [g for g in group if not (g["market"] == "result" and g["selection"] == "DRAW")] or [rand_pick()]
    d, err = W.sgm_group_price(group, ch, ca, knockout=ko)
    fair = ref_joint(group, ch, ca, knockout=ko)
    if d is None:
        refused += 1
        if fair > 1e-6 and fair <= W.SGM_MAX_PROB and err and "Couldn't price" not in err and "can't all win" in err:
            bad += 1; print("    wrongly refused as impossible:", group, ch, ca, fair)
        continue
    priced += 1
    if implied(d) < fair * (1 + W.SGM_MIN_MARGIN) - 1e-9:
        bad += 1
        print("    UNDERPRICED (%g,%g)%s %r sold %.4f fair %.4f" % (ch, ca, " KO" if ko else "", group, implied(d), fair))
ck("no sold combo under fair x margin, no possible combo refused as impossible (%d priced, %d refused)" % (priced, refused),
   bad == 0 and priced > 300, bad)

print("\n== degenerate subsets: a redundant leg can't beat the tight single's price ==")
d, _ = W.sgm_group_price([{"market": "ou", "selection": "OVER", "line": 2.5}, {"market": "ou", "selection": "OVER", "line": 1.5}], 50, 50)
single = W.goals_odds(50, 50)["2.5"]["OVER"]
ck("Over 2.5 + Over 1.5 (joint == Over 2.5) never pays more than the Over 2.5 single",
   d is None or d["decimal"] <= single["decimal"] + 1e-9, (d, single))

print("\n== contradictions + impossibles are refused with the right message ==")
for group in ([{"market": "ou", "selection": "OVER", "line": 2.5}, {"market": "ou", "selection": "UNDER", "line": 2.5}],
              [{"market": "result", "selection": "HOME"}, {"market": "result", "selection": "AWAY"}],
              [{"market": "result", "selection": "HOME"}, {"market": "cs", "selection": "0-2"}],
              [{"market": "btts", "selection": "YES"}, {"market": "ou", "selection": "UNDER", "line": 1.5}]):
    d, err = W.sgm_group_price(group, 60, 40)
    ck("refused: %s" % ("; ".join("%s %s" % (g["market"], g["selection"]) for g in group)),
       d is None and "can't all win" in (err or ""), (d, err))

print("\n== placement: SGM acca stores the group price; duplicates + mov-in-group rejected; legacy intact ==")
FUT = lambda mid, st="GROUP_STAGE": {"id": mid, "home": "A" + mid, "away": "B" + mid, "stage": st,
                                     "utcDate": "2099-01-01T00:00:00Z", "status": "TIMED"}
ws = []
ok, w = W.place_acca(ws, "Erol",
                     [{"match": FUT("g1"), "selection": "HOME", "comp_home": 80, "comp_away": 20},
                      {"match": FUT("g1"), "selection": "OVER", "comp_home": 80, "comp_away": 20, "market": "ou", "line": 2.5},
                      {"match": FUT("g2"), "selection": "AWAY", "comp_home": 30, "comp_away": 70}],
                     5, 100, now=NOW)
ck("a same-game pair + a cross-game leg places", ok and w.get("groups") and len(w["groups"]) == 2, w.get("groups"))
g1 = next(g for g in w["groups"] if len(g["legs"]) == 2)
ref = ref_joint([{"market": "result", "selection": "HOME"}, {"market": "ou", "selection": "OVER", "line": 2.5}], 80, 20)
ck("the same-game group's stored price beats the reference fair joint",
   (g1["den"] / (g1["num"] + g1["den"])) >= ref * (1 + W.SGM_MIN_MARGIN) - 1e-9, (g1["frac"], ref))
naive = 1.0
for l in w["legs"][:2]:
    naive *= 1 + l["num"] / l["den"]
ck("the group pays LESS than the naive per-leg product (correlation captured)",
   g1["decimal"] < naive - 1e-9, (g1["decimal"], naive))
okd, msgd = W.place_acca([], "Erol",
                         [{"match": FUT("g1"), "selection": "HOME", "comp_home": 80, "comp_away": 20},
                          {"match": FUT("g1"), "selection": "HOME", "comp_home": 80, "comp_away": 20}],
                         5, 100, now=NOW)
ck("the exact same pick twice is rejected", not okd, msgd if okd else None)
okm, msgm = W.place_acca([], "Erol",
                         [{"match": FUT("k1", "QUARTER_FINALS"), "selection": "HOME_REG", "comp_home": 70, "comp_away": 40, "market": "mov"},
                          {"match": FUT("k1", "QUARTER_FINALS"), "selection": "OVER", "comp_home": 70, "comp_away": 40, "market": "ou", "line": 2.5}],
                         5, 100, now=NOW)
ck("a MoV leg in a SAME-game group is rejected (the grid can't split 90 vs ET)", not okm and "method" in str(msgm).lower(), msgm)
okc, msgc = W.place_acca([], "Erol",
                         [{"match": FUT("g1"), "selection": "OVER", "comp_home": 60, "comp_away": 60, "market": "ou", "line": 2.5},
                          {"match": FUT("g1"), "selection": "UNDER", "comp_home": 60, "comp_away": 60, "market": "ou", "line": 2.5}],
                         5, 100, now=NOW)
ck("a contradictory same-game combo is rejected at placement", not okc and "can't all win" in str(msgc), msgc)
wl = []
okL, wL = W.place_acca(wl, "Erol",
                       [{"match": FUT("g1"), "selection": "HOME", "comp_home": 70, "comp_away": 40},
                        {"match": FUT("g2"), "selection": "AWAY", "comp_home": 30, "comp_away": 70}],
                       5, 100, now=NOW)
ck("a plain cross-game acca has NO groups field (legacy records unchanged)", okL and "groups" not in wL, sorted(wL))

print("\n== settlement: group pays its joint price only when EVERY leg wins; a void leg voids the GROUP ==")
def fin(mid, h, a, **kw):
    m = {"id": mid, "home": "A" + mid, "away": "B" + mid, "stage": "GROUP_STAGE", "status": "FINISHED",
         "utcDate": "2099-01-01T00:00:00Z", "homeScore": h, "awayScore": a}
    m.update(kw); return m
W.settle(ws, fin("g1", 3, 1, winner="HOME"))        # home win + over 2.5: both legs won
W.settle(ws, fin("g2", 0, 2, winner="AWAY"))
exp = round(5 * w["decimal"], 2)
ck("full win pays stake x (group price x other legs) exactly", ws[0]["status"] == "won" and abs(ws[0]["return"] - exp) < 0.01, (ws[0]["return"], exp))
ws2 = []
ok2, w2 = W.place_acca(ws2, "Erol",
                       [{"match": FUT("g1"), "selection": "HOME", "comp_home": 80, "comp_away": 20},
                        {"match": FUT("g1"), "selection": "OVER", "comp_home": 80, "comp_away": 20, "market": "ou", "line": 2.5},
                        {"match": FUT("g2"), "selection": "AWAY", "comp_home": 30, "comp_away": 70}],
                       5, 100, now=NOW)
W.settle(ws2, fin("g1", 2, 1, winner="HOME"))       # home won but only 3 goals > 2.5 ✓... 2-1 IS over 2.5 -> both won
W.settle(ws2, fin("g2", 1, 0, winner="HOME"))       # the cross-game leg LOSES -> acca lost
ck("a losing leg anywhere still kills the whole acca", ws2[0]["status"] == "lost" and ws2[0]["return"] == 0, ws2[0])
ws3 = []
ok3, w3 = W.place_acca(ws3, "Erol",
                       [{"match": FUT("g1"), "selection": "HOME", "comp_home": 80, "comp_away": 20},
                        {"match": FUT("g1"), "selection": "OVER", "comp_home": 80, "comp_away": 20, "market": "ou", "line": 2.5},
                        {"match": FUT("g2"), "selection": "AWAY", "comp_home": 30, "comp_away": 70}],
                       5, 100, now=NOW)
W.settle(ws3, fin("g1", None, None, status="CANCELLED"))    # the same-game group's match is cancelled -> group void
W.settle(ws3, fin("g2", 0, 2, winner="AWAY"))
g2leg = next(l for l in w3["legs"] if l["matchId"] == "g2")
exp3 = round(5 * (1 + g2leg["num"] / g2leg["den"]), 2)
ck("a void GROUP drops to 1x and the acca pays on the remaining leg", ws3[0]["status"] == "won" and abs(ws3[0]["return"] - exp3) < 0.01, (ws3[0]["return"], exp3))

print("\n" + ("All SGM QA passed." if not fails else "SGM QA FAILED (%d): %s" % (len(fails), ", ".join(fails))))
import sys
sys.exit(1 if fails else 0)
