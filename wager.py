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
MAX_STAKE = 25            # most you can put on a single bet
MAX_RETURN = 120          # most a single bet can return (stake + profit); keeps one bet from skewing the table
MAX_PENDING = 8           # most simultaneous open bets per player
OVERROUND = 1.08          # ~8% bookmaker margin
MAX_PROB = 0.92           # never price a selection shorter than ~1/12

SELECTIONS = ("HOME", "DRAW", "AWAY")
OPEN_STATUSES = ("SCHEDULED", "TIMED")
VOID_STATUSES = ("CANCELLED", "POSTPONED", "ABANDONED")

# common British betting fractions (num, den) — placement snaps to the nearest of these
_FRACTIONS = [(1, 5), (2, 9), (1, 4), (2, 7), (3, 10), (1, 3), (4, 11), (2, 5), (4, 9), (1, 2),
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
        d = out.setdefault(w["player"], {"settled_net": 0.0, "pending_stake": 0.0, "pending_count": 0})
        st = w.get("status")
        if st == "pending":
            d["pending_stake"] += w["stake"]
            d["pending_count"] += 1
        elif st == "won":
            d["settled_net"] += (w.get("return", 0) - w["stake"])   # profit only (stake is returned)
        elif st == "lost":
            d["settled_net"] -= w["stake"]
        # "void" -> no effect (stake refunded)
    return out


def available_points(player, settled_points, wagers):
    """Points a player can still stake: settled points + settled wager profit/loss - points already on open bets, floored at 0."""
    d = player_deltas(wagers).get(player, {})
    return max(0.0, round(settled_points + d.get("settled_net", 0.0) - d.get("pending_stake", 0.0), 1))


def applied_points(base_points, player, wagers):
    """A player's displayed points once wagers are applied: base + settled profit/loss - open stakes, floored at 0."""
    d = player_deltas(wagers).get(player, {})
    return max(0.0, round(base_points + d.get("settled_net", 0.0) - d.get("pending_stake", 0.0), 1))


def place(wagers, player, match, selection, stake, settled_points, comp_home, comp_away, now=None):
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
    if not can_bet_on(match, now):
        return False, "Betting on that game is closed — it has kicked off or finished."
    try:
        stake = round(float(stake), 1)
    except (TypeError, ValueError):
        return False, "Enter a number of points to stake."
    if stake != stake or stake in (float("inf"), float("-inf")):   # NaN / inf guard
        return False, "Enter a valid stake."
    if stake < MIN_STAKE:
        return False, "Minimum stake is %d point(s)." % MIN_STAKE
    if stake > MAX_STAKE:
        return False, "Max stake is %d points on a single bet." % MAX_STAKE
    d = player_deltas(wagers).get(player, {})
    if d.get("pending_count", 0) >= MAX_PENDING:
        return False, "You already have %d open bets — settle some first." % MAX_PENDING
    avail = available_points(player, settled_points, wagers)
    if stake > avail + 1e-9:
        return False, "You only have %g points available to stake." % avail
    odds = match_odds(comp_home, comp_away).get(selection)
    ret = potential_return(stake, odds["num"], odds["den"])
    if ret > MAX_RETURN + 1e-9:
        return False, "That would return %g — the cap is %d per bet. Lower your stake." % (ret, MAX_RETURN)
    w = {"id": uuid.uuid4().hex[:12], "player": player, "matchId": match_id(match),
         "home": match.get("home"), "away": match.get("away"), "stage": match.get("stage"),
         "utcDate": match.get("utcDate"), "selection": selection, "stake": stake,
         "num": odds["num"], "den": odds["den"], "frac": odds["frac"], "return": ret,
         "status": "pending", "placed_at": int(now if now is not None else time.time())}
    wagers.append(w)
    return True, w


def settle(wagers, match, now=None):
    """Settle every pending wager on a finished/void match. Mutates `wagers`. Returns the number settled."""
    mid = match_id(match)
    status = match.get("status")
    side = _winner_side(match)
    ts = int(now if now is not None else time.time())
    n = 0
    for w in wagers or []:
        if w.get("status") != "pending" or w.get("matchId") != mid:
            continue
        if status in VOID_STATUSES:
            w["status"] = "void"
            w["settled_at"] = ts
            n += 1
            continue
        if status not in ("FINISHED", "AWARDED") or side is None:
            continue
        if w["selection"] == side:
            w["status"] = "won"
        else:
            w["status"] = "lost"
            w["return"] = 0
        w["result"] = side
        w["settled_at"] = ts
        n += 1
    return n


def settle_all(wagers, matches, now=None):
    """Run settlement across all matches (idempotent — only touches still-pending bets)."""
    by = {match_id(m): m for m in (matches or [])}
    total = 0
    for w in wagers or []:
        if w.get("status") == "pending" and w.get("matchId") in by:
            pass  # settled below in one pass per match
    for m in (matches or []):
        total += settle(wagers, m, now)
    return total
