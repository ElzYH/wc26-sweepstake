# Deploying the World Cup Sweepstake

A short, safe way to run this on an always-on Linux box (e.g. Oracle Cloud Free Tier, Ubuntu 22.04/24.04). Zero dependencies — just Python 3.

---

## 1. Get the code on the box

```bash
sudo adduser --disabled-password --gecos "" sweep   # a normal, non-root user to run it
sudo su - sweep
git clone <your-repo-url> wc26 && cd wc26            # or scp the folder up
python3 --version                                    # needs 3.9+
```

> `config.json` is gitignored, so your API token and admin key never go to GitHub.

## 2. First run (to set a strong admin key + token)

Pick your own admin key (15+ chars) and your football-data.org token so they're stable and memorable:

```bash
ADMIN_KEY='choose-a-long-key-here' FOOTBALL_DATA_TOKEN='your-token' python3 server.py
```

Open `http://<server-ip>:8000/`, run setup, do the draw. Ctrl-C when done — step 3 makes it permanent.

## 3. Run it as a service (auto-start, auto-restart, non-root)

Create `/etc/systemd/system/wc26.service`:

```ini
[Unit]
Description=WC26 Sweepstake
After=network.target

[Service]
User=sweep
WorkingDirectory=/home/sweep/wc26
Environment=ADMIN_KEY=choose-a-long-key-here
Environment=FOOTBALL_DATA_TOKEN=your-token
ExecStart=/usr/bin/python3 server.py
Restart=on-failure
# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/sweep/wc26
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wc26
sudo systemctl status wc26          # check it's running
journalctl -u wc26 -f               # logs (admin key prints here on start)
```

`ProtectSystem=strict` + `ReadWritePaths` means the process can only write inside its own folder — it can't touch system files even if compromised.

## 4. Firewall — expose only what you need

Oracle has **two** layers; open the port in both:

1. Oracle console → your VCN → Security List → add Ingress rule: TCP **8000** (or 443 if using nginx below) from `0.0.0.0/0`.
2. On the box:
```bash
sudo ufw allow OpenSSH
sudo ufw allow 8000/tcp     # or 443 if behind nginx
sudo ufw enable
```

## 5. (Recommended) HTTPS via nginx

Running plain HTTP on the internet is fine for a private link but better behind TLS. Put nginx in front, keep the app on localhost:

- Change the app to bind localhost only: in `server.py`, the bind line `("0.0.0.0", PORT)` → `("127.0.0.1", PORT)`.
- `sudo apt install nginx certbot python3-certbot-nginx`
- Minimal site config proxying `https://yourdomain` → `http://127.0.0.1:8000`, then `sudo certbot --nginx -d yourdomain`.
- Firewall: open 443 instead of 8000.

## 6. Protecting source + config on the server

- **Run as `sweep`, never root.** The server already warns if started as root.
- **`config.json` is auto-chmod 600** (only the `sweep` user can read the token/admin key). Verify: `ls -l config.json` → `-rw-------`.
- Lock down the folder so other users can't edit the code:
```bash
chmod -R go-w /home/sweep/wc26     # nobody but the owner can modify files
```
- The web server only serves a fixed whitelist of files (the HTML pages + `teams.json`), so `*.py` and `config.json` **cannot be fetched over HTTP** — a visitor can't download your source or secrets.
- Editing the source on the server is an OS concern: don't share the `sweep` login, use SSH keys (disable password SSH), and consider `fail2ban` (`sudo apt install fail2ban`) to throttle brute-force SSH.
- The app already rate-limits the admin-key endpoint (10 tries/min/IP) and uses a constant-time key check.

## 7. Updating later

```bash
sudo su - sweep && cd wc26
git pull
sudo systemctl restart wc26
```

The draw is preserved across restarts (it lives in `draw_result.json`). To start a fresh tournament, go to Setup and unlock with your admin key.

---

## Data safety & updating without losing the draw

Your live data lives in JSON files that are **gitignored**, so a code update never touches them:
`config.json` (players, modes, token, admin key), `draw_result.json` (the draw), `results.json` (API results), `tracker_data.json` (computed tracker).

- **Updating code** (`git pull` + `sudo systemctl restart wc26`) only changes `.py`/`.html` — your draw and results stay exactly as they were.
- **Roll back a bad change:** `git checkout <previous-commit>` and restart; data is untouched.

**Built-in protection:**
- If a live refresh fails or the API returns nothing, the server keeps the last good `results.json` / `tracker_data.json` instead of blanking them.
- Every successful refresh snapshots data to `backups/last_good/`.
- Every re-draw snapshots the old draw to `backups/draws/draw-<timestamp>.json` before wiping it.

**Manual backup / restore:**
```bash
mkdir -p ~/wc26-backup && cp config.json draw_result.json results.json tracker_data.json ~/wc26-backup/
# restore the last good draw:
cp backups/last_good/draw_result.json . && sudo systemctl restart wc26
```

**Easiest backup/restore (no terminal):** open **Tracker → ⚙ Settings**, unlock with the admin key, then **⬇ Download backup** — it saves a single JSON file with your draw, results and settings (no secrets). If data is ever lost, even on a brand-new box, use **⬆ Restore from file** to bring it all back; the tracker rebuilds instantly (your API token stays in the environment/Settings and is never in the backup).

**Moving to a new box:** either restore the backup file as above, or copy the four JSON files into the new folder before starting — the tracker resumes exactly where it left off.

### Files you actually need to deploy
Core: `server.py`, `scoring.py`, `update_results.py`, `draw.py`, `players.py`, `teams.json`, `wheel.html`, `tracker.html`, `setup.html`, `me.html`.
Optional: `main.py` (the command-line draw), `simulate_2026.py` + `test_2022.py` (testing), `.github/workflows/update.yml` (if you ever want a cron refresh).
Delete these leftovers before deploying — `draw.html` especially (a stale copy can hijack the "Reveal" link):
`draw.html`, `wheel.py`, `build_2026_demo.py`, `compare_modes.py`. The server no longer serves `draw.html` and redirects any old link to `/wheel`.
