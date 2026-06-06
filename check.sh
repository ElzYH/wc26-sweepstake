#!/usr/bin/env bash
# Pre-deploy gate for the WC26 sweepstake. Run from the repo root before every push:
#   ./check.sh
# Exits non-zero if anything fails, so you never ship a syntax error or broken scoring again.
set -u
cd "$(dirname "$0")"
FAIL=0
say(){ printf '\n=== %s ===\n' "$1"; }

say "Python syntax (compile)"
for f in *.py; do
  if python3 -c "import sys; compile(open('$f').read(), '$f', 'exec')" 2>/tmp/pyerr; then
    echo "  ok   $f"
  else
    echo "  FAIL $f"; cat /tmp/pyerr; FAIL=1
  fi
done

say "HTML inline JS (node --check)"
if command -v node >/dev/null 2>&1; then
  for f in *.html; do
    python3 - "$f" <<'PY' > /tmp/extract.js
import re,sys
src=open(sys.argv[1]).read()
open('/tmp/extract.js','w').write('\n'.join(re.findall(r'<script>(.*?)</script>', src, re.S)))
PY
    if [ -s /tmp/extract.js ]; then
      if node --check /tmp/extract.js 2>/tmp/jserr; then echo "  ok   $f"; else echo "  FAIL $f"; cat /tmp/jserr; FAIL=1; fi
    else
      echo "  --   $f (no inline script)"
    fi
  done
else
  echo "  (node not found — skipping JS check)"
fi

say "HTML <style> brace balance"
for f in *.html; do
  res=$(awk '/<style>/{s=1} s{o+=gsub(/{/,"{");c+=gsub(/}/,"}")} /<\/style>/{s=0} END{print o+0, c+0}' "$f")
  o=${res% *}; c=${res#* }
  if [ "$o" = "$c" ]; then echo "  ok   $f ($o)"; else echo "  FAIL $f ($o open / $c close)"; FAIL=1; fi
done

say "Structural integrity (critical functions + routes present)"
python3 - <<'PY'
import ast, sys
src = open("server.py").read()
tree = ast.parse(src)
fns = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
need = ["_atomic_write_json", "save_config", "load_config", "compute_assignment", "build_summary",
        "maybe_send_daily_digest", "record_access", "access_summary", "poller", "run_auto_draw",
        "discord_command", "_client_ip"]
missing = [f for f in need if f not in fns]
# every POST route should be reachable from do_POST source (guards against a str_replace eating a route)
routes = ['"/api/setup"', '"/api/save_draw"', '"/api/start_draw"', '"/api/settings"',
          '"/api/redraw"', '"/api/access_log"', '"/api/discord_summary"', '"/api/push_subscribe"',
          '"/api/export.csv"']
route_missing = [r for r in routes if r not in src]
if missing or route_missing:
    print("  FAIL  missing functions: %s | missing routes: %s" % (missing, route_missing)); sys.exit(1)
print("  ok   %d critical functions + %d routes present" % (len(need), len(routes)))
PY
if [ $? -ne 0 ]; then FAIL=1; fi

say "Scoring unit tests"
if python3 test_scoring.py; then echo "  ok"; else echo "  FAIL"; FAIL=1; fi

say "Unexpected-scenario tests (kickoff, forfeit, abandoned, corrections)"
if python3 test_scenarios.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 test_scenarios.py | grep FAIL; FAIL=1; fi

say "Full-tournament replay (2022: knockouts, penalties, champion)"
if python3 test_2022.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 test_2022.py | tail -8; FAIL=1; fi

say "Result-correctness tests (exact points, champion bonus, pens, survival, defence)"
if python3 test_results.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 test_results.py | tail -12; FAIL=1; fi

say "Wagering engine tests (odds, payout, caps, pre-kickoff lock, settlement, balances)"
if python3 test_wager.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 test_wager.py | tail -14; FAIL=1; fi

say "Betting QA (void lifecycle, mid-game/last-min void, accas, sequencing, free-points)"
if python3 qa_betting.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 qa_betting.py | tail -20; FAIL=1; fi

say "Bot command tests"
if python3 test_bot.py; then echo "  ok"; else echo "  FAIL"; FAIL=1; fi

say "Win-odds forecast (live-aware sim) tests"
if command -v node >/dev/null 2>&1; then
  if node test_sim.js; then echo "  ok"; else echo "  FAIL"; FAIL=1; fi
else
  echo "  (node not found — skipping; run this gate on your Mac for the JS checks)"
fi

say "Live smoke + security tests"
if python3 smoke_test.py; then echo "  ok"; else echo "  FAIL"; FAIL=1; fi

echo
if [ "$FAIL" = 0 ]; then echo "ALL CHECKS PASSED — safe to commit + push."; else echo "CHECKS FAILED — do NOT deploy."; fi
exit $FAIL
