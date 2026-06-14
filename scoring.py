"""
Compute sweepstake standings in three modes from match results.

  points    - performance: goals, wins, draws, clean sheets + round bonuses
  survival  - progression only: how far your teams go / last-man-standing
  hybrid    - both

Knockout advancement/elimination uses each match's `winner` field, so games
decided on penalties resolve correctly — and a knockout tie won on penalties
counts as a WIN for the advancing team (the loser takes the loss).
"""
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
try:
    import wager                       # optional wagering engine; tracker works fine without it
except Exception:
    wager = None

SCORING = {
    "per_goal": 1, "win": 3, "draw": 1, "clean_sheet": 1,
    "stage_bonus": {"LAST_32": 4, "LAST_16": 7, "QUARTER_FINALS": 11,
                    "SEMI_FINALS": 16, "THIRD_PLACE": 20, "FINAL": 50, "WINNER": 100},
}
SURVIVAL_VALUE = {"LAST_32": 18, "LAST_16": 26, "QUARTER_FINALS": 34,
                  "SEMI_FINALS": 44, "FINAL": 85, "WINNER": 135}
KO_ORDER = ["LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "FINAL"]
# The 3rd-place play-off isn't on the path to the final — both teams already lost their semi, so for SURVIVAL
# they're eliminated (capped at the semi-final value; no THIRD_PLACE survival). It still awards a POINTS bonus to
# its WINNER (bronze) and its goals/win count for points/hybrid, but it must not affect bracket advancement/survival.
BRACKET_KO = ("LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "FINAL")
# A match counts as decided when FINISHED or AWARDED (a walkover/forfeit — the API gives it a winner + score).
FINAL_STATUSES = ("FINISHED", "AWARDED")


def _load(path):
    with open(path) as f:
        return json.load(f)


def _winner_side(m):
    w = m.get("winner")
    if w in ("HOME", "AWAY", "DRAW"):
        return w
    hs, as_ = m.get("homeScore"), m.get("awayScore")
    if hs is None or as_ is None:
        return None
    return "HOME" if hs > as_ else ("AWAY" if as_ > hs else "DRAW")


def _player_totals(fin, teams, owner):
    """Points + survival per PLAYER from a subset of finished matches (used for the over-time history)."""
    pts = defaultdict(int); reached = defaultdict(set)
    for m in fin:
        hs, as_ = m.get("homeScore"), m.get("awayScore")
        if hs is None or as_ is None:
            continue
        for team, sc, co in ((m["home"], hs, as_), (m["away"], as_, hs)):
            if team not in teams:
                continue
            pts[team] += sc * SCORING["per_goal"]
            if co == 0:
                pts[team] += SCORING["clean_sheet"]
            if sc > co:
                pts[team] += SCORING["win"]
            elif sc == co:
                pts[team] += SCORING["draw"]
        if m["stage"] != "GROUP_STAGE":
            for s in ("home", "away"):
                if m[s] in teams:
                    reached[m[s]].add(m["stage"])
            side = _winner_side(m)
            champ = m["home"] if side == "HOME" else (m["away"] if side == "AWAY" else None)
            if m["stage"] == "FINAL" and champ in teams:
                reached[champ].add("WINNER")
    for team, stages in reached.items():
        pts[team] += max((SCORING["stage_bonus"].get(st, 0) for st in stages), default=0)
    P = defaultdict(int); S = defaultdict(int)
    for team, o in owner.items():
        P[o] += pts[team]
        S[o] += max((SURVIVAL_VALUE.get(s, 0) for s in reached.get(team, ())), default=0)
    return P, S


def _build_history(finished, teams, owner, players):
    """One snapshot per finished match, in chronological order — points/survival/hybrid per player."""
    fin = sorted([m for m in finished if m.get("homeScore") is not None],
                 key=lambda m: m.get("utcDate") or "")
    hist = []
    for i in range(1, len(fin) + 1):
        P, S = _player_totals(fin[:i], teams, owner)
        hist.append({"m": i, "p": {pl: {"pts": P[pl], "srv": S[pl], "hyb": P[pl] + S[pl]}
                                   for pl in players}})
    return hist


def _mid(m):
    """Stable match id, mirrors wager.match_id so fixtures and wagers agree (works even if wager is absent)."""
    i = m.get("id")
    if i not in (None, ""):
        return str(i)
    return "%s|%s|%s" % (m.get("home"), m.get("away"), (m.get("utcDate") or "")[:16])


def compute(teams_path="teams.json", draw_path="draw_result.json",
            results_path="results.json", out="tracker_data.json", default_mode="hybrid", wagers=None,
            clocks_path="match_clocks.json"):
    teams = {t["name"]: t for t in _load(teams_path)["teams"]}
    draw = _load(draw_path)
    results = _load(results_path)
    matches = results.get("matches", [])
    # Defensive normalisation: a malformed/partial match (hand-edited results.json, an old backup with a
    # different schema, or a feed format change) must NEVER crash the whole rebuild — that would freeze the
    # tracker for everyone. Drop non-dicts, guarantee the keys the engine reads, sanitise scores to safe
    # non-negative whole numbers (so a string/inf/NaN/negative score can't crash or create silly points),
    # and de-duplicate so the same match appearing twice can't double-count points.
    def _score(v):
        if v is None:
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None                      # non-numeric score -> treat as "no score yet"
        if v != v or v in (float("inf"), float("-inf")):
            return None                      # NaN / inf -> no points can ever be infinite
        return max(0, min(1000, int(v)))     # whole, non-negative, sanely bounded goals
    _clean, _seen = [], set()
    for _m in matches:
        if not isinstance(_m, dict):
            continue
        _m = dict(_m)
        _m.setdefault("status", "SCHEDULED")
        _m.setdefault("stage", "GROUP_STAGE")
        _m.setdefault("home", None)
        _m.setdefault("away", None)
        _m.setdefault("utcDate", None)
        _m["homeScore"] = _score(_m.get("homeScore"))
        _m["awayScore"] = _score(_m.get("awayScore"))
        key = _m.get("id")
        if key in (None, ""):
            key = (_m.get("home"), _m.get("away"), _m.get("utcDate"), _m.get("stage"))
        if key in _seen:
            continue                         # same match twice -> count it once
        _seen.add(key)
        _clean.append(_m)
    matches = _clean
    # Defensive normalisation of the DRAW too: a corrupt/hand-edited draw_result.json (or a bad import
    # bundle) must not crash the rebuild. Guarantee draw["players"] is a list of {name, teams:[{name,...}]}.
    _players = draw.get("players") if isinstance(draw, dict) else None
    _dp = []
    for _p in (_players if isinstance(_players, list) else []):
        if not isinstance(_p, dict) or not _p.get("name"):
            continue
        _teams = [_t for _t in (_p.get("teams") if isinstance(_p.get("teams"), list) else [])
                  if isinstance(_t, dict) and _t.get("name")]
        _dp.append({**_p, "name": _p["name"], "teams": _teams})
    draw = {"players": _dp}
    owner = {t["name"]: p["name"] for p in draw["players"] for t in p["teams"]}

    finished = [m for m in matches if m["status"] in FINAL_STATUSES]
    live = [m for m in matches if m["status"] in ("IN_PLAY", "PAUSED")]
    scoring_matches = finished + live      # points/goals/clean-sheets accrue LIVE (fantasy-style); recomputed each refresh so a VAR-disallowed goal just lowers the running total
    ko_matches = [m for m in matches if m["stage"] != "GROUP_STAGE"]
    ko_teams = {m["home"] for m in ko_matches} | {m["away"] for m in ko_matches}
    ko_started = any(m["status"] in FINAL_STATUSES + ("IN_PLAY", "PAUSED") for m in ko_matches)

    pts = defaultdict(int); record = defaultdict(lambda: [0, 0, 0])
    gf = defaultdict(int); ga = defaultdict(int); cs = defaultdict(int)
    live_pts = defaultdict(int)                        # points currently accruing from in-play matches (the live "+N")
    reached = defaultdict(set); lost_ko_at = {}

    for m in matches:                                  # which KO stages a team appears in
        if m["stage"] in BRACKET_KO:
            for s in ("home", "away"):
                if m[s] in teams:
                    reached[m[s]].add(m["stage"])

    for m in scoring_matches:                          # match points — full-time AND live (provisional, from the current score)
        h, a, hs, as_ = m["home"], m["away"], m["homeScore"], m["awayScore"]
        if hs is None or as_ is None:
            continue
        is_live = m["status"] in ("IN_PLAY", "PAUSED")
        side = _winner_side(m)                         # HOME / AWAY / DRAW — reflects penalties + walkovers
        for team, scored, conceded in ((h, hs, as_), (a, as_, hs)):
            if team not in teams:
                continue
            d = scored * SCORING["per_goal"]
            gf[team] += scored; ga[team] += conceded
            if conceded == 0:
                d += SCORING["clean_sheet"]; cs[team] += 1
            won = (side == "HOME" and team == h) or (side == "AWAY" and team == a)
            if won:
                d += SCORING["win"]; record[team][0] += 1                  # incl. a knockout tie won on penalties
            elif side in ("HOME", "AWAY"):
                record[team][2] += 1                                       # lost (incl. on penalties)
            else:
                d += SCORING["draw"]; record[team][1] += 1                 # genuine draw (group stage)
            pts[team] += d
            if is_live:
                live_pts[team] += d

    for m in [x for x in finished if x["stage"] in BRACKET_KO]:   # KO resolution (pens-aware)
        side = _winner_side(m)
        if side == "HOME":
            loser, champ = m["away"], m["home"]
        elif side == "AWAY":
            loser, champ = m["home"], m["away"]
        else:
            continue
        if loser in teams:
            lost_ko_at[loser] = m["stage"]
        if m["stage"] == "FINAL" and champ in teams:
            reached[champ].add("WINNER")

    for m in finished:                                 # 3rd-place play-off: only the WINNER earns the bronze bonus
        if m["stage"] == "THIRD_PLACE":
            side = _winner_side(m)
            w = m["home"] if side == "HOME" else (m["away"] if side == "AWAY" else None)
            if w in teams:
                reached[w].add("THIRD_PLACE")

    for team, stages in reached.items():       # furthest stage reached only (not stacked per round)
        pts[team] += max((SCORING["stage_bonus"].get(st, 0) for st in stages), default=0)

    def furthest_stage(team):
        prog = KO_ORDER + ["WINNER"]
        ko = [s for s in reached[team] if s in prog]
        return max(ko, key=lambda s: prog.index(s)) if ko else "GROUP_STAGE"

    def survival_pts(team):
        return max((SURVIVAL_VALUE.get(s, 0) for s in reached[team]), default=0)

    def status(team):
        if team in lost_ko_at:
            return "out", lost_ko_at[team]
        if team in ko_teams:
            return "alive", furthest_stage(team)
        if ko_started:
            return "out", "GROUP_STAGE"
        return "alive", "GROUP_STAGE"

    players_out = []
    for p in draw["players"]:
        teams_out = []
        for t in p["teams"]:
            name = t["name"]; st, stage = status(name); w, d, l = record[name]
            teams_out.append({"name": name, "tier": t.get("tier"), "group": t.get("group"),
                              "points": pts[name], "survival": survival_pts(name),
                              "status": st, "stage": stage, "record": f"{w}-{d}-{l}",
                              "gf": gf[name], "ga": ga[name], "live": live_pts[name],
                              "composite": teams.get(name, {}).get("composite", 0),
                              "odds": teams.get(name, {}).get("implied_prob", 0)})
        teams_out.sort(key=lambda x: -(x["points"] + x["survival"]))
        tot_p = sum(x["points"] for x in teams_out); tot_s = sum(x["survival"] for x in teams_out)
        players_out.append({"name": p["name"], "points": tot_p, "survival": tot_s, "hybrid": tot_p + tot_s,
                            "live": sum(x["live"] for x in teams_out),
                            "alive_teams": sum(1 for x in teams_out if x["status"] == "alive"),
                            "total_teams": len(teams_out), "teams": teams_out})

    # ---- wagering (optional, off by default): adjust each player's POINTS by settled bet profit/loss
    #      minus points held in open bets. Survival is never affected. Wrapped so a bug here can't break the tracker.
    if wagers is not None and wager is not None:
        try:
            pdel = wager.player_deltas(wagers)
            # what-if list: every open bet flipped to won (returns were locked at placement) — used to show
            # "+N if your bets land" on the leaderboard WITHOUT ever adding it to the real score
            hyp = [(dict(w, status="won") if (isinstance(w, dict) and w.get("status") == "pending" and not w.get("credit")) else w) for w in wagers]
            for p in players_out:
                p["points_settled"] = round(p["points"] - p.get("live", 0), 1)   # finished-game points, before bets — the bettable balance
                d = pdel.get(p["name"], {})
                lnet = wager.leaderboard_net(p["name"], wagers)   # bonus-cushioned: only winnings (and losses beyond the free bonus) move the leaderboard
                held_disp = round(d.get("pending_stake", 0.0), 1)        # full open stake — for the "N riding" display
                held_lb = wager.leaderboard_held(p["name"], wagers)      # only stakes beyond the free cushion come off the board
                p["wager_net"] = lnet
                p["wager_held"] = held_disp
                p["bets_open"] = d.get("pending_count", 0)
                p["bettable"] = wager.available_points(p["name"], p["points_settled"], wagers)  # earned + free bonus + winnings - held
                hnet = wager.leaderboard_net(p["name"], hyp)
                hheld = wager.leaderboard_held(p["name"], hyp)
                p["bet_potential"] = max(0.0, round((hnet - hheld) - (lnet - held_lb), 1))   # score-if-all-bets-win minus score-now
                if lnet or held_lb:
                    newp = max(0.0, round(p["points"] + lnet - held_lb, 1))
                    p["points"] = newp
                    p["hybrid"] = round(newp + p["survival"], 1)
        except Exception:
            pass

    def board(key):
        if key == "survival":
            rows = sorted(players_out, key=lambda p: (-p["alive_teams"], -p["survival"]))
            return [{"name": p["name"], "score": p["alive_teams"], "alive_teams": p["alive_teams"],
                     "live": p["live"], "total_teams": p["total_teams"],
                     "wager_held": p.get("wager_held", 0), "wager_net": p.get("wager_net", 0),
                     "bets_open": p.get("bets_open", 0), "points_settled": p.get("points_settled"), "bet_potential": p.get("bet_potential", 0)} for p in rows]
        rows = sorted(players_out, key=lambda p: (-p[key], -p["alive_teams"]))
        return [{"name": p["name"], "score": p[key], "alive_teams": p["alive_teams"],
                 "live": p["live"], "total_teams": p["total_teams"],
                 "wager_held": p.get("wager_held", 0), "wager_net": p.get("wager_net", 0),
                 "bets_open": p.get("bets_open", 0), "points_settled": p.get("points_settled"), "bet_potential": p.get("bet_potential", 0)} for p in rows]

    # live "odds to own the champion" from bookmaker implied probabilities, alive-aware
    def implied(name):
        t = teams.get(name, {})
        return t.get("implied_prob") or (1.0 / t["decimal_odds"] if t.get("decimal_odds") else 0.0)
    alive_prob = {n: implied(n) for n in teams if status(n)[0] == "alive"}
    tot_alive = sum(alive_prob.values()) or 1.0
    for p in players_out:
        p["champion_odds"] = round(100 * sum(alive_prob.get(t["name"], 0.0) for t in p["teams"]) / tot_alive, 1)
        p["squad_strength"] = round(sum(teams.get(t["name"], {}).get("composite", 0) for t in p["teams"]))
        p["favourites"] = sum(1 for t in p["teams"] if t["tier"] == 1)

    # ---- projected points: expected tournament points per player ----
    # group games (modelled from composite gaps) + an advance-to-knockout bonus,
    # so group difficulty matters, not just raw squad strength.
    import math
    by_group = {}
    for _n, _t in teams.items():
        by_group.setdefault(_t.get("group"), []).append((_n, _t.get("composite", 0)))

    def _exp_group_points(name):
        t = teams.get(name, {}); c = t.get("composite", 0)
        ep = 0.0
        for m, mc in by_group.get(t.get("group"), []):
            if m == name:
                continue
            diff = c - mc
            pwx = 1.0 / (1.0 + 10 ** (-diff / 60.0))     # win prob excl. draws
            pd = 0.26 * math.exp(-abs(diff) / 90.0)        # draws rarer in mismatches
            pw = (1 - pd) * pwx
            ep += 3 * pw + 1 * pd
        return ep

    def _advance_prob(name):
        t = teams.get(name, {}); c = t.get("composite", 0)
        comps = sorted([mc for (_m, mc) in by_group.get(t.get("group"), [])], reverse=True)
        rank = (comps.index(c) + 1) if c in comps else len(comps)
        return {1: 0.85, 2: 0.62, 3: 0.33}.get(rank, 0.18)

    KO_VALUE = 5.0   # expected extra points from a knockout run, weighted by reaching it
    proj_team = {n: _exp_group_points(n) + _advance_prob(n) * KO_VALUE for n in teams}
    for p in players_out:
        p["projected_points"] = round(sum(proj_team.get(t["name"], 0.0) for t in p["teams"]), 1)
    # ---- fair (handicap): points scored above/below your expected share ----
    # expected share = your projected_points as a fraction of everyone's, applied to the
    # total points actually scored. Beating it (positive) = overperforming a weaker/clustered squad.
    # Uses SETTLED points only (excludes live, in-progress provisional points) so a match merely being
    # live can't swing the handicap — fair stays 0 for everyone until games actually finish.
    _tot_pts = sum((p["points"] - (p.get("live") or 0)) for p in players_out)
    _tot_proj = sum(p["projected_points"] for p in players_out) or 1.0
    for p in players_out:
        _settled = p["points"] - (p.get("live") or 0)
        p["fair"] = round(_settled - _tot_pts * p["projected_points"] / _tot_proj)
    by_proj = sorted(players_out, key=lambda p: -p["projected_points"])
    champ_board = [{"name": p["name"], "odds": p["champion_odds"], "alive_teams": p["alive_teams"],
                    "total_teams": p["total_teams"]} for p in sorted(players_out, key=lambda p: -p["champion_odds"])]
    fav = max(alive_prob, key=alive_prob.get) if alive_prob else None
    def _best_tier(min_tier):
        c = [n for n in alive_prob if teams.get(n, {}).get("tier", 4) >= min_tier]
        return max(c, key=alive_prob.get) if c else None
    dark = _best_tier(3) or _best_tier(2)      # genuine dark horse: best chance among non-favourites
    gc = defaultdict(float)
    for n, tt in teams.items():
        gc[tt.get("group")] += tt.get("composite", 0)
    god = max(gc, key=gc.get) if gc else None                               # group of death (strongest)
    _bw = [(abs(m["homeScore"] - m["awayScore"]),
            f'{m["home"]} {m["homeScore"]}-{m["awayScore"]} {m["away"]}')
           for m in finished if m.get("homeScore") is not None and m.get("awayScore") is not None]
    bw = max(_bw, key=lambda c: c[0]) if _bw else None          # biggest winning margin (first one on a tie)
    topt = max(gf, key=gf.get) if gf and max(gf.values()) > 0 else None
    by_strength = sorted(players_out, key=lambda p: -p["squad_strength"])
    pair_counts = defaultdict(int)                                   # how often two players' teams meet
    for m in matches:
        ho, ao = owner.get(m["home"]), owner.get(m["away"])
        if ho and ao and ho not in ("-", "—") and ao not in ("-", "—") and ho != ao:
            pair_counts[tuple(sorted((ho, ao)))] += 1
    rival = max(pair_counts, key=pair_counts.get) if pair_counts else None
    # most tier-1 favourites owned (static)
    mf = max(players_out, key=lambda p: p["favourites"]) if players_out else None
    # match-based player/team analysis (only meaningful once games are played)
    played = {m["home"] for m in finished} | {m["away"] for m in finished}
    played = [n for n in played if n in teams]
    pgf = {p["name"]: sum(t["gf"] for t in p["teams"]) for p in players_out}
    pga = {p["name"]: sum(t["ga"] for t in p["teams"]) for p in players_out}
    pcs = {p["name"]: sum(cs[t["name"]] for t in p["teams"]) for p in players_out}
    over_t = under_t = best_def = over_p = under_p = top_sc = most_con = gl = most_cs = None
    if played:
        # Over/under-performer = points earned vs a strength-weighted expectation (composite share of points
        # scored so far). Continuous (not rank-based), so a chalk result no longer ties everyone to 0 and
        # collapses both cards onto the same team. Only shown once there's a real spread between two teams.
        _tot_comp = sum(max(0.0, teams[n].get("composite", 0) or 0) for n in played) or 1.0
        _tot_pts = sum(pts[n] for n in played)
        tres = {n: pts[n] - (max(0.0, teams[n].get("composite", 0) or 0) / _tot_comp) * _tot_pts for n in played}
        _ord = sorted(played, key=lambda n: (tres[n], gf[n], n))   # ascending residual; tie-break: fewer goals, then name
        if len(_ord) >= 2 and (tres[_ord[-1]] - tres[_ord[0]]) > 1e-9:
            over_t, under_t = _ord[-1], _ord[0]
        best_def = max(played, key=lambda n: (cs[n], -ga[n]))   # most clean sheets (fewest conceded breaks ties)
        pp = sorted(players_out, key=lambda p: -p["points"]); psr = sorted(players_out, key=lambda p: -p["squad_strength"])
        prkp = {p["name"]: i for i, p in enumerate(pp)}; srkp = {p["name"]: i for i, p in enumerate(psr)}
        presid = {p["name"]: srkp[p["name"]] - prkp[p["name"]] for p in players_out}
        _po = sorted(presid, key=lambda n: (presid[n], n))
        if len(_po) >= 2 and presid[_po[-1]] != presid[_po[0]]:    # only when players actually differ (no duplicate card)
            over_p, under_p = _po[-1], _po[0]
        top_sc = max(pgf, key=pgf.get) if max(pgf.values(), default=0) > 0 else None
        most_con = max(pga, key=pga.get) if max(pga.values(), default=0) > 0 else None
        most_cs = max(pcs, key=pcs.get) if max(pcs.values(), default=0) > 0 else None
        leaders = defaultdict(int)
        for st_ in results.get("standings", []):
            if not isinstance(st_, dict) or not isinstance(st_.get("table"), list):
                continue
            for r in st_["table"]:
                if not isinstance(r, dict):
                    continue
                tm = r.get("team")
                if r.get("position") == 1 and tm and owner.get(tm, "-") not in ("-", "—"):
                    leaders[owner[tm]] += 1
        gl = max(leaders, key=leaders.get) if leaders else None
    fin_goals = sum((m["homeScore"] or 0) + (m["awayScore"] or 0)
                    for m in finished if m.get("homeScore") is not None)
    stats = {"goals": fin_goals, "matches_played": len(finished),
             "favourite_team": fav, "favourite_owner": (owner.get(fav, "-") if fav else "-"),
             "favourite_odds": (round(100 * alive_prob[fav] / tot_alive, 1) if fav else 0),
             "teams_remaining": len(alive_prob),
             "goals_per_match": (round(fin_goals / len(finished), 2) if finished else 0),
             "dark_horse": dark, "dark_horse_owner": (owner.get(dark, "-") if dark else "-"),
             "dark_horse_odds": (round(100 * alive_prob[dark] / tot_alive, 1) if dark else 0),
             "group_of_death": god,
             "biggest_win": (bw[1] if bw and bw[0] > 0 else None),
             "top_team": topt, "top_team_goals": (gf.get(topt, 0) if topt else 0),
             "strongest_player": (by_strength[0]["name"] if by_strength else None),
             "strongest_player_strength": (by_strength[0]["squad_strength"] if by_strength else 0),
             "underdog_player": (by_strength[-1]["name"] if by_strength else None),
             "underdog_player_strength": (by_strength[-1]["squad_strength"] if by_strength else 0),
             "rivalry": (f"{rival[0]} vs {rival[1]}" if rival else None),
             "rivalry_count": (pair_counts[rival] if rival else 0),
             "most_favourites_player": (mf["name"] if mf else None),
             "most_favourites": (mf["favourites"] if mf else 0),
             "top_scorer_player": top_sc, "top_scorer_player_goals": (pgf.get(top_sc, 0) if top_sc else 0),
             "most_conceded_player": most_con, "most_conceded_player_goals": (pga.get(most_con, 0) if most_con else 0),
             "clean_sheets_player": most_cs, "clean_sheets_value": (pcs.get(most_cs, 0) if most_cs else 0),
             "over_player": over_p, "under_player": under_p,
             "over_team": over_t, "under_team": under_t,
             "best_defence_team": best_def, "best_defence_conceded": (ga[best_def] if best_def else 0),
             "best_defence_cs": (cs[best_def] if best_def else 0),
             "group_leaders_player": gl,
             "proj_points_player": (by_proj[0]["name"] if by_proj else None),
             "proj_points_value": (by_proj[0]["projected_points"] if by_proj else 0)}
    strength_board = [{"name": p["name"], "strength": p["squad_strength"], "favourites": p["favourites"]}
                      for p in sorted(players_out, key=lambda p: -p["squad_strength"])]

    champion_decided = None
    for m in matches:
        if m.get("stage") == "FINAL" and m.get("status") in FINAL_STATUSES:
            side = _winner_side(m)
            win = m["home"] if side == "HOME" else (m["away"] if side == "AWAY" else None)
            if win:
                champion_decided = {"team": win, "owner": owner.get(win, "—"),
                                    "runnerUp": (m["away"] if side == "HOME" else m["home"])}
            break

    data = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "competition": results.get("competition", "WC"), "default_mode": default_mode,
            "scoring": {"points": SCORING, "survival": SURVIVAL_VALUE},
            "champion_decided": champion_decided,
            "leaderboards": {"points": board("points"), "survival": board("survival"), "hybrid": board("hybrid"), "fair": board("fair")},
            "champion": champ_board, "strength": strength_board, "stats": stats,
            "players": players_out,
            "groups": [{"group": s.get("group"),
                        "table": [{**r, "owner": owner.get(r.get("team"), "—"),
                                   "tier": teams.get(r.get("team"), {}).get("tier"),
                                   "composite": teams.get(r.get("team"), {}).get("composite", 0),
                                   "implied": teams.get(r.get("team"), {}).get("implied_prob", 0)}
                                  for r in s["table"] if isinstance(r, dict)]}
                       for s in results.get("standings", []) if isinstance(s, dict) and isinstance(s.get("table"), list)],
            "fixtures": [{"utcDate": m.get("utcDate"), "stage": m.get("stage"), "group": m.get("group"),
                          "matchId": _mid(m),
                          "status": m.get("status"), "home": m.get("home"), "away": m.get("away"),
                          "homeOwner": owner.get(m.get("home"), "—"), "awayOwner": owner.get(m.get("away"), "—"),
                          "homeScore": m.get("homeScore"), "awayScore": m.get("awayScore"),
                          "minute": m.get("minute"),
                          "aet": m.get("aet"), "shootout": m.get("shootout"),
                          "penHome": m.get("penHome"), "penAway": m.get("penAway"),
                          "winner": _winner_side(m)} for m in matches]}
    # odds on still-to-play fixtures (only when wagering is active; guarded)
    if wagers is not None and wager is not None:
        try:
            for f in data["fixtures"]:
                m = next((x for x in matches if x.get("home") == f["home"] and x.get("away") == f["away"]
                          and (x.get("utcDate") or "")[:16] == (f.get("utcDate") or "")[:16]), None)
                if m and wager.can_bet_on(f) and f.get("home") in teams and f.get("away") in teams:
                    ch = wager.live_strength(teams.get(f["home"], {}).get("composite", 0), f["home"], matches)
                    ca = wager.live_strength(teams.get(f["away"], {}).get("composite", 0), f["away"], matches)
                    f["odds"] = wager.match_odds(ch, ca)
                    f["ouOdds"] = wager.goals_odds(ch, ca)        # Over/Under prices per line (0.5..8.5)
                    f["matchId"] = wager.match_id(m)
                    f["maxStake"] = wager.stage_max_stake(f.get("stage"))
            data["wager_stats"] = wager.stats(wagers)
            data["wager_leaders"] = wager.leaders(wagers)
            data["betting_locked"] = wager.betting_locked(data)
        except Exception:
            pass
    # live match clock: attach accurate ticking seconds from the server's real kickoff/half-time tracking.
    # liveSec = match seconds elapsed (the frontend ticks on from here); liveHT = currently at half-time.
    # Optional + fully defensive: missing/old clocks file just means the frontend shows the feed minute instead.
    try:
        _clocks = {}
        try:
            with open(clocks_path) as _cf:
                _clocks = json.load(_cf)
        except Exception:
            _clocks = {}
        if isinstance(_clocks, dict) and _clocks:
            _now = time.time()
            for f in data["fixtures"]:
                try:
                    st = f.get("status")
                    # A penalty shootout is NOT match time: stop the clock and let the UI show 'PENS'.
                    in_shootout = bool(f.get("shootout")) or f.get("penHome") is not None or f.get("penAway") is not None
                    if st == "PAUSED":
                        f["liveHT"] = True
                    elif st in ("IN_PLAY", "LIVE", "SUSPENDED") and not in_shootout:
                        rec = _clocks.get(f.get("matchId"))
                        if isinstance(rec, dict) and rec.get("ko") is not None:
                            ko = float(rec["ko"])
                            htp = float(rec.get("htp") or 0.0)
                            if htp < 0 or htp != htp or htp > 60 * 60:    # guard a corrupt half-time bank (NaN/negative/absurd)
                                htp = 0.0
                            el = _now - ko - htp
                            ps = rec.get("ps")
                            if ps:
                                el -= max(0.0, _now - float(ps))
                            if el == el and 0 <= el < 1e9:                  # finite, non-negative (rejects NaN / ±inf)
                                # Cap the clock so a half-time the feed never reported (no PAUSED) can't run it away
                                # — the "72:00 when it's really 50:00" bug. WITH a real broadcast minute the clock is
                                # re-locked to it upstream, so trust it up to the end-of-ET ceiling. WITHOUT a minute
                                # we're estimating off wall-clock, so hold at the first-half ceiling until we've actually
                                # banked a half-time, then the 90' ceiling. Either way it can never overshoot reality.
                                mn = f.get("minute")
                                has_minute = isinstance(mn, (int, float)) and mn is not None and mn >= 0
                                banked_ht = htp > 60.0
                                if has_minute:
                                    # On a livescores feed the broadcast minute is ground truth: never let the
                                    # ticking clock run more than ~2 min past it (covers the smooth tick between
                                    # polls), so it can't drift to 72:00 while the feed says 50'. Hard ET ceiling too.
                                    ceil = min(int((float(mn) + 2) * 60), 125 * 60)
                                elif banked_ht:
                                    ceil = 92 * 60           # 2nd-half estimate: never past 90'(+stoppage) without a minute
                                else:
                                    ceil = 47 * 60           # 1st-half estimate: never past 45'(+stoppage) until HT is seen
                                f["liveSec"] = int(min(el, ceil))
                except Exception:
                    continue
    except Exception:
        pass
    data["history"] = _build_history(finished, teams, owner, [p["name"] for p in draw["players"]])
    if out:
        tmp = out + ".tmp"                      # write-then-rename: a crash can't leave a half-written tracker
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
    return data
