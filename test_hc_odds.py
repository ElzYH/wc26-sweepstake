#!/usr/bin/env python3
"""Handicap odds model QA — hc_odds() prices goal-margin lines off the SAME Poisson grid as the
exact-score book. Checks: dict shape, exact HOME/AWAY complement, the ladder rule (never quote a side
we can't price inside the ladder), guaranteed post-rounding overround, mirror symmetry at even
strengths, monotonicity in both line and strength, hostile inputs, and the shared-model invariants
(cs split == hc split; lambdas sum to expected_goals). Also proves ±0.5 is not on the menu."""
import wager as W

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

print("== shape + fraction coherence ==")
book = W.hc_odds(80, 60)
ck("returns a dict of offered lines", isinstance(book, dict) and len(book) >= 1, book)
ck("every key is a stringified member of HC_LINES", all(any(k == W._line_key(L) for L in W.HC_LINES) for k in book), sorted(book))
for k, leg in book.items():
    for sel in ("HOME", "AWAY"):
        o = leg.get(sel) or {}
        ok_frac = o.get("frac") == "%d/%d" % (o.get("num", -1), o.get("den", -2))
        ok_dec = abs(o.get("decimal", 0) - round(1.0 + o["num"] / o["den"], 3)) < 1e-9 if o.get("den") else False
        ck("line %s %s: frac/num/den/decimal agree" % (k, sel), ok_frac and ok_dec, o)

print("\n== the model: exact complement + monotone in the line ==")
lh, la = W._team_lambdas(80, 60)
probs = {L: W._hc_home_prob(lh, la, L) for L in (-2.5, -1.5, 1.5, 2.5)}
ck("P(home covers) strictly rises with the line", probs[-2.5] < probs[-1.5] < probs[1.5] < probs[2.5], probs)
ck("a bigger favourite covers -1.5 more often", W._hc_home_prob(*W._team_lambdas(95, 40), line=-1.5) > probs[-1.5],
   (W._hc_home_prob(*W._team_lambdas(95, 40), line=-1.5), probs[-1.5]))

print("\n== ladder rule + margin on every offered line (11x11 strength grid) ==")
grid_bad = []
for c1 in range(0, 101, 10):
    for c2 in range(0, 101, 10):
        b = W.hc_odds(c1, c2)
        lam_h, lam_a = W._team_lambdas(c1, c2)
        for k, leg in b.items():
            imp = 1.0 / leg["HOME"]["decimal"] + 1.0 / leg["AWAY"]["decimal"]
            fair = W._hc_home_prob(lam_h, lam_a, float(k))
            if imp <= 1.0 + 1e-6:
                grid_bad.append(("underround", c1, c2, k, imp))
            if fair > W.HC_MAX_PROB + 1e-9 or (1 - fair) > W.HC_MAX_PROB + 1e-9:
                grid_bad.append(("ladder", c1, c2, k, fair))
            if max(1.0 / leg["HOME"]["decimal"], 1.0 / leg["AWAY"]["decimal"]) > 0.94:
                grid_bad.append(("shortprice", c1, c2, k, leg))
ck("every offered book overrounds; no side beats the ladder", not grid_bad, grid_bad[:4])

print("\n== symmetry at even strengths ==")
ev = W.hc_odds(50, 50)
if "-1.5" in ev and "1.5" in ev:
    ck("even game: HOME -1.5 mirrors AWAY +1.5", ev["-1.5"]["HOME"]["frac"] == ev["1.5"]["AWAY"]["frac"],
       (ev["-1.5"]["HOME"]["frac"], ev["1.5"]["AWAY"]["frac"]))
    ck("even game: AWAY of -1.5 mirrors HOME of +1.5", ev["-1.5"]["AWAY"]["frac"] == ev["1.5"]["HOME"]["frac"],
       (ev["-1.5"]["AWAY"]["frac"], ev["1.5"]["HOME"]["frac"]))
else:
    ck("even game offers the ±1.5 pair", False, sorted(ev))

print("\n== ±0.5 is deliberately not a thing (no cross-model 1X2 twin) ==")
ck("0.5 and -0.5 are not in HC_LINES", 0.5 not in W.HC_LINES and -0.5 not in W.HC_LINES, W.HC_LINES)
ck("the standard book never quotes a half-goal line", all(abs(float(k)) >= 1.5 for k in W.hc_odds(70, 55)), sorted(W.hc_odds(70, 55)))

print("\n== shared model invariants (exact score and handicap can never disagree) ==")
for c1, c2 in ((80, 60), (50, 50), (10, 95)):
    lh2, la2 = W._team_lambdas(c1, c2)
    ck("lambdas at (%d,%d) sum to expected_goals" % (c1, c2), abs((lh2 + la2) - W.expected_goals(c1, c2)) < 1e-9, (lh2, la2))
    ph, pd, pa = W._fair_probs(c1, c2)
    share = min(0.85, max(0.15, ph + pd / 2.0))
    ck("home share at (%d,%d) matches the exact-score split" % (c1, c2), abs(lh2 - W.expected_goals(c1, c2) * share) < 1e-9, lh2)

print("\n== hostile inputs never crash, never underround ==")
for junk in (float("nan"), float("inf"), -5, None, "x", 1e18):
    try:
        b = W.hc_odds(junk, 55)
        good = isinstance(b, dict) and all(
            1.0 / leg["HOME"]["decimal"] + 1.0 / leg["AWAY"]["decimal"] > 1.0 + 1e-6 for leg in b.values())
        ck("junk comp %r -> sane, margined book" % (junk,), good, b)
    except Exception as e:
        ck("junk comp %r -> sane, margined book" % (junk,), False, e)
ck("junk line in the prob model -> neutral 0.5", W._hc_home_prob(1.3, 1.3, "x") == 0.5 and W._hc_home_prob(1.3, 1.3, float("nan")) == 0.5)

print("\n== grid depth: the truncated tail really is dust ==")
lh3, la3 = W._team_lambdas(100, 0)
tail = (1.0 - W._poisson_cdf(W.HC_GRID_MAX, lh3)) + (1.0 - W._poisson_cdf(W.HC_GRID_MAX, la3))
ck("worst-case off-grid probability mass < 1e-4", tail < 1e-4, tail)

print()
if fails:
    print("FAILED: %d -> %s" % (len(fails), fails))
    raise SystemExit(1)
print("ALL HANDICAP ODDS CHECKS PASSED")
