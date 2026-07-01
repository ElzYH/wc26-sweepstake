#!/usr/bin/env python3
"""READ-ONLY diagnostic. Changes nothing. Prints, per player, the exact betting internals the leaderboard
is built from, so we can see which display is drifting. Run from the repo dir:  python3 diag_points.py
Optionally pass the wagers file:  python3 diag_points.py /opt/wc26/repo/wagers.json"""
import json, os, sys
import wager

def find(path_hint):
    for p in ([path_hint] if path_hint else []) + ["wagers.json", "/opt/wc26/repo/wagers.json",
                                                   os.path.join(os.path.dirname(__file__), "wagers.json")]:
        if p and os.path.exists(p):
            return p
    return None

wf = find(sys.argv[1] if len(sys.argv) > 1 else None)
if not wf:
    print("Could not find wagers.json — pass its path: python3 diag_points.py /path/to/wagers.json")
    sys.exit(1)
wl = json.load(open(wf))
print("wagers file: %s   (%d records)\n" % (wf, len(wl)))

players = sorted({w.get("player") for w in wl if isinstance(w, dict) and w.get("player")})
pdel = wager.player_deltas(wl)

hdr = ("player", "raw_net", "free_bonus", "lb_net", "pend_stk", "lb_held", "board_contrib", "won_sum", "lost_sum", "n_won", "n_lost", "n_void", "n_open")
print("%-8s %8s %10s %8s %8s %8s %13s %8s %8s %5s %6s %6s %6s" % hdr)
for p in players:
    d = pdel.get(p, {})
    raw = round(d.get("settled_net", 0.0), 2)
    fb = wager.free_bonus(p, wl)
    lnet = wager.leaderboard_net(p, wl)
    pend = round(d.get("pending_stake", 0.0), 2)
    lheld = wager.leaderboard_held(p, wl)
    board = round(lnet - lheld, 2)          # what SHOULD hit the board from betting (base + this = total)
    # raw summary-style tallies (excludes free bets & voids to compare with the on-screen summary)
    won = lost = 0.0; nw = nl = nv = no = 0
    for w in wl:
        if not isinstance(w, dict) or w.get("player") != p or w.get("credit"):
            continue
        st = w.get("status"); stake = w.get("stake", 0) or 0; ret = w.get("return", 0) or 0
        if st == "won":  won += (ret - stake); nw += 1
        elif st == "lost": lost += stake; nl += 1
        elif st == "void": nv += 1
        elif st == "pending": no += 1
    print("%-8s %8.2f %10.1f %8.2f %8.2f %8.2f %13.2f %8.2f %8.2f %5d %6d %6d %6d"
          % (p, raw, fb, lnet, pend, lheld, board, round(won, 2), round(lost, 2), nw, nl, nv, no))

print("\nHow to read: board_contrib is exactly what betting adds/removes on the leaderboard (total = match+bonus+live + board_contrib).")
print("lb_net = settled net after the free cushion (what the activity feed's end-of-day standings should use).")
print("Compare board_contrib and lb_net here against the 🎲 column and the feed standings on the site.")
