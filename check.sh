#!/usr/bin/env bash
# Pre-deploy gate for the WC26 sweepstake. Run from the repo root before every push:
#   ./check.sh
# Exits non-zero if anything fails, so you never ship a syntax error or broken scoring again.
set -u
cd "$(dirname "$0")"
FAIL=0
say(){ printf '\n=== %s ===\n' "$1"; }

say "Python syntax (ast.parse)"
for f in *.py; do
  if python3 -c "import ast,sys; ast.parse(open('$f').read())" 2>/tmp/pyerr; then
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

say "Scoring unit tests"
if python3 test_scoring.py; then echo "  ok"; else echo "  FAIL"; FAIL=1; fi

say "Live smoke + security tests"
if python3 smoke_test.py; then echo "  ok"; else echo "  FAIL"; FAIL=1; fi

echo
if [ "$FAIL" = 0 ]; then echo "ALL CHECKS PASSED — safe to commit + push."; else echo "CHECKS FAILED — do NOT deploy."; fi
exit $FAIL
