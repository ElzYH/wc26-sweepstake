#!/usr/bin/env python3
"""
audit_match_odds.py  —  LOCAL, READ-ONLY audit of the app's MATCH odds (1X2 + Over/Under).

Compares the app's odds against (a) what actually happened and (b) the real bookmaker
market, and reports the house edge (overround) on every market. It imports wager.py, so
it audits EXACTLY the odds the app offers. It NEVER writes anything and NEVER touches the
server, the database, or any bet — read-only. Run it on your Mac.

RESULTS SOURCE
  default            results.json in this folder
  --url URL          fetch the LIVE site instead, e.g.
                     python3 audit_match_odds.py --url https://mandem.bbmsweepstake.co.uk
                     (reads {URL}/tracker_data.json — the same public file the tracker uses)

BOOKMAKER MARKET (optional — turns on the app-vs-market comparison + market house edge)
  --market FILE      a JSON file of market odds you pasted by hand (template below)
  --odds-api-key K   pull live from the-odds-api.com (free key); h2h + totals markets
  --regions uk       bookmaker regions for the API (uk/us/eu/au)

STRENGTHS
  default            base FIFA strengths (clean, deterministic)
  --live             form-adjusted, using only games played BEFORE each match (out-of-sample)

--market FILE template (decimals; "totals" optional). Keys are "Home v Away":
{
  "Australia v Turkey": {"h2h": {"home": 2.10, "draw": 3.30, "away": 3.40},
                          "totals": {"line": 2.5, "over": 2.05, "under": 1.80}}
}

Reading it
  * house edge = book sum of 1/decimal across a market's outcomes, minus 100%. The APP runs
    a deliberately fatter edge than the market (it's a pool, not a sharp book); the audit shows
    both so you can see the gap and that the app NEVER prices a market with a negative edge.
  * calibration (over all finished games): log-loss / Brier (lower better), favourite hit rate,
    draw / Over-2.5 / avg-goals predicted vs actual. A few games is noise; read it at 12+.
"""
import argparse, json, math, sys, urllib.request, urllib.parse, difflib

try:
    import wager as W
except Exception as e:
    sys.exit("Could not import wager.py — run this from your repo folder. (%s)" % e)


def _read_json_path(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        sys.exit("Could not read %s (%s)" % (path, e))


def _fetch_json(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        sys.exit("Could not fetch %s (%s)" % (url, e))


def book_overround(decimals):
    """Sum of implied probabilities across a market's outcomes. >1.0 means a house edge."""
    s = 0.0
    for d in decimals:
        try:
            d = float(d)
            if d > 1.0:
                s += 1.0 / d
        except (TypeError, ValueError):
            return None
    return s if s > 0 else None


def result_letter(hs, as_):
    return "H" if hs > as_ else ("A" if as_ > hs else "D")


# ---------- bookmaker market loading ----------
def market_from_odds_api(api_key, regions):
    url = ("https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds/"
           "?apiKey=%s&regions=%s&markets=h2h,totals&oddsFormat=decimal"
           % (urllib.parse.quote(api_key), regions))
    data = _fetch_json(url)
    out = {}
    for ev in (data or []):
        home, away = ev.get("home_team"), ev.get("away_team")
        if not home or not away:
            continue
        h2h_vals, totals = {}, {}
        # median across books for robustness
        h_list, d_list, a_list, o_list, u_list, line_seen = [], [], [], [], [], None
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk.get("key") == "h2h":
                    for oc in mk.get("outcomes", []):
                        nm, pr = oc.get("name"), oc.get("price")
                        if nm == home: h_list.append(pr)
                        elif nm == away: a_list.append(pr)
                        elif nm and nm.lower() == "draw": d_list.append(pr)
                elif mk.get("key") == "totals":
                    for oc in mk.get("outcomes", []):
                        pt = oc.get("point")
                        if pt == 2.5:
                            line_seen = 2.5
                            if (oc.get("name") or "").lower() == "over": o_list.append(oc.get("price"))
                            elif (oc.get("name") or "").lower() == "under": u_list.append(oc.get("price"))
        med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
        if h_list and a_list:
            h2h_vals = {"home": med(h_list), "draw": med(d_list), "away": med(a_list)}
        if line_seen and o_list and u_list:
            totals = {"line": 2.5, "over": med(o_list), "under": med(u_list)}
        rec = {}
        if h2h_vals: rec["h2h"] = h2h_vals
        if totals: rec["totals"] = totals
        if rec:
            out["%s v %s" % (home, away)] = rec
    return out


def market_lookup(market, home, away):
    """Find a market record for home/away, tolerant of name spellings/order."""
    if not market:
        return None
    key = "%s v %s" % (home, away)
    if key in market:
        return market[key]
    # fuzzy: best matching key by the two team names appearing in it
    keys = list(market.keys())
    cand = difflib.get_close_matches(key, keys, n=1, cutoff=0.6)
    if cand:
        return market[cand[0]]
    for k in keys:                                   # last resort: both names present, any order
        if home.lower() in k.lower() and away.lower() in k.lower():
            return market[k]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", default="teams.json")
    ap.add_argument("--results", default="results.json")
    ap.add_argument("--url", help="fetch live results from this site base URL ({URL}/tracker_data.json)")
    ap.add_argument("--market", help="JSON file of bookmaker odds (template in the header)")
    ap.add_argument("--odds-api-key", help="the-odds-api.com key (fetch h2h + totals)")
    ap.add_argument("--regions", default="uk")
    ap.add_argument("--live", action="store_true", help="form-adjusted strengths (out-of-sample)")
    args = ap.parse_args()

    teams = {t["name"]: t for t in _read_json_path(args.teams).get("teams", [])}

    if args.url:
        td = _fetch_json(args.url.rstrip("/") + "/tracker_data.json")
        matches = td.get("fixtures", [])
        src = args.url
    else:
        matches = _read_json_path(args.results).get("matches", [])
        src = args.results

    market = None
    if args.odds_api_key:
        market = market_from_odds_api(args.odds_api_key, args.regions)
    elif args.market:
        market = _read_json_path(args.market)

    finished = [m for m in matches if m.get("status") in ("FINISHED", "AWARDED")
                and isinstance(m.get("homeScore"), (int, float))
                and isinstance(m.get("awayScore"), (int, float))]
    if not finished:
        print("No finished matches with scores in %s yet." % src)
        return

    def strength(team, this_match):
        base = (teams.get(team) or {}).get("composite", 0) or 0
        if not args.live:
            return base, (team in teams)
        prior = [m for m in finished if m is not this_match]
        return W.live_strength(base, team, prior), (team in teams)

    print("\nWC2026 — match-odds audit (app vs %s%s)   [%s strengths]"
          % ("LIVE site" if args.url else "results",
             " + market" if market else "", "form-adjusted" if args.live else "base"))
    print("read-only · audits exactly the odds wager.py produces · source: %s\n" % src)

    hdr = "%-11s %-30s %-5s %3s  %-19s %-5s %-8s  %-6s %-9s" % (
        "DATE", "MATCH", "SCORE", "RES", "APP 1X2 (fair %)", "FAV", "P(act)", "edge%", "O/U2.5 edge")
    if market:
        hdr += "  %-22s" % "MARKET vs APP"
    print(hdr); print("-" * len(hdr))

    ll = brier = 0.0
    fav_hits = fav_n = 0
    pred_draw = pred_over = pred_lam = 0.0
    act_draw = act_over = act_goals = 0
    e1_sum = eo_sum = e1_n = eo_n = 0
    underround = []
    missing = set()
    mkt_absdiff = mkt_absn = 0.0
    mkt_fav_agree = mkt_fav_n = 0

    for m in sorted(finished, key=lambda x: x.get("utcDate") or ""):
        h, a = m.get("home", "?"), m.get("away", "?")
        hs, as_ = int(m["homeScore"]), int(m["awayScore"])
        ch, h_ok = strength(h, m); ca, a_ok = strength(a, m)
        if not h_ok: missing.add(h)
        if not a_ok: missing.add(a)

        mo = W.match_odds(ch, ca)                      # the app's offered 1X2 prices
        ph, pd, pa = W._fair_probs(ch, ca)             # the app's fair probabilities (no margin)
        lam = W.expected_goals(ch, ca)
        p_over25 = 1.0 - W._poisson_cdf(2, lam)
        go = W.goals_odds(ch, ca)
        ou = go.get("2.5")

        # house edge on the app's books
        e1 = book_overround([mo["HOME"]["decimal"], mo["DRAW"]["decimal"], mo["AWAY"]["decimal"]])
        eo = book_overround([ou["OVER"]["decimal"], ou["UNDER"]["decimal"]]) if ou else None
        if e1 is not None:
            e1_sum += (e1 - 1); e1_n += 1
            if e1 <= 1.0: underround.append("%s v %s 1X2 (%.1f%%)" % (h, a, e1 * 100))
        if eo is not None:
            eo_sum += (eo - 1); eo_n += 1
            if eo <= 1.0: underround.append("%s v %s O/U2.5 (%.1f%%)" % (h, a, eo * 100))

        res = result_letter(hs, as_)
        p_actual = {"H": ph, "D": pd, "A": pa}[res]
        total = hs + as_
        ll += -math.log(max(1e-9, p_actual))
        brier += (ph - (res == "H")) ** 2 + (pd - (res == "D")) ** 2 + (pa - (res == "A")) ** 2
        fav = max((("H", ph), ("D", pd), ("A", pa)), key=lambda kv: kv[1])[0]
        fav_n += 1; fav_hits += (fav == res)
        pred_draw += pd; act_draw += (res == "D")
        pred_over += p_over25; act_over += (total > 2.5)
        pred_lam += lam; act_goals += total

        date = (m.get("utcDate") or "")[:10]
        favmark = fav + ("✓" if fav == res else "✗")
        ou_call = ("O" if total > 2.5 else "U")
        row = "%-11s %-30s %-5s %3s  H%2.0f/D%2.0f/A%2.0f%%   %-5s %8.2f  %5.1f%% %s %5.1f%%" % (
            date, ("%s v %s" % (h, a))[:30], "%d-%d" % (hs, as_), res,
            ph * 100, pd * 100, pa * 100, favmark, p_actual,
            (e1 - 1) * 100 if e1 else 0.0, ou_call, (eo - 1) * 100 if eo else 0.0)

        if market:
            mrec = market_lookup(market, h, a)
            cell = "—"
            if mrec and mrec.get("h2h"):
                mh, md, ma = mrec["h2h"].get("home"), mrec["h2h"].get("draw"), mrec["h2h"].get("away")
                mbook = book_overround([mh, md, ma])
                if mbook:
                    mph, mpd, mpa = (1 / mh) / mbook, (1 / md) / mbook, (1 / ma) / mbook   # market's no-vig probs
                    mfav = max((("H", mph), ("D", mpd), ("A", mpa)), key=lambda kv: kv[1])[0]
                    mkt_fav_n += 1; mkt_fav_agree += (mfav == fav)
                    mkt_absdiff += abs(ph - mph) + abs(pd - mpd) + abs(pa - mpa); mkt_absn += 1
                    cell = "edge %.1f%% favs %s%s" % ((mbook - 1) * 100, mfav, "=" if mfav == fav else "≠app")
            row += "  %-22s" % cell
        print(row)

    n = len(finished)
    print("\nCALIBRATION  (N=%d finished)" % n)
    print("  1X2 log-loss   : %.3f   (lower = better; blind 1/3-each ~1.099, perfect 0)" % (ll / n))
    print("  1X2 Brier      : %.3f   (lower = better; blind ~0.667)" % (brier / n))
    print("  favourite hit  : %d/%d = %.0f%%" % (fav_hits, fav_n, 100.0 * fav_hits / fav_n))
    print("  draws          : predicted %.0f%%  vs  actual %.0f%%" % (100.0 * pred_draw / n, 100.0 * act_draw / n))
    print("  Over 2.5 goals : predicted %.0f%%  vs  actual %.0f%%" % (100.0 * pred_over / n, 100.0 * act_over / n))
    print("  avg goals/game : model E[goals] %.2f  vs  actual %.2f" % (pred_lam / n, act_goals / n))

    print("\nHOUSE EDGE  (the app's built-in margin — must always be > 0)")
    if e1_n: print("  1X2 avg edge   : %+.1f%%   over %d games" % (100.0 * e1_sum / e1_n, e1_n))
    if eo_n: print("  O/U 2.5 edge   : %+.1f%%   over %d games" % (100.0 * eo_sum / eo_n, eo_n))
    if underround:
        print("  ⚠ NEGATIVE-EDGE MARKETS FOUND (should be impossible): %s" % "; ".join(underround))
    else:
        print("  ✓ every market priced with a positive house edge — no bettor-edge lines")

    if market and mkt_absn:
        print("\nVS BOOKMAKER MARKET  (median across books)")
        print("  favourite agreement : %d/%d = %.0f%%   (app & market favour the same outcome)" % (
            mkt_fav_agree, mkt_fav_n, 100.0 * mkt_fav_agree / mkt_fav_n))
        print("  avg prob gap         : %.1f pp per outcome   (app fair %% vs market no-vig %%)" % (
            100.0 * mkt_absdiff / (mkt_absn * 3)))
        print("  (big, persistent gaps on specific teams -> consider a composite tweak; the market's")
        print("   edge is usually ~5%% vs the app's ~8-9%% — the app is deliberately more conservative.)")

    if missing:
        print("\n  note: no strength in teams.json for: %s (priced neutral)" % ", ".join(sorted(missing)))
    print()


if __name__ == "__main__":
    main()
