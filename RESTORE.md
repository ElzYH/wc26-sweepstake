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

## Provision a NEW instance (e.g. start brothers/extra)
```bash
INST=brothers   # already has env + dir from initial setup; for a brand-new name, create /etc/wc26/$INST.env and the dir first
sudo ln -sf /opt/wc26/repo/teams.json /opt/wc26/sites/$INST/teams.json   # REQUIRED, or the draw crashes
sudo chown -h wc26:wc26 /opt/wc26/sites/$INST/teams.json
sudo systemctl enable --now wc26@$INST
# add a Caddy block for $INST.bbmsweepstake.co.uk + a DNS A record, then: sudo systemctl reload caddy
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
