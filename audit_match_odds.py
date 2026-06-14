#!/usr/bin/env python3
"""
audit_match_odds.py  —  LOCAL, READ-ONLY audit of the app's MATCH odds vs real results.

Run it on your Mac after a matchday to sanity-check that the 1X2 + Over/Under odds the
app produces are sensible against what actually happened. It imports wager.py, so it
audits EXACTLY the odds the app offers — no separate model to drift out of sync.

It NEVER writes anything and NEVER touches the server, the database, or any bet. It only
reads teams.json (team strengths) and results.json (final scores) and prints a report.

Usage (from your repo folder):
    python3 audit_match_odds.py                 # base FIFA strengths (clean, deterministic)
    python3 audit_match_odds.py --live          # form-adjusted strengths, using only games
                                                #   played BEFORE each match (out-of-sample)
    python3 audit_match_odds.py --teams teams.json --results results.json

Reading the report
------------------
Per game: the model's fair 1X2 split, which outcome it favoured, whether that happened,
the probability it gave the ACTUAL outcome (low = a surprise/upset), and the Over/Under 2.5
call vs the real goals.

Calibration (the important bit, over all finished games):
  * log-loss / Brier  — lower is better-calibrated (a blind 1/3-1/3-1/3 guess ~1.10 log-loss)
  * favourite hit rate — how often the model's favourite actually won
  * draw / Over-2.5 / avg-goals — predicted rate vs what really happened
A handful of games is noisy; this gets meaningful once a dozen+ are in.
"""
import argparse, json, math, sys

try:
    import wager as W
except Exception as e:
    sys.exit("Could not import wager.py — run this from your repo folder. (%s)" % e)


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        sys.exit("Could not read %s (%s)" % (path, e))


def result_letter(hs, as_):
    return "H" if hs > as_ else ("A" if as_ > hs else "D")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", default="teams.json")
    ap.add_argument("--results", default="results.json")
    ap.add_argument("--live", action="store_true", help="use form-adjusted strengths (out-of-sample) instead of base")
    args = ap.parse_args()

    teams = {t["name"]: t for t in load_json(args.teams).get("teams", [])}
    matches = load_json(args.results).get("matches", [])
    finished = [m for m in matches if m.get("status") in ("FINISHED", "AWARDED")
                and isinstance(m.get("homeScore"), (int, float))
                and isinstance(m.get("awayScore"), (int, float))]

    if not finished:
        print("No finished matches with scores in %s yet." % args.results)
        return

    def strength(team, this_match):
        base = (teams.get(team) or {}).get("composite", 0) or 0
        if not args.live:
            return base, (team in teams)
        # form from FINISHED games that are NOT this match (so we never use a result to predict itself)
        prior = [m for m in finished if m is not this_match]
        return W.live_strength(base, team, prior), (team in teams)

    print("\nWC2026 — match-odds audit (model vs results)   [%s strengths]" % ("form-adjusted" if args.live else "base"))
    print("read-only · audits exactly the odds wager.py produces\n")
    hdr = "%-11s %-32s %-6s %3s %4s   %-22s %-6s %-9s %-9s" % (
        "DATE", "MATCH", "SCORE", "RES", "GLS", "MODEL 1X2 (fair %)", "FAV", "P(actual)", "O/U 2.5")
    print(hdr); print("-" * len(hdr))

    ll = brier = 0.0
    fav_hits = fav_n = 0
    pred_draw = pred_over = pred_lam = 0.0
    act_draw = act_over = act_goals = 0
    missing = set()

    for m in sorted(finished, key=lambda x: x.get("utcDate") or ""):
        h, a = m.get("home", "?"), m.get("away", "?")
        hs, as_ = int(m["homeScore"]), int(m["awayScore"])
        ch, h_ok = strength(h, m)
        ca, a_ok = strength(a, m)
        if not h_ok:
            missing.add(h)
        if not a_ok:
            missing.add(a)

        ph, pd, pa = W._fair_probs(ch, ca)               # the model's true probability estimate (no margin)
        lam = W.expected_goals(ch, ca)
        p_over25 = 1.0 - W._poisson_cdf(2, lam)          # P(3+ goals)

        res = result_letter(hs, as_)
        p_actual = {"H": ph, "D": pd, "A": pa}[res]
        total = hs + as_

        # calibration accumulators
        ll += -math.log(max(1e-9, p_actual))
        brier += (ph - (res == "H")) ** 2 + (pd - (res == "D")) ** 2 + (pa - (res == "A")) ** 2
        fav = max((("H", ph), ("D", pd), ("A", pa)), key=lambda kv: kv[1])[0]
        fav_n += 1
        fav_hits += (fav == res)
        pred_draw += pd; act_draw += (res == "D")
        pred_over += p_over25; act_over += (total > 2.5)
        pred_lam += lam; act_goals += total

        date = (m.get("utcDate") or "")[:10]
        match_lbl = ("%s vs %s" % (h, a))[:32]
        favmark = fav + (" ✓" if fav == res else " ✗")
        ou_lbl = ("Over" if total > 2.5 else "Under") + " (%.0f%% O)" % (p_over25 * 100)
        upset = "  ⚠ upset" if p_actual < 0.22 else ""
        print("%-11s %-32s %-6s %3s %4d   H%2.0f/D%2.0f/A%2.0f%%%10s %-6s %8.2f   %-9s%s" % (
            date, match_lbl, "%d-%d" % (hs, as_), res, total,
            ph * 100, pd * 100, pa * 100, "", favmark, p_actual, ou_lbl, upset))

    n = len(finished)
    print("\nCALIBRATION  (N=%d finished)" % n)
    print("  1X2 log-loss   : %.3f   (lower = better; blind 1/3-each ~1.099, perfect = 0)" % (ll / n))
    print("  1X2 Brier      : %.3f   (lower = better; blind ~0.667)" % (brier / n))
    print("  favourite hit  : %d/%d = %.0f%%   (model's favoured outcome actually happened)" % (fav_hits, fav_n, 100.0 * fav_hits / fav_n))
    print("  draws          : predicted %.0f%%  vs  actual %.0f%%" % (100.0 * pred_draw / n, 100.0 * act_draw / n))
    print("  Over 2.5 goals : predicted %.0f%%  vs  actual %.0f%%" % (100.0 * pred_over / n, 100.0 * act_over / n))
    print("  avg goals/game : model E[goals] %.2f  vs  actual %.2f" % (pred_lam / n, act_goals / n))
    if missing:
        print("\n  note: no strength found in teams.json for: %s (priced as neutral)" % ", ".join(sorted(missing)))
    print("\nA few games is noisy — read the calibration once a dozen+ are in. Big gaps (e.g. predicted")
    print("Over 2.5 ~55%% but actual ~25%%) are the signal to revisit GOALS_BASE; a low favourite-hit")
    print("rate with normal log-loss usually just means upsets, not a broken model.\n")


if __name__ == "__main__":
    main()
