"""
Wagering engine for the WC26 sweepstake — Paddy-Power-style fractional odds on match results.

Design goals (this is points, not money, but treat it carefully):
  * You stake the points you've EARNED. Crucially, only SETTLED (finished-game) points count toward what's
    available to stake — never live/provisional points — so a VAR-disallowed goal can't retroactively
    overdraw you.
  * Balances can never go below zero (checked at placement; the displayed total is also floored).
  * Bets can ONLY be placed BEFORE kick-off. No cash-out. Odds are LOCKED at placement.
  * Hard caps (max stake, max return) so a single bet can't skew the standings.
  * Odds carry a bookmaker margin (overround) so the book sums to >100% like a real book.

This module is pure (no I/O) and isolated: scoring/server only apply it when wagering is switched on
AND wagers exist, so it is a complete no-op by default.
"""
import calendar
import math
import re
import time
import uuid

# ---- safety caps — all tuning lives here ----
MIN_STAKE = 1
MAX_STAKE = 30           # base single-bet cap (group stage); rises each knockout round (see STAGE_MAX_STAKE)
MAX_RETURN = None         # most a single bet can return; None = no limit (admin can set a number)
MAX_PENDING = 8           # most simultaneous open bets per player
MAX_ACCA_LEGS = 5         # default legs in one accumulator; admin can raise (up to 10)
MAX_ACTIVE_ACCAS = 2      # most simultaneous OPEN accumulators per player (single bets are unlimited)
BLOCK_OPPOSING_BETS = True  # admin toggle: when on, a player can't hold result bets on two different outcomes of the same match
FREE_BET_STAKE = 5        # a claimed free bet stakes this many points; the stake is NEVER credited — only winnings (profit) count
STARTING_BONUS = 5        # everyone starts with this many free betting points so they can bet before earning any.
                          # It's bet-only: it never sits on the leaderboard, and it cushions the first 5 of net losses.
STAGE_BUDGET = 50         # base staking allowance (group stage). Rises +5 each knockout round (see STAGE_BUDGET_MAP),
                          # and fully regenerates each "epoch": group 1st half, group 2nd half, then every KO round.
STAGE_BUDGET_MAP = {      # per-epoch budget — always 20 above that round's per-bet cap, so the cap is always reachable.
    "GROUP_1":         50, "GROUP_2": 50,
    "LAST_32":         55,
    "LAST_16":         60,
    "QUARTER_FINALS":  65,
    "SEMI_FINALS":     70,
    "THIRD_PLACE":     75,
    "FINAL":           80,
    "WINNER":          80,
    "KO_EARLY": 75, "KO_LATE": 95,   # merged blocks: R32+R16 share one pot, QF->final share another
}
                          # Resets automatically because budget_remaining only sums bets within the same epoch.
OVERROUND = 1.08          # ~8% bookmaker margin
MAX_PROB = 0.857          # shortest RESULT price is 1/6 (6/7 implied) — favourites never pay worse than 1/6
OU_MAX_PROB = 0.93        # O/U gets a DEEPER ladder (down to ~1/14): near-certainties (Under 4.5) must pay
                          #   visibly worse than a 1/6 favourite — a likelier outcome can't share its price.
                          #   With the ladder rule (fair <= this cap) implied is ALWAYS >= fair, so the old
                          #   capped-value residual on O/U is gone entirely, not just filtered.

# Max stake rises as the tournament gets deeper: +5 per knockout round, and an extra +15 for the final.
# WC2026 round dates (UTC, for reference — actual dates come from the live fixture feed):
#   Group stage 11–27 Jun · Round of 32 28 Jun–3 Jul · Round of 16 4–7 Jul · Quarter-finals 9–11 Jul ·
#   Semi-finals 14–15 Jul · Third place 18 Jul · Final 19 Jul. The 50-pt budget also resets at each of these.
STAGE_MAX_STAKE = {
    "GROUP_STAGE":     30,
    "LAST_32":         35,
    "LAST_16":         40,
    "QUARTER_FINALS":  45,
    "SEMI_FINALS":     50,
    "THIRD_PLACE":     55,
    "FINAL":           60,     # +5 every round: 30 → 35 → 40 → 45 → 50 → 55 → 60
    "WINNER":          60,
}


def stage_max_stake(stage):
    """Single-bet cap for a match in the given stage (defaults to the group-stage base)."""
    return STAGE_MAX_STAKE.get(stage, MAX_STAKE)


SELECTIONS = ("HOME", "DRAW", "AWAY")
OPEN_STATUSES = ("SCHEDULED", "TIMED")
VOID_STATUSES = ("CANCELLED", "POSTPONED", "ABANDONED")

# common British betting fractions (num, den) — placement snaps to the nearest of these
_FRACTIONS = [(1, 20), (1, 16), (1, 14), (1, 12), (1, 10), (1, 9), (1, 8), (1, 7), (1, 6),
              (1, 5), (2, 9), (1, 4), (2, 7), (3, 10), (1, 3), (4, 11), (2, 5), (4, 9), (1, 2),
              (8, 15), (4, 7), (8, 13), (4, 6), (8, 11), (4, 5), (5, 6), (10, 11), (1, 1), (11, 10),
              (6, 5), (5, 4), (11, 8), (6, 4), (13, 8), (7, 4), (15, 8), (2, 1), (9, 4), (5, 2),
              (11, 4), (3, 1), (7, 2), (4, 1), (9, 2), (5, 1), (11, 2), (6, 1), (13, 2), (7, 1),
              (15, 2), (8, 1), (9, 1), (10, 1), (12, 1), (14, 1), (16, 1), (20, 1), (25, 1), (33, 1),
              (40, 1), (50, 1), (66, 1), (80, 1), (100, 1), (150, 1), (200, 1), (250, 1), (300, 1), (400, 1), (500, 1)]


def _dec(fr):
    return 1.0 + fr[0] / fr[1]


def _nearest_fraction(decimal):
    return min(_FRACTIONS, key=lambda fr: abs(_dec(fr) - decimal))


def _floor_fraction(decimal):
    """Largest ladder rung that does NOT pay more than `decimal` — snapping a price DOWN in payout terms is
    always house-side, so a market using this can never round itself punter-positive (the exact-score tail
    rungs are sparse enough that nearest-snap could otherwise land above fair value)."""
    under = [fr for fr in _FRACTIONS if _dec(fr) <= decimal + 1e-9]
    return max(under, key=_dec) if under else min(_FRACTIONS, key=_dec)


def _fair_probs(ch, ca):
    """Home/draw/away probabilities from team strength — same shape as the tracker's win-prob model."""
    def _fin(x):
        try:
            x = float(x)
        except (TypeError, ValueError):
            return 40.0
        if x != x or x in (float("inf"), float("-inf")) or x < 0:   # NaN / inf / negative -> neutral default
            return 40.0
        return x
    ch = _fin(ch) + 1
    ca = _fin(ca) + 1
    pw = ch / (ch + ca)
    edge = abs(pw - 0.5)
    pd = max(0.12, 0.30 - edge * 0.55)
    ph = pw * (1 - pd)
    pa = (1 - pw) * (1 - pd)
    s = ph + pd + pa
    return ph / s, pd / s, pa / s


def is_knockout(match_or_stage):
    """True for any knockout stage (i.e. not the group stage / pre-tournament). Accepts a match dict or a stage string."""
    s = match_or_stage.get("stage") if isinstance(match_or_stage, dict) else match_or_stage
    return s not in (None, "GROUP_STAGE")


def match_odds(comp_home, comp_away, knockout=False):
    """1X2 prices with a bookmaker margin: {'HOME': {'frac','num','den','decimal'}, 'DRAW':..., 'AWAY':...}.

    On a KNOCKOUT the draw isn't offered or settled (a shootout decides who goes through), so the DRAW is
    dropped and we price a 2-way 'to advance' book instead: the draw probability is folded into Home/Away
    (split by strength) and the SAME house edge is applied to just those two, so the offered prices sum to
    ~OVERROUND and backing both sides still LOSES the margin — no risk-free hedge. If a heavy favourite hits
    the shortest-price cap (MAX_PROB) the leftover margin is loaded onto the underdog, so a 2-way book can
    never drop under 100%."""
    ph, pd, pa = _fair_probs(comp_home, comp_away)
    if knockout:
        tot = ph + pa
        ph, pa = (0.5, 0.5) if tot <= 0 else (ph / tot, pa / tot)   # fold the draw in -> a true 2-way market summing to 1.0
        ih = min(MAX_PROB, ph * OVERROUND)
        ia = min(MAX_PROB, pa * OVERROUND)
        short = OVERROUND - ih - ia                                  # a heavy favourite hits the 1/6 price cap and can't
        if short > 0:                                                #   carry the full margin -> load the rest onto the
            if ih >= ia:                                            #   underdog (whichever side that is) so the 2-way
                ia = min(MAX_PROB, ia + short)                      #   book still sums to ~OVERROUND -> no risk-free hedge
            else:
                ih = min(MAX_PROB, ih + short)
        pairs = (("HOME", ih), ("AWAY", ia))
    else:
        pairs = tuple((sel, min(MAX_PROB, p * OVERROUND)) for sel, p in (("HOME", ph), ("DRAW", pd), ("AWAY", pa)))
    out = {}
    for sel, implied in pairs:
        num, den = _nearest_fraction(1.0 / implied)
        out[sel] = {"frac": "%d/%d" % (num, den), "num": num, "den": den, "decimal": round(_dec((num, den)), 3)}
    return out


# ---- Over/Under total-goals market (Poisson model, priced like the 1X2 book) -------------------
GOALS_BASE = 2.6              # expected total goals for an evenly-matched game (~World Cup average)
GOALS_GAP_COEF = 1.2          # mismatches trend a little higher-scoring (the favourite runs the score up)
GOALS_LAMBDA_MIN = 1.6        # clamp the match goal expectation to a sane band so no line is mispriced
GOALS_LAMBDA_MAX = 4.2
OU_OVERROUND = 1.13          # a touch more margin than the 1X2 book (1.08) -> O/U returns trimmed ~3-4%,
                             #   which also keeps multi-leg O/U accas from paying out silly amounts
OU_MIN_MARGIN = 0.02         # minimum book overround on any offered O/U line (so a near-certain line still has an edge)
OU_LINES = [0.5, 1.5, 2.5, 3.5, 4.5, 5.5]   # half-lines only -> a bet can never push; a line is offered only when both sides fit under MAX_PROB


def _finite_comp(x):
    """Coerce a composite to a sane non-negative float (NaN / inf / negative / junk -> neutral 40)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 40.0
    if x != x or x in (float("inf"), float("-inf")) or x < 0:
        return 40.0
    return x


def expected_goals(comp_home, comp_away):
    """Match total-goals expectation (Poisson mean) from team strengths. Even game -> GOALS_BASE;
    a bigger strength gap nudges it up. Clamped to [GOALS_LAMBDA_MIN, GOALS_LAMBDA_MAX]."""
    ch = _finite_comp(comp_home) + 1
    ca = _finite_comp(comp_away) + 1
    pw = ch / (ch + ca)
    edge = abs(pw - 0.5)                       # 0 (even) .. 0.5 (total mismatch)
    lam = GOALS_BASE + GOALS_GAP_COEF * edge
    return max(GOALS_LAMBDA_MIN, min(GOALS_LAMBDA_MAX, lam))


def _poisson_cdf(n, lam):
    """P(X <= n) for a Poisson(lam), summed iteratively so factorials never overflow."""
    if n < 0:
        return 0.0
    term = math.exp(-lam)                      # k = 0 term
    cdf = term
    for k in range(1, n + 1):
        term *= lam / k
        cdf += term
    return min(1.0, cdf)


def goals_odds(comp_home, comp_away, lines=None):
    """Over/Under odds for each total-goals line, same dict shape + margin as match_odds().
    Returns {'2.5': {'OVER': {...}, 'UNDER': {...}}, ...}. A line L = n+0.5 settles on total goals:
    OVER wins if total >= n+1, UNDER wins if total <= n (half-lines, so never a push)."""
    lam = expected_goals(comp_home, comp_away)
    out = {}
    for L in (lines or OU_LINES):
        n = int(L)                              # floor of the half-line, e.g. 2.5 -> 2
        p_under = min(0.999, max(1e-6, _poisson_cdf(n, lam)))   # total <= n
        p_over = min(0.999, max(1e-6, 1.0 - p_under))           # total >= n+1
        # A line is only OFFERED when BOTH sides fit inside the price ladder (fair prob <= OU_MAX_PROB).
        # Beyond that, the near-certain side would sit at the 1/6 price floor while being 93-99% true —
        # a permanent bettor edge anyone could farm every match (e.g. Under 5.5 in a knockout, priced the
        # same 1/6 as backing the favourite but far likelier). Real books don't quote a line they can't
        # price inside their ladder; neither do we. Settlement of already-placed bets is unaffected —
        # it uses the bet's stored line and locked odds, never today's offering.
        if p_over > OU_MAX_PROB or p_under > OU_MAX_PROB:
            continue
        iO = min(OU_MAX_PROB, p_over * OU_OVERROUND)
        iU = min(OU_MAX_PROB, p_under * OU_OVERROUND)
        # Guarantee a house edge on EVERY line so even 0.5 on a lopsided game stays on the board (not dropped).
        # On a line far from the expected total one side is near-certain and caps at OU_MAX_PROB, which alone leaves
        # the book < 100%. Lift the underdog (smaller) side to a minimum-margin book; it's still a long price, just
        # not a bettor-edge one — exactly how a real book quotes a near-certain Over/Under.
        target = 1.0 + OU_MIN_MARGIN
        if iO + iU < target:
            if iO <= iU:
                iO = min(OU_MAX_PROB, target - iU)
            else:
                iU = min(OU_MAX_PROB, target - iO)
        leg = {}
        for _ in range(8):                      # rebuild + re-check after fraction rounding; nudge the underdog if a round trip dipped the book under 100%
            leg = {}
            for sel, implied in (("OVER", iO), ("UNDER", iU)):
                num, den = _nearest_fraction(1.0 / implied)
                leg[sel] = {"frac": "%d/%d" % (num, den), "num": num, "den": den, "decimal": round(_dec((num, den)), 3)}
            if (1.0 / leg["OVER"]["decimal"]) + (1.0 / leg["UNDER"]["decimal"]) > 1.0 + 1e-6:
                break
            if iO <= iU:
                iO = min(OU_MAX_PROB, iO + 0.01)
            else:
                iU = min(OU_MAX_PROB, iU + 0.01)
        out[_line_key(L)] = leg                 # every OFFERED line overrounds and neither side beats the ladder
    return out


def _line_key(L):
    """Stable string key for a line: '2.5' (kept as given; halves only)."""
    return ("%g" % float(L))


FORM_SWING = 0.12             # tournament form can move a team's strength by at most ±12% (FIFA ranking still dominates)


def team_form(team, matches):
    """A bounded form multiplier (~0.88..1.12) from a team's FINISHED games so far.
    Returns 1.0 until they've played once. Uses win/draw/loss plus a capped goal-difference nudge.
    Pure function of results -> deterministic (same data, same number), so the board never wobbles."""
    played, score = 0, 0.0
    for m in matches or []:
        if m.get("status") not in ("FINISHED", "AWARDED"):
            continue                                # only settled results count — a live half-time score must not move odds
        if team not in (m.get("home"), m.get("away")):
            continue
        hs, as_ = m.get("homeScore"), m.get("awayScore")
        if hs is None or as_ is None:
            continue
        try:
            hs = float(hs); as_ = float(as_)
        except (TypeError, ValueError):
            continue                                # a non-numeric score (bad data) can't poison the odds
        if hs != hs or as_ != as_ or hs in (float("inf"), float("-inf")) or as_ in (float("inf"), float("-inf")):
            continue                                # NaN / inf guard
        is_home = (m.get("home") == team)
        gf, ga = (hs, as_) if is_home else (as_, hs)
        side = _winner_side(m)
        if side == "DRAW":
            res = 0.0
        elif side == ("HOME" if is_home else "AWAY"):
            res = 1.0
        else:
            res = -1.0
        gd = max(-3, min(3, gf - ga)) / 3.0       # goal difference, capped to ±3 then scaled to ±1
        score += res + 0.5 * gd                    # one game ranges roughly -1.5 (heavy loss) .. +1.5 (big win)
        played += 1
    if played == 0:
        return 1.0
    f = max(-1.0, min(1.0, (score / played) / 1.5))   # average per game, normalised to -1..+1
    return 1.0 + FORM_SWING * f


def live_strength(base, team, matches):
    """A team's base FIFA strength nudged by current tournament form (bounded). Used only for pricing odds —
    never touches an already-placed bet, which keeps the odds it was struck at."""
    try:
        b = float(base or 0)
    except (TypeError, ValueError):
        b = 0.0
    if b != b or b in (float("inf"), float("-inf")):   # NaN / inf -> treat as unknown strength
        b = 0.0
    return b * team_form(team, matches)


def potential_return(stake, num, den):
    """Total returned if it wins = stake + profit (profit = stake * num/den). e.g. 5 @ 9/2 -> 27.5."""
    return round(stake * (1.0 + num / den), 2)


def match_id(m):
    if m.get("id") not in (None, ""):
        return str(m["id"])
    return "%s|%s|%s" % (m.get("home"), m.get("away"), (m.get("utcDate") or "")[:16])


def _utc_ts(iso):
    try:
        return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def _winner_side(m):
    w = m.get("winner")
    if w in ("HOME", "AWAY", "DRAW"):
        return w
    hs, as_ = m.get("homeScore"), m.get("awayScore")
    if hs is None or as_ is None:
        return None
    try:
        hs = float(hs); as_ = float(as_)
    except (TypeError, ValueError):
        return None                                 # junk-typed score: unreadable, and it must NEVER raise —
                                                    #   settle() calls this once per match, so an exception here
                                                    #   would abort settlement of EVERY pending bet on the pass
    if hs == as_:                                   # level after 90/120 mins
        ph, pa = m.get("penHome"), m.get("penAway")
        if ph is not None and pa is not None and ph != pa:
            return "HOME" if ph > pa else "AWAY"     # a shootout decides who advances (counts as a win)
    return "HOME" if hs > as_ else ("AWAY" if as_ > hs else "DRAW")


def can_bet_on(match, now=None):
    """Only before kick-off: status must be pre-match AND the kick-off time must still be in the future."""
    if not isinstance(match, dict):
        return False
    if match.get("status") not in OPEN_STATUSES:
        return False
    ts = _utc_ts(match.get("utcDate") or "")
    if ts is not None and ts <= (now if now is not None else time.time()):
        return False
    return True


def _num(x, default=0.0):
    """Safe float: a non-numeric / NaN / inf value (corrupt or old-format record) becomes the default
    instead of throwing. Used wherever the wager log feeds money math on the hot path."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


CANCEL_CUTOFF_S = 2 * 3600   # players can void their own pending bets until this long before kick-off


def player_cancel(wagers, player, bet_id, matches_by_id, now=None):
    """A player voids their OWN pending bet, up to CANCEL_CUTOFF_S before kick-off (the EARLIEST leg for an
    acca). The cutoff is enforced HERE against the fixture's utcDate — server-side truth, so no stale page,
    cached clock or replayed request can slip a late void through. Refund uses standard void semantics
    (stake back; voids never count against the budget; board/cushion recompute). Admin cancel stays a
    separate, unrestricted path. Mutates the record in place; returns (ok, wager_or_error)."""
    now = time.time() if now is None else now
    w = next((x for x in (wagers or []) if isinstance(x, dict) and x.get("id") == bet_id), None)
    if not w:
        return False, "That bet could not be found."
    if w.get("player") != player:
        return False, "That bet isn't yours."
    if w.get("credit"):
        return False, "That's not a bet."
    if w.get("status") != "pending":
        return False, "That bet has already settled — too late to void."
    kos = []
    for lg in (w.get("legs") or [w]):
        m = (matches_by_id or {}).get(lg.get("matchId"))
        ts = _utc_ts((m or {}).get("utcDate") or "")
        if ts is None:
            return False, "Can't verify kick-off for this bet — ask the organiser to void it."
        kos.append(ts)
    if now > min(kos) - CANCEL_CUTOFF_S:
        return False, "Too late to void — bets lock in 2 hours before kick-off."
    w["status"] = "void"
    w["return"] = _num(w.get("stake"))
    w["settled_at"] = int(now)
    w["cancelled_by"] = "player"
    for lg in (w.get("legs") or []):
        lg["result"] = "void"
    return True, w


def player_deltas(wagers):
    """Per-player effect of the wager log: settled profit/loss, points held in open bets, open count.
    Defensive: a malformed record (not a dict, missing/blank player, non-numeric stake/return) is skipped
    rather than crashing — this runs on EVERY request, so one bad row must never take down the
    leaderboard, the odds, or /api/status."""
    out = {}
    seen = set()                                         # dedup guard against a duplicated record (the same bet written
                                                         # to the log twice). Every bet gets a unique uuid `id` at
                                                         # placement and settle() mutates it IN PLACE, so the same id —
                                                         # or the same placement nonce — appearing twice is always an
                                                         # accidental duplicate, never two distinct wagers. Counting it
                                                         # once therefore can't undercount real bets (which have
                                                         # distinct ids), it only neutralises a double-write.
    for w in wagers or []:
        if not isinstance(w, dict):
            continue
        player = w.get("player")
        if not player or not isinstance(player, str):
            continue
        _keys = [k for k in (("id", w.get("id")),
                             ("nonce", player, w.get("nonce"))) if k[-1]]
        if _keys and any(k in seen for k in _keys):      # already counted this exact record -> skip the duplicate
            continue
        for k in _keys:
            seen.add(k)
        if w.get("credit"):                              # a claimed free-points drop: not a bet (see free_bonus); skip
            continue
        stake = _num(w.get("stake"))
        ret = _num(w.get("return"))
        d = out.setdefault(player, {"settled_net": 0.0, "pending_stake": 0.0, "pending_count": 0})
        st = w.get("status")
        if w.get("free"):                                # a free bet: only a WIN matters, and only its profit counts.
            if st == "won":                              # the stake was never the player's, so credit return - stake (profit).
                d["settled_net"] += (ret - stake)
            continue                                     # pending/lost/void free bets hold nothing and cost nothing
        if st == "pending":
            d["pending_stake"] += stake
            d["pending_count"] += 1
        elif st == "won":
            d["settled_net"] += (ret - stake)            # profit only (stake is returned)
        elif st == "lost":
            d["settled_net"] -= stake
        # "void" -> no effect (stake refunded)
    return out


def _norm_stage(s):
    return s or "GROUP_STAGE"


def epoch_of(match, group_mid_ts=None):
    """Which staking 'epoch' a match falls in. The group stage splits in two at group_mid_ts
    (the calendar midpoint of the group games); each knockout round is its own epoch. Every epoch
    gets a fresh STAGE_BUDGET, so the allowance resets at the group midpoint and at each KO round."""
    stage = _norm_stage(match.get("stage"))
    if stage == "GROUP_STAGE":
        ts = _utc_ts(match.get("utcDate") or "")
        if group_mid_ts is not None and ts is not None and ts >= group_mid_ts:
            return "GROUP_2"
        return "GROUP_1"
    return _epoch_group(stage)


_EPOCH_COLLAPSE = {"LAST_32": "KO_EARLY", "LAST_16": "KO_EARLY",
                   "QUARTER_FINALS": "KO_LATE", "SEMI_FINALS": "KO_LATE",
                   "THIRD_PLACE": "KO_LATE", "FINAL": "KO_LATE"}


def _epoch_group(e):
    """Knockout budget epochs are TWO blocks (R32+R16, then QF through the final) instead of per-round.
    Normalises both freshly-derived epochs AND the tags stored on old bet records, so bets placed under
    the per-round scheme keep counting against the right block — the merge can never refill a budget."""
    return _EPOCH_COLLAPSE.get(e, e)


def stage_budget(epoch):
    """Per-round staking budget for an epoch (group halves 50, then +5 each knockout round). Regenerates
    each round automatically because budget_remaining only sums bets tagged with the same epoch."""
    return STAGE_BUDGET_MAP.get(epoch, STAGE_BUDGET)


def budget_remaining(wagers, player, epoch, budget=None):
    """Points a player can still stake in this epoch:
    budget - (stakes placed this epoch) + (returns from won bets this epoch), clamped to [0, budget].
    Losing leaves the budget down (you climb back only by winning); winnings top it up, never above budget.
    Void bets refund so they don't count. Resets per epoch because we only sum bets tagged with this `epoch`.
    The budget defaults to this epoch's allowance (stage_budget) — group 50, rising +5 each knockout round."""
    if budget is None:
        budget = stage_budget(epoch)
    spent = 0.0
    back = 0.0
    for w in wagers or []:
        if w.get("player") != player or _epoch_group(w.get("epoch")) != epoch or w.get("free") or w.get("credit"):
            continue                                    # free bets & free-point credits sit outside the staking budget entirely
        st = w.get("status")
        if st in ("pending", "won", "lost"):       # void = refunded, ignore
            spent += w.get("stake", 0) or 0
        if st == "won":
            back += w.get("return", 0) or 0
    return max(0.0, min(float(budget), round(budget - spent + back, 2)))


def free_bonus(player, wagers):
    """A player's total FREE betting points: the 5 everyone starts with, plus 5 for each free-points drop they've
    claimed. These never sit on the leaderboard — they let you bet, and they cushion the first `free_bonus` of net
    losses (so only genuine winnings, and losses beyond your free points, move the leaderboard)."""
    extra = sum(_num(w.get("amount")) for w in (wagers or [])
                if isinstance(w, dict) and w.get("credit") and w.get("player") == player)
    return float(STARTING_BONUS + extra)


def available_points(player, settled_points, wagers):
    """Points a player can still stake: their earned points + their free points (starting bonus + claimed drops)
    + settled bet profit/loss - points already on open bets, floored at 0."""
    d = player_deltas(wagers).get(player, {})
    return max(0.0, round(settled_points + free_bonus(player, wagers) + d.get("settled_net", 0.0) - d.get("pending_stake", 0.0), 2))


def leaderboard_net(player, wagers, bonus=None):
    """The bet profit/loss that should hit a player's LEADERBOARD total. The free points (and any free-bet winnings
    already in settled_net) mean the first `bonus` points of net losses are absorbed and never cost real points —
    only genuine winnings, and losses beyond the free points, move the leaderboard."""
    b = free_bonus(player, wagers) if bonus is None else float(bonus)
    net = player_deltas(wagers).get(player, {}).get("settled_net", 0.0)
    return round(net + min(b, max(0.0, -net)), 2)


def grant_free_points(wagers, player, drop_id, amount=None, now=None):
    """Claim a free-points drop: append a non-bet credit that boosts the player's free betting points by `amount`.
    Returns (ok, credit_record). Caller is responsible for one-claim-per-drop enforcement."""
    if not player or player in ("—", "-"):
        return False, "Pick which player is claiming first."
    amt = FREE_BET_STAKE if amount is None else amount
    rec = {"id": uuid.uuid4().hex[:12], "player": player, "credit": True, "amount": amt,
           "drop": drop_id, "status": "credit", "placed_at": int(now if now is not None else time.time())}
    wagers.append(rec)
    return True, rec


def leaderboard_held(player, wagers, bonus=None):
    """How much of a player's OPEN-bet stakes should come OFF their leaderboard total. Open stakes are covered by
    the free bonus first — the same bonus that cushions losses — so stakes funded by free points never drag a
    player's real standing down (free points were never on the leaderboard to begin with). Only stakes riding on
    genuinely earned points (those beyond the free cushion) are held off the board. This keeps the leaderboard
    consistent with available_points and with 'losing your free points costs you nothing'."""
    d = player_deltas(wagers).get(player, {})
    pend = d.get("pending_stake", 0.0)
    if pend <= 0:
        return 0.0
    b = free_bonus(player, wagers) if bonus is None else float(bonus)
    net = d.get("settled_net", 0.0)
    free_left = max(0.0, b - max(0.0, -net))          # free bonus left after cushioning any settled losses
    return round(max(0.0, pend - free_left), 2)


def applied_points(base_points, player, wagers):
    """A player's displayed points once wagers are applied: base + leaderboard net (free-cushioned) - the real
    (free-cushioned) held stake, floored at 0. Free-bonus-funded open bets don't pull the leaderboard down, so a
    settled win still shows even while other stakes are riding."""
    return max(0.0, round(base_points + leaderboard_net(player, wagers) - leaderboard_held(player, wagers), 2))


def _open_result_picks(wagers, player):
    """{matchId: set(result selections)} the player ALREADY has riding on OPEN bets — across singles AND acca legs.
    Over/Under and handicap legs are ignored (those markets carry their own margin, their HOME/AWAY are
    goal-margin picks rather than match-result picks, and neither is a both-sides arb)."""
    out = {}
    for w in (wagers or []):
        if w.get("player") != player or w.get("status") != "pending" or w.get("credit"):
            continue
        rows = w.get("legs") or [w]
        for r in rows:
            if (str(r.get("market") or "result")).lower() not in ("ou", "hc"):
                mid, sel = r.get("matchId"), r.get("selection")
                if mid and sel in SELECTIONS:
                    out.setdefault(mid, set()).add(sel)
    return out


def _hedges_open(wagers, player, mid, selection):
    """True if backing result `selection` on match `mid` would oppose a result bet the player already has open
    (i.e. they'd hold two different outcomes of the same match — both sides). Same-side re-backs are allowed.
    Returns False outright when the admin has switched the block off (BLOCK_OPPOSING_BETS)."""
    if not BLOCK_OPPOSING_BETS:
        return False
    have = _open_result_picks(wagers, player).get(mid)
    return bool(have) and any(p != selection for p in have)


_HEDGE_MSG = "You've already got a bet on a different outcome in this game — you can't back both sides of the same match."


def place(wagers, player, match, selection, stake, settled_points, comp_home, comp_away, now=None, group_mid_ts=None, market="result", line=None):
    """
    Validate and append a pending wager. Returns (ok, wager_or_error_string).
    `wagers` is mutated only on success. The server prices the match here (odds can't be spoofed by the client).
    market="result" -> 1X2 (HOME/DRAW/AWAY). market="ou" -> Over/Under total goals (selection OVER/UNDER, `line` a half-line).
    """
    if not player or player in ("—", "-"):
        return False, "Pick which player is betting first."
    if not isinstance(match, dict):
        return False, "That game could not be found."
    market = (str(market or "result")).lower()
    if market == "cs":
        if not isinstance(selection, str) or not re.fullmatch(r"[0-%d]-[0-%d]" % (CS_GRID_MAX, CS_GRID_MAX), selection):
            return False, "Pick a scoreline — home goals then away, each 0-%d." % CS_GRID_MAX
        # exact-score cells are mutually exclusive + margin-protected, so the no-hedging block doesn't apply
    elif market == "ou":
        if selection not in ("OVER", "UNDER"):
            return False, "Pick Over or Under."
        try:
            line = float(line)
        except (TypeError, ValueError):
            return False, "Pick a goals line."
        if line not in OU_LINES:
            return False, "That goals line isn't offered."
    elif market == "hc":
        if selection not in ("HOME", "AWAY"):
            return False, "Pick a side for a handicap bet — home or away."
        try:
            line = float(line)
        except (TypeError, ValueError):
            return False, "Pick a handicap line."
        if line not in HC_LINES:
            return False, "That handicap line isn't offered."
        # a handicap book carries its own margin and its two sides can't both win, so backing both is a
        # guaranteed margin LOSS, never an arb — like O/U it's exempt from the result opposing-bet block
    else:
        if selection not in SELECTIONS:
            return False, "Pick home, draw or away."
        if selection == "DRAW" and match.get("stage") not in (None, "GROUP_STAGE"):
            return False, "No draw bets on knockout games — pick the side to go through."
        if _hedges_open(wagers, player, match_id(match), selection):
            return False, _HEDGE_MSG
    if not can_bet_on(match, now):
        return False, "Betting on that game is closed — it has kicked off or finished."
    try:
        stake = round(float(stake), 2)
    except (TypeError, ValueError):
        return False, "Enter a number of points to stake."
    if stake != stake or stake in (float("inf"), float("-inf")):   # NaN / inf guard
        return False, "Enter a valid stake."
    if stake <= 0:
        return False, "You have to stake at least %d point(s) — you can't bet nothing." % MIN_STAKE
    if stake < MIN_STAKE:
        return False, "Minimum stake is %d point(s)." % MIN_STAKE
    cap = stage_max_stake(match.get("stage"))
    if stake > cap:
        return False, "Max stake here is %d points (it rises each knockout round)." % cap
    d = player_deltas(wagers).get(player, {})
    if d.get("pending_count", 0) >= MAX_PENDING:
        return False, "You already have %d open bets — settle some first." % MAX_PENDING
    pending = d.get("pending_stake", 0.0)
    if pending + stake > cap + 1e-9:
        return False, ("You can have at most %d points riding on open bets at once — you've already got %g out there, "
                       "so you can add %g more until one settles." % (cap, pending, round(max(0.0, cap - pending), 2)))
    avail = available_points(player, settled_points, wagers)
    if stake > avail + 1e-9:
        return False, "You only have %g points available to stake." % avail
    epoch = epoch_of(match, group_mid_ts)              # per-round staking budget (both this and the per-bet cap apply)
    eb = stage_budget(epoch)
    brem = budget_remaining(wagers, player, epoch)
    if brem <= 1e-9:
        return False, ("You've used up your %d-point staking budget for this round. It resets at the next round "
                       "(the group-stage midpoint, then each knockout round) — you can't bet again until then." % eb)
    if stake > brem + 1e-9:
        return False, ("Your staking budget left this round is %g of %d points — stake that or less. "
                       "It tops back up when your bets win (never above %d) and resets next round." % (brem, eb, eb))
    if market == "cs":
        odds = cs_odds(comp_home, comp_away).get(selection)
    elif market == "ou":
        odds = goals_odds(comp_home, comp_away).get(_line_key(line), {}).get(selection)
    elif market == "hc":
        odds = hc_odds(comp_home, comp_away).get(_line_key(line), {}).get(selection)
    else:
        odds = match_odds(comp_home, comp_away, knockout=is_knockout(match)).get(selection)
    if not odds:
        return False, "Couldn't price that bet — try again."
    ret = potential_return(stake, odds["num"], odds["den"])
    if MAX_RETURN is not None and ret > MAX_RETURN + 1e-9:
        return False, "That would return %g — the cap is %g per bet. Lower your stake." % (ret, MAX_RETURN)
    w = {"id": uuid.uuid4().hex[:12], "player": player, "matchId": match_id(match),
         "home": match.get("home"), "away": match.get("away"), "stage": match.get("stage"),
         "utcDate": match.get("utcDate"), "selection": selection, "stake": stake, "epoch": epoch,
         "num": odds["num"], "den": odds["den"], "frac": odds["frac"], "return": ret,
         "status": "pending", "placed_at": int(now if now is not None else time.time())}
    if market == "ou":
        w["market"] = "ou"
        w["line"] = line
    elif market == "hc":
        w["market"] = "hc"
        w["line"] = line
    elif market == "cs":
        w["market"] = "cs"
    wagers.append(w)
    return True, w


def place_free(wagers, player, match, selection, comp_home, comp_away, now=None):
    """Place a CLAIMED free bet: a fixed FREE_BET_STAKE wager that costs the player nothing.
    It ignores the staking budget, the available-points check and the open-stake cap (it's free),
    but still respects the no-draw-on-knockouts rule and the pre-kickoff lock. On a win only the
    PROFIT is credited (the stake was never the player's); a loss costs nothing. Returns (ok, wager_or_error)."""
    if not player or player in ("—", "-"):
        return False, "Pick which player is betting first."
    if selection not in SELECTIONS:
        return False, "Pick home, draw or away."
    if match is None:
        return False, "That game could not be found."
    if selection == "DRAW" and _norm_stage(match.get("stage")) != "GROUP_STAGE":
        return False, "No draw bets on knockout games — pick the side to go through."
    if _hedges_open(wagers, player, match_id(match), selection):
        return False, _HEDGE_MSG
    if not can_bet_on(match, now):
        return False, "Betting on that game is closed — it has kicked off or finished."
    odds = match_odds(comp_home, comp_away, knockout=is_knockout(match)).get(selection)
    ret = potential_return(FREE_BET_STAKE, odds["num"], odds["den"])
    if MAX_RETURN is not None and ret > MAX_RETURN + 1e-9:
        ret = float(MAX_RETURN)
    w = {"id": uuid.uuid4().hex[:12], "player": player, "matchId": match_id(match),
         "home": match.get("home"), "away": match.get("away"), "stage": match.get("stage"),
         "utcDate": match.get("utcDate"), "selection": selection, "stake": FREE_BET_STAKE,
         "epoch": epoch_of(match), "free": True,
         "num": odds["num"], "den": odds["den"], "frac": odds["frac"], "return": ret,
         "status": "pending", "placed_at": int(now if now is not None else time.time())}
    wagers.append(w)
    return True, w


CS_OVERROUND = 1.22          # correct-score books run heavy margin at real bookies (120-135%); every cell is
CS_DRAW_BOOST = 1.25         # independent Poissons starve the draw diagonal vs reality — boost h==a cells like a
                             #   real book (shorter price = MORE house margin there, so it can only reduce exploitability)
CS_MAX_DEC = 201.0           # longest exact-score price is 200/1 (real books cap ~150-250/1; 4-4 lands realistic,
                             #   junk scores stop at 200/1 which is far above fair -> pure house-side)
CS_GRID_MAX = 9              #   fair*1.22 so no selection is EVER punter-positive, and cells cap out ~20% implied,
                             #   nowhere near the 1/6 price floor -- the capped-value farm that hit O/U can't occur.


def _poisson_pmf(k, lam):
    """P(X = k) for Poisson(lam); tiny, exact, never raises for k>=0."""
    try:
        p = math.exp(-lam)
        for i in range(1, int(k) + 1):
            p *= lam / i
        return p
    except Exception:
        return 0.0


def cs_odds(comp_home, comp_away):
    """Exact-scoreline prices: {'2-1': {frac,num,den,decimal}, ..., 'OTHER': {...}}.
    Model: total goals lam from expected_goals(), split into independent home/away Poissons by the
    1X2 fair strengths (home share = p_home + p_draw/2). Grid covers 0-0..%d-%d; every score outside
    the grid is the single 'OTHER' bucket, so the selections PARTITION all outcomes -- pricing each at
    fair * CS_OVERROUND makes the whole book overround by construction and dutching every cell a
    guaranteed loss. Settles on the final score (after extra time, penalties excluded).""" % (CS_GRID_MAX, CS_GRID_MAX)
    lh, la = _team_lambdas(comp_home, comp_away)
    out = {}
    for h in range(CS_GRID_MAX + 1):
        for a in range(CS_GRID_MAX + 1):
            p = _poisson_pmf(h, lh) * _poisson_pmf(a, la)
            if h == a:
                p *= CS_DRAW_BOOST
            implied = min(MAX_PROB, max(1.0 / CS_MAX_DEC, p * CS_OVERROUND))
            num, den = _floor_fraction(1.0 / implied)
            out["%d-%d" % (h, a)] = {"frac": "%d/%d" % (num, den), "num": num, "den": den, "decimal": round(_dec((num, den)), 3)}
    # No 'Any other' bucket any more: the grid IS the market (0-0..6-6). Dropping the bucket only removes a
    # bettable selection, so the remaining book keeps every cell at fair*1.22 — dutching any subset still loses.
    return out


def _cs_result(selection, match):
    """won/lost for an exact-score pick against the FINAL score (extra time included, pens excluded).
    Legacy 'OTHER' bets (bought when the grid was 0-4 + a bucket) keep their ORIGINAL terms: they win on
    any score outside that 0-4 grid — new bets are exact scorelines only."""
    hs, as_ = match.get("homeScore"), match.get("awayScore")
    if hs is None or as_ is None:
        return None
    if selection == "OTHER":
        return "won" if (int(hs) > 4 or int(as_) > 4) else "lost"
    return "won" if selection == ("%d-%d" % (int(hs), int(as_))) else "lost"


# ---- Handicap (goal-margin) market — the SAME Poisson grid as the exact-score book -------------
HC_OVERROUND = 1.13          # same margin class as the O/U goals book (a touch above the 1X2's 1.08)
HC_MIN_MARGIN = 0.02         # minimum book overround on any offered line (a capped near-certainty still has an edge)
HC_MAX_PROB = 0.93           # deep price ladder like O/U (shortest ~1/13); beyond it the line is NOT offered
HC_LINES = [-3.5, -2.5, -1.5, 1.5, 2.5, 3.5]   # HOME-team half-lines only -> a bet can never push. ±0.5 is
                                    #   deliberately absent: it duplicates the 1X2 result market under a DIFFERENT
                                    #   probability model (this Poisson grid vs _fair_probs), and two prices for one
                                    #   event across two models is exactly the cross-market dutch a sharp bettor
                                    #   farms. With |L| >= 1.5 no handicap side has a 1X2 twin, and every combination
                                    #   of selections that covers all outcomes must route through a margined book.
                                    #   ±3.5 only survives the ladder rule for big favourites — elsewhere it's filtered.
HC_GRID_MAX = 14             # score grid depth for margin probs (lambda tops out at 4.2 -> the tail beyond is dust)


def _team_lambdas(comp_home, comp_away):
    """Split the match goal expectation into independent home/away Poisson means by the 1X2 fair
    strengths (home share = p_home + p_draw/2, kept off the rails). This is the ONE model behind both
    the exact-score grid and the handicap book, so those two markets can never disagree with each other."""
    lam = expected_goals(comp_home, comp_away)
    ph, pd, pa = _fair_probs(comp_home, comp_away)
    share = min(0.85, max(0.15, ph + pd / 2.0))     # home goal share, kept off the rails
    return lam * share, lam * (1.0 - share)


def _hc_home_prob(lh, la, line):
    """P(home covers `line`) = P(home_goals + line > away_goals), summed on the shared Poisson grid and
    normalised over the grid so HOME and AWAY are EXACT complements (the book maths depends on that).
    Half-lines only, so equality is impossible and the two sides partition every scoreline."""
    try:
        line = float(line)
    except (TypeError, ValueError):
        return 0.5
    if line != line or line in (float("inf"), float("-inf")):
        return 0.5
    ph_pmf = [_poisson_pmf(k, lh) for k in range(HC_GRID_MAX + 1)]
    pa_pmf = [_poisson_pmf(k, la) for k in range(HC_GRID_MAX + 1)]
    cover = mass = 0.0
    for h in range(HC_GRID_MAX + 1):
        for a in range(HC_GRID_MAX + 1):
            p = ph_pmf[h] * pa_pmf[a]
            mass += p
            if h + line > a:
                cover += p
    return (cover / mass) if mass > 0 else 0.5


def hc_odds(comp_home, comp_away, lines=None):
    """Handicap (goal-margin) prices per HOME line: {'-1.5': {'HOME': {...}, 'AWAY': {...}}, ...}.
    The line is the handicap applied to the HOME side: HOME wins when home + line > away; AWAY is the exact
    complement (half-lines -> never a push). Settles on the 90'+ET score, penalties excluded — the same basis
    as Over/Under and exact score (NOT the knockout 'to advance' book, which a shootout can decide).
    Built exactly like goals_odds(): the ladder rule (never quote a line one side of which we can't price
    inside the ladder), a guaranteed minimum book margin, and a post-rounding re-check, so an offered line
    can never go bettor-positive."""
    lh, la = _team_lambdas(comp_home, comp_away)
    out = {}
    for L in (lines or HC_LINES):
        p_home = min(0.999, max(1e-6, _hc_home_prob(lh, la, L)))
        p_away = min(0.999, max(1e-6, 1.0 - p_home))
        if p_home > HC_MAX_PROB or p_away > HC_MAX_PROB:
            continue                                # same rule as O/U: a capped near-certainty is a farmable edge
        iH = min(HC_MAX_PROB, p_home * HC_OVERROUND)
        iA = min(HC_MAX_PROB, p_away * HC_OVERROUND)
        target = 1.0 + HC_MIN_MARGIN
        if iH + iA < target:                        # a capped side can't carry the margin -> lift the other side
            if iH <= iA:
                iH = min(HC_MAX_PROB, target - iA)
            else:
                iA = min(HC_MAX_PROB, target - iH)
        leg = {}
        for _ in range(8):                          # rebuild + re-check after fraction rounding, like goals_odds()
            leg = {}
            for sel, implied in (("HOME", iH), ("AWAY", iA)):
                num, den = _nearest_fraction(1.0 / implied)
                leg[sel] = {"frac": "%d/%d" % (num, den), "num": num, "den": den, "decimal": round(_dec((num, den)), 3)}
            if (1.0 / leg["HOME"]["decimal"]) + (1.0 / leg["AWAY"]["decimal"]) > 1.0 + 1e-6:
                break
            if iH <= iA:
                iH = min(HC_MAX_PROB, iH + 0.01)
            else:
                iA = min(HC_MAX_PROB, iA + 0.01)
        out[_line_key(L)] = leg                     # every OFFERED line overrounds; neither side beats the ladder
    return out


def _hc_result(line, selection, match):
    """'won'/'lost' for a handicap bet against the FINAL score (extra time included, penalties excluded),
    or None while it can't be settled. `line` is the stored HOME-team line; HOME covers when home + line > away,
    AWAY covers the exact complement — half-lines only, so a push is impossible."""
    try:
        line = float(line)
    except (TypeError, ValueError):
        return None
    if line != line or line in (float("inf"), float("-inf")) or selection not in ("HOME", "AWAY"):
        return None
    hs, as_ = match.get("homeScore"), match.get("awayScore")
    if hs is None or as_ is None:
        return None
    try:
        hs = float(hs); as_ = float(as_)
    except (TypeError, ValueError):
        return None
    if hs != hs or as_ != as_ or hs in (float("inf"), float("-inf")) or as_ in (float("inf"), float("-inf")) or hs < 0 or as_ < 0:
        return None
    home_covers = (hs + line) > as_
    return "won" if (home_covers if selection == "HOME" else not home_covers) else "lost"


def _match_total(match):
    """Final total goals for a finished match, or None if no valid score. Penalties never count
    (a shootout isn't goals); a knockout's score is its 90+ET total, which is what the tracker shows."""
    hs, as_ = match.get("homeScore"), match.get("awayScore")
    if hs is None or as_ is None:
        return None
    try:
        hs = float(hs); as_ = float(as_)
    except (TypeError, ValueError):
        return None
    if hs != hs or as_ != as_ or hs in (float("inf"), float("-inf")) or as_ in (float("inf"), float("-inf")) or hs < 0 or as_ < 0:
        return None
    return hs + as_


def _ou_result(line, selection, total):
    """'won'/'lost' for an Over/Under bet, or None if it can't be settled yet. Half-lines only -> never a push."""
    try:
        line = float(line)
    except (TypeError, ValueError):
        return None
    if total is None or selection not in ("OVER", "UNDER"):
        return None
    over = total > line
    return "won" if (over if selection == "OVER" else not over) else "lost"


def _leg_result(leg, match):
    status = match.get("status")
    if status in VOID_STATUSES:
        return "void"
    if leg.get("market") == "ou":                    # Over/Under leg settles on total goals, not the winner
        if status not in ("FINISHED", "AWARDED"):
            return None
        return _ou_result(leg.get("line"), leg.get("selection"), _match_total(match))
    if leg.get("market") == "cs":                    # exact score settles on the final score (ET incl., pens excl.)
        if status not in ("FINISHED", "AWARDED"):
            return None
        return _cs_result(leg.get("selection"), match)
    if leg.get("market") == "hc":                    # handicap settles on the goal margin (ET incl., pens excl.)
        if status not in ("FINISHED", "AWARDED"):    #   an hc leg must NEVER fall through and be settled as a
            return None                              #   match-winner pick
        return _hc_result(leg.get("line"), leg.get("selection"), match)
    side = _winner_side(match)
    if status not in ("FINISHED", "AWARDED") or side is None:
        return None
    if side == "DRAW" and _norm_stage(match.get("stage")) != "GROUP_STAGE":
        return None                      # a knockout can't truly end level — wait for valid (winner/pens) data, don't lose the leg on a glitch
    return "won" if leg["selection"] == side else "lost"


def settle(wagers, match, now=None):
    """Settle every pending wager affected by this match. Singles settle outright; accumulators settle
    leg-by-leg and only resolve once every leg has a result. Mutates `wagers`. Returns the number fully settled."""
    if not isinstance(match, dict):
        return 0
    mid = match_id(match)
    status = match.get("status")
    side = _winner_side(match)
    ts = int(now if now is not None else time.time())
    n = 0
    for w in wagers or []:
        if not isinstance(w, dict) or w.get("status") != "pending":
            continue
        legs = w.get("legs")
        if legs:                                   # ---- accumulator ----
            if not isinstance(legs, list) or not all(isinstance(leg, dict) for leg in legs):
                continue                           # malformed acca record — leave it untouched rather than crash
            touched = False
            for leg in legs:
                if leg.get("matchId") == mid and not leg.get("result"):
                    r = _leg_result(leg, match)
                    if r:
                        leg["result"] = r
                        touched = True
            if not touched:
                continue
            results = [leg.get("result") for leg in legs]
            if "lost" in results:                  # any leg down -> the whole acca is down
                w["status"] = "lost"; w["return"] = 0; w["settled_at"] = ts; n += 1
            elif None not in results:              # every leg decided, none lost
                won_legs = [leg for leg in legs if leg.get("result") == "won"]
                dec = 1.0
                for leg in won_legs:               # void legs drop out (odds treated as 1.0)
                    den = _num(leg.get("den"))
                    num = _num(leg.get("num"))
                    if den > 0:
                        dec *= (1.0 + num / den)
                rv = round(_num(w.get("stake")) * dec, 2)   # all-void -> dec 1.0 -> return == stake (refund)
                w["return"] = min(MAX_RETURN, rv) if MAX_RETURN is not None else rv
                w["status"] = "void" if not won_legs else "won"   # every leg void = a push (stake back)
                w["settled_at"] = ts; n += 1
            continue
        # ---- single ----
        if w.get("matchId") != mid:
            continue
        if status in VOID_STATUSES:
            w["status"] = "void"; w["return"] = _num(w.get("stake")); w["settled_at"] = ts; n += 1; continue
        if w.get("market") == "cs":                  # exact-score single: settle on the final score (ET incl., pens excl.)
            if status not in ("FINISHED", "AWARDED"):
                continue
            r = _cs_result(w.get("selection"), match)
            if r == "won":
                rv = _num(w.get("return"))
                w["return"] = min(MAX_RETURN, rv) if MAX_RETURN is not None else rv
                w["status"] = "won"
            elif r == "lost":
                w["status"] = "lost"; w["return"] = 0
            else:
                continue
            w["settled_at"] = ts; n += 1; continue
        if w.get("market") == "ou":                  # Over/Under single: settle on final total goals (pens excluded)
            if status not in ("FINISHED", "AWARDED"):
                continue
            r = _ou_result(w.get("line"), w.get("selection"), _match_total(match))
            if r is None:
                continue                             # no valid score yet -> leave pending
            if r == "won":
                w["status"] = "won"
            else:
                w["status"] = "lost"; w["return"] = 0
            w["result"] = r
            w["settled_at"] = ts; n += 1; continue
        if w.get("market") == "hc":                  # handicap single: settle on the final goal margin (pens excluded)
            if status not in ("FINISHED", "AWARDED"):
                continue
            r = _hc_result(w.get("line"), w.get("selection"), match)
            if r is None:
                continue                             # no valid score yet -> leave it pending
            if r == "won":
                w["status"] = "won"
            else:
                w["status"] = "lost"; w["return"] = 0
            w["result"] = r
            w["settled_at"] = ts; n += 1; continue
        if status not in ("FINISHED", "AWARDED") or side is None:
            continue
        if side == "DRAW" and _norm_stage(w.get("stage")) != "GROUP_STAGE":
            continue                     # knockout shouldn't end level; don't settle side bets as lost on glitchy data — wait for winner/pens
        sel = w.get("selection")
        if sel not in SELECTIONS:
            continue                     # malformed single (no valid selection) — don't settle it
        if sel == side:
            w["status"] = "won"
        else:
            w["status"] = "lost"; w["return"] = 0
        w["result"] = side
        w["settled_at"] = ts
        n += 1
    return n


def place_acca(wagers, player, selections, stake, settled_points, now=None, group_mid_ts=None):
    """
    Place a 1-3 leg accumulator. `selections` is a list of dicts:
        {"match": <fixture>, "selection": "HOME|DRAW|AWAY", "comp_home": int, "comp_away": int}
    All legs must win for it to pay; combined odds = product of each leg's decimal price.
    """
    if not selections:
        return False, "Add at least one pick."
    if len(selections) > MAX_ACCA_LEGS:
        return False, "An accumulator can have at most %d legs." % MAX_ACCA_LEGS
    if len(selections) == 1:                       # a 1-leg acca is just a normal single
        s = selections[0]
        return place(wagers, player, s["match"], s["selection"], stake, settled_points,
                     s["comp_home"], s["comp_away"], now, group_mid_ts,
                     market=s.get("market", "result"), line=s.get("line"))
    keys = [match_id(s["match"]) for s in selections]
    if len(set(keys)) != len(keys):
        return False, ("Each game can only go in an accumulator once. A result and a goals bet on the same "
                       "match are correlated — e.g. Under 0.5 is always a draw, and Under 0.5 with a team to "
                       "win can never happen — so combining them would be mispriced. Back those as separate singles.")
    try:
        stake = round(float(stake), 2)
    except (TypeError, ValueError):
        return False, "Enter a number of points to stake."
    if stake != stake or stake in (float("inf"), float("-inf")):
        return False, "Enter a valid stake."
    if stake <= 0:
        return False, "You have to stake at least %d point(s) — you can't bet nothing." % MIN_STAKE
    if stake < MIN_STAKE:
        return False, "Minimum stake is %d point(s)." % MIN_STAKE
    cap = min((stage_max_stake(s["match"].get("stage")) for s in selections), default=MAX_STAKE)
    if stake > cap:
        return False, "Max stake on this accumulator is %d points." % cap
    d = player_deltas(wagers).get(player, {})
    if d.get("pending_count", 0) >= MAX_PENDING:
        return False, "You already have %d open bets — settle some first." % MAX_PENDING
    open_accas = sum(1 for w in (wagers or [])
                     if w.get("player") == player and w.get("status") == "pending" and w.get("legs"))
    if open_accas >= MAX_ACTIVE_ACCAS:
        return False, ("You can only have %d accumulators running at once — wait for one to settle "
                       "(single bets don't count toward this)." % MAX_ACTIVE_ACCAS)
    pending = d.get("pending_stake", 0.0)
    if pending + stake > cap + 1e-9:
        return False, ("You can have at most %d points riding on open bets at once — you've already got %g out there, "
                       "so you can add %g more until one settles." % (cap, pending, round(max(0.0, cap - pending), 2)))
    avail = available_points(player, settled_points, wagers)
    if stake > avail + 1e-9:
        return False, "You only have %g points available to stake." % avail
    epoch = epoch_of(min(selections, key=lambda s: _utc_ts(s["match"].get("utcDate") or "") or 0)["match"], group_mid_ts)
    eb = stage_budget(epoch)
    brem = budget_remaining(wagers, player, epoch)
    if brem <= 1e-9:
        return False, ("You've used up your %d-point staking budget for this round (resets next round)." % eb)
    if stake > brem + 1e-9:
        return False, ("Your staking budget left this round is %g of %d points — stake that or less." % (brem, eb))
    legs = []
    dec = 1.0
    for s in selections:
        if not can_bet_on(s.get("match"), now):
            return False, "One of those games has kicked off or finished — accas must be all upcoming."
        mk = (str(s.get("market") or "result")).lower()
        if mk == "cs":
            # Exact-score legs are fine ACROSS matches: each cell is priced off the 1.22-margined grid and
            # legs on different games are independent, so the product of prices only compounds the margin.
            # The dangerous combo (a cs leg correlated with another market on the SAME game) is impossible
            # here — the one-leg-per-game dedupe above blocks it before any leg is priced.
            if not isinstance(s.get("selection"), str) or not re.fullmatch(r"[0-%d]-[0-%d]" % (CS_GRID_MAX, CS_GRID_MAX), s.get("selection")):
                return False, "Pick a scoreline for every exact-score leg — home goals then away, each 0-%d." % CS_GRID_MAX
            o = cs_odds(s["comp_home"], s["comp_away"]).get(s["selection"])
            if not o:
                return False, "Couldn't price one of those exact-score legs — try again."
            legs.append({"matchId": match_id(s["match"]), "selection": s["selection"], "market": "cs",
                         "home": s["match"].get("home"), "away": s["match"].get("away"),
                         "stage": s["match"].get("stage"), "num": o["num"], "den": o["den"], "frac": o["frac"]})
        elif mk == "hc":
            # Handicap legs, same reasoning: distinct matches (enforced above) -> independent margined
            # prices, no covering combination exists across games. Settles by _leg_result on the margin.
            if s.get("selection") not in ("HOME", "AWAY"):
                return False, "Pick a side for every handicap leg — home or away."
            try:
                ln = float(s.get("line"))
            except (TypeError, ValueError):
                return False, "Pick a handicap line for every handicap leg."
            if ln not in HC_LINES:
                return False, "That handicap line isn't offered."
            o = hc_odds(s["comp_home"], s["comp_away"]).get(_line_key(ln), {}).get(s["selection"])
            if not o:
                return False, "Couldn't price one of those handicap legs — try again."
            legs.append({"matchId": match_id(s["match"]), "selection": s["selection"], "market": "hc", "line": ln,
                         "home": s["match"].get("home"), "away": s["match"].get("away"),
                         "stage": s["match"].get("stage"), "num": o["num"], "den": o["den"], "frac": o["frac"]})
        elif mk == "ou":
            if s.get("selection") not in ("OVER", "UNDER"):
                return False, "Pick Over or Under for every goals leg."
            try:
                ln = float(s.get("line"))
            except (TypeError, ValueError):
                return False, "Pick a goals line for every goals leg."
            if ln not in OU_LINES:
                return False, "That goals line isn't offered."
            o = goals_odds(s["comp_home"], s["comp_away"]).get(_line_key(ln), {}).get(s["selection"])
            if not o:
                return False, "Couldn't price one of those goals legs — try again."
            legs.append({"matchId": match_id(s["match"]), "selection": s["selection"], "market": "ou", "line": ln,
                         "home": s["match"].get("home"), "away": s["match"].get("away"),
                         "stage": s["match"].get("stage"), "num": o["num"], "den": o["den"], "frac": o["frac"]})
        else:
            if s.get("selection") not in SELECTIONS:
                return False, "Pick home, draw or away for every leg."
            if (s["match"].get("stage") not in (None, "GROUP_STAGE")) and s["selection"] == "DRAW":
                return False, "Knockout legs can't be a draw — pick the side to go through."
            if _hedges_open(wagers, player, match_id(s["match"]), s["selection"]):
                return False, "One of these legs backs the opposite side of a game you already have a bet on — you can't back both sides of the same match."
            o = match_odds(s["comp_home"], s["comp_away"], knockout=is_knockout(s["match"]))[s["selection"]]
            legs.append({"matchId": match_id(s["match"]), "selection": s["selection"],
                         "home": s["match"].get("home"), "away": s["match"].get("away"),
                         "stage": s["match"].get("stage"), "num": o["num"], "den": o["den"], "frac": o["frac"]})
        dec *= o["decimal"]
    ret = round(stake * dec, 2)
    if MAX_RETURN is not None and ret > MAX_RETURN + 1e-9:
        return False, "That acca would return %g — the cap is %g per bet. Lower your stake." % (ret, MAX_RETURN)
    w = {"id": uuid.uuid4().hex[:12], "player": player, "type": "acca", "legs": legs, "epoch": epoch,
         "home": legs[0]["home"], "away": legs[0]["away"], "selection": "ACCA",
         "stake": stake, "decimal": round(dec, 3), "frac": "%d-fold" % len(legs),
         "return": ret, "status": "pending", "placed_at": int(now if now is not None else time.time())}
    wagers.append(w)
    return True, w


def settle_all(wagers, matches, now=None):
    """Run settlement across all matches (idempotent — only touches still-pending bets)."""
    total = 0
    for m in (matches or []):
        total += settle(wagers, m, now)
    return total


def betting_locked(tracker):
    """Once the tournament is decided (final played, one team left) no new bets can be placed."""
    st = (tracker or {}).get("stats") or {}
    return (st.get("teams_remaining") is not None and st.get("teams_remaining") <= 1
            and (st.get("matches_played") or 0) > 0)


def stats(wagers):
    """Per-player wager stats for the analysis board: staked, profit won, points lost, biggest win, counts.
    Skips malformed records (not a dict, blank player) and coerces money safely, so the analysis board
    never crashes on a bad row."""
    out = {}
    for w in wagers or []:
        if not isinstance(w, dict):
            continue
        player = w.get("player")
        if not player or not isinstance(player, str):
            continue
        if w.get("credit") or w.get("status") == "void":
            continue                              # free-points credits and cancelled/voided bets aren't real bets — don't tally them
        stake = _num(w.get("stake"))
        ret = _num(w.get("return"))
        d = out.setdefault(player, {"player": player, "staked": 0.0, "won": 0.0, "lost": 0.0,
                                    "net": 0.0, "bets": 0, "open": 0, "biggest_win": 0.0})
        d["bets"] += 1
        d["staked"] = round(d["staked"] + stake, 2)
        st = w.get("status")
        if st == "pending":
            d["open"] += 1
        elif st == "won":
            prof = round(ret - stake, 2)
            d["won"] = round(d["won"] + prof, 2)
            d["net"] = round(d["net"] + prof, 2)
            d["biggest_win"] = max(d["biggest_win"], prof)
        elif st == "lost":
            d["lost"] = round(d["lost"] + stake, 2)
            d["net"] = round(d["net"] - stake, 2)
    return out


def leaders(wagers):
    """Headline leaders for the analysis section: most staked, most won, most lost (None if no bets)."""
    s = list(stats(wagers).values())
    if not s:
        return {"most_wagered": None, "most_won": None, "most_lost": None}
    top = lambda key: max(s, key=lambda d: d[key]) if any(d[key] > 0 for d in s) else None
    return {"most_wagered": top("staked"), "most_won": top("won"), "most_lost": top("lost")}
