# Lessons from WC26 — read this before the Euros

## The big one: betting was overpowered

Ismail won the sweepstake on 550.1 pts with roughly a **third of his total coming from betting**
(+162.7 at the QF stage alone). The match-points game — the actual sweepstake — was decided by who
played the book best. Two separate causes:

**1. The RESULT market was the leak.** Erol's verdict after living with it: *"winning at anytime
was the only good one"* — meaning it was the only market punters found worth hammering, and they
were right. The knockout advance book priced off our composite model with a modest margin and a
price cap, and our composites were sharper about longshots than about heavy favourites: backing
short favourites to advance was near-free compounding, and accas of favourites multiplied it.
Every *other* market (O/U, handicap, cards, BTTS, exact score, MoV) carried 13–40% books that
nobody could beat — which is why they felt "terrible" to bet and the result market felt great.
Run `python3 review.py` on the final data for the per-market punter-vs-house table.

**2. Unlimited upside.** Even a fair book loses sometimes; with stakes up to 1500-capped returns,
one landed acca outweighs weeks of goals and clean sheets.

### Retunes for the next event (all are one-line knobs now)

| Knob | Where | WC26 value | Euros suggestion |
|---|---|---|---|
| `BET_NET_CAP` | `wager.py` | `None` (off) | **±75–100 pts** — betting can flavour the leaderboard, never decide it |
| KO result margin | `wager.py` `match_odds` overround | ~1.08 | **1.15+**, and *lower* the favourite price cap so short prices shorten further |
| Stage max stakes | `stage_max_stake` | generous late | halve knockout-stage caps |
| Free points | `FREE_BET_STAKE` + drops | 5 + drops | keep — the cushion worked well |
| Per-market margins | `*_OVERROUND` | OU/hc 1.13–1.15, BTTS 1.40, MoV 1.25, cards 1.17 | fine — these held; it was never these markets leaking |

The margin asymmetry is the real fix: don't make the fun markets harsher (nobody bet them);
tighten the one market everyone actually beat.

### Draw fairness for next time
`review.py` now scores the DRAW itself: Spearman rank correlation between our seeding (composite)
and where teams actually finished, plus the biggest over/under-performers. Read it before building
the Euros `teams.json`: a low rho means the tiers should be FLATTER (compress weights so a "bad"
draw hurts less) and the composite should lean harder on bookmaker odds than FIFA ranking — books
priced this tournament far better than the rankings did. Also worth considering: value the draw's
leftover/pool teams by the same composite rather than treating them as free filler.

### What held up well
- The exploit gates: no dutch/arb/correlation farm ever landed (SGM joint pricing, trio floors,
  ladder rules, hedge blocks — all gate-proven and none was beaten in the wild).
- Premature-FT guard + auto-repair, banked-leg persistence, void-with-refund on missing data —
  settlement ended the tournament with a clean, reconciling ledger.
- Free-tier degradation: everything except cards/lineups/scorers runs on the free API.

### Ops lessons
- The feed's list endpoint ships **empty arrays** (`goals: []`) on some tiers — absence, not zero.
  Already fixed; keep `python3 update_results.py diag` as the first move when data looks missing.
- Feeds tick knockouts FINISHED at 90' before extra time. The `match_decided` guard is permanent.
- Keep the SSH key in `~/.ssh/` with `chmod 600`, never in Downloads, never in a repo or chat.

### Next-event checklist
1. `COMPETITION = "EC"` (setup wizard or config), regenerate `teams.json` for the field.
2. Set `BET_NET_CAP` and the result-book margin BEFORE the first game.
3. `alerts: "off"` stays in config until the draw is done.
4. Run the gate (`bash check.sh`) — `test_replay_wc26.py` replays the real WC26 as regression data.
