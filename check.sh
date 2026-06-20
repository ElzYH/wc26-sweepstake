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

say "Over/Under goals odds model (Poisson pricing, realism, margin, monotonicity, hostile inputs)"
if python3 test_ou_odds.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 test_ou_odds.py | tail -20; FAIL=1; fi

say "Over/Under placement (line/selection validation, kickoff lock, caps, return cap; result bets unchanged)"
if python3 test_ou_place.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 test_ou_place.py | tail -24; FAIL=1; fi

say "Over/Under settlement (final-goals golden vectors, push-free, pens excluded, void/hostile scores; result settle intact)"
if python3 test_ou_settle.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 test_ou_settle.py | tail -24; FAIL=1; fi

say "Over/Under accumulators (O/U legs, mixed with 1X2, combined odds, partial/void/losing-leg settle; 1X2 accas unchanged)"
if python3 test_ou_acca.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 test_ou_acca.py | tail -24; FAIL=1; fi

say "Betting QA (void lifecycle, mid-game/last-min void, accas, sequencing, free-points)"
if python3 qa_betting.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 qa_betting.py | tail -20; FAIL=1; fi

say "Deep betting QA (~111 checks: odds, limits, settlement, accas, free bets, money conservation, adversarial)"
if python3 qa_betting_deep.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 qa_betting_deep.py | tail -24; FAIL=1; fi

say "Deep scoring QA (~60 checks: point math, ownership, live points, survival, NO infinity/negative/crash)"
if python3 qa_scoring_deep.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 qa_scoring_deep.py | tail -24; FAIL=1; fi

say "Deep survival/forecast QA (~66 checks: alive/out, furthest-stage, champion odds, leaderboards, churn)"
if python3 qa_survival_deep.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 qa_survival_deep.py | tail -24; FAIL=1; fi

say "Concurrency QA (free claims strictly one-per-player-per-drop under load)"
if python3 qa_concurrency.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 qa_concurrency.py | tail -16; FAIL=1; fi

say "Bet-concurrency QA (~25 checks: simultaneous bets can't overspend/breach caps/go negative)"
if python3 qa_concurrency_bets.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 qa_concurrency_bets.py | tail -20; FAIL=1; fi

say "Settlement QA (FT, extra time, penalty shootout, abandoned, glitch guard)"
if python3 qa_settlement.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 qa_settlement.py | tail -20; FAIL=1; fi

say "Resilience QA (corruption recovery, empty-clobber guard, 6h snapshots)"
if python3 qa_resilience.py >/dev/null; then echo "  ok"; else echo "  FAIL"; python3 qa_resilience.py | tail -16; FAIL=1; fi

say "HTTP robustness QA (malformed/hostile input never 500s; server survives)"
if python3 qa_http.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_http.py 2>&1 | tail -16; FAIL=1; fi

say "Idempotency + live-edge QA (no double bets; odds/settle/compute survive weird data)"
if python3 qa_idempotency.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_idempotency.py 2>&1 | tail -16; FAIL=1; fi

say "End-to-end integration QA (real HTTP bets, live settlement + scoring together, concurrent over the wire)"
if python3 qa_integration.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_integration.py 2>&1 | tail -24; FAIL=1; fi
echo "[qa] tiebreak + claim-window (FIFA 2026 group order, whole-day drops)"
if python3 qa_tiebreak.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_tiebreak.py 2>&1 | tail -20; FAIL=1; fi

say "Bet-race QA (~35 checks: kickoff/void flip rejects a bet — engine matrix + real-HTTP flip + concurrency around a flip)"
if python3 qa_bet_race.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_bet_race.py 2>&1 | tail -30; FAIL=1; fi

say "Notification QA (opening-day kickoff/goal/full-time alerts fire; pre-tournament stays silent; no live-shuffle leader spam)"
if python3 qa_notify.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_notify.py 2>&1 | tail -20; FAIL=1; fi

say "Match-clock QA (real kickoff/half-time tracking: anchors, excludes HT, ticks accurately, never guesses without a feed minute)"
if python3 qa_clock.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_clock.py 2>&1 | tail -20; FAIL=1; fi

say "Stats QA (over/under-performer never collapses onto one team; reads right on a chalk result; flips on an upset)"
if python3 qa_stats.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_stats.py 2>&1 | tail -25; FAIL=1; fi

say "Admin/IO QA (~51 checks: caps clamping, export/import round-trip + secret whitelist, hostile payloads)"
if python3 qa_admin_io.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_admin_io.py 2>&1 | tail -28; FAIL=1; fi

say "Claims/pins QA (~41 checks: passcode set/change/no-hijack, deterministic drops, one-per-person free claim)"
if python3 qa_claims_pins.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_claims_pins.py 2>&1 | tail -24; FAIL=1; fi

say "Odds-audit QA (book overround, market lookup, house-edge integrity guard, auto matchday audit idempotency + resilience)"
if python3 qa_odds_audit.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_odds_audit.py 2>&1 | tail -24; FAIL=1; fi

say "Calibration QA (overlay loader, goals knob, every guard, integrity ABORT, 1000-case market fuzz: no crash / no underround / in-band)"
if python3 qa_calibration.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_calibration.py 2>&1 | tail -30; FAIL=1; fi

say "Odds display==placement QA (fixture-list odds priced from the same calibrated strengths as the bet slip; junk overlay ignored)"
if python3 qa_odds_display_match.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_odds_display_match.py 2>&1 | tail -30; FAIL=1; fi

say "Frontend QA (~58 checks: JS parses, XSS escaping, owner lookup, KO captions, 2-dp money, wheel draw, multi-page)"
if python3 qa_frontend.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_frontend.py 2>&1 | tail -22; FAIL=1; fi

say "Teams/odds integrity (~296 checks: per-team decimal/implied/american agree; favourite not inverted; composites usable)"
if python3 qa_teams_integrity.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_teams_integrity.py 2>&1 | tail -20; FAIL=1; fi

say "Discord QA (~34 checks: command dispatch + Ed25519 signature boundary — forged interactions rejected)"
if python3 qa_discord.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_discord.py 2>&1 | tail -22; FAIL=1; fi

say "Guild-gate QA (only Discord members can claim a name; safe-off until configured; fails closed)"
if python3 qa_guild.py >/dev/null 2>&1; then echo "  ok"; else echo "  FAIL"; python3 qa_guild.py 2>&1 | tail -16; FAIL=1; fi

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
