# WC26 Sweepstake — Betting Guide

Three parts:
1. **Test it on your Mac** (nothing goes live)
2. **Admin: turn it on for the live draw** (step by step)
3. **Players: how to bet** (share this bit with the group)
4. **When something goes wrong** (troubleshooting)

Betting is **off by default** and **never touches your draw** — switching it on with no bets placed leaves every leaderboard score identical, and you can switch it off again anytime.

---

## 1) Test it on your Mac first

**Easiest (no passcodes — just test the betting):**
```bash
python3 demo_live.py --bet-test
```
- Fast-forwards the group stage, then **pauses before each knockout round**.
- Open **http://localhost:8080/** → **Bets**.
- Pick a player, place a **single**, a **2-fold** and a **3-fold** acca. Type a stake — the box now stays put while the page refreshes.
- Try a stake of **0**, **-5**, or a huge number → you get a clear warning and the button is disabled.
- Watch the **max stake rise each round**: R32 = 35, R16 = 40, QF = 45, SF = 50, **Final = 65**.
- Make several small bets and watch the **open-stake limit** (total on open bets can't exceed the round max; it frees up as bets settle).
- Press **Enter** in the terminal to play the round and watch bets settle (wins/losses/accas/penalty shootouts all resolve).

**To also test the passcode protection** (the "only you can bet your points" bit):
```bash
python3 demo_live.py --bet-test --pins
```
This prints a passcode per player in the terminal. Paste a player's code to bet as them; try someone else's code → rejected.

**Test the admin limits in the demo** (no admin UI in the demo, so use flags):
```bash
python3 demo_live.py --bet-test --max-acca 5        # allow 5-leg accas
python3 demo_live.py --bet-test --max-return 100    # cap winnings at 100/bet
```
By default the demo matches the live defaults: **no winnings cap** and **3-leg accas**.

> `python3 demo_live.py --wager` = a full slow lifelike run with example bets already placed.

---

## 2) Admin — deploy + turn betting on for the live draw

> **Why this is safe with the draw already done:** betting ships **off**. Deploying the code changes nothing players see. The scoring engine is **byte-identical** when no bets exist — turning betting on with zero bets placed leaves every leaderboard score exactly the same. Your draw, results, and points are never touched by this feature; betting only ever *adds* settled winnings on top. You can also turn it off again at any time.

### Step 0 — Pre-flight (2 minutes, do this first)
1. **Run the tests on your Mac** so you never push broken code:
   ```bash
   cd <your repo folder>
   bash check.sh            # must end with "ALL CHECKS PASSED"
   ```
   If it doesn't pass, stop — don't deploy.
2. **Note the current commit** (so you know exactly what to roll back to):
   ```bash
   git rev-parse --short HEAD
   ```
3. **SSH in and take a backup of the live data** (config, draw, results, wagers):
   ```bash
   ssh -i ~/Downloads/ssh-key-2026-06-04.key ubuntu@145.241.215.63
   sudo cp -r /opt/wc26/sites /opt/wc26/sites.bak.$(date +%F-%H%M)
   ls -d /opt/wc26/sites.bak.*          # confirm the backup folder exists
   ```
   (Belt and braces: also do **Settings → Export** in the site to download a JSON bundle to your laptop.)

### Step 1 — Push from your Mac
```bash
git add -A
git commit -m "Add betting feature (off by default)"
bash check.sh && git push            # the && means it only pushes if all tests pass
```

### Step 2 — Pull + restart on the box
On the box (same SSH session):
```bash
cd /opt/wc26/repo
sudo git pull                                   # pulls the new code
sudo systemctl restart wc26@mandem              # restart each site instance
sudo systemctl restart wc26@family
sudo systemctl status wc26@mandem --no-pager | head -6   # look for "active (running)"
```
Then **verify nothing changed for players**:
- Open the site in a browser — it loads as normal.
- The **leaderboard and points are exactly the same** as before.
- There is **no Bets tab** yet (betting is still off).
- Tail the log briefly to confirm no errors:
  ```bash
  sudo journalctl -u wc26@mandem -n 30 --no-pager
  ```

**If anything looks wrong — roll back immediately:**
```bash
cd /opt/wc26/repo
sudo git reset --hard <the short commit from Step 0.2>     # back to the old code
sudo cp -r /opt/wc26/sites.bak.<your-date>/. /opt/wc26/sites/   # only if data looks off
sudo systemctl restart wc26@mandem && sudo systemctl restart wc26@family
```
Players never saw anything change, so a rollback is invisible to them.

### Step 3 — Register the Discord commands (once)
Get your admin key from the box:
```bash
sudo python3 -c "import json;print(json.load(open('/opt/wc26/sites/mandem/config.json'))['admin_key'])"
```
Register the slash commands:
```bash
curl -X POST https://bbmsweepstake.co.uk/api/register_commands \
  -H 'Content-Type: application/json' -d '{"admin_key":"<ADMIN_KEY>"}'
```
(Guild commands appear instantly; global can take up to an hour.)

### Step 4 — Smoke-test it privately before anyone knows
- Site → **Settings** (unlock with the admin key) → **Discord bot** → **Send test notification**. You should see a test line in your Discord channel (and a DM if you've run `/notifyme`). This confirms alerts work — **no bets or points are touched**.

### Step 5 — Turn betting on
Site → **Settings** → **Betting** → toggle **Betting on**. The **Bets tab** now appears for everyone.

### Step 5b (optional) — Set your limits
In the same **Betting** panel:
- **Max winnings per bet** — leave **blank for no limit** (the default), or enter a number to cap how much any single bet/acca can return.
- **Max accumulator legs** — **default 3**; raise it (up to 10) if you want bigger accas.

Tap **Save bet limits**. Changes apply immediately. (Stakes also auto-cap per round: 30 in the group stage, rising to 65 in the final — that's fixed.)

### Step 6 — Passcodes (now self-serve — you can skip this)
Players set up their **own** bet passcodes on the website, so you don't have to generate or hand out anything. Once you've switched betting on (Step 4), each player just:
- opens the **Bets** tab, picks their name, and — first time — **chooses their own passcode** and taps **Create my passcode**. That claims their name; nobody can bet their points without it.

You only need the admin **Generate passcodes** panel if you'd rather issue codes yourself (e.g. to hand them out). It still works exactly as before:
```
🔗 Erol   DMT9T   [DM via bot] [Unlink]
   James  X69D7   [DM via bot]
```
🔗 = that player's Discord is linked for betting. **Regenerate** invalidates old codes. A player who forgets a self-chosen passcode can tap **Change** (needs the old one) or, if their Discord is linked, DM the bot `/resetpin`; otherwise you can reset it for them from this panel.

### Step 7 — (Only if you issued codes) get each player their code, privately
- **DM via bot** (best) — if the player has already run `/notifyme` in Discord, this DMs them their code privately **and** links their Discord so they can bet with no passcode in chat.
- Or message them the code yourself.
- **Never post a passcode in a public channel.** The system never requires that.

### Step 8 (optional but recommended) — "Log in with Discord" on the website
This is the strongest "is it really you" check: a player taps **Log in with Discord** on the Bets tab, approves once, and bets with **no passcode** — their real Discord account is the proof. It's **off until you add the credentials below**, and the passcode system keeps working either way, so this can't break anything that's already live.

1. Go to the **Discord Developer Portal** → your application (you can reuse the same app as the bot) → **OAuth2**.
2. Copy the **Client ID** and **Client Secret**.
3. Under **Redirects**, add the callback URL for *each* site you run, exactly:
   - `https://bbmsweepstake.co.uk/api/discord_oauth_callback`
   - …and the same for any other subdomain you use (e.g. the family site). Discord allows several.
4. On the box, add three keys to each site's `config.json` (it's git-ignored, so this stays private):
```bash
cd /opt/wc26/sites/mandem
sudo python3 - <<'PY'
import json; f="config.json"; c=json.load(open(f))
c["discord_oauth_client_id"]="YOUR_CLIENT_ID"
c["discord_oauth_client_secret"]="YOUR_CLIENT_SECRET"
c["site_url"]="https://bbmsweepstake.co.uk"   # must match the redirect host above
json.dump(c, open(f,"w"), indent=2); print("saved")
PY
sudo systemctl restart wc26@mandem
```
5. Reload the tracker — the **Log in with Discord** button now appears on the Bets tab. First login asks the player which name they are (first come, first served); after that it's automatic.

**Note:** the redirect host in `site_url` and in the Discord portal must match the host players actually visit, and it must be **https** (Caddy already gives you that). If login bounces back with "didn't complete", that mismatch is the usual cause.

---

## 3) Players — how to bet (share this with the group)

You bet the **points you've already earned** from finished games. Win at the odds shown and they're added to your total; lose and the stake's gone. No real money — it's all sweepstake points.

### On the website (simplest)
1. Go to the tracker → **Bets** tab.
2. Pick **your name**. Two ways to prove it's you:
   - **Log in with Discord** (if the organiser enabled it) — tap the button, approve once, and you bet with **no passcode**. Strongest option, and the site knows it's really you.
   - **Or a passcode** — first time, choose your own (4+ characters) and tap **Create my passcode** (no organiser needed). On a new device later, type the same passcode and tap **Save**; a wrong one is never stored. It's saved in your browser, so you only enter it once.
3. **Single:** tap an odds button, type a stake, **Place bet**.
4. **Accumulator:** tap **Accumulator**, tap 2–5 games, type one stake, **Place acca** — odds multiply and **all** legs must win.

### On Discord (your passcode never gets typed in a channel)
You link your Discord account once, then you never type your code in chat:

**To join / get set up:**
1. Join the group's Discord server (use the invite the organiser sends).
2. In Discord, run `/notifyme <your name>` (e.g. `/notifyme Erol`) — this claims your player for match alerts.
3. Ask the organiser to hit **DM via bot** for you → the bot sends you your passcode **and** links your account.
   - *Or* self-link: on the website Bets tab, type your passcode, tap **Connect Discord**, it shows a one-time code, then run `/linkdiscord code:THATCODE` in a DM to the bot.

**Once linked, the betting commands:**
- `/games` — upcoming games + odds
- `/bet team:<name> pick:<home/draw/away> stake:<n>` — as you type **team**, Discord suggests the actual upcoming games ("Brazil — v Spain"); pick one to lock in the game + team, then choose **pick** (home/draw/away) and **stake**. Preview shows the payout; add `confirm:true` to place.
- `/mybets` — your open + settled bets
- `/points` — how many points you have to bet + your current max bet
- `/allbets` — everyone's open bets
- `/scores` — live scores + recent results
- `/mypin` — DM yourself your own passcode
- `/unlink` — disconnect this Discord (if you linked the wrong player)

### The rules (so nobody's surprised)
- Bet **before kick-off** only; odds **lock** when you place; **no cash-out**.
- **Max stake rises each round:** Group 30 → R32 35 → R16 40 → QF 45 → SF 50 → **Final 65**.
- **Open-stake limit:** the total you have on open bets at once can't beat the current max — it frees up as bets settle.
- Up to **8** open bets. **No cap on winnings by default** — the organiser can optionally set a max return per bet.
- **Accumulator:** up to **3 legs by default** (the organiser can change this); all must win, and **if one leg loses the whole acca pays nothing**; a postponed leg drops out and it pays on the rest.
- **Handicap bets:** back a team with a goal start or deficit (−1.5, +1.5, −2.5, +2.5 where offered). Settles on the **90'+extra-time score — penalties don't count** (same basis as Over/Under and exact score, *not* the "to advance" market: a 1–1 game decided on pens means −1.5 **loses** and +1.5 **wins** regardless of the shootout). Half-lines only, so a handicap bet can never push. Handicap and exact-score picks can go in **accumulators** too — any mix of markets. **Same-game combos are allowed** and priced off the **joint probability** of all your picks landing together, never the straight multiply (correlated picks — say a team −1.5 *and* Over 2.5 — overlap, so the honest combined price pays less than multiplying the two; the slip flags it and your bet card shows the exact odds). Impossible combos are refused, the exact same pick can't go in twice, and method-of-victory only combines across games. Web only for now (not on Discord `/bet`).
- **Both teams to score:** Yes/No on both sides finding the net (90'+extra time). Settles on the plain score, so it works on **every feed tier**.
- **Method of victory (knockouts):** back a side to go through **in 90'**, **in extra time**, or **on penalties** — six prices, settled from the official match data. Acca-able across games like everything else.
- **Cards (Over/Under):** total bookings in **90 minutes only** (every card counts 1 — a straight red, a yellow, a second yellow each count once; extra-time bookings don't). Needs the deep-data feed: if a game's cards data never arrives, the bet **pushes and your stake comes back** automatically. On a feed plan with no bookings at all the cards market simply doesn't appear (auto-detected; `cards_market` in config.json forces it on/off).
- **No draw bets on knockouts** (it goes to extra time / penalties — pick the side to go through).
- You can't stake more than you have, and your points can never go below zero.

### Feed tiers — everything degrades gracefully
The site runs on any football-data.org plan. On the **free tier**: results, points, result/O/U/handicap/exact-score/BTTS betting and all score alerts work fully; the cards market auto-hides; a method-of-victory bet that the feed can't decide (no 90-vs-ET breakdown) **pushes with a full refund** after a grace instead of guessing — pens games still settle properly on every tier. On **deep data**: cards betting switches on, goal alerts name the scorer, line-ups appear on match cards with a 📋 notification when they're released, and red cards alert live.

### Alerts: disallowed goals (VAR)
If a live score **drops back** (a goal chalked off by VAR), everyone gets told once — the chalked team's owner gets a personal DM/push, the channel and all-games feed get a 🚫 line with the corrected score. A feed flap replaying the same reversion, or a server restart, can never re-send it. A score correction to an already-finished game is treated as a results fix and stays silent.

---

## 4) When something goes wrong (client side)

| Symptom | What it means / fix |
|---|---|
| **Can't type a stake / box keeps clearing** | You're on an old `tracker.html`. Make sure the deployed file is the latest (`grep -c refreshBetsLive tracker.html` should be ≥1). |
| **"Enter your bet passcode"** | Betting has passcodes on. Type the code the organiser gave you; it's then remembered in your browser. (In the local test, run `--bet-test` *without* `--pins` to skip this.) |
| **"Wrong bet passcode"** | Wrong code, or it was regenerated. Ask the organiser; on Discord use `/mypin` once linked. |
| **"No draw bets on knockout games"** | Correct — knockouts can't be a draw. Pick the side to go through. |
| **"Max stake here is N"** | That's the cap for the current round. Lower the stake. |
| **"You can have at most N points riding on open bets"** | You've hit the open-stake limit. Wait for one of your bets to settle, then bet again. |
| **"You only have X points available"** | You can only stake points from **finished** games — live/provisional points don't count. |
| **"That would return … — the cap is N"** | The organiser set a winnings cap and this bet exceeds it; lower the stake. (By default there's no cap.) |
| **Bets tab missing** | Betting is off, or refresh the page. Viewing the site never needs an account. |
| **Discord `/bet` says "not linked"** | Do the link step (Step 3 / Connect Discord → `/linkdiscord`), or ask the organiser for **DM via bot**. |
| **Slash commands don't appear in Discord** | Admin: re-run the register-commands curl (Step 2). |
| **Placed a bet but it's not showing** | It refreshes within a few seconds; pull to refresh. If it truly didn't place, you'll have seen a red warning explaining why. |

**Admin debugging:** every bet, acca, passcode generation and unlink is timestamped in the service log:
```bash
sudo journalctl -u wc26@mandem -n 100 --no-pager      # recent activity
sudo journalctl -u wc26@mandem -f                      # follow live
```
Look for lines like `wager placed: Erol HOME on 73` or `acca placed: James 3 legs`. Failed Discord DMs/announcements log as `bot dm failed:` / `bet announce … failed:` (these don't stop the bet — they just mean Discord delivery had an issue).

If a player linked the wrong account: **Settings → Betting → Unlink** next to their name (or they run `/unlink`), then they re-link.

---

## What's tested vs. what to confirm live

Automated gate (`bash check.sh`) + a 4,000-operation fuzz test cover the engine, odds, single/acca placement, all the caps and limits, settlement (full-time / extra-time / penalties / void / forfeit), the open-stake rule, no-negative-balance, persistence across restarts, and every API endpoint. Zero invariant violations.

**Can't be tested off your server:** real Discord **DM / webhook delivery**. After going live, do one round-trip — hit **DM via bot** for yourself and place one real `/bet` — to confirm Discord works on your box.
