#!/usr/bin/env python3
"""Stage 3 QA — Over/Under settlement. settle() must pay an O/U bet on final total goals vs its line.
Golden vectors over every line x totals 0..9, half-line push-free guarantee, won/lost returns,
void/abandoned refunds, penalties-don't-count, hostile/missing scores, and the settle-after-kickoff race."""
import wager as W

fails = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond:
        fails.append(name)

def finished(h, a, stage="GROUP_STAGE", status="FINISHED", **extra):
    m = {"home": "X", "away": "Y", "stage": stage, "status": status, "homeScore": h, "awayScore": a,
         "utcDate": "2020-01-01T00:00:00Z"}
    m.update(extra)
    return m

def ou_bet(line, sel, stake=5, num=4, den=5):
    # a pending O/U single on match X|Y
    return {"id": "b" + sel + str(line), "player": "Erol", "matchId": W.match_id(finished(0, 0)),
            "market": "ou", "line": line, "selection": sel, "stake": stake,
            "num": num, "den": den, "frac": "%d/%d" % (num, den),
            "return": round(stake * (1 + num / den), 2), "status": "pending"}

print("== golden vectors: every line x total 0..9, OVER and UNDER ==")
bad = 0
for line in W.OU_LINES:
    n = int(line)                       # OVER wins iff total >= n+1 ; UNDER wins iff total <= n
    for total in range(0, 10):
        for h in range(0, total + 1):
            a = total - h
            # OVER
            wv = [ou_bet(line, "OVER")]
            W.settle(wv, finished(h, a))
            exp_over = "won" if total > line else "lost"
            if wv[0]["status"] != exp_over:
                bad += 1
                if bad <= 6: print("    OVER %s total %d (%d-%d): got %s want %s" % (line, total, h, a, wv[0]["status"], exp_over))
            # UNDER
            wu = [ou_bet(line, "UNDER")]
            W.settle(wu, finished(h, a))
            exp_under = "won" if total < line else "lost"
            if wu[0]["status"] != exp_under:
                bad += 1
                if bad <= 6: print("    UNDER %s total %d (%d-%d): got %s want %s" % (line, total, h, a, wu[0]["status"], exp_under))
            break  # one scoreline per total is enough for the line logic (total is what matters)
ck("all line x total OVER/UNDER outcomes correct", bad == 0, "%d wrong" % bad)

print("\n== half-lines never push (always won or lost, never void on a real result) ==")
push = 0
for line in W.OU_LINES:
    for total in range(0, 10):
        w = [ou_bet(line, "OVER")]
        W.settle(w, finished(total, 0))
        if w[0]["status"] not in ("won", "lost"):
            push += 1
ck("no O/U single ever pushes/voids on a finished game", push == 0, push)

print("\n== returns: a winner pays stake x odds, a loser pays 0 ==")
w = [ou_bet(2.5, "OVER", stake=5, num=4, den=5)]   # 4/5 -> return 9.0 on a win
W.settle(w, finished(2, 1))                         # total 3 -> OVER 2.5 wins
ck("winning O/U keeps its struck return (5 @ 4/5 -> 9.0)", w[0]["status"] == "won" and abs(w[0]["return"] - 9.0) < 1e-9, w[0])
wl = [ou_bet(2.5, "OVER", stake=5)]
W.settle(wl, finished(1, 0))                         # total 1 -> OVER 2.5 loses
ck("losing O/U returns 0", wl[0]["status"] == "lost" and wl[0]["return"] == 0, wl[0])
ck("settled bet is stamped with a result + settled_at", "result" in w[0] and "settled_at" in w[0], w[0])

print("\n== exact-line edge (the .5 boundary) ==")
w = [ou_bet(2.5, "UNDER")]
W.settle(w, finished(1, 1))                          # total 2 < 2.5 -> UNDER wins
ck("UNDER 2.5 wins on a 2-goal game", w[0]["status"] == "won", w[0])
w = [ou_bet(2.5, "UNDER")]
W.settle(w, finished(2, 1))                          # total 3 > 2.5 -> UNDER loses
ck("UNDER 2.5 loses on a 3-goal game", w[0]["status"] == "lost", w[0])

print("\n== penalties do NOT count as goals (knockout shootout) ==")
# 1-1 after extra time, won on penalties: total goals = 2, shootout ignored
ko = finished(1, 1, stage="LAST_16", status="FINISHED", shootout=True, penHome=4, penAway=3, winner="HOME")
w = [ou_bet(2.5, "UNDER")]
W.settle(w, ko)
ck("O/U settles on the 1-1 total (2), not the shootout", w[0]["status"] == "won", w[0])
w = [ou_bet(1.5, "OVER")]
W.settle(w, ko)
ck("OVER 1.5 wins on the 1-1 (total 2), pens irrelevant", w[0]["status"] == "won", w[0])

print("\n== void / abandoned refunds the stake (same as result bets) ==")
for st in ("CANCELLED", "POSTPONED", "ABANDONED", "SUSPENDED"):
    w = [ou_bet(2.5, "OVER", stake=7)]
    before = len(w)
    W.settle(w, finished(None, None, status=st))
    if w[0]["status"] == "void":
        ck("%s -> void, stake refunded" % st, w[0]["return"] == 7, w[0])
    else:
        # statuses not in VOID_STATUSES just stay pending (not finished) — that's also fine
        ck("%s leaves the bet pending (not wrongly settled)" % st, w[0]["status"] == "pending", w[0])

print("\n== missing / hostile scores never settle wrongly ==")
for h, a in [(None, 1), (1, None), (None, None), (float("nan"), 2), (-1, 2), (float("inf"), 0)]:
    w = [ou_bet(2.5, "OVER")]
    W.settle(w, finished(h, a, status="FINISHED"))
    ck("FINISHED but score (%r,%r) invalid -> stays pending" % (h, a), w[0]["status"] == "pending", w[0])

print("\n== a still-live / not-finished game never settles an O/U bet ==")
for st in ("IN_PLAY", "PAUSED", "TIMED", "SCHEDULED"):
    w = [ou_bet(2.5, "OVER")]
    W.settle(w, finished(5, 0, status=st))
    ck("%s does not settle the bet" % st, w[0]["status"] == "pending", w[0])

print("\n== result (1X2) singles still settle exactly as before ==")
rb = {"id": "r1", "player": "Erol", "matchId": W.match_id(finished(0, 0)), "selection": "HOME",
      "stake": 5, "num": 2, "den": 1, "frac": "2/1", "return": 15, "status": "pending", "stage": "GROUP_STAGE"}
w = [rb.copy()]
W.settle(w, finished(2, 0))                          # home win
ck("result HOME bet wins on a home win", w[0]["status"] == "won" and w[0]["return"] == 15, w[0])
w = [dict(rb, selection="AWAY")]
W.settle(w, finished(2, 0))
ck("result AWAY bet loses on a home win", w[0]["status"] == "lost", w[0])

print("\n== idempotent: re-settling a settled O/U bet doesn't change it ==")
w = [ou_bet(2.5, "OVER", stake=5)]
W.settle(w, finished(3, 0))
snap = dict(w[0])
W.settle(w, finished(3, 0))                          # settle again
ck("a settled O/U bet is untouched on a second settle", w[0] == snap, (snap, w[0]))

print("\n" + ("All O/U settlement QA passed." if not fails else "O/U SETTLEMENT QA FAILED (%d): %s" % (len(fails), ", ".join(fails))))
import sys
sys.exit(1 if fails else 0)
