"""
Fetch results from football-data.org and write the normalised results.json.

Key safety: the token is read from the FOOTBALL_DATA_TOKEN environment variable,
NEVER hard-coded. On GitHub it comes from a repo Secret (see README).

Reusable: set COMPETITION = "EC" for the Euros, etc. (same free tier).
"""
import json
import os
import sys
import unicodedata
import urllib.request

COMPETITION = os.environ.get("COMPETITION", "WC")
BASE = "https://api.football-data.org/v4"

# football-data.org name -> our canonical name (teams.json). Only the differences.
ALIASES = {
    "United States": "USA", "South Korea": "Korea Republic", "IR Iran": "Iran",
    "Türkiye": "Turkey", "Turkey": "Turkey", "Czechia": "Czech Republic",
    "Côte d'Ivoire": "Ivory Coast", "Cabo Verde": "Cape Verde",
    "Curaçao": "Curacao", "DR Congo": "DR Congo", "Congo DR": "DR Congo",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina",
}


def _norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.lower().replace("&", "and").replace(".", "").strip()


def build_name_map(teams_path="teams.json"):
    canon = [t["name"] for t in json.load(open(teams_path))["teams"]]
    norm_to_canon = {_norm(c): c for c in canon}
    def resolve(api_name):
        if api_name in ALIASES:
            return ALIASES[api_name]
        return norm_to_canon.get(_norm(api_name), api_name)   # fall back to raw if unknown
    return resolve


def normalize_matches(api_matches, resolve):
    out = []
    for m in api_matches:
        h = resolve(m["homeTeam"]["name"] or "")
        a = resolve(m["awayTeam"]["name"] or "")
        score = m.get("score", {}) or {}
        ft = score.get("fullTime", {}) or {}
        rt = score.get("regularTime", {}) or {}
        et = score.get("extraTime", {}) or {}
        pen = score.get("penalties", {}) or {}
        duration = score.get("duration", "REGULAR")
        winner = {"HOME_TEAM": "HOME", "AWAY_TEAM": "AWAY", "DRAW": "DRAW"}.get(score.get("winner"))

        # v4 quirk: score/fullTime INCLUDES extra-time AND penalty-shootout goals. For our points and
        # the on-screen match score we want the real on-field goals (90' + ET, excluding the shootout).
        # Prefer regularTime+extraTime (unambiguous); fall back to fullTime - penalties; else fullTime.
        def on_field(side):
            if rt.get(side) is not None:
                return rt.get(side, 0) + (et.get(side) or 0)
            if pen.get(side) is not None and ft.get(side) is not None:
                return ft.get(side) - pen.get(side, 0)
            return ft.get(side)

        out.append({
            "id": m["id"], "stage": m.get("stage", "GROUP_STAGE"),
            "group": ((m.get("group") or "").replace("GROUP_", "").strip() or None), "utcDate": m.get("utcDate"),
            "status": m.get("status", "SCHEDULED"),
            "home": h, "away": a,
            "homeScore": on_field("home"), "awayScore": on_field("away"),   # goals only (no shootout)
            "winner": winner,                                               # already reflects the shootout result
            "minute": m.get("minute"),          # populated for live matches on paid (live) tiers
            "duration": duration,
            "aet": duration in ("EXTRA_TIME", "PENALTY_SHOOTOUT"),          # went to extra time
            "shootout": duration == "PENALTY_SHOOTOUT",
            "penHome": pen.get("home"), "penAway": pen.get("away"),         # shootout score, if any
        })
    return out


def normalize_standings(api_standings, resolve):
    out = []
    for s in api_standings:
        if s.get("type") not in (None, "TOTAL"):
            continue
        table = [{
            "position": r["position"], "team": resolve(r["team"]["name"]),
            "playedGames": r["playedGames"], "won": r["won"], "draw": r["draw"],
            "lost": r["lost"], "goalsFor": r["goalsFor"], "goalsAgainst": r["goalsAgainst"],
            "goalDifference": r["goalDifference"], "points": r["points"],
        } for r in s["table"]]
        grp = (s.get("group") or "").replace("GROUP_", "") or None
        out.append({"group": grp, "table": table})
    return out


def unmatched_names(api_names, resolve, canon):
    """Names from the feed whose resolved value isn't a canonical team."""
    return sorted({n for n in api_names if resolve(n) not in canon})


def audit(token=None, teams_path="teams.json"):
    """Dry-run: fetch the feed and report team names that don't map to teams.json."""
    token = token or os.environ.get("FOOTBALL_DATA_TOKEN")
    if not token:
        sys.exit("Set FOOTBALL_DATA_TOKEN first.")
    resolve = build_name_map(teams_path)
    canon = {t["name"] for t in json.load(open(teams_path))["teams"]}
    matches = _get(f"/competitions/{COMPETITION}/matches", token).get("matches", [])
    api_names = {m[s]["name"] for m in matches for s in ("homeTeam", "awayTeam") if m[s].get("name")}
    bad = unmatched_names(api_names, resolve, canon)
    if not bad:
        print(f"All {len(api_names)} feed names map cleanly to teams.json.")
    else:
        print(f"{len(bad)} unmatched name(s) - add to ALIASES in update_results.py:")
        for n in bad:
            print(f'    "{n}": "<canonical name>",')
    return bad


def _get(path, token):
    req = urllib.request.Request(f"{BASE}{path}", headers={"X-Auth-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch(out="results.json", token=None):
    token = token or os.environ.get("FOOTBALL_DATA_TOKEN")
    if not token:
        sys.exit("Set FOOTBALL_DATA_TOKEN (env var / GitHub Secret) first.")
    resolve = build_name_map()
    matches = _get(f"/competitions/{COMPETITION}/matches", token).get("matches", [])
    try:
        standings = _get(f"/competitions/{COMPETITION}/standings", token).get("standings", [])
    except Exception:
        standings = []          # standings 404 before group stage starts — fine
    data = {"competition": COMPETITION,
            "matches": normalize_matches(matches, resolve),
            "standings": normalize_standings(standings, resolve)}
    with open(out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {out}: {len(data['matches'])} matches, {len(data['standings'])} groups")
    return data


if __name__ == "__main__":
    if "--check" in sys.argv:
        audit()
    else:
        fetch()
