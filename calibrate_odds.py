#!/usr/bin/env python3
"""
calibrate_odds.py  —  LOCAL, READ-ONLY. Suggests bounded nudges to each team's `composite` in
teams.json so the app's MATCH odds drift toward the bookmaker market — anchored to your existing
(preconceived) numbers, never swinging wildly. Run it after a matchday, eyeball it, apply what you
like by hand, commit, redeploy. Editing composites only ever affects FUTURE bets; bets already
placed keep the odds they were struck at.

It NEVER writes teams.json and NEVER touches the server, the database or any bet. Same design as
compare_odds.py: you stay in the loop. (See the note at the bottom about why this isn't fully auto.)

MARKET SOURCE (required — this tool calibrates TOWARD the market, so it needs one)
  --market FILE       JSON of bookmaker odds (same format audit_match_odds.py uses)
  --odds-api-key K    pull live h2h from the-odds-api.com (median across books)
  --regions uk        bookmaker regions for the API

OPTIONS
  --max-step N        cap how far any composite may move in one run (default 5.0 points)
  --teams teams.json  (default)
  --results FILE / --url URL   optional, only used to show which teams just played

How the nudge works
  For every game with a market price, it inverts the app's own 1X2 model to find the `composite`
  that would reproduce the market's no-vig probability for each team (holding the opponent fixed).
  That's the "market-implied" strength. It then moves your current composite TOWARD it by at most
  --max-step. Over a few matchdays the board converges on the market without lurching on one noisy
  snapshot. The house edge is untouched: composites feed match_odds(), which re-applies the margin.
"""
import argparse, json, sys

import audit_match_odds as AUD           # reuse the loaders, resolver, overround + market helpers
import wager as W

C_MIN, C_MAX = 1.0, 105.0                # search/clamp band for a composite


def implied_composite(target_p, opp_comp, side):
    """Bisect the composite that makes the app's 1X2 model give `target_p` to `side` ('home'/'away'),
    holding the opponent's composite fixed. The model is monotonic in strength, so bisection is exact."""
    lo, hi = C_MIN, C_MAX
    for _ in range(40):
        mid = (lo + hi) / 2.0
        ph, pd, pa = W._fair_probs(mid, opp_comp) if side == "home" else W._fair_probs(opp_comp, mid)
        p = ph if side == "home" else pa
        if p < target_p:
            lo = mid
        else:
            hi = mid
    return max(C_MIN, min(C_MAX, (lo + hi) / 2.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", default="teams.json")
    ap.add_argument("--results", default="results.json")
    ap.add_argument("--url")
    ap.add_argument("--market")
    ap.add_argument("--odds-api-key")
    ap.add_argument("--regions", default="uk")
    ap.add_argument("--max-step", type=float, default=5.0)
    args = ap.parse_args()

    teams = {t["name"]: t for t in AUD._read_json_path(args.teams).get("teams", [])}
    if args.odds_api_key:
        market = AUD.market_from_odds_api(args.odds_api_key, args.regions)
    elif args.market:
        market = AUD._read_json_path(args.market)
    else:
        sys.exit("Give a market to calibrate toward: --market FILE  or  --odds-api-key KEY")
    if not market:
        sys.exit("No market odds loaded — nothing to calibrate against.")

    # which teams just played (only to annotate the table)
    played = set()
    try:
        if args.url:
            fx = AUD._fetch_json(args.url.rstrip("/") + "/tracker_data.json").get("fixtures", [])
        else:
            fx = AUD._read_json_path(args.results).get("matches", [])
        for m in fx:
            if m.get("status") in ("FINISHED", "AWARDED"):
                played.add(AUD._resolve_name(m.get("home", ""))); played.add(AUD._resolve_name(m.get("away", "")))
    except SystemExit:
        pass

    # gather a market-implied composite for each team from every game it appears in
    implied = {}
    for key, rec in market.items():
        h2h = (rec or {}).get("h2h") or {}
        mh, md, ma = h2h.get("home"), h2h.get("draw"), h2h.get("away")
        book = AUD.book_overround([mh, md, ma])
        if not book:
            continue
        if " v " in key:
            hn, an = [s.strip() for s in key.split(" v ", 1)]
        else:
            continue
        hn, an = AUD._resolve_name(hn), AUD._resolve_name(an)
        ch = (teams.get(hn) or {}).get("composite")
        ca = (teams.get(an) or {}).get("composite")
        if ch is None or ca is None:
            continue                                   # unknown team -> skip (don't invent a strength)
        mph, mpa = (1.0 / mh) / book, (1.0 / ma) / book
        implied.setdefault(hn, []).append(implied_composite(mph, ca, "home"))
        implied.setdefault(an, []).append(implied_composite(mpa, ch, "away"))

    if not implied:
        print("No market games matched teams.json — check team-name spellings in the market file.")
        return

    rows = []
    for name, vals in implied.items():
        cur = teams[name]["composite"]
        target = sum(vals) / len(vals)
        step = max(-args.max_step, min(args.max_step, target - cur))
        new = round(max(C_MIN, min(C_MAX, cur + step)), 1)
        rows.append((name, len(vals), cur, target, new, new - cur))
    rows.sort(key=lambda r: -abs(r[5]))

    print("\nWC2026 — composite calibration toward the market   (max move %.1f / run)" % args.max_step)
    print("read-only · review, then edit teams.json by hand · only affects FUTURE bets\n")
    h = "%-26s %5s %9s %14s %9s %8s" % ("TEAM", "GAMES", "CURRENT", "MARKET-IMPLIED", "SUGGEST", "MOVE")
    print(h); print("-" * len(h))
    for name, n, cur, target, new, mv in rows:
        flag = " *" if name in played else ""
        print("%-26s %5d %9.1f %14.1f %9.1f %+8.1f%s" % (name, n, cur, target, new, mv, flag))
    print("\n* = played in the latest results.  MARKET-IMPLIED = the composite that reproduces the")
    print("  market's no-vig price; SUGGEST = your current value nudged toward it, capped at the step.")

    changed = [(n, new) for (n, _, cur, _, new, mv) in rows if abs(mv) >= 0.05]
    if changed:
        print("\nCopy-paste — set these `composite` values in teams.json (only the ones you agree with):")
        for n, new in changed:
            print('  %-26s composite: %.1f' % (n, new))
    print("\nGuards: every move is capped at --max-step and clamped to [%.0f, %.0f]; the house edge is" % (C_MIN, C_MAX))
    print("untouched (composites feed match_odds(), which re-applies the margin). Re-run audit_match_odds.py")
    print("after editing to confirm calibration improved and every market still carries a positive edge.\n")


if __name__ == "__main__":
    main()
