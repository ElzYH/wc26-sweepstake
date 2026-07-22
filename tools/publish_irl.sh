#!/usr/bin/env bash
# Publish the REAL tournament into the GitHub repo — one command, run from the repo root on the Mac:
#
#     bash tools/publish_irl.sh              # uses the "wc26" ssh alias
#     bash tools/publish_irl.sh myhost       # or any ssh host/alias
#     bash tools/publish_irl.sh wc26 --no-push   # stage + commit locally, don't push
#
# What it does:
#   1. On the box: refresh the archive snapshots + review.json (idempotent, read-only for the site).
#   2. Pull results_wc2026.json / wagers_wc2026.json / draw_result_wc2026.json / review.json
#      and the static wc26-demo/ into the repo.
#   3. Commit + push. From then on, straight from a clone:
#         python3 demo.py --mode irl --irl-bets     # the real WC26 replaying live, real bets riding
#         python3 tools/make_demos.py               # static GitHub-Pages replays incl. the IRL one
#      ...and test_replay_wc26.py switches from SKIP to replaying the real tournament in the gate.
set -euo pipefail
HOST="${1:-wc26}"
SITE="/opt/wc26/sites/mandem"
REPO_ON_BOX="/opt/wc26/repo"

[ -f teams.json ] && [ -f demo.py ] || { echo "Run this from the repo root."; exit 1; }

echo "==> refreshing snapshots on ${HOST}..."
ssh "$HOST" "cd $SITE && sudo -u wc26 python3 $REPO_ON_BOX/archive.py >/dev/null && sudo -u wc26 python3 $REPO_ON_BOX/review.py --json >/dev/null 2>&1 || true"

echo "==> pulling the IRL data..."
scp -q "$HOST:$SITE/results_wc2026.json" "$HOST:$SITE/wagers_wc2026.json" "$HOST:$SITE/draw_result_wc2026.json" .
scp -q "$HOST:$SITE/review.json" . 2>/dev/null || echo "    (no review.json — skipped)"
echo "==> pulling the static demo snapshot..."
scp -qr "$HOST:$SITE/wc26-demo" . 2>/dev/null || echo "    (no wc26-demo — skipped)"

echo "==> sanity check..."
python3 - <<'PY'
import json
r = json.load(open("results_wc2026.json"))
fin = [m for m in r.get("matches", []) if m.get("status") in ("FINISHED", "AWARDED")]
assert len(fin) >= 100, "results_wc2026.json looks incomplete (%d finished)" % len(fin)
json.load(open("draw_result_wc2026.json"))
print("    %d matches (%d finished), draw OK" % (len(r.get("matches", [])), len(fin)))
PY

git add results_wc2026.json wagers_wc2026.json draw_result_wc2026.json 2>/dev/null
git add review.json 2>/dev/null || true
git add wc26-demo 2>/dev/null || true
git commit -m "Publish the real WC26: replay data + static demo" || echo "    (nothing new to commit)"
if [ "${2:-}" != "--no-push" ]; then
    git push
    echo "==> pushed. Anyone can now run: python3 demo.py --mode irl --irl-bets"
else
    echo "==> committed locally (--no-push). Push when ready."
fi
