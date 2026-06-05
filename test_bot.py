"""Functional check: every read-only bot command + the summary, run against a
real computed draw — once in the pre-tournament state and once mid-tournament.
Run: python3 test_bot.py"""
import os, json, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
D = tempfile.mkdtemp(prefix="wc26bot_")
for f in ("server.py", "scoring.py", "draw.py", "players.py", "teams.json", "update_results.py"):
    shutil.copy(os.path.join(HERE, f), D)
os.environ["WC26_CONFIG"] = os.path.join(D, "config.json")
os.environ["WC26_DATA"] = D
PLAYERS = ["Erol", "James", "Louis", "Ismail", "Reuben"]
json.dump({"scoring_mode": "hybrid", "players": PLAYERS}, open(os.path.join(D, "config.json"), "w"))
os.chdir(D)

import server, scoring  # noqa: E402

TEAMS = {t["name"]: t for t in json.load(open("teams.json"))["teams"]}
assign, bonus = server.compute_assignment("fair", PLAYERS)
payload = {"mode": "fair", "leftover": "pool",
           "players": [{"name": p, "teams": [t["name"] for t in assign[p]]} for p in assign],
           "bonus_pool": [t["name"] for t in bonus]}
json.dump(server.build_draw_result(payload), open("draw_result.json", "w"))


def owned_first():
    return next(iter(assign.values()))[0]["name"]


CMDS = [("help", {}), ("summary", {}), ("leaderboard", {}), ("odds", {}), ("stats", {}),
        ("fixtures", {}), ("groups", {}), ("players", {}), ("myteams", {"player": "Erol"}),
        ("myteams", {"player": "nobody"}), ("team", {"name": owned_first()}),
        ("team", {"name": "Atlantis"}), ("team", {}), ("bogus", {})]
fails = []


def run_all(tag):
    scoring.compute(out="tracker_data.json", default_mode="hybrid")
    local = []
    for cmd, opts in CMDS:
        try:
            r = server.discord_command(cmd, opts)
            if not (isinstance(r, str) and r.strip()):
                local.append((cmd, "empty reply"))
            elif len(r) > 2000:
                local.append((cmd, "reply over Discord's 2000-char limit (%d)" % len(r)))
        except Exception as e:
            local.append((cmd, repr(e)))
    try:
        s = server.build_summary()
        assert isinstance(s, list) and s
    except Exception as e:
        local.append(("build_summary", repr(e)))
    print("[%s] all commands OK" % tag if not local else "[%s] FAIL: %s" % (tag, local))
    return local


# 1) pre-tournament: groups 0-0-0, no games — the real state at kickoff
server._write_pretournament("WC")
fails += run_all("pre-tournament")

# 2) mid-tournament: a few finished games + one live (with a minute)
def grp(n):
    return TEAMS[n]["group"]

owned = [t["name"] for v in assign.values() for t in v]
bygroup = {}
for n in owned:
    bygroup.setdefault(grp(n), []).append(n)
res = json.load(open("results.json"))  # keeps pre-tournament standings shape
matches, mid, made = [], 1, 0
for g, ns in bygroup.items():
    if len(ns) >= 2 and made < 4:
        matches.append({"id": mid, "stage": "GROUP_STAGE", "group": g, "utcDate": "2026-06-11T16:00:00Z",
                        "status": "FINISHED", "home": ns[0], "away": ns[1],
                        "homeScore": 2, "awayScore": 1, "minute": None, "winner": "HOME"})
        mid += 1
        made += 1
ag = next(iter(bygroup))
if len(bygroup[ag]) >= 2:
    matches.append({"id": 99, "stage": "GROUP_STAGE", "group": ag, "utcDate": "2026-06-12T16:00:00Z",
                    "status": "IN_PLAY", "home": bygroup[ag][0], "away": bygroup[ag][1],
                    "homeScore": 1, "awayScore": 0, "minute": 67, "winner": None})
res["matches"] = matches
json.dump(res, open("results.json", "w"))
fails += run_all("mid-tournament")

# spot-check the live minute reached /fixtures
fx = server.discord_command("fixtures", {})
if "67'" not in fx:
    fails.append(("fixtures", "live minute 67' not shown"))
else:
    print("[mid-tournament] /fixtures shows live minute 67' OK")

# /team should resolve a real owner
tl = server.discord_command("team", {"name": owned_first()})
if owned_first() not in tl or "owned by" not in tl:
    fails.append(("team", "did not resolve owner for %s" % owned_first()))
if "No team called" not in server.discord_command("team", {"name": "Atlantis"}):
    fails.append(("team", "unknown team not handled"))

# server auto-draw reveal: runs to completion, locks, recomputes (runs last — overwrites the draw)
import threading, time
server._draw_state["gen"] += 1; server._draw_state["running"] = True
_g = server._draw_state["gen"]
_t = threading.Thread(target=server.run_auto_draw, args=(_g, PLAYERS, "fair", None, "pool", 0.0, 0.0), daemon=True)
_t.start(); _t.join(timeout=10)
_st = server.live_load()
_dr = json.load(open("draw_result.json"))
_total = sum(len(p["teams"]) for p in _dr["players"])
_want = (len(TEAMS) // len(PLAYERS)) * len(PLAYERS)
if not (_st.get("done") and _st.get("phase") == "done"):
    fails.append(("auto-draw", "did not reach done: %s" % _st.get("phase")))
elif _total != _want:
    fails.append(("auto-draw", "assigned %d teams, expected %d" % (_total, _want)))
else:
    print("[auto-draw] server reveal completed and locked %d teams OK" % _total)

# daily digest: posts once per day, idempotent across calls/restarts (delivery is stubbed)
_sent = []
server.discord_send = lambda text: _sent.append(text)
_cfg = server.load_config()
_cfg.update({"discord_webhook": "https://discord.com/api/webhooks/x/y", "digest_enabled": True,
             "digest_hour": 0, "last_digest_date": None})
server.save_config(_cfg)
server.maybe_send_daily_digest(server.load_config())
server.maybe_send_daily_digest(server.load_config())   # second call same day must NOT re-send
if len(_sent) != 1:
    fails.append(("digest", "expected exactly 1 send, got %d" % len(_sent)))
elif not server.load_config().get("last_digest_date"):
    fails.append(("digest", "last_digest_date not recorded"))
else:
    print("[digest] posts once per day, idempotent OK")

shutil.rmtree(D, ignore_errors=True)
if fails:
    print("\nFAILED:", fails)
    raise SystemExit(1)
print("\nAll bot tests passed.")
