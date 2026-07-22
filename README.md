# World Cup 2026 Sweepstake

Weighted draw → spinning-wheel reveal → live tracker → a full margined sportsbook, run for five
friends through the whole of WC26 on a zero-dependency Python 3 stack (stdlib only, one Oracle
free-tier box). **WC26 final: Ismail 550.1 pts.** The live site is preserved as a static demo in
`wc26-demo/` and the complete tournament data ships as replay/test fixtures.

## Layout

| File | What it does |
|---|---|
| `teams.json` / `players.py` / `draw.py` / `main.py` | tiered weighted draw → `draw_result.json` |
| `wheel.html` | the live spinning-wheel reveal |
| `update_results.py` | football-data.org → `results.json`; tier-aware: near-live detail enrichment, finished-game backfill, deep-field carry, `diag` probe |
| `scoring.py` | results + draw → `tracker_data.json` (points / survival / both, odds, review data) |
| `wager.py` | the sportsbook: result, O/U goals, European 3-way handicap, BTTS, cards, method of victory, exact score; accas + jointly-priced same-game multis; settlement incl. the premature-FT guard |
| `server.py` | HTTP server, poller, Discord bot + web push alerts, admin |
| `tracker.html` | the whole frontend: live scores, leaderboards, bracket, betting, match sheets |
| `archive.py` / `review.py` | end-of-tournament preservation + analysis (see below) |
| `check.sh` | the gate: 120+ suites (unit, HTTP end-to-end, exploit sweeps, replay). **Green or no deploy.** |

## Scoring
**Points** (goal +1, win +3, draw +1, clean sheet +1, round bonuses) · **Survival** (furthest-stage
value) · **Both**. Betting winnings feed the leaderboard through a free-points cushion; `BET_NET_CAP`
in `wager.py` can cap the swing (see `LESSONS-WC26.md` — you probably want it on next time).

## Betting markets
Six markets per game, each a margined book with a price-ladder cap, priced off a shared Poisson
model of the two teams' composites. Accumulators across games; **same-game multis are priced off
the joint distribution** (never the leg product — correlation is priced, not given away). The
exploit surface — dutching, hedging, correlation farms, capped-price farming — is enumerated and
fuzz-tested in the gate.

## Feed tiers
Free tier: results, points, result/O-U/handicap/exact-score/BTTS betting, all score alerts.
Deep-data tier adds: cards betting (auto-detected), scorer-named goal alerts, line-ups +
line-ups-released alerts, red-card alerts, match-sheet timelines. Everything degrades gracefully —
undecidable bets push with a refund, absent markets hide rather than mis-settle.

## Publish YOUR tournament to GitHub (one command)
```
bash tools/publish_irl.sh          # box -> repo -> push (uses the `wc26` ssh alias)
```
Pulls the real replay data + static demo off the server, sanity-checks it, commits and pushes.
After that, straight from any clone: `python3 demo.py --mode irl --irl-bets` replays the real
tournament live with the real bets riding, `tools/make_demos.py` builds the GitHub-Pages replays,
and the gate's `test_replay_wc26.py` starts replaying the real tournament as regression data.

## End of tournament
```
python3 archive.py    # in the site dir: zip of every data file, static wc26-demo/, repo snapshots
python3 review.py     # participants / teams / betting-health report (--json to save)
```
`config.json: {"alerts": "off"}` silences notifications (they also auto-quiet once every game is
finished). `test_replay_wc26.py` replays the real tournament through the engine as regression data.

## Try it in 30 seconds (live demo, no API, no setup)
```
git clone https://github.com/ElzYH/wc26-sweepstake && cd wc26-sweepstake
python3 demo.py            # -> http://localhost:8000/tracker.html
```
A self-contained mini-app (the production server is untouched) boots with a random crew and a
simulated tournament that plays itself: games go live, finish, points score, and betting is fully
on through the real engine — odds, accas, same-game multis, settlement (everyone's PIN is `DEMO`).
Options:
`--speed fast|matchday|slow` (10/30/120s per tournament day), `--seed N`, `--players a,b,c`,
`--reset` for a fresh game. For static, GitHub-Pages-hostable replays of the real WC26, see
`tools/make_demos.py`.

## Run it
See `DEPLOY.md` (server) and `RESTORE.md` (disaster recovery). Setup wizard at `/` on first boot.
For the next tournament: `LESSONS-WC26.md` first, then set `COMPETITION` and regenerate `teams.json`.
