# WC26 Sweepstake — Operations Runbook

Everything you need to recover, reset, or re-provision an instance.

## Where things live (on the box)

- **Code:** `/opt/wc26/repo` (git clone of `ElzYH/wc26-sweepstake`)
- **Per-instance data:** `/opt/wc26/sites/<inst>/` — `config.json` (players, modes, token, **admin key**), `draw_result.json`, `results.json`, `tracker_data.json`, and `backups/` (in-app snapshots)
- **Per-instance env:** `/etc/wc26/<inst>.env` (PORT, HOST, WC26_CONFIG, WC26_DATA, token)
- **Service:** `wc26@<inst>` (systemd). Reverse proxy: `/etc/caddy/Caddyfile`
- **Instances:** `mandem` (8001), `brothers` (8002), `family` (8003), `extra` (8004)
- **teams.json:** ships with the code; **symlinked** into each data dir (required — see Provisioning)

## Backups

- **In-app:** every redraw/import snapshots the draw to `sites/<inst>/backups/draws/`; each good update snapshots to `backups/last_good/`.
- **Off-box (automatic):** hourly encrypted bundle pushed to the private repo `ElzYH/wc26-backups`, keeping the last 72. Each bundle = all `sites/`, all `etc/*.env` + `Caddyfile`, and `MANIFEST.txt`.
- **Encryption:** AES-256 via `openssl`, passphrase in `/etc/wc26/backup.pass` (root-only). **Lose that passphrase = backups unrecoverable.**

### Verify a backup (non-destructive — do this anytime)
```bash
LATEST=$(ls -1t /opt/wc26/backup-repo/wc26-backup-*.tar.gz.enc | head -1)
sudo bash -c "openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in '$LATEST' -pass file:/etc/wc26/backup.pass | tar -tzf -"
```
Should list `sites/<inst>/config.json`, `etc/Caddyfile`, `MANIFEST.txt`, etc.

### Prove restore works (safe — restores into a scratch dir, touches nothing live)
```bash
LATEST=$(ls -1t /opt/wc26/backup-repo/wc26-backup-*.tar.gz.enc | head -1)
sudo rm -rf /tmp/wc26-restore && sudo mkdir -p /tmp/wc26-restore
sudo bash -c "openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in '$LATEST' -pass file:/etc/wc26/backup.pass | tar -xzf - -C /tmp/wc26-restore"
sudo diff -q /tmp/wc26-restore/sites/mandem/config.json /opt/wc26/sites/mandem/config.json && echo "RESTORE VERIFIED: decrypted config matches live"
sudo cat /tmp/wc26-restore/MANIFEST.txt
sudo rm -rf /tmp/wc26-restore
```
If it prints "RESTORE VERIFIED", your backup→restore path is proven end-to-end.

## Restore one instance from the latest backup (real recovery)
Use this if data was lost/corrupted. Replace `mandem` as needed.
```bash
INST=mandem
LATEST=$(ls -1t /opt/wc26/backup-repo/wc26-backup-*.tar.gz.enc | head -1)
sudo rm -rf /tmp/wc26-restore && sudo mkdir -p /tmp/wc26-restore
sudo bash -c "openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in '$LATEST' -pass file:/etc/wc26/backup.pass | tar -xzf - -C /tmp/wc26-restore"

sudo systemctl stop wc26@$INST
# restore the data files (config, draw, results, tracker)
sudo cp -a /tmp/wc26-restore/sites/$INST/config.json        /opt/wc26/sites/$INST/ 2>/dev/null || true
sudo cp -a /tmp/wc26-restore/sites/$INST/draw_result.json   /opt/wc26/sites/$INST/ 2>/dev/null || true
sudo cp -a /tmp/wc26-restore/sites/$INST/results.json       /opt/wc26/sites/$INST/ 2>/dev/null || true
sudo cp -a /tmp/wc26-restore/sites/$INST/tracker_data.json  /opt/wc26/sites/$INST/ 2>/dev/null || true
# (optional) restore env + Caddyfile if those were lost
# sudo cp -a /tmp/wc26-restore/etc/$INST.env /etc/wc26/
# sudo cp -a /tmp/wc26-restore/etc/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy

# fix ownership/permissions + re-link teams.json
sudo chown -R wc26:wc26 /opt/wc26/sites/$INST
sudo chmod 600 /opt/wc26/sites/$INST/config.json
sudo ln -sf /opt/wc26/repo/teams.json /opt/wc26/sites/$INST/teams.json
sudo chown -h wc26:wc26 /opt/wc26/sites/$INST/teams.json

sudo systemctl start wc26@$INST
sudo rm -rf /tmp/wc26-restore
```

## Reset one sweepstake (wipe and start fresh)
Clears the draw + config so you can run a brand-new sweepstake on that subdomain.
```bash
INST=mandem
sudo systemctl stop wc26@$INST
sudo rm -f /opt/wc26/sites/$INST/{config.json,draw_result.json,results.json,tracker_data.json}
sudo rm -rf /opt/wc26/sites/$INST/backups
sudo ln -sf /opt/wc26/repo/teams.json /opt/wc26/sites/$INST/teams.json   # ensure teams.json present
sudo chown -h wc26:wc26 /opt/wc26/sites/$INST/teams.json
sudo systemctl start wc26@$INST
# grab the freshly generated admin key:
sudo grep -o '"admin_key": *"[^"]*"' /opt/wc26/sites/$INST/config.json
```
Then open `/setup`, enter that key, and run the new draw.

## Provision a NEW instance / subdomain from scratch
Run a whole separate sweepstake on its own subdomain (e.g. `brothers.bbmsweepstake.co.uk`).
Pick a short instance name and a free port (mandem=8001, brothers=8002, family=8003, extra=8004 — use the next free number for new ones, e.g. 8005).
```bash
INST=brothers; PORT=8002        # <-- change these two

# 1) data dir + env file
sudo mkdir -p /opt/wc26/sites/$INST/backups
sudo tee /etc/wc26/$INST.env >/dev/null <<EOF
WC26_CONFIG=/opt/wc26/sites/$INST/config.json
WC26_DATA=/opt/wc26/sites/$INST
PORT=$PORT
HOST=127.0.0.1
FOOTBALL_DATA_TOKEN=
EOF

# 2) teams.json symlink (MANDATORY — draw crashes without it)
sudo ln -sf /opt/wc26/repo/teams.json /opt/wc26/sites/$INST/teams.json
sudo chown -R wc26:wc26 /opt/wc26/sites/$INST
sudo chown -h wc26:wc26 /opt/wc26/sites/$INST/teams.json

# 3) start the service
sudo systemctl enable --now wc26@$INST
systemctl is-active wc26@$INST

# 4) grab the auto-generated admin key for this instance
sudo grep -o '"admin_key": *"[^"]*"' /opt/wc26/sites/$INST/config.json
```
**5) DNS:** in Squarespace DNS add an **A record**: host `brothers` → the reserved IP `145.241.215.63`.

**6) Caddy:** add a block to `/etc/caddy/Caddyfile` (Caddy auto-issues HTTPS):
```
brothers.bbmsweepstake.co.uk {
    reverse_proxy 127.0.0.1:8002
}
```
then `sudo systemctl reload caddy`.

**7)** Open `https://brothers.bbmsweepstake.co.uk/setup`, enter the admin key from step 4, add the players, and run the draw. Each subdomain is fully independent — its own players, draw, token, admin key, Discord webhook/bot, and push subscribers.

### Re-provision an instance that already has an env + dir
```bash
INST=brothers
sudo ln -sf /opt/wc26/repo/teams.json /opt/wc26/sites/$INST/teams.json   # REQUIRED
sudo chown -h wc26:wc26 /opt/wc26/sites/$INST/teams.json
sudo systemctl enable --now wc26@$INST
```
**The teams.json symlink is mandatory for every instance** — without it the draw save crashes (the server runs from the data dir and can't find the shipped teams.json).

## Rotate the admin key
```bash
sudo /opt/wc26/rotate-key.sh <inst>
```
Prints a new key, updates config, restarts the service. Save it; re-unlock on the site.

## Update the app code
```bash
cd /opt/wc26/repo && sudo git pull && sudo systemctl restart wc26@mandem wc26@family
sudo journalctl -u wc26@mandem -f   # watch for errors / activity
```

## Health / activity log
```bash
sudo journalctl -u wc26@mandem -f          # live tail (setup saves, draw locks, write attempts, errors)
systemctl is-active wc26@mandem caddy      # quick up/down check
sudo systemctl list-timers wc26-backup.timer   # confirm hourly backup is scheduled
```

## Features & integrations (config keys)
All of these live in `sites/<inst>/config.json` (mode 600). **A re-run of `/setup` preserves every one of them** — Setup only rewrites the player/draw fields, so the Discord webhook, bot, push keys and tracker link survive. Setup does reset the draw itself.

| Key | What it does |
|---|---|
| `admin_key` | unlocks Setup / Settings / draw lock / bot registration |
| `token` / `FOOTBALL_DATA_TOKEN` | football-data.org API token for live scores |
| `poll_minutes` | how often to fetch scores. **On the €12 live plan set this to 1.** |
| `discord_webhook` | channel webhook — every kickoff/goal/knockout/lead change/champion posts here |
| `discord_invite` | invite link shown behind the in-app "Join the Discord" button |
| `site_url` | the public tracker URL, embedded in Discord posts |
| `vapid_private` / `vapid_public` | auto-generated Web Push keys (native phone notifications) |
| `discord_app_id` | bot Application ID (public) |
| `discord_pubkey` | bot Public Key (public) — verifies incoming slash commands |
| `discord_guild_id` | server ID for instant command registration (optional) |
| `discord_bot_token` | **secret** — never paste in chat/logs; used only to register commands |
| `digest_enabled` | when true, auto-posts a once-a-day summary to the Discord webhook |
| `digest_hour` | hour (0–23, UTC) at/after which the daily summary is posted |

### Notifications
- **Two channels:** Discord webhook (works for everyone in the channel) and native **Web Push** (per phone).
- Web Push needs `pywebpush` on the box: `sudo pip3 install pywebpush --break-system-packages`. The log prints `Web Push: ENABLED` when it's working.
- **iPhone:** native push only works if the user **Adds the site to their Home Screen** (Share → Add to Home Screen) and opens it from that icon — Safari tabs can't receive push. The in-app 🔔 modal explains this.
- Discord webhook posts **must** send a custom `User-Agent` header (Cloudflare 403s the default Python one) — already handled in `discord_send`.

### Discord bot (read-only slash commands)
- Commands: `/help /summary /leaderboard /groups /odds /stats /fixtures /myteams <player> /players /team <name>`. All read-only; nothing can change the draw from Discord.
- **Interactions endpoint** (set in the Discord Developer Portal → your app → General → Interactions Endpoint URL):
  `https://<subdomain>.bbmsweepstake.co.uk/api/discord_interactions`
- Register/refresh commands after setting App ID + Bot token in Settings → **Register commands** (or `POST /api/register_commands` with the admin key). Guild registration is instant; global can take ~1h.

### Server-side auto-draw
- On `/wheel`, **Run draw on the server** computes + reveals the draw on the server and writes the live state, so it keeps going even if the host closes the tab. Everyone follows on `/watch`; it locks the draw + recomputes when done.
- A redraw/reset bumps an internal generation counter so any running reveal stops cleanly.

### Live (paid) tier — what €12 gives
- **Live (real-time) scores** (free tier delays them) + **20 calls/min** (free = 10). Set `poll_minutes = 1`.
- The live **match minute** now shows in the Overview "LIVE" cards and `/fixtures`.
- **Goal scorers / goal minute are NOT in the €12 tier** — they need the €29 deep-data pack. Not enabled.
- The app handles **both** tiers safely: if the API returns a minimal/free-tier payload (no minute, no score breakdown), scoring and the live cards degrade gracefully instead of erroring.

### Daily summary digest (auto-posts once a day)
- Settings → **Daily summary** → tick *Post a daily summary*, set the hour (UTC). Saves `digest_enabled` + `digest_hour`.
- The poller posts the summary to the Discord webhook **once per day** at/after that hour (idempotent — a persisted `last_digest_date` stops repeats even across restarts). Needs a webhook set and the draw locked.
- **Send a test now** (Settings) posts the current summary immediately so you can eyeball it.
- The summary shows the top-3 leaderboard (real points/teams-in), teams still in, top team + most goals, games played, and — once decided — the champion.

### Who's visiting (access log)
- Settings → **Who's visiting → Load visitors** (admin-only) shows recent page opens: IP, device (from user-agent), most-viewed page, view count, last seen, plus unique-today / unique-all-time / total views.
- It records **page views only** (`/setup /tracker /wheel /me /watch`), not the high-frequency API polls, so it reflects real visits. Held in memory (last 600 views, up to ~1500 distinct visitors), so it resets on restart — it's a "who's around" view, not an audit log.
- Behind Caddy the real client IP comes from `X-Forwarded-For` (already handled). The endpoint `POST /api/access_log` is admin-key-gated and never returns secrets.

### The fair draw (what "Fair" guarantees)
- **Round 1 is guaranteed:** every player gets one of the true top-N favourites (band 1 is always the strongest N teams, shuffled one-per-player).
- **After that it's loose** — better teams are just more likely, not banded.
- The draw then **re-draws until two conditions hold**: squads are balanced in strength (within ~10% of an equal share — this keeps the *pre-tournament forecast* fair) **and** no player is below a **15%-on-5-players champion-odds floor**. If no draw clears both inside the budget, it keeps the most balanced one found.
- **Note on the forecast:** the Overview "forecast" is a stochastic, winner-take-all simulation that *amplifies* small strength gaps, so it can't be hard-pinned to exactly 15% — but balancing squad strength tightens it a lot (typical spread ~17–23% instead of 34/12). The champion-odds floor *is* a hard guarantee. The forecast only updates when a **fresh draw** is run.

## Runs unattended (you can leave it alone)
The server is built to run for the whole tournament with no babysitting:
- **systemd** keeps it up: the unit has `Restart=always`, so a crash or reboot brings it straight back. `systemctl is-active wc26@mandem` to confirm.
- **Poller** fetches scores every `poll_minutes`, recomputes the tracker, fires Discord/push alerts, and posts the daily digest — all on its own. Each loop is wrapped so one bad fetch can't kill the thread.
- **Atomic writes**: every JSON write (config, results, tracker, live state, push subs) goes via temp-file + `fsync` + rename, so a crash mid-write can't corrupt a file — the old one stays intact.
- **Fail-safe loads**: a missing/half-written config or data file falls back to safe defaults instead of crashing.
- **Off-box backups**: hourly encrypted bundle to `ElzYH/wc26-backups` (keeps 72). Verify anytime with the "Verify a backup" block above.
- **Self-running draw**: "Run draw on the server" keeps revealing even if the host closes the tab.

If you check one thing periodically: `sudo journalctl -u wc26@mandem -f` (live activity + any errors).

## Final-build prep (clean slate for the real players)
Do this once, just before the tournament, on each live instance (`mandem`, `family`):
```bash
INST=mandem                                   # repeat for: family
# 1) get the latest code + the new tests/gate
cd /opt/wc26/repo && sudo git pull

# 2) rotate the admin key (the old one was shared in chat earlier — rotate it)
sudo /opt/wc26/rotate-key.sh $INST            # prints the NEW key — save it

# 3) clear old backups + any test/demo draw so the build starts clean
sudo systemctl stop wc26@$INST
sudo rm -rf /opt/wc26/sites/$INST/backups
sudo rm -f  /opt/wc26/sites/$INST/{draw_result.json,results.json,tracker_data.json,live_draw.json}
sudo ln -sf /opt/wc26/repo/teams.json /opt/wc26/sites/$INST/teams.json   # keep the symlink
sudo chown -h wc26:wc26 /opt/wc26/sites/$INST/teams.json
sudo systemctl start wc26@$INST

# 4) set live-tier polling + (optionally) the daily summary, then verify
#    /setup preserves token/Discord/push — it only resets players + the draw.
```
Then: open `/setup`, enter the new key, add the real players, set `poll_minutes` to **1** (live tier), turn on the daily summary if you want it, run the draw, and check **Who's visiting** + the tracker. Config (token, Discord, push) survives the reset — only the draw + players are rewritten.

## Pre-kickoff dry-run (do this before 11 June)
1. **Reset** the instance (Reset section above) or just `/setup` a throwaway set of players.
2. **Draw:** open `/wheel`, unlock, hit **Run draw on the server** → confirm it reveals on `/watch` and locks.
3. **Tracker:** `/tracker` shows groups with owners, the 📋 summary, and (pre-tournament) 0-0-0 tables.
4. **Alerts:** on a phone, add to Home Screen, open it, 🔔 → pick your name → enable. Tap **Send test** → confirm it arrives. Post a Discord **Demo a live game** from Settings → confirm it lands in the channel.
5. **Bot:** in Discord type `/summary`, `/groups`, `/fixtures` → confirm replies.
6. **Clean up:** Reset the instance so it's empty for the real players, then re-run Setup for real.

## Local pre-deploy gate
On the Mac, from the repo root, before every push:
```bash
bash check.sh && git push
```
`check.sh` runs Python syntax, inline-JS `node --check`, CSS brace balance, a **structural-integrity check** (asserts the critical functions + POST routes still exist — guards against an edit accidentally dropping one), the scoring unit tests, the full-2022 replay, the **bot command tests** (`test_bot.py`, incl. the fair-draw floor + daily-digest idempotency + summary regression), and the live **smoke/security/stress tests** (`smoke_test.py`, incl. 40 concurrent requests, the access-log endpoint, key-gating, path-traversal and no-secret-leak checks). If it doesn't say "ALL CHECKS PASSED", don't deploy.
