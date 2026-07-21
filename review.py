#!/usr/bin/env python3
"""End-of-tournament review. Run in the site directory (works from local files only, any API tier):

    python3 review.py            # prints the report
    python3 review.py --json     # also writes review.json next to the data

Covers the three angles Erol asked for:
  PARTICIPANTS — final points split (match points vs betting), betting ROI, bets by market.
  TEAMS       — performance vs seed (composite), goals for/against, clean sheets, cards.
  BETTING HEALTH — per-market punter-vs-house P&L: the "was betting overpowered?" report.
"""
import json
import sys
from collections import defaultdict

import wager as W


def _load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def review():
    results = _load("results.json", {}).get("matches", [])
    wagers = _load("wagers.json", [])
    draw = _load("draw_result.json", {})
    teams = _load("teams.json", {})
    td = _load("tracker_data.json", {})

    out = {"participants": [], "teams": [], "betting": {}}

    # ---- participants
    owners = {}
    for p in (draw.get("players") or []):
        owners[p.get("name")] = [t for t in (p.get("teams") or [])]
    lb = {r.get("name"): r for r in ((td.get("leaderboards") or {}).get("points") or [])}
    deltas = W.player_deltas(wagers)
    for name, tm in owners.items():
        d = deltas.get(name, {})
        staked = d.get("staked", 0.0)
        net = d.get("settled_net", 0.0)
        bets = [w for w in wagers if isinstance(w, dict) and w.get("player") == name and not w.get("credit")]
        by_market = defaultdict(lambda: [0, 0.0])
        for w in bets:
            mkts = sorted({(l.get("market") or "result") for l in w.get("legs") or []}) if w.get("legs") \
                else [w.get("market") or "result"]
            key = "+".join(mkts)
            by_market[key][0] += 1
            if w.get("status") in ("won", "lost", "void"):
                by_market[key][1] += (w.get("return") or 0) - (w.get("stake") or 0)
        total = (lb.get(name) or {}).get("points")
        out["participants"].append({
            "name": name, "teams": tm, "final_points": total,
            "bet_count": len(bets), "staked": round(staked, 2), "betting_net": round(net, 2),
            "betting_roi_pct": round(100.0 * net / staked, 1) if staked else None,
            "match_points": round(total - W.leaderboard_net(name, wagers), 2) if total is not None else None,
            "by_market": {k: {"bets": v[0], "net": round(v[1], 2)} for k, v in sorted(by_market.items())},
        })
    out["participants"].sort(key=lambda p: -(p["final_points"] or 0))

    # ---- teams
    comp = {t: (v.get("composite") if isinstance(v, dict) else None) for t, v in teams.items()} if isinstance(teams, dict) else {}
    stats = defaultdict(lambda: {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "cs": 0, "cards": 0, "furthest": "GROUP_STAGE"})
    ORDER = ["GROUP_STAGE", "LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL"]
    for m in results:
        if m.get("status") not in ("FINISHED", "AWARDED"):
            continue
        for side, opp in (("home", "away"), ("away", "home")):
            t = m.get(side)
            if not t:
                continue
            st = stats[t]
            st["p"] += 1
            gf, ga = m.get(side + "Score"), m.get(opp + "Score")
            if gf is not None and ga is not None:
                st["gf"] += gf; st["ga"] += ga
                if ga == 0:
                    st["cs"] += 1
            c = m.get("cardsHome" if side == "home" else "cardsAway")
            if c:
                st["cards"] += c
            wsd = m.get("winner")
            if wsd == "DRAW":
                st["d"] += 1
            elif wsd:
                st["w" if (wsd == "HOME") == (side == "home") else "l"] += 1
            stg = m.get("stage") or "GROUP_STAGE"
            if stg in ORDER and ORDER.index(stg) > ORDER.index(st["furthest"]):
                st["furthest"] = stg
    for t, st in sorted(stats.items(), key=lambda kv: (-kv[1]["w"], -kv[1]["gf"])):
        out["teams"].append(dict(st, team=t, composite=comp.get(t)))

    # ---- draw calibration: did the SEEDING match reality? (feeds fairer draws next time)
    ORDER_PTS = {"GROUP_STAGE": 0, "LAST_32": 1, "LAST_16": 2, "QUARTER_FINALS": 3,
                 "SEMI_FINALS": 4, "THIRD_PLACE": 4, "FINAL": 5}
    seeded = [(t["team"], t["composite"], ORDER_PTS.get(t["furthest"], 0), t["w"], t["gf"] - t["ga"])
              for t in out["teams"] if t.get("composite") is not None]
    if len(seeded) >= 8:
        by_comp = sorted(seeded, key=lambda x: -x[1])
        by_real = sorted(seeded, key=lambda x: (-x[2], -x[3], -x[4]))
        comp_rank = {t[0]: i for i, t in enumerate(by_comp)}
        real_rank = {t[0]: i for i, t in enumerate(by_real)}
        n = len(seeded)
        d2 = sum((comp_rank[t] - real_rank[t]) ** 2 for t in comp_rank)
        rho = 1 - (6.0 * d2) / (n * (n * n - 1))          # Spearman: how well seeding predicted reality
        gaps = sorted(((real_rank[t] - comp_rank[t], t) for t in comp_rank), key=lambda x: x[0])
        out["draw_calibration"] = {
            "seed_vs_reality_spearman": round(rho, 3),
            "overperformers": [{"team": t, "seeded": comp_rank[t] + 1, "finished": real_rank[t] + 1}
                               for g, t in gaps[:5] if g < 0],
            "underperformers": [{"team": t, "seeded": comp_rank[t] + 1, "finished": real_rank[t] + 1}
                                for g, t in reversed(gaps[-5:]) if g > 0],
            "reading": ("seeding predicted the tournament well — keep the current tier weights" if rho >= 0.6 else
                        "seeding was a weak predictor — compress tier weights (flatter draw) and lean the "
                        "composite further toward bookmaker odds over FIFA ranking next time"),
        }

    # ---- betting health (the overpowered check)
    mk = defaultdict(lambda: {"bets": 0, "staked": 0.0, "punter_net": 0.0})
    for w in wagers:
        if not isinstance(w, dict) or w.get("credit") or w.get("status") not in ("won", "lost", "void"):
            continue
        mkts = sorted({(l.get("market") or "result") for l in w.get("legs") or []}) if w.get("legs") \
            else [w.get("market") or "result"]
        key = ("acca:" if w.get("legs") else "") + "+".join(mkts)
        mk[key]["bets"] += 1
        mk[key]["staked"] += w.get("stake") or 0
        mk[key]["punter_net"] += (w.get("return") or 0) - (w.get("stake") or 0)
    total_staked = sum(v["staked"] for v in mk.values())
    total_net = sum(v["punter_net"] for v in mk.values())
    out["betting"] = {
        "per_market": {k: {"bets": v["bets"], "staked": round(v["staked"], 2), "punter_net": round(v["punter_net"], 2),
                           "house_edge_pct": round(-100.0 * v["punter_net"] / v["staked"], 1) if v["staked"] else None}
                       for k, v in sorted(mk.items())},
        "total_staked": round(total_staked, 2),
        "punter_net_total": round(total_net, 2),
        "house_edge_pct": round(-100.0 * total_net / total_staked, 1) if total_staked else None,
        "verdict": ("PUNTERS BEAT THE BOOK — margins were too generous for the swing betting adds to a "
                    "sweepstake; see LESSONS-WC26.md" if total_net > 0 else
                    "the book held its edge overall — check per-market lines for leaks anyway"),
    }

    return out


def _fmt(out):
    L = ["=== PARTICIPANTS ==="]
    for p in out["participants"]:
        L.append("%-8s pts %-7s match %-7s betting %+7.2f (ROI %s%%) over %d bets"
                 % (p["name"], p["final_points"], p["match_points"], p["betting_net"],
                    p["betting_roi_pct"], p["bet_count"]))
        for k, v in p["by_market"].items():
            L.append("           %-22s %2d bets  net %+7.2f" % (k, v["bets"], v["net"]))
    L.append("\n=== TEAMS (top 12 by wins) ===")
    for t in out["teams"][:12]:
        L.append("%-14s P%-3d %d-%d-%d  GF %-3d GA %-3d CS %-2d cards %-3d furthest %s  (seed %s)"
                 % (t["team"], t["p"], t["w"], t["d"], t["l"], t["gf"], t["ga"], t["cs"], t["cards"],
                    t["furthest"], t["composite"]))
    dc = out.get("draw_calibration")
    if dc:
        L.append("\n=== DRAW CALIBRATION (seeding vs reality) ===")
        L.append("Spearman rho %.3f — %s" % (dc["seed_vs_reality_spearman"], dc["reading"]))
        for k in ("overperformers", "underperformers"):
            for e in dc[k]:
                L.append("  %-16s seeded #%-3d finished #%-3d (%s)" % (e["team"], e["seeded"], e["finished"], k[:-1]))
    L.append("\n=== BETTING HEALTH ===")
    b = out["betting"]
    L.append("total staked %s · punters net %+g · house edge %s%%" % (b["total_staked"], b["punter_net_total"], b["house_edge_pct"]))
    for k, v in b["per_market"].items():
        L.append("  %-26s %3d bets  staked %-8g punters %+8.2f  edge %s%%"
                 % (k, v["bets"], v["staked"], v["punter_net"], v["house_edge_pct"]))
    L.append("\nVERDICT: " + b["verdict"])
    return "\n".join(L)


if __name__ == "__main__":
    out = review()
    print(_fmt(out))
    if "--json" in sys.argv:
        json.dump(out, open("review.json", "w"), indent=2)
        print("\nreview.json written (downloadable from the site).")
