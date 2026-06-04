# World Cup 2026 Sweepstake

Weighted draw + spinning-wheel reveal + live tracker. Teams are tiered by a
blend of FIFA ranking and bookmaker odds, dealt fairly, revealed on a wheel,
and tracked through to the final in three scoring modes.

## Files
| File | What it does |
|------|--------------|
| `teams.json` | 48 teams: tier, weight, group, FIFA/odds blend |
| `players.py` / `draw.py` | draw engine (snake / weighted, leftover drop / pool) |
| `main.py` | run the draw in the terminal → `draw_result.json` |
| `wheel.html` | spinning-wheel reveal (the live draw) |
| `update_results.py` | football-data.org → `results.json` |
| `scoring.py` | → `tracker_data.json` (points + survival + hybrid) |
| `tracker.html` | live tracker GUI, mode toggle, auto-refresh |
| `server.py` / `setup.html` | self-host: web setup wizard + auto-poller |
| `.github/workflows/update.yml` | scheduled refresh (GitHub Pages path) |

## Scoring modes (toggle on the tracker; pick a default at setup)
- **Points** — per goal +1, win +3, draw +1, clean sheet +1, round bonuses.
- **Survival** — value of the furthest stage each team reaches (R32 15 … Winner 150). Last-team-standing.
- **Both** — points + survival.

Edit `SCORING` / `SURVIVAL_VALUE` in `scoring.py` to retune.

---

## Option A — Self-host on a server (Oracle Cloud Free Tier)
Zero dependencies — just Python 3. Good for an always-on box like a Minecraft VM.

1. Create an **Always Free** VM (Ampere A1 / Ubuntu) in Oracle Cloud.
2. Open the port: add an **ingress rule for TCP 8000** to the VCN Security List
   (and `sudo ufw allow 8000` if the firewall is on).
3. Copy the project up and run it:
   ```bash
   git clone <your-repo> sweepstake && cd sweepstake
   python3 server.py            # serves on 0.0.0.0:8000
   ```
4. Visit `http://<your-server-ip>:8000/` → the **setup wizard**: add players,
   pick draw + scoring modes, paste your football-data.org token, hit run.
   Share that same URL with everyone — they get the wheel and live tracker.

Keep it running like a game server with systemd:
```ini
# /etc/systemd/system/sweepstake.service
[Unit]
Description=WC Sweepstake
After=network.target
[Service]
WorkingDirectory=/home/ubuntu/sweepstake
ExecStart=/usr/bin/python3 server.py
Restart=always
User=ubuntu
Environment=PORT=8000
[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now sweepstake
```
The token lives in `config.json` (gitignored) on your private box — keep the VM secure.

## Option B — GitHub Pages + Actions (static, no server)
1. Push to GitHub; run the draw locally (`python main.py`) and commit `draw_result.json`.
2. Add your token as a repo **Secret** `FOOTBALL_DATA_TOKEN` (never in the code).
3. Enable **GitHub Pages** (root). The Action refreshes `tracker_data.json` every 10 min.
4. Share `https://<you>.github.io/<repo>/tracker.html`.

## Reuse for another tournament (e.g. Euro 2028)
Set competition `EC` (in the setup wizard, or `COMPETITION` in the workflow) and
regenerate `teams.json` for that field — same free tier covers the Euros.
