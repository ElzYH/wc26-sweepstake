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
import time
import uuid

# ---- safety caps — all tuning lives here ----
MIN_STAKE = 1
MAX_STAKE = 30           # base single-bet cap (group stage); rises each knockout round (see STAGE_MAX_STAKE)
MAX_RETURN = None         # most a single bet can return; None = no limit (admin can set a number)
MAX_PENDING = 8           # most simultaneous open bets per player
MAX_ACCA_LEGS = 3         # default legs in one accumulator; admin can raise
MAX_ACTIVE_ACCAS = 2      # most simultaneous OPEN accumulators per player (single bets are unlimited)
FREE_BET_STAKE = 5        # a claimed free bet stakes this many points; the stake is NEVER credited — only winnings (profit) count
STARTING_BONUS = 5        # everyone starts with this many free betting points so they can bet before earning any.
                          # It's bet-only: it never sits on the leaderboard, and it cushions the first 5 of net losses.
STAGE_BUDGET = 100        # staking allowance per "epoch": group 1st half, group 2nd half, then each KO round.
                          # Resets automatically because budget_remaining only sums bets within the same epoch.
OVERROUND = 1.08          # ~8% bookmaker margin
MAX_PROB = 0.95           # favourites can be priced shorter (down to ~1/20) so the margin holds on them too

# Max stake rises as the tournament gets deeper: +5 per knockout round, and an extra +15 for the final.
# WC2026 round dates (UTC, for reference — actual dates come from the live fixture feed):
#   Group stage 11–27 Jun · Round of 32 28 Jun–3 Jul · Round of 16 4–7 Jul · Quarter-finals 9–11 Jul ·
#   Semi-finals 14–15 Jul · Third place 18 Jul · Final 19 Jul. The 100-pt budget also resets at each of these.
STAGE_MAX_STAKE = {
    "GROUP_STAGE":     30,
    "LAST_32":         35,
    "LAST_16":         40,
    "QUARTER_FINALS":  45,
    "SEMI_FINALS":     50,
    "THIRD_PLACE":     50,
    "FINAL":           65,     # 50 + extra 15
    "WINNER":          65,
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
              (40, 1), (50, 1), (66, 1), (80, 1), (100, 1)]


def _dec(fr):
    return 1.0 + fr[0] / fr[1]


def _nearest_fraction(decimal):
    return min(_FRACTIONS, key=lambda fr: abs(_dec(fr) - decimal))


def _fair_probs(ch, ca):
    """Home/draw/away probabilities from team strength — same shape as the tracker's win-prob model."""
    ch = (ch or 40) + 1
    ca = (ca or 40) + 1
    pw = ch / (ch + ca)
    edge = abs(pw - 0.5)
    pd = max(0.12, 0.30 - edge * 0.55)
    ph = pw * (1 - pd)
    pa = (1 - pw) * (1 - pd)
    s = ph + pd + pa
    return ph / s, pd / s, pa / s


def match_odds(comp_home, comp_away):
    """{'HOME': {'frac':'9/2','num':9,'den':2,'decimal':5.5}, 'DRAW':..., 'AWAY':...} with a bookmaker margin."""
    ph, pd, pa = _fair_probs(comp_home, comp_away)
    out = {}
    for sel, p in (("HOME", ph), ("DRAW", pd), ("AWAY", pa)):
        implied = min(MAX_PROB, p * OVERROUND)
        num, den = _nearest_fraction(1.0 / implied)
        out[sel] = {"frac": "%d/%d" % (num, den), "num": num, "den": den, "decimal": round(_dec((num, den)), 3)}
    return out


def potential_return(stake, num, den):
    """Total returned if it wins = stake + profit (profit = stake * num/den). e.g. 5 @ 9/2 -> 27.5."""
    return round(stake * (1.0 + num / den), 1)


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
    if hs == as_:                                   # level after 90/120 mins
        ph, pa = m.get("penHome"), m.get("penAway")
        if ph is not None and pa is not None and ph != pa:
            return "HOME" if ph > pa else "AWAY"     # a shootout decides who advances (counts as a win)
    return "HOME" if hs > as_ else ("AWAY" if as_ > hs else "DRAW")


def can_bet_on(match, now=None):
    """Only before kick-off: status must be pre-match AND the kick-off time must still be in the future."""
    if match.get("status") not in OPEN_STATUSES:
        return False
    ts = _utc_ts(match.get("utcDate") or "")
    if ts is not None and ts <= (now if now is not None else time.time()):
        return False
    return True


def player_deltas(wagers):
    """Per-player effect of the wager log: settled profit/loss, points held in open bets, open count."""
    out = {}
    for w in wagers or []:
        if w.get("credit"):                              # a claimed free-points drop: not a bet (see free_bonus); skip
            continue
        d = out.setdefault(w["player"], {"settled_net": 0.0, "pending_stake": 0.0, "pending_count": 0})
        st = w.get("status")
        if w.get("free"):                                # a free bet: only a WIN matters, and only its profit counts.
            if st == "won":                              # the 5-point stake was never the player's, so credit return - stake (profit).
                d["settled_net"] += (w.get("return", 0) - w["stake"])
            continue                                     # pending/lost/void free bets hold nothing and cost nothing
        if st == "pending":
            d["pending_stake"] += w["stake"]
            d["pending_count"] += 1
        elif st == "won":
            d["settled_net"] += (w.get("return", 0) - w["stake"])   # profit only (stake is returned)
        elif st == "lost":
            d["settled_net"] -= w["stake"]
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
    return stage


def budget_remaining(wagers, player, epoch, budget=STAGE_BUDGET):
    """Points a player can still stake in this epoch:
    budget - (stakes placed this epoch) + (returns from won bets this epoch), clamped to [0, budget].
    Losing leaves the budget down (you climb back only by winning); winnings top it up, never above budget.
    Void bets refund so they don't count. Resets per epoch because we only sum bets tagged with this `epoch`."""
    spent = 0.0
    back = 0.0
    for w in wagers or []:
        if w.get("player") != player or w.get("epoch") != epoch or w.get("free") or w.get("credit"):
            continue                                    # free bets & free-point credits sit outside the staking budget entirely
        st = w.get("status")
        if st in ("pending", "won", "lost"):       # void = refunded, ignore
            spent += w.get("stake", 0) or 0
        if st == "won":
            back += w.get("return", 0) or 0
    return max(0.0, min(float(budget), round(budget - spent + back, 1)))


def free_bonus(player, wagers):
    """A player's total FREE betting points: the 5 everyone starts with, plus 5 for each free-points drop they've
    claimed. These never sit on the leaderboard — they let you bet, and they cushion the first `free_bonus` of net
    losses (so only genuine winnings, and losses beyond your free points, move the leaderboard)."""
    extra = sum(w.get("amount", 0) for w in (wagers or []) if w.get("credit") and w.get("player") == player)
    return float(STARTING_BONUS + extra)


def available_points(player, settled_points, wagers):
    """Points a player can still stake: their earned points + their free points (starting bonus + claimed drops)
    + settled bet profit/loss - points already on open bets, floored at 0."""
    d = player_deltas(wagers).get(player, {})
    return max(0.0, round(settled_points + free_bonus(player, wagers) + d.get("settled_net", 0.0) - d.get("pending_stake", 0.0), 1))


def leaderboard_net(player, wagers, bonus=None):
    """The bet profit/loss that should hit a player's LEADERBOARD total. The free points (and any free-bet winnings
    already in settled_net) mean the first `bonus` points of net losses are absorbed and never cost real points —
    only genuine winnings, and losses beyond the free points, move the leaderboard."""
    b = free_bonus(player, wagers) if bonus is None else float(bonus)
    net = player_deltas(wagers).get(player, {}).get("settled_net", 0.0)
    return round(net + min(b, max(0.0, -net)), 1)


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


def applied_points(base_points, player, wagers):
    """A player's displayed points once wagers are applied: base + leaderboard net (free-cushioned) - open stakes, floored at 0."""
    d = player_deltas(wagers).get(player, {})
    return max(0.0, round(base_points + leaderboard_net(player, wagers) - d.get("pending_stake", 0.0), 1))


def place(wagers, player, match, selection, stake, settled_points, comp_home, comp_away, now=None, group_mid_ts=None):
    """
    Validate and append a pending wager. Returns (ok, wager_or_error_string).
    `wagers` is mutated only on success. The server prices the match here (odds can't be spoofed by the client).
    """
    if not player or player in ("—", "-"):
        return False, "Pick which player is betting first."
    if selection not in SELECTIONS:
        return False, "Pick home, draw or away."
    if match is None:
        return False, "That game could not be found."
    if selection == "DRAW" and match.get("stage") not in (None, "GROUP_STAGE"):
        return False, "No draw bets on knockout games — pick the side to go through."
    if not can_bet_on(match, now):
        return False, "Betting on that game is closed — it has kicked off or finished."
    try:
        stake = round(float(stake), 1)
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
                       "so you can add %g more until one settles." % (cap, pending, round(max(0.0, cap - pending), 1)))
    avail = available_points(player, settled_points, wagers)
    if stake > avail + 1e-9:
        return False, "You only have %g points available to stake." % avail
    epoch = epoch_of(match, group_mid_ts)              # per-round staking budget (both this and the per-bet cap apply)
    brem = budget_remaining(wagers, player, epoch)
    if brem <= 1e-9:
        return False, ("You've used up your %d-point staking budget for this round. It resets at the next round "
                       "(the group-stage midpoint, then each knockout round) — you can't bet again until then." % STAGE_BUDGET)
    if stake > brem + 1e-9:
        return False, ("Your staking budget left this round is %g of %d points — stake that or less. "
                       "It tops back up when your bets win (never above %d) and resets next round." % (brem, STAGE_BUDGET, STAGE_BUDGET))
    odds = match_odds(comp_home, comp_away).get(selection)
    ret = potential_return(stake, odds["num"], odds["den"])
    if MAX_RETURN is not None and ret > MAX_RETURN + 1e-9:
        return False, "That would return %g — the cap is %g per bet. Lower your stake." % (ret, MAX_RETURN)
    w = {"id": uuid.uuid4().hex[:12], "player": player, "matchId": match_id(match),
         "home": match.get("home"), "away": match.get("away"), "stage": match.get("stage"),
         "utcDate": match.get("utcDate"), "selection": selection, "stake": stake, "epoch": epoch,
         "num": odds["num"], "den": odds["den"], "frac": odds["frac"], "return": ret,
         "status": "pending", "placed_at": int(now if now is not None else time.time())}
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
    if not can_bet_on(match, now):
        return False, "Betting on that game is closed — it has kicked off or finished."
    odds = match_odds(comp_home, comp_away).get(selection)
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


def _leg_result(leg, match):
    status = match.get("status")
    if status in VOID_STATUSES:
        return "void"
    side = _winner_side(match)
    if status not in ("FINISHED", "AWARDED") or side is None:
        return None
    return "won" if leg["selection"] == side else "lost"


def settle(wagers, match, now=None):
    """Settle every pending wager affected by this match. Singles settle outright; accumulators settle
    leg-by-leg and only resolve once every leg has a result. Mutates `wagers`. Returns the number fully settled."""
    mid = match_id(match)
    status = match.get("status")
    side = _winner_side(match)
    ts = int(now if now is not None else time.time())
    n = 0
    for w in wagers or []:
        if w.get("status") != "pending":
            continue
        legs = w.get("legs")
        if legs:                                   # ---- accumulator ----
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
                if not won_legs:                   # all void -> refund the stake
                    w["status"] = "void"; w["settled_at"] = ts; n += 1
                else:
                    dec = 1.0
                    for leg in won_legs:           # void legs drop out (odds treated as 1.0)
                        dec *= (1.0 + leg["num"] / leg["den"])
                    rv = round(w["stake"] * dec, 1)
                    w["return"] = min(MAX_RETURN, rv) if MAX_RETURN is not None else rv
                    w["status"] = "won"; w["settled_at"] = ts; n += 1
            continue
        # ---- single ----
        if w.get("matchId") != mid:
            continue
        if status in VOID_STATUSES:
            w["status"] = "void"; w["settled_at"] = ts; n += 1; continue
        if status not in ("FINISHED", "AWARDED") or side is None:
            continue
        if w["selection"] == side:
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
                     s["comp_home"], s["comp_away"], now, group_mid_ts)
    ids = [match_id(s["match"]) for s in selections]
    if len(set(ids)) != len(ids):
        return False, "You can't pick the same game twice in one accumulator."
    try:
        stake = round(float(stake), 1)
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
                       "so you can add %g more until one settles." % (cap, pending, round(max(0.0, cap - pending), 1)))
    avail = available_points(player, settled_points, wagers)
    if stake > avail + 1e-9:
        return False, "You only have %g points available to stake." % avail
    epoch = epoch_of(min(selections, key=lambda s: _utc_ts(s["match"].get("utcDate") or "") or 0)["match"], group_mid_ts)
    brem = budget_remaining(wagers, player, epoch)
    if brem <= 1e-9:
        return False, ("You've used up your %d-point staking budget for this round (resets next round)." % STAGE_BUDGET)
    if stake > brem + 1e-9:
        return False, ("Your staking budget left this round is %g of %d points — stake that or less." % (brem, STAGE_BUDGET))
    legs = []
    dec = 1.0
    for s in selections:
        if s.get("selection") not in SELECTIONS:
            return False, "Pick home, draw or away for every leg."
        if not can_bet_on(s.get("match"), now):
            return False, "One of those games has kicked off or finished — accas must be all upcoming."
        if (s["match"].get("stage") not in (None, "GROUP_STAGE")) and s["selection"] == "DRAW":
            return False, "Knockout legs can't be a draw — pick the side to go through."
        o = match_odds(s["comp_home"], s["comp_away"])[s["selection"]]
        legs.append({"matchId": match_id(s["match"]), "selection": s["selection"],
                     "home": s["match"].get("home"), "away": s["match"].get("away"),
                     "stage": s["match"].get("stage"), "num": o["num"], "den": o["den"], "frac": o["frac"]})
        dec *= o["decimal"]
    ret = round(stake * dec, 1)
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
    """Per-player wager stats for the analysis board: staked, profit won, points lost, biggest win, counts."""
    out = {}
    for w in wagers or []:
        if w.get("credit") or w.get("status") == "void":
            continue                              # free-points credits and cancelled/voided bets aren't real bets — don't tally them
        d = out.setdefault(w["player"], {"player": w["player"], "staked": 0.0, "won": 0.0, "lost": 0.0,
                                         "net": 0.0, "bets": 0, "open": 0, "biggest_win": 0.0})
        d["bets"] += 1
        d["staked"] = round(d["staked"] + w.get("stake", 0), 1)
        st = w.get("status")
        if st == "pending":
            d["open"] += 1
        elif st == "won":
            prof = round(w.get("return", 0) - w["stake"], 1)
            d["won"] = round(d["won"] + prof, 1)
            d["net"] = round(d["net"] + prof, 1)
            d["biggest_win"] = max(d["biggest_win"], prof)
        elif st == "lost":
            d["lost"] = round(d["lost"] + w["stake"], 1)
            d["net"] = round(d["net"] - w["stake"], 1)
    return out


def leaders(wagers):
    """Headline leaders for the analysis section: most staked, most won, most lost (None if no bets)."""
    s = list(stats(wagers).values())
    if not s:
        return {"most_wagered": None, "most_won": None, "most_lost": None}
    top = lambda key: max(s, key=lambda d: d[key]) if any(d[key] > 0 for d in s) else None
    return {"most_wagered": top("staked"), "most_won": top("won"), "most_lost": top("lost")}
