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

# fair draw: round-1 favourite guaranteed (band 1 = true top-n), squads complete + unique, champion-odds
# floor holds (>=15% on 5 players), and squad strengths are balanced (keeps the pre-tournament forecast fair)
_top = set(t["name"] for t in sorted(TEAMS.values(), key=lambda t: -t["composite"])[:len(PLAYERS)])
_per = len(TEAMS) // len(PLAYERS)
_ti = sum(t.get("implied_prob", 0) for t in TEAMS.values()) or 1.0
_champ = {t["name"]: 100.0 * t.get("implied_prob", 0) / _ti for t in TEAMS.values()}
_tc = sum(t.get("composite", 0) for t in TEAMS.values()) or 1.0
_str = {t["name"]: 100.0 * t.get("composite", 0) / _tc for t in TEAMS.values()}
_eq = 100.0 / len(PLAYERS)
_fair = "ok"; _worst = 100.0; _max_spread = 0.0
for _ in range(200):
    _a, _ = server.compute_assignment("fair", PLAYERS)
    _all = [t["name"] for p in PLAYERS for t in _a[p]]
    if set(_a[p][0]["name"] for p in PLAYERS) != _top:
        _fair = "round-1 favourite not guaranteed (band 1 != top-%d)" % len(PLAYERS); break
    if len(_all) != len(set(_all)):
        _fair = "duplicate team in draw"; break
    if any(len(_a[p]) != _per for p in PLAYERS):
        _fair = "uneven squad sizes"; break
    _cmin = min(sum(_champ.get(t["name"], 0) for t in _a[p]) for p in PLAYERS)
    _worst = min(_worst, _cmin)
    if _cmin < 0.75 * _eq - 0.01:
        _fair = "champion-odds floor breached: %.1f%% < %.1f%%" % (_cmin, 0.75 * _eq); break
    _ss = [sum(_str.get(t["name"], 0) for t in _a[p]) for p in PLAYERS]
    _max_spread = max(_max_spread, max(_ss) - min(_ss))
if _fair != "ok":
    fails.append(("fair-draw", _fair))
elif _max_spread > 8.0:                                      # squads should stay within a tight strength band
    fails.append(("fair-draw", "squad-strength spread too wide: %.1fpts" % _max_spread))
else:
    print("[fair-draw] favourite guaranteed, champ floor >=%.0f%% (worst %.1f%%), strength spread <=%.1fpts OK"
          % (0.75 * _eq, _worst, _max_spread))

# summary: medal lines must surface the real leaderboard score (regression: was reading wrong key -> always 0)
json.dump({"stats": {"matches_played": 104, "goals": 303, "goals_per_match": 2.91, "teams_remaining": 1},
           "leaderboards": {"hybrid": [{"name": "Zed", "score": 42, "alive_teams": 1, "total_teams": 9}]},
           "champion_decided": {"team": "Argentina", "owner": "Zed"}, "teams": []},
          open(os.path.join(D, "tracker_data.json"), "w"))
_sm = server.build_summary()
_line = next((l for l in _sm if l.startswith("🥇")), "")
_champ = next((l for l in _sm if l.startswith("🏆")), "")
if "42" not in _line or "Zed" not in _line:
    fails.append(("summary", "top score 42 not surfaced in medal line %r" % _line))
elif "Argentina" not in _champ:
    fails.append(("summary", "champion not surfaced: %r" % _champ))
else:
    print("[summary] leaderboard score (key='score') + champion line surface OK")

# champion alert must actually FIRE on the final (regression: read wrong key -> KeyError -> alert silently skipped)
server.push_broadcast = lambda *a, **k: None
_sent.clear()
_fin = {"stage": "FINAL", "status": "FINISHED", "home": "Brazil", "away": "Spain",
        "homeScore": 2, "awayScore": 1, "homeOwner": "Erol", "awayOwner": "James", "winner": "HOME_TEAM"}
_lb = {"hybrid": [{"name": "Erol", "score": 120, "alive_teams": 1, "total_teams": 9}], "points": [], "survival": []}
_new = {"stats": {"matches_played": 104, "teams_remaining": 1}, "fixtures": [_fin],
        "leaderboards": _lb, "players": [], "champion_decided": {"team": "Brazil", "owner": "Erol"}}
_old = json.loads(json.dumps(_new)); _old["fixtures"][0]["status"] = "IN_PLAY"   # final not yet finished
json.dump(_new, open(os.path.join(D, "tracker_data.json"), "w"))
server.notify_changes(_old)
_champ_msgs = [s for s in _sent if "Champions" in s or "champions" in s]
if not _champ_msgs:
    fails.append(("champion-alert", "no champion alert fired on the final"))
elif "120" not in _champ_msgs[0]:
    fails.append(("champion-alert", "champion alert missing the finishing score: %r" % _champ_msgs[0]))
else:
    print("[champion-alert] champion notification fires with finishing score OK")

shutil.rmtree(D, ignore_errors=True)
if fails:
    print("\nFAILED:", fails)
    raise SystemExit(1)
print("\nAll bot tests passed.")
