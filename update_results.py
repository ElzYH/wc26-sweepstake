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
import time
import urllib.request

COMPETITION = os.environ.get("COMPETITION", "WC")
BASE = "https://api.football-data.org/v4"

# football-data.org name -> our canonical name (teams.json). Only the differences.
ALIASES = {
    "United States": "USA", "South Korea": "Korea Republic", "IR Iran": "Iran",
    "Türkiye": "Turkey", "Turkey": "Turkey", "Czechia": "Czech Republic",
    "Côte d'Ivoire": "Ivory Coast", "Cabo Verde": "Cape Verde", "Cape Verde Islands": "Cape Verde",
    "Curaçao": "Curacao", "DR Congo": "DR Congo", "Congo DR": "DR Congo",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina", "Bosnia-Herzegovina": "Bosnia & Herzegovina",
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


def _scorers(m, h, a, resolve):
    """Deep-data goals[] -> a compact scorer list, or None when the feed carries no scorers at all.
    An EMPTY goals array from the list endpoint is treated as absence (some tiers ship goals: [] on
    every row) unless a detail fetch confirmed this match — then empty genuinely means no goals.
    Own goals are attributed to the team CREDITED with the goal (that's what the score shows)."""
    if not isinstance(m.get("goals"), list):
        return None
    if not m["goals"] and not m.get("_deepConfirmed"):
        return None
    out = []
    for g in m["goals"][:40]:
        if not isinstance(g, dict):
            continue
        name = ((g.get("scorer") or {}).get("name")) or ""
        team = resolve(((g.get("team") or {}).get("name")) or "")
        side = "HOME" if team == h else ("AWAY" if team == a else None)
        if not name or side is None:
            continue
        out.append({"minute": g.get("minute"), "team": side, "player": name,
                    "type": (g.get("type") or "REGULAR")})
    return out


def _lineup(team_obj):
    """Deep-data starting XI -> [{name, position, shirtNumber}], or None when the tier has no line-ups."""
    lu = (team_obj or {}).get("lineup")
    if not isinstance(lu, list) or not lu:
        return None
    out = []
    for p in lu[:11]:
        if isinstance(p, dict) and p.get("name"):
            out.append({"name": p.get("name"), "position": p.get("position"),
                        "shirtNumber": p.get("shirtNumber")})
    return out or None


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
        duration_known = ("duration" in score) or ("regularTime" in score) or ("penalties" in score)
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

        # Deep-data tiers include a bookings[] array (each: {minute, team, card}). Cards basis is the
        # bookmaker-standard 90 MINUTES: extra-time bookings (minute > 90) don't count; a missing minute
        # counts (conservative). Every booking = 1 card regardless of colour. cardsHome/cardsAway stay
        # None when the payload has no bookings key at all — settlement must tell "no data on this plan"
        # (leave pending, void at FT) apart from "genuinely zero cards" (an empty list).
        _confirmed = bool(m.get("_deepConfirmed"))
        cards_h = cards_a = None
        reds_h = reds_a = 0
        card_events = None
        if isinstance(m.get("bookings"), list) and (m["bookings"] or _confirmed):
            cards_h = cards_a = 0
            card_events = []
            for bk in m["bookings"][:40]:
                if not isinstance(bk, dict):
                    continue
                minute = bk.get("minute")
                t = ((bk.get("team") or {}).get("name")) or ""
                side = "HOME" if resolve(t) == h else ("AWAY" if resolve(t) == a else None)
                is_red = (bk.get("card") or "").upper() in ("RED", "YELLOW_RED", "RED_CARD", "SECOND_YELLOW")
                if side:                                                    # the timeline shows EVERY booking (ET incl.)
                    card_events.append({"minute": minute, "team": side, "red": is_red,
                                        "player": ((bk.get("player") or {}).get("name")) or None})
                if isinstance(minute, (int, float)) and minute > 90:
                    continue                                                # the BETTING count stays 90' only
                if side == "HOME":
                    cards_h += 1
                    reds_h += 1 if is_red else 0
                elif side == "AWAY":
                    cards_a += 1
                    reds_a += 1 if is_red else 0

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
            "cardsHome": cards_h, "cardsAway": cards_a,                     # 90' bookings count (None = feed has none)
            "redHome": reds_h, "redAway": reds_a,                           # red / second-yellow count within 90'
            "durationKnown": duration_known,                                # False on bare free-tier payloads ->
                                                                            #   method-of-victory won't guess REG vs ET
            "scorers": _scorers(m, h, a, resolve),                          # [{minute, team: HOME/AWAY, player}] or None
            "cardEvents": card_events,                                      # every booking with minute (timeline)
            "deepChecked": _confirmed,                                      # detail fetched at least once for this game
            "homeLineup": _lineup(m.get("homeTeam")),                       # [{name, position, shirtNumber}] or None
            "awayLineup": _lineup(m.get("awayTeam")),
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


def _resolve_token(token=None):
    """football-data token from: explicit arg -> $FOOTBALL_DATA_TOKEN -> config.json's "token" (so CLI tools
    like --check just work inside the repo folder). Returns None if none found."""
    if token:
        return token
    t = os.environ.get("FOOTBALL_DATA_TOKEN")
    if t:
        return t
    try:
        return (json.load(open("config.json")).get("token") or "").strip() or None
    except Exception:
        return None


def audit(token=None, teams_path="teams.json"):
    """Dry-run: fetch the feed and report team names that don't map to teams.json."""
    token = _resolve_token(token)
    if not token:
        sys.exit("No football-data token. Set $FOOTBALL_DATA_TOKEN, or run inside the repo folder where config.json has it.")
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


DETAIL_WINDOW_BEFORE_S = 80 * 60      # line-ups publish ~1h before kickoff
DETAIL_WINDOW_AFTER_S = 4 * 3600      # bookings/scorers keep updating a while after FT
DETAIL_BUDGET_PER_POLL = 8            # never more than this many /matches/{id} calls per poll (30/min plan cap)
BACKFILL_BUDGET_PER_POLL = 3          # old FINISHED games get their scorers/cards backfilled a few per poll


DEEP_STATS = {"enriched": 0, "backfilled": 0, "errors": 0, "last_error": None}


def _note_err(e):
    DEEP_STATS["errors"] += 1
    DEEP_STATS["last_error"] = "%s: %s" % (type(e).__name__, str(e)[:200])


def _enrich_near_live(api_matches, token):
    """For matches near kickoff / live / just finished, fetch the match DETAIL and merge the deep-data
    blocks (bookings, goals/scorers, line-ups) the LIST endpoint may not carry. Every part is optional:
    on the free tier the detail simply has none of it and the merge is a no-op; any error skips that
    match. Mutates api_matches in place. Hard-budgeted per poll to respect the plan's rate limit."""
    import calendar as _cal
    now = time.time() if hasattr(time, "time") else 0
    budget = DETAIL_BUDGET_PER_POLL
    for m in api_matches:
        if budget <= 0:
            break
        try:
            iso = (m.get("utcDate") or "")[:19]
            ko = _cal.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%S")) if iso else None
        except Exception:
            ko = None
        near = ko is not None and (ko - DETAIL_WINDOW_BEFORE_S) <= now <= (ko + DETAIL_WINDOW_AFTER_S)
        live = m.get("status") in ("IN_PLAY", "PAUSED", "LIVE", "SUSPENDED")
        if not (near or live):
            continue
        try:
            detail = _get("/matches/%s" % m.get("id"), token)
            detail = detail.get("match", detail) or {}
            for k in ("bookings", "goals", "substitutions", "referees"):
                if isinstance(detail.get(k), list):
                    m[k] = detail[k]
            for side in ("homeTeam", "awayTeam"):
                d = detail.get(side) or {}
                if isinstance(d.get("lineup"), list) and d.get("lineup"):
                    m.setdefault(side, {}).update({"lineup": d.get("lineup"), "bench": d.get("bench"),
                                                   "formation": d.get("formation"), "coach": d.get("coach")})
            if isinstance(detail.get("score"), dict):        # detail score is at least as fresh as the list's
                m["score"] = detail["score"]
            m["_deepConfirmed"] = True                        # detail seen: empty arrays now MEAN empty
            budget -= 1
            DEEP_STATS["enriched"] += 1
        except Exception as e:
            _note_err(e)
            continue                                          # a detail blip never blocks the whole poll
    return api_matches


def _carry_deep_fields(new_matches, prev_path):
    """Detail-fetched extras (scorers, line-ups, cards) don't come back on the LIST endpoint, so a fresh
    poll would wipe them — carry any field the new payload LACKS over from the previous results.json.
    Fresh data always wins; this only fills Nones. Never raises."""
    try:
        prev = {str(m.get("id")): m for m in (json.load(open(prev_path)).get("matches") or []) if isinstance(m, dict)}
    except Exception:
        return
    for m in new_matches:
        old = prev.get(str(m.get("id")))
        if not old:
            continue
        for k in ("scorers", "homeLineup", "awayLineup", "cardsHome", "cardsAway", "redHome", "redAway", "cardEvents"):
            if m.get(k) is None and old.get(k) is not None:
                m[k] = old[k]
        if old.get("deepChecked") and not m.get("deepChecked"):
            m["deepChecked"] = True                 # one detail fetch per game, EVER — the flag survives polls
        if not m.get("durationKnown") and old.get("durationKnown"):
            m["durationKnown"] = True
            for k in ("duration", "aet", "shootout"):
                if old.get(k) is not None:
                    m[k] = old[k]


def _backfill_finished(api_matches, norm_prev, token):
    """A few FINISHED games per poll that still lack a scorer timeline get their detail fetched — over the
    polls the whole tournament's timelines fill in. norm_prev: previous normalised matches by id (so games
    already backfilled aren't re-fetched)."""
    budget = BACKFILL_BUDGET_PER_POLL
    for m in api_matches:
        if budget <= 0:
            break
        if m.get("status") not in ("FINISHED", "AWARDED"):
            continue
        pv = norm_prev.get(str(m.get("id"))) or {}
        if pv.get("deepChecked"):
            continue                                # already detail-fetched once — never again
        have_goals = isinstance(m.get("goals"), list) and m["goals"]        # an EMPTY list is not "in hand"
        have_cards = isinstance(m.get("bookings"), list) and m["bookings"]  #   (some tiers ship [] on every row)
        if have_goals and have_cards:
            continue                                # the list genuinely carries this game's deep data
        try:
            detail = _get("/matches/%s" % m.get("id"), token)
            detail = detail.get("match", detail) or {}
            for k in ("bookings", "goals"):
                if isinstance(detail.get(k), list):
                    m[k] = detail[k]
            if isinstance(detail.get("score"), dict):
                m["score"] = detail["score"]
            m["_deepConfirmed"] = True
            budget -= 1
            DEEP_STATS["backfilled"] += 1
        except Exception as e:
            _note_err(e)
            continue


def fetch(out="results.json", token=None):
    token = _resolve_token(token)
    if not token:
        sys.exit("Set FOOTBALL_DATA_TOKEN (env var / GitHub Secret) first.")
    resolve = build_name_map()
    matches = _get(f"/competitions/{COMPETITION}/matches", token).get("matches", [])
    try:
        _enrich_near_live(matches, token)
    except Exception:
        pass                        # enrichment is a bonus layer — the base list always stands alone
    _prev_path = "results.json" if os.path.exists("results.json") else out   # update_now writes to a tmp file
    try:
        _prev_norm = {}
        if os.path.exists(_prev_path):
            _prev_norm = {str(x.get("id")): x for x in (json.load(open(_prev_path)).get("matches") or []) if isinstance(x, dict)}
        _backfill_finished(matches, _prev_norm, token)
    except Exception:
        pass
    try:
        standings = _get(f"/competitions/{COMPETITION}/standings", token).get("standings", [])
    except Exception:
        standings = []          # standings 404 before group stage starts — fine
    _norm = normalize_matches(matches, resolve)
    _carry_deep_fields(_norm, _prev_path)           # keep earlier detail merges alive across polls
    data = {"competition": COMPETITION,
            "matches": _norm,
            "standings": normalize_standings(standings, resolve)}
    with open(out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {out}: {len(data['matches'])} matches, {len(data['standings'])} groups")
    ds = DEEP_STATS
    print("deep-data: enriched %d, backfilled %d, errors %d%s" % (
        ds["enriched"], ds["backfilled"], ds["errors"],
        (" — last: " + ds["last_error"]) if ds["last_error"] else ""))
    return data


def diag(token=None):
    """Run on the box: python3 update_results.py diag — proves what THIS token/plan actually returns.
    Checks the list payload for deep fields, then fetches ONE finished match's detail and reports which
    blocks (goals / bookings / lineups) come back, or the exact HTTP error if the plan refuses."""
    token = _resolve_token(token)
    if not token:
        print("no token set"); return
    ms = _get(f"/competitions/{COMPETITION}/matches", token).get("matches", [])
    fin = [m for m in ms if m.get("status") in ("FINISHED", "AWARDED")]
    print("list: %d matches, %d finished" % (len(ms), len(fin)))
    if fin:
        sample = fin[-1]
        print("list payload carries: goals=%s bookings=%s lineup=%s duration=%s" % (
            isinstance(sample.get("goals"), list), isinstance(sample.get("bookings"), list),
            bool((sample.get("homeTeam") or {}).get("lineup")), (sample.get("score") or {}).get("duration")))
        try:
            d = _get("/matches/%s" % sample.get("id"), token)
            d = d.get("match", d) or {}
            print("detail %s (%s v %s): goals=%s bookings=%s lineup=%s" % (
                sample.get("id"), (sample.get("homeTeam") or {}).get("name"), (sample.get("awayTeam") or {}).get("name"),
                len(d.get("goals") or []), len(d.get("bookings") or []),
                len(((d.get("homeTeam") or {}).get("lineup")) or [])))
        except Exception as e:
            print("detail fetch FAILED — this is the blocker: %s: %s" % (type(e).__name__, e))
    else:
        print("no finished matches to probe")


if __name__ == "__main__":
    if "--check" in sys.argv:
        audit()
    elif "diag" in sys.argv:
        diag()
    else:
        fetch()

