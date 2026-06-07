#!/usr/bin/env python3
"""
compare_odds.py  —  LOCAL, READ-ONLY odds comparison (run on your Mac, never the box).

What it does
------------
Reads your repo's teams.json (the app's odds) and compares the OUTRIGHT/champion
display against the current bookmaker market, then prints:
  * a sorted table (app vs market, with the gap),
  * flags for any team that's materially off or an inverted favourite,
  * copy-paste-ready teams.json field values for anything you choose to change.

It NEVER writes teams.json and NEVER touches the server, the database, or bets.
You review the output and decide what (if anything) to edit by hand. Good cadence:
run it at each stage boundary (when the points budget resets).

IMPORTANT: this only informs the OUTRIGHT/champion DISPLAY (decimal/implied/american).
It does NOT suggest changing `composite`, which drives MATCH-betting odds — those stay
model-derived on purpose. And per the app's design, editing teams.json odds can only
ever affect FUTURE bets + displays; bets already placed store their own odds.

Two ways to give it the market
------------------------------
1) MANUAL (no key, always works): edit MARKET_MANUAL below with current decimal odds
   from oddschecker.com/football/world-cup (takes ~2 min), then:  python3 compare_odds.py

2) AUTO (optional, free key from the-odds-api.com): 
       export ODDS_API_KEY=xxxx
       python3 compare_odds.py --api-key "$ODDS_API_KEY" --regions uk
   If the sport key has changed, list them with:
       curl "https://api.the-odds-api.com/v4/sports/?apiKey=YOURKEY"
   and pass e.g.  --sport soccer_fifa_world_cup_winner
"""
import argparse, json, os, sys, statistics, urllib.request, urllib.error, urllib.parse

# ---------------------------------------------------------------- MANUAL market
# Decimal odds, e.g. 9/2 = 5.5, 5/1 = 6.0. Only list the teams you care about; the
# rest are ignored. Update from oddschecker before a stage-boundary check.
MARKET_MANUAL = {
    "Spain": 5.5, "France": 6.0, "England": 7.5, "Brazil": 9.0, "Argentina": 10.0,
    "Portugal": 9.5, "Germany": 13.0, "Netherlands": 17.0, "Norway": 34.0,
}

# teams.json names vs how books/feeds spell them -> map market names onto app names
ALIASES = {
    "united states": "USA", "usa": "USA", "us": "USA",
    "south korea": "Korea Republic", "korea republic": "Korea Republic", "korea": "Korea Republic",
    "ivory coast": "Côte d'Ivoire", "cote d'ivoire": "Côte d'Ivoire",
    "iran": "IR Iran", "ir iran": "IR Iran",
    "czechia": "Czech Republic", "czech republic": "Czech Republic",
    "turkey": "Türkiye", "turkiye": "Türkiye",
}

FLAG_PP = 0.010   # only flag a team when the champion display would move by >=1.0 percentage point.
                  # (A pure relative % gap is misleading: USA 2.4% vs 1.3% looks like a "92% gap" but is
                  #  only 1.1pp, and a 50/1 longshot moving to 80/1 changes the leaderboard by nothing.)


def implied(dec):
    return 1.0 / dec if dec and dec > 0 else 0.0


def american(dec):
    if not dec or dec <= 1.0:
        return None
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


def canon(name):
    return ALIASES.get(str(name).strip().lower(), str(name).strip())


def load_app(path):
    doc = json.load(open(path))
    out = {}
    for t in doc["teams"]:
        out[t["name"]] = {
            "decimal": t.get("decimal_odds"),
            "implied": t.get("implied_prob") or implied(t.get("decimal_odds")),
            "american": t.get("american_odds"),
            "composite": t.get("composite"),
        }
    return out, doc.get("odds_source", "")


def fetch_api(api_key, sport, regions):
    """Median decimal per team across all bookmakers from The Odds API (stdlib only)."""
    url = ("https://api.the-odds-api.com/v4/sports/%s/odds/?apiKey=%s&regions=%s&markets=outrights&oddsFormat=decimal"
           % (sport, urllib.parse.quote(api_key), regions))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit("Odds API HTTP %s — check the key and the --sport key (list: "
                 "https://api.the-odds-api.com/v4/sports/?apiKey=YOURKEY). Body: %s"
                 % (e.code, e.read()[:200].decode("utf-8", "replace")))
    except Exception as e:
        sys.exit("Odds API request failed: %s" % e)
    prices = {}
    for event in data if isinstance(data, list) else []:
        for bk in event.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk.get("key") != "outrights":
                    continue
                for oc in mk.get("outcomes", []):
                    nm = oc.get("name"); pr = oc.get("price")
                    if nm and isinstance(pr, (int, float)) and pr > 1.0:
                        prices.setdefault(canon(nm), []).append(float(pr))
    if not prices:
        sys.exit("Odds API returned no outright prices — try a different --sport or --regions.")
    return {nm: round(statistics.median(v), 2) for nm, v in prices.items()}


def main():
    ap = argparse.ArgumentParser(description="Compare the app's outright odds to the live market (read-only).")
    ap.add_argument("--teams", default="teams.json", help="path to teams.json (default: ./teams.json)")
    ap.add_argument("--api-key", default=os.environ.get("ODDS_API_KEY"), help="The Odds API key (or set ODDS_API_KEY)")
    ap.add_argument("--sport", default="soccer_fifa_world_cup_winner", help="The Odds API sport key")
    ap.add_argument("--regions", default="uk", help="bookmaker regions: uk, us, eu, au (default: uk)")
    args = ap.parse_args()

    if not os.path.exists(args.teams):
        sys.exit("Can't find %s — run this from your repo folder (where teams.json lives)." % args.teams)

    app, src = load_app(args.teams)
    if args.api_key:
        market_raw = fetch_api(args.api_key, args.sport, args.regions)
        mode = "The Odds API (%s, median across books)" % args.regions
    else:
        market_raw = {canon(k): float(v) for k, v in MARKET_MANUAL.items()}
        mode = "MANUAL list in compare_odds.py (edit MARKET_MANUAL to refresh)"

    # match market names onto app names
    market, unmatched = {}, []
    for nm, dec in market_raw.items():
        key = canon(nm)
        if key in app:
            market[key] = dec
        else:
            hit = next((a for a in app if a.lower() == key.lower()), None)
            (market.__setitem__(hit, dec) if hit else unmatched.append(nm))

    print("App odds source : %s" % (src or "?"))
    print("Market source   : %s" % mode)
    if unmatched:
        print("Unmatched market names (ignored): %s" % ", ".join(sorted(unmatched)))
    print()

    rows = []
    for nm, dec in market.items():
        a = app.get(nm, {})
        a_imp, m_imp = a.get("implied") or 0.0, implied(dec)
        rows.append((nm, a.get("decimal"), a_imp, dec, m_imp, abs(a_imp - m_imp)))
    rows.sort(key=lambda r: -r[4])  # by market implied prob (most likely champions first)

    print("Flagged (<-- review) = the champion DISPLAY would move by >=%.1fpp. Longshot noise is ignored." % (100 * FLAG_PP))
    print("%-18s %8s %8s   %8s %8s   %7s" % ("team", "app dec", "app %", "mkt dec", "mkt %", "diff"))
    print("-" * 72)
    flagged = []
    for nm, ad, ai, md, mi, pp in rows:
        material = pp >= FLAG_PP
        mark = "  <-- review" if material else ""
        if material:
            flagged.append((nm, md, pp))
        ad_s = ("%.1f" % ad) if isinstance(ad, (int, float)) else "—"
        print("%-18s %8s %7.1f%%   %8.1f %7.1f%%   %5.1fpp%s" % (nm, ad_s, 100 * ai, md, 100 * mi, 100 * pp, mark))

    # favourite-order sanity (an inverted favourite is always worth knowing, regardless of size)
    app_fav = min((n for n in market if app.get(n, {}).get("decimal")), key=lambda n: app[n]["decimal"], default=None)
    mkt_fav = min(market, key=market.get, default=None)
    print()
    if app_fav and mkt_fav:
        if app_fav == mkt_fav:
            print("Favourite: app and market agree on %s." % mkt_fav)
        else:
            print("Favourite MISMATCH: app shortest = %s, market shortest = %s  <-- review" % (app_fav, mkt_fav))
            for f in (mkt_fav, app_fav):
                if f not in [x[0] for x in flagged]:
                    flagged.append((f, market[f], abs((app.get(f, {}).get("implied") or 0.0) - implied(market[f]))))

    flagged.sort(key=lambda x: -x[2])   # biggest mover first
    if flagged:
        print("\n%d team(s) materially off. If you want to match the market, set these three fields in teams.json" % len(flagged))
        print("(OUTRIGHT display only — do NOT change `composite`; match-betting odds and bets already placed are unaffected.")
        print(" It is completely fine to leave these — the champion board is cosmetic):")
        for nm, dec, pp in flagged:
            print('  %-18s (%+.1fpp) -> "decimal_odds": %s, "implied_prob": %.4f, "american_odds": %s'
                  % (nm, 100 * pp, dec, round(implied(dec), 4), american(dec)))
    else:
        print("\nNothing moves the board by >=%.1fpp — the app's outright odds track the market. No change needed." % (100 * FLAG_PP))

    print("\n(Read-only: this script changed nothing. Edit teams.json by hand if you choose to.)")


if __name__ == "__main__":
    main()
