#!/usr/bin/env python3
"""teams.json odds integrity — guards against a future hand-edit silently desyncing
the three outright fields (decimal/implied/american) or the match-odds composite.
Reads teams.json only; no server import. Run from the repo dir."""
import json, sys

FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  [%s]" % extra) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)

print("== teams.json odds integrity ==")
try:
    doc = json.load(open("teams.json"))
    teams = doc["teams"]
    ck("teams.json parses and has a team list", isinstance(teams, list) and len(teams) >= 24, "n=%s" % (len(teams) if isinstance(teams, list) else "?"))
except Exception as e:
    print("  FAIL could not load teams.json [%s]" % e)
    sys.exit(1)

names = [t.get("name") for t in teams]
ck("every team has a non-empty name", all(isinstance(n, str) and n for n in names), "")
ck("team names are unique", len(set(names)) == len(names), "dupes: %s" % [n for n in set(names) if names.count(n) > 1])

for t in teams:
    nm = t.get("name", "?")
    dec = t.get("decimal_odds"); imp = t.get("implied_prob"); am = t.get("american_odds"); comp = t.get("composite")
    ck("%s: has decimal_odds/implied_prob/composite" % nm, dec is not None and imp is not None and comp is not None, "")
    if dec is None or imp is None or comp is None:
        continue
    ck("%s: decimal_odds > 1.0" % nm, isinstance(dec, (int, float)) and dec > 1.0, dec)
    ck("%s: implied_prob in (0,1)" % nm, isinstance(imp, (int, float)) and 0.0 < imp < 1.0, imp)
    ck("%s: composite > 0" % nm, isinstance(comp, (int, float)) and comp > 0, comp)
    # implied must track 1/decimal (this file's convention: overround lives in the champion normalisation, not per team)
    if dec and dec > 1.0:
        ck("%s: implied_prob agrees with 1/decimal_odds" % nm, abs(imp - 1.0 / dec) <= 0.012, "imp %.4f vs 1/dec %.4f" % (imp, 1.0 / dec))
    # american must track decimal
    if am is not None and dec and dec > 1.0:
        exp = round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))
        ck("%s: american_odds agrees with decimal_odds" % nm, abs(am - exp) <= max(6, 0.04 * abs(exp)), "am %s vs ~%s" % (am, exp))

# field-level sanity: the implied probs carry an overround (sum>1) but stay sane for a ~48-team field
finite = [t for t in teams if isinstance(t.get("implied_prob"), (int, float))]
s = sum(t["implied_prob"] for t in finite)
ck("sum of implied_prob carries an overround (>1.0)", s > 1.0, "sum=%.3f" % s)
ck("sum of implied_prob is sane (<2.5)", s < 2.5, "sum=%.3f" % s)

# the favourite is internally consistent: shortest decimal == highest implied == (a) tier-1 team
have = [t for t in teams if isinstance(t.get("decimal_odds"), (int, float)) and isinstance(t.get("implied_prob"), (int, float))]
if have:
    by_dec = min(have, key=lambda t: t["decimal_odds"])
    by_imp = max(have, key=lambda t: t["implied_prob"])
    ck("the shortest-priced team is also the highest-implied (no inverted favourite)", by_dec["name"] == by_imp["name"], "%s vs %s" % (by_dec["name"], by_imp["name"]))
    ck("the favourite sits in tier 1", by_imp.get("tier") == 1, "%s tier=%s" % (by_imp["name"], by_imp.get("tier")))

# composite drives MATCH odds; make sure it's a usable spread (not all-equal / not degenerate)
comps = [t["composite"] for t in teams if isinstance(t.get("composite"), (int, float))]
ck("composites span a real range (match odds aren't degenerate)", comps and (max(comps) - min(comps)) > 5, "range=%.1f" % ((max(comps) - min(comps)) if comps else 0))

if FAILS:
    print("\nTEAMS INTEGRITY FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nAll teams.json odds-integrity checks passed.")
