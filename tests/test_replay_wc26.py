#!/usr/bin/env python3
"""Replay the REAL World Cup 2026 through the engine — the strongest regression data there is.
Runs only when the frozen snapshots exist (commit them via archive.py: results_wc2026.json,
wagers_wc2026.json, draw_result_wc2026.json); exits 0 with a SKIP note otherwise so the gate
stays green on a fresh clone. Mirrors the results_wc2022.json pattern."""
import json
import os
import sys

import wager as W

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)


if not os.path.exists("results_wc2026.json"):
    print("SKIP: results_wc2026.json not present (run archive.py on the box and commit the snapshots)")
    sys.exit(0)

results = json.load(open("results_wc2026.json")).get("matches", [])
wagers = json.load(open("wagers_wc2026.json")) if os.path.exists("wagers_wc2026.json") else []

fin = [m for m in results if m.get("status") in ("FINISHED", "AWARDED")]
ck("the tournament completed (100+ finished games)", len(fin) >= 100, len(fin))

ck("every finished game is DECIDED (the premature-90' guard never strands a real result)",
   all(W.match_decided(m) for m in fin), [W.match_id(m) for m in fin if not W.match_decided(m)][:4])

ko_pens = [m for m in fin if W.is_knockout(m) and m.get("homeScore") == m.get("awayScore")]
ck("every level knockout carries shootout evidence", all(
    (m.get("penHome") is not None and m.get("penAway") is not None) or m.get("winner") in ("HOME", "AWAY")
    for m in ko_pens), len(ko_pens))

replay = json.loads(json.dumps(wagers))
for w in replay:
    if isinstance(w, dict) and not w.get("credit"):
        if w.get("legs"):
            for l in w["legs"]:
                l.pop("result", None)
        if w.get("status") in ("won", "lost"):
            w["status"] = "pending"; w.pop("result", None); w.pop("settled_at", None)
for m in fin:
    W.settle(replay, m)
orig = {w.get("id"): w.get("status") for w in wagers if isinstance(w, dict) and not w.get("credit")}
got = {w.get("id"): w.get("status") for w in replay if isinstance(w, dict) and not w.get("credit")}
diff = {k: (orig[k], got[k]) for k in orig if orig[k] in ("won", "lost") and got.get(k) != orig[k]}
ck("re-settling every bet from scratch reproduces the recorded outcomes (deterministic settlement)",
   not diff, dict(list(diff.items())[:4]))

deltas = W.player_deltas(wagers)
total_net = sum(d.get("settled_net", 0.0) for d in deltas.values())
print("  (info) punters' combined net vs the book: %+.2f pts" % total_net)
ck("betting ledger reconciles (every settled bet has stake and return recorded)",
   all((w.get("return") is not None and w.get("stake") is not None)
       for w in wagers if isinstance(w, dict) and not w.get("credit") and w.get("status") in ("won", "lost", "void")), None)

print()
print("FAILED (%d): %s" % (len(fails), ", ".join(fails)) if fails else "WC26 replay passed.")
sys.exit(1 if fails else 0)
