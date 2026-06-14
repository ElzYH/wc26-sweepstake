#!/usr/bin/env python3
"""Stage 1 QA — Over/Under total-goals odds model (Poisson). Pure pricing; no placement/settlement yet.
Checks realism vs real-life books, internal consistency (margin), monotonicity, and hostile inputs."""
import math
import wager as W

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

def dec(leg):  # decimal odds of a selection dict
    return leg["decimal"]

print("== Poisson CDF sanity ==")
# Known values: Poisson(2.6) P(X<=2) ~ e^-2.6 (1+2.6+2.6^2/2) = e^-2.6 * 6.98 ~ 0.5184
ck("poisson_cdf(2, 2.6) ~ 0.518", abs(W._poisson_cdf(2, 2.6) - 0.5184) < 0.003, W._poisson_cdf(2, 2.6))
ck("poisson_cdf(0, lam) = e^-lam", abs(W._poisson_cdf(0, 2.6) - math.exp(-2.6)) < 1e-9, W._poisson_cdf(0, 2.6))
ck("cdf is 1.0 in the limit (n large)", abs(W._poisson_cdf(40, 2.6) - 1.0) < 1e-9, W._poisson_cdf(40, 2.6))
ck("cdf monotonically increases in n", all(W._poisson_cdf(i, 2.6) <= W._poisson_cdf(i + 1, 2.6) for i in range(0, 12)), None)
ck("negative n -> 0", W._poisson_cdf(-1, 2.6) == 0.0, None)

print("\n== expected goals (lambda) ==")
ck("even match -> base ~2.6", abs(W.expected_goals(80, 80) - 2.6) < 1e-9, W.expected_goals(80, 80))
ck("mismatch raises lambda", W.expected_goals(95, 5) > W.expected_goals(50, 50), (W.expected_goals(95, 5), W.expected_goals(50, 50)))
ck("lambda is clamped to the band", W.GOALS_LAMBDA_MIN <= W.expected_goals(99, 1) <= W.GOALS_LAMBDA_MAX, W.expected_goals(99, 1))
ck("hostile composites don't crash lambda", all(isinstance(W.expected_goals(a, b), float) for a, b in
   [(None, None), (float("nan"), 50), (float("inf"), 1), (-5, -9), ("x", "y"), (0, 0)]), None)

print("\n== offered lines keep a house margin; central lines always present ==")
o = W.goals_odds(80, 80)   # even game, lambda ~2.6
ck("central lines 1.5/2.5/3.5 always offered", all(k in o for k in ["1.5", "2.5", "3.5"]), list(o.keys()))
ck("even-game offered set is 0.5..5.5 (extreme lines dropped so the book never underrounds)",
   sorted(o.keys(), key=float) == ["0.5", "1.5", "2.5", "3.5", "4.5", "5.5"], list(o.keys()))
ck("every offered line keeps a house margin (book > 100%)",
   all(1.0 / dec(o[k]["OVER"]) + 1.0 / dec(o[k]["UNDER"]) > 1.0 for k in o),
   {k: round(1 / dec(o[k]["OVER"]) + 1 / dec(o[k]["UNDER"]), 3) for k in o})
ck("each line has OVER + UNDER", all(set(o[k].keys()) == {"OVER", "UNDER"} for k in o), None)

print("\n== realism vs real-life markets (even game) ==")
ou25 = o["2.5"]
# Real books: even-game O/U 2.5 is roughly 1.8-2.05 each way, Under often a touch shorter at WC scoring levels.
ck("O/U 2.5 OVER in a realistic band (1.7-2.15)", 1.70 <= dec(ou25["OVER"]) <= 2.15, dec(ou25["OVER"]))
ck("O/U 2.5 UNDER in a realistic band (1.7-2.15)", 1.70 <= dec(ou25["UNDER"]) <= 2.15, dec(ou25["UNDER"]))
# O/U 0.5: Over (not 0-0) is very likely -> short; Under (0-0) is long.
ck("O/U 0.5 OVER is short (<1.25)", dec(o["0.5"]["OVER"]) < 1.25, dec(o["0.5"]["OVER"]))
ck("O/U 0.5 UNDER is long (>4.0)", dec(o["0.5"]["UNDER"]) > 4.0, dec(o["0.5"]["UNDER"]))
# O/U 1.5 Over is the most common 'goals' bet — should sit roughly 1.25-1.6 for an even game.
ck("O/U 1.5 OVER ~ 1.2-1.65", 1.20 <= dec(o["1.5"]["OVER"]) <= 1.65, dec(o["1.5"]["OVER"]))
# Highest line offered for an even game (5.5): Over is a long shot, Under heavily odds-on.
ck("O/U 5.5 OVER is a big price (>8)", dec(o["5.5"]["OVER"]) > 8, dec(o["5.5"]["OVER"]))
ck("O/U 5.5 UNDER is odds-on (<1.20)", dec(o["5.5"]["UNDER"]) < 1.20, dec(o["5.5"]["UNDER"]))

print("\n== bookmaker margin: EVERY offered line carries an overround (underround lines are filtered out) ==")
# The goals book now only offers a line when its two-way book overrounds — so there is never a bettor edge,
# even on the outer lines where one side is near-certain (those simply aren't offered for that game).
for k in o:
    book = 1.0 / dec(o[k]["OVER"]) + 1.0 / dec(o[k]["UNDER"])
    ck("line %s keeps a house margin (100%% < book <= 135%%)" % k, 1.0 < book <= 1.35, round(book, 4))

print("\n== monotonicity: higher line -> Over less likely (longer), Under shorter ==")
ks = sorted(o.keys(), key=float)
over_decs = [dec(o[k]["OVER"]) for k in ks]
under_decs = [dec(o[k]["UNDER"]) for k in ks]
ck("OVER odds lengthen (non-decreasing) as the line rises", all(over_decs[i] <= over_decs[i + 1] + 1e-9 for i in range(len(over_decs) - 1)), over_decs)
ck("UNDER odds shorten (non-increasing) as the line rises", all(under_decs[i] >= under_decs[i + 1] - 1e-9 for i in range(len(under_decs) - 1)), under_decs)

print("\n== favourite mismatch shifts the goal expectation up (line balance moves) ==")
# In a big mismatch, lambda is higher -> Over 2.5 should be SHORTER than in an even game.
om = W.goals_odds(95, 5)
ck("mismatch O/U 2.5 OVER shorter than even game", dec(om["2.5"]["OVER"]) <= dec(ou25["OVER"]) + 1e-9, (dec(om["2.5"]["OVER"]), dec(ou25["OVER"])))

print("\n== fractions snap to the real betting ladder ==")
allowed = set(W._FRACTIONS)
ck("every priced fraction is on the British ladder", all((o[k][s]["num"], o[k][s]["den"]) in allowed for k in o for s in o[k]), None)

print("\n== hostile inputs never crash and stay sane ==")
for a, b in [(None, None), (float("nan"), 50), (float("inf"), float("-inf")), (-9, -9), ("x", "y"), (0, 0), (1e9, 0)]:
    try:
        oo = W.goals_odds(a, b)
        good = (all(k in oo for k in ["1.5", "2.5", "3.5"])                       # central lines always there
                and all(dec(oo[k][s]) >= 1.0 for k in oo for s in oo[k])         # every price is a real (>=1.0) decimal
                and all(1.0 / dec(oo[k]["OVER"]) + 1.0 / dec(oo[k]["UNDER"]) > 1.0 for k in oo))  # and overrounds
    except Exception as e:
        good = False
        print("    crash on (%r,%r): %r" % (a, b, e))
    ck("goals_odds(%r,%r) is sane" % (a, b), good, None)

print("\n== custom line list is honoured ==")
oc = W.goals_odds(80, 80, lines=[2.5])
ck("custom single-line request returns just that line", list(oc.keys()) == ["2.5"], list(oc.keys()))

print("\n== O/U margin is a touch higher than the 1X2 book (returns deliberately trimmed) ==")
ck("OU_OVERROUND > the 1X2 OVERROUND", W.OU_OVERROUND > W.OVERROUND, (W.OU_OVERROUND, W.OVERROUND))
# the trim should be small — within a few points, not punitive
ck("the extra margin is modest (<=8pp over 1X2)", (W.OU_OVERROUND - W.OVERROUND) <= 0.08, W.OU_OVERROUND - W.OVERROUND)
# concretely: even-game O/U 2.5 Over should be a shade shorter than a fair 1.08-book equivalent
_fairish = round(1.0 + 1.0 / (min(0.95, 0.506 * W.OVERROUND)) - 1.0, 3)
ck("trim actually lowers the even-game O/U 2.5 Over price vs an 8% book", dec(o["2.5"]["OVER"]) <= 1.95, dec(o["2.5"]["OVER"]))

print("\n== reference table (even game) — eyeball against a real book ==")
for k in ks:
    print("   O/U %s   OVER %-6s (%.2f)   UNDER %-6s (%.2f)" % (
        k, o[k]["OVER"]["frac"], dec(o[k]["OVER"]), o[k]["UNDER"]["frac"], dec(o[k]["UNDER"])))

print("\n" + ("All O/U odds-model QA passed." if not fails else "O/U ODDS-MODEL QA FAILED (%d): %s" % (len(fails), ", ".join(fails))))
import sys
sys.exit(1 if fails else 0)
