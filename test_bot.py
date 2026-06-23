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
json.dump({"scoring_mode": "points", "players": PLAYERS}, open(os.path.join(D, "config.json"), "w"))
os.chdir(D)

import server, scoring  # noqa: E402

TEAMS = {t["name"]: t for t in json.load(open("teams.json"))["teams"]}
assign, bonus = server.compute_assignment("fair", PLAYERS)
payload = {"mode": "fair", "leftover": "pool",
           "players": [{"name": p, "teams": [t["name"] for t in assign[p]]} for p in assign],
           "bonus_pool": [t["name"] for t in bonus]}
json.dump(server.build_draw_result(payload), open("draw_result.json", "w"))

# draw announcement: round-by-round + final squads, chunked for Discord
_da = "\n".join(server.build_draw_announcement())
_need_da = ["The WC26 draw is in", "Round 1", "Round 9", "Final squads"] + PLAYERS
_miss_da = [w for w in _need_da if w not in _da]
assert not _miss_da, "draw announcement missing: %s" % _miss_da
print("[draw-announce] round-by-round + final squads built OK")


def owned_first():
    return next(iter(assign.values()))[0]["name"]


CMDS = [("help", {}), ("summary", {}), ("leaderboard", {}), ("odds", {}), ("stats", {}),
        ("fixtures", {}), ("groups", {}), ("players", {}), ("myteams", {"player": "Erol"}),
        ("myteams", {"player": "nobody"}), ("team", {"name": owned_first()}),
        ("team", {"name": "Atlantis"}), ("team", {}), ("bogus", {})]
fails = []


def run_all(tag):
    scoring.compute(out="tracker_data.json", default_mode="points")
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

# /myteams must actually LIST a player's teams — regression guard for the handler that was unreachable
# (registered command, but its body was stranded after the /scores return, so it returned "Unknown command").
_mt = server.discord_command("myteams", {"player": PLAYERS[0]})
if "teams" not in _mt.lower() or "Unknown command" in _mt or PLAYERS[0] not in _mt:
    fails.append(("myteams", "did not list the player's teams: %r" % _mt[:80]))
else:
    print("[mid-tournament] /myteams lists the player's teams OK")
if "No player" not in server.discord_command("myteams", {"player": "definitely-nobody"}):
    fails.append(("myteams", "unknown-player path not handled"))
else:
    print("[mid-tournament] /myteams handles an unknown player OK")

# welcome DM: a brand-new Discord user is greeted once (short list), repeat users are not re-greeted
_wd = []
_orig_botdm = server._bot_dm
server._bot_dm = lambda uid, text: (_wd.append((uid, text)) or (True, None))
try:
    if os.path.exists(server.WELCOMED_FILE):
        os.remove(server.WELCOMED_FILE)
except Exception:
    pass
server.discord_command("leaderboard", {}, uid="newcomer1")
server.discord_command("fixtures", {}, uid="newcomer1")      # same user again -> no second welcome
server.discord_command("help", {}, uid="newcomer2")          # different user -> welcomed
server._bot_dm = _orig_botdm
_greets = [d for d in _wd if "Welcome" in d[1]]
if len(_greets) != 2:
    fails.append(("welcome", "expected 2 greetings (one per new user), got %d" % len(_greets)))
elif not ("/leaderboard" in _greets[0][1] and "/help" in _greets[0][1] and _greets[0][1].count("`/") <= 7):
    fails.append(("welcome", "welcome text wrong or too long: %r" % _greets[0][1][:80]))
else:
    print("[mid-tournament] first-interaction welcome DM fires once per user (short list) OK")

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
           "leaderboards": {"points": [{"name": "Zed", "score": 42, "alive_teams": 1, "total_teams": 9}]},
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
_lb = {"points": [{"name": "Erol", "score": 120, "alive_teams": 1, "total_teams": 9}], "survival": []}
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

# head-to-head rivalry: overtaking another player (position 2+) now DMs that player (no channel spam)
_sent.clear()
_dms = []
_orig_dm = server._bot_dm_player
server._bot_dm_player = lambda player, text, match_id=None: (_dms.append((player, text)) or 1)
_lo = {"points": [{"name": "A", "score": 50, "alive_teams": 3, "total_teams": 9},
                  {"name": "B", "score": 40, "alive_teams": 3, "total_teams": 9},
                  {"name": "C", "score": 30, "alive_teams": 3, "total_teams": 9}], "survival": []}
_ln = {"points": [{"name": "A", "score": 50, "alive_teams": 3, "total_teams": 9},
                  {"name": "C", "score": 45, "alive_teams": 3, "total_teams": 9},
                  {"name": "B", "score": 42, "alive_teams": 3, "total_teams": 9}], "survival": []}
_oldr = {"stats": {"matches_played": 9}, "fixtures": [], "leaderboards": _lo, "players": []}
_newr = {"stats": {"matches_played": 10}, "fixtures": [], "leaderboards": _ln, "players": []}   # a result just settled (9->10) and reordered the board -> overtake fires (live churn alone would NOT)
json.dump(_newr, open(os.path.join(D, "tracker_data.json"), "w"))
server.notify_changes(_oldr)
server._bot_dm_player = _orig_dm
_rivup = [d for d in _dms if "overtook" in d[1]]      # the overtaker's DM
_rivdn = [d for d in _dms if "passed you" in d[1]]    # the overtaken player's DM
if not _rivup:
    fails.append(("rivalry", "no overtake DM fired to the overtaker"))
elif not (_rivup[0][0] == "C" and "B" in _rivup[0][1] and "2nd" in _rivup[0][1]):
    fails.append(("rivalry", "overtaker DM wrong: %r" % (_rivup[0],)))
elif not (_rivdn and _rivdn[0][0] == "B"):
    fails.append(("rivalry", "the overtaken player (B) was not DM'd"))
elif not any("overtakes" in s for s in _sent):
    fails.append(("rivalry", "overtake should ALSO post to the channel (head-to-head moment)"))
else:
    print("[rivalry] overtake DMs both players AND posts to the channel (C overtook B for 2nd) OK")

# 'your day' digest: today's fixtures grouped per player
_today = time.strftime("%Y-%m-%dT15:00:00Z", time.gmtime())
_dayd = {"fixtures": [{"utcDate": _today, "status": "TIMED", "stage": "GROUP_STAGE",
                       "home": "Brazil", "away": "Spain", "homeOwner": "Erol", "awayOwner": "James"},
                      {"utcDate": "2026-01-01T15:00:00Z", "status": "TIMED", "stage": "GROUP_STAGE",
                       "home": "Japan", "away": "Ghana", "homeOwner": "Erol", "awayOwner": "James"}]}
_by = server._day_by_player(_dayd)
_lines = server.build_day_lines(_dayd)
if not (_by.get("Erol") == ["Brazil vs Spain (15:00)"] and _by.get("James") == ["Spain vs Brazil (15:00)"]):
    fails.append(("day-digest", "today grouping wrong: %r" % _by))
elif not any("Today's games" in l for l in _lines):
    fails.append(("day-digest", "no today section: %r" % _lines))
else:
    print("[day-digest] today's games grouped per player (excludes other days) OK")

# end-of-tournament wrap-up: champion + podium + final table once the final is done
_wd = {"stats": {"matches_played": 104, "goals": 250, "goals_per_match": 2.4, "top_team": "Brazil",
                 "top_team_goals": 16, "teams_remaining": 1},
       "leaderboards": {"points": [{"name": "James", "score": 140}, {"name": "Erol", "score": 130}],
                        "survival": [{"name": "Louis", "score": 210}, {"name": "Erol", "score": 180}],
                        },
       "champion_decided": {"team": "Brazil", "owner": "Erol", "runnerUp": "Spain"},
       "fixtures": [{"stage": "THIRD_PLACE", "status": "FINISHED", "home": "France", "away": "Germany",
                     "homeScore": 2, "awayScore": 1, "winner": "HOME"}],
       "players": [{"name": "Erol", "teams": [{"name": "Brazil"}]}]}
json.dump(_wd, open(os.path.join(D, "tracker_data.json"), "w"))
_wt = "\n".join(server.build_wrapup())
_need = ["Brazil", "Spain", "France", "Final table", "Golden-boot",
         "Points winner", "Survival winner", "different winners"]
_miss = [w for w in _need if w not in _wt]
if _miss:
    fails.append(("wrapup", "missing from recap: %s" % _miss))
else:
    print("[wrapup] recap has champion + podium + two separate mode-winners + golden boot OK")

shutil.rmtree(D, ignore_errors=True)

# personal Discord pings: /notifyme stores a sub, /stopnotify clears it, and discord_mention
# only fires for subscribed players (and pings with allowed_mentions). Delivery is stubbed.
D2 = tempfile.mkdtemp(prefix="wc26notify_"); os.chdir(D2)
server.CONFIG = os.path.join(D2, "config.json")     # the import-time CONFIG pointed at the now-deleted dir
json.dump({"teams": list(TEAMS.values())}, open("teams.json", "w"))
_names3 = [t["name"] for t in list(TEAMS.values())[:3]]
def _brief3(nm):
    t = TEAMS[nm] if nm in TEAMS else next(x for x in TEAMS.values() if x["name"] == nm)
    return {"name": nm, "tier": t.get("tier", 1), "group": t.get("group", "A"), "composite": t.get("composite", 50), "confederation": "?"}
json.dump({"players": [{"name": "Erol", "teams": [_brief3(_names3[0]), _brief3(_names3[1])]},
                       {"name": "James", "teams": [_brief3(_names3[2])]}],
           "bonus_pool": []}, open("draw_result.json", "w"))
json.dump({"matches": []}, open("results.json", "w"))
scoring.compute()
server.save_config({"discord_webhook": "https://discord.com/api/webhooks/x/y"})

r1 = server.discord_command("notifyme", {"player": "erol"}, uid="123")
if "123" not in server.load_config().get("discord_subs", {}) or server.load_config()["discord_subs"]["123"] != "Erol":
    fails.append(("notifyme", "did not store sub: %r" % r1))
elif "No player" not in server.discord_command("notifyme", {"player": "Nobody"}, uid="9"):
    fails.append(("notifyme", "accepted an unknown player"))
else:
    print("[notify] /notifyme stores a subscription (and rejects unknown players) OK")

# capture mention payloads
_pings = []
_orig_url = server.urllib.request.urlopen
class _FakeReq:
    pass
def _cap(req, timeout=8):
    try:
        _pings.append(json.loads(req.data.decode()))
    except Exception:
        pass
    class _R:
        def read(self_inner): return b"{}"
        def __enter__(self_inner): return self_inner
        def __exit__(self_inner, *a): return False
    return _R()
server.urllib.request.urlopen = _cap
server.discord_mention("Erol", "⚽ Brazil scored")     # subscribed -> should ping
server.discord_mention("James", "⚽ Spain scored")     # not subscribed -> no ping
server.urllib.request.urlopen = _orig_url
if len(_pings) != 1:
    fails.append(("mention", "expected exactly 1 ping, got %d" % len(_pings)))
elif "<@123>" not in _pings[0].get("content", "") or _pings[0].get("allowed_mentions", {}).get("users") != ["123"]:
    fails.append(("mention", "ping did not target the subscribed user: %r" % _pings[0]))
else:
    print("[notify] discord_mention pings only the subscribed player with allowed_mentions OK")

r2 = server.discord_command("stopnotify", {}, uid="123")
if "123" in server.load_config().get("discord_subs", {}):
    fails.append(("stopnotify", "did not clear sub: %r" % r2))
else:
    print("[notify] /stopnotify clears the subscription OK")

# ---- Discord betting (/games, /bet preview + confirm, /mybets) ----
_t0, _t1, _t2 = _names3[0], _names3[1], _names3[2]   # Erol owns _t0,_t1 ; James owns _t2
json.dump({"matches": [
    {"id": 1, "stage": "GROUP_STAGE", "group": "A", "utcDate": "2026-06-11T18:00:00Z", "status": "FINISHED",
     "home": _t0, "away": _t2, "homeScore": 3, "awayScore": 0, "winner": "HOME", "minute": None,
     "duration": "REGULAR", "aet": False, "shootout": False, "penHome": None, "penAway": None},
    {"id": 2, "stage": "GROUP_STAGE", "group": "A", "utcDate": "2099-06-15T18:00:00Z", "status": "TIMED",
     "home": _t1, "away": _t2, "homeScore": None, "awayScore": None, "winner": None, "minute": None,
     "duration": "REGULAR", "aet": False, "shootout": False, "penHome": None, "penAway": None}]},
    open("results.json", "w"))
_c = server.load_config(); _c["wagering_enabled"] = True; _c["discord_subs"] = {"123": "Erol"}
_c["wager_pins"] = {"Erol": "ABCDE", "James": "ZZZZZ"}; _c.pop("wager_links", None); _c.pop("wager_link_codes", None)
server.save_config(_c)
scoring.compute(out="tracker_data.json", wagers=[])
_g = server.discord_command("games", {}, uid="123")
if _t1 not in _g or "/bet" not in _g:
    fails.append(("/games", "didn't list the upcoming game + odds: %r" % _g))
# unlinked Discord user can't bet, and is told to link (passcode is NEVER asked for in-channel)
_unl = server.discord_command("bet", {"match": _t1, "team": _t1, "result": "win", "stake": 5}, uid="123")
if "linked" not in _unl.lower() or "linkdiscord" not in _unl.lower():
    fails.append(("/bet", "unlinked bet didn't point to the link flow: %r" % _unl))
# a wrong link code is rejected
if "expired" not in server.discord_command("linkdiscord", {"code": "NOPE12"}, uid="123").lower():
    fails.append(("/linkdiscord", "accepted a bad code"))
# the website issues a code only for the correct passcode (simulate the endpoint's effect)
_cc = server.load_config(); _cc["wager_link_codes"] = {"GOOD12": {"player": "Erol", "exp": time.time() + 900}}
server.save_config(_cc)
_lk = server.discord_command("linkdiscord", {"code": "GOOD12"}, uid="123")
if "linked" not in _lk.lower() or server.load_config().get("wager_links", {}).get("123") != "Erol":
    fails.append(("/linkdiscord", "did not link with a valid code: %r" % _lk))
if server.load_config().get("wager_link_codes", {}).get("GOOD12"):
    fails.append(("/linkdiscord", "code was not single-use"))
# linked -> preview shows payout, no passcode ever typed in Discord
_prev = server.discord_command("bet", {"match": _t1, "team": _t1, "result": "win", "stake": 5}, uid="123")
if "preview" not in _prev.lower() or "returns" not in _prev.lower():
    fails.append(("/bet preview", "no payout preview once linked: %r" % _prev))
# new /bet shape: typing a team that ISN'T in the chosen match is rejected (no bet placed)
_badteam = server.discord_command("bet", {"match": _t1, "team": "Narnia", "result": "win", "stake": 5}, uid="123")
if "isn't in that match" not in _badteam.lower() and "isn’t in that match" not in _badteam.lower():
    fails.append(("/bet validate", "a team not in the match wasn't rejected: %r" % _badteam))
# 'lose' backs the OPPONENT to win — preview should name the typed team losing
_lose = server.discord_command("bet", {"match": _t1, "team": _t1, "result": "lose", "stake": 3}, uid="123")
if "preview" not in _lose.lower() or "to lose" not in _lose.lower():
    fails.append(("/bet lose", "lose-result preview wrong: %r" % _lose))
# 'draw' in a group game is allowed and previews a draw
_draw = server.discord_command("bet", {"match": _t1, "team": _t1, "result": "draw", "stake": 3}, uid="123")
if "preview" not in _draw.lower() or "draw" not in _draw.lower():
    fails.append(("/bet draw", "draw-result preview wrong: %r" % _draw))
# the match autocomplete returns the upcoming game (and the typed team can be matched in it)
_mc = server._bet_match_choices(_t1)
if not _mc or not any(_t1.lower() in (c.get("name", "").lower()) for c in _mc):
    fails.append(("/bet autocomplete", "match choices didn't include the upcoming game: %r" % _mc))
# none of those previews placed anything yet
if len(server.load_wagers()) != 0:
    fails.append(("/bet", "a preview accidentally placed a bet: %d wagers" % len(server.load_wagers())))
# a 2-decimal stake works through Discord too (shared engine) — preview shows it, doesn't reject
_dec = server.discord_command("bet", {"match": _t1, "team": _t1, "result": "win", "stake": 1.23}, uid="123")
if "preview" not in _dec.lower() or "1.23" not in _dec:
    fails.append(("/bet decimals", "a 2-decimal stake wasn't accepted on Discord: %r" % _dec))
# typing letters instead of a number is rejected cleanly on Discord (no crash, no bet)
_lett = server.discord_command("bet", {"match": _t1, "team": _t1, "result": "win", "stake": "abc"}, uid="123")
if "number" not in _lett.lower() or len(server.load_wagers()) != 0:
    fails.append(("/bet non-numeric", "letters as a stake weren't rejected on Discord: %r" % _lett))
# now actually place (this recomputes the tracker)
# ---- Over/Under via Discord /bet (previews + validation; run BEFORE any confirm, which recomputes/clears the test fixtures) ----
_oup = server.discord_command("bet", {"match": _t1, "result": "over", "goals": 2.5, "stake": 3}, uid="123")
if "preview" not in _oup.lower() or "Over 2.5 goals" not in _oup:
    fails.append(("/bet over preview", "O/U preview wrong: %r" % _oup))
_oubad = server.discord_command("bet", {"match": _t1, "result": "over", "goals": 3.0, "stake": 3}, uid="123")
if "isn't offered" not in _oubad and "0.5 to 8.5" not in _oubad:
    fails.append(("/bet over bad-line", "off-grid line not rejected: %r" % _oubad))
_oumiss = server.discord_command("bet", {"match": _t1, "result": "over", "stake": 3}, uid="123")
if "goals" not in _oumiss.lower():
    fails.append(("/bet over no-line", "missing goals line not prompted: %r" % _oumiss))
_nowin = server.discord_command("bet", {"match": _t1, "result": "win", "stake": 3}, uid="123")   # win needs a team now
if "team" not in _nowin.lower():
    fails.append(("/bet win no-team", "win without a team not prompted: %r" % _nowin))
# webhook announcements must label O/U correctly (not 'a draw')
_sent.clear()
server._announce_bet("Erol", {"player": "Erol", "selection": "OVER", "market": "ou", "line": 2.5,
                              "home": "Brazil", "away": "Serbia", "stake": 5, "frac": "5/6", "return": 9.15})
if not any("Over 2.5 goals" in s for s in _sent):
    fails.append(("ou-announce", "O/U single not labelled 'Over 2.5 goals': %r" % _sent))
_sent.clear()
server._announce_bet("Erol", {"player": "Erol", "stake": 4, "return": 20, "legs": [
    {"selection": "OVER", "market": "ou", "line": 1.5, "home": "Brazil", "away": "Serbia", "frac": "1/4"},
    {"selection": "HOME", "home": "Spain", "away": "Japan", "frac": "4/9"}]})
if not any("Over 1.5 goals" in s and "Spain" in s for s in _sent):
    fails.append(("ou-announce-acca", "O/U acca leg mislabelled: %r" % _sent))
if server._wager_desc({"selection": "UNDER", "market": "ou", "line": 3.5, "home": "A", "away": "B"}) != "Under 3.5 goals":
    fails.append(("ou-wager-desc", "O/U wager_desc wrong"))
if server._wager_desc({"selection": "HOME", "home": "Brazil", "away": "Serbia"}) != "Brazil":
    fails.append(("result-wager-desc", "1X2 desc regressed"))
if not [f for f in fails if "/bet over" in f[0] or "/bet win no-team" in f[0] or "ou-" in f[0]]:
    print("[wager] Over/Under Discord betting + announcements OK")

_place = server.discord_command("bet", {"match": _t1, "team": _t1, "result": "win", "stake": 5, "confirm": True}, uid="123")
_wl = server.load_wagers()
if "placed" not in _place.lower() or len(_wl) != 1 or _wl[0]["player"] != "Erol":
    fails.append(("/bet confirm", "did not place once linked: %r / %r" % (_place, _wl)))
# a DIFFERENT, unlinked Discord account cannot bet as anyone
_other = server.discord_command("bet", {"match": _t1, "team": _t1, "result": "win", "stake": 5}, uid="999")
if "link" not in _other.lower() or len(server.load_wagers()) != 1:
    fails.append(("/bet", "an unlinked account could act: %r" % _other))
_mb = server.discord_command("mybets", {}, uid="123")
if _t1 not in _mb:
    fails.append(("/mybets", "didn't show the placed bet: %r" % _mb))
# /points shows the linked player's available points + their max bet
_pts = server.discord_command("points", {}, uid="123")
if "Erol" not in _pts or "available" not in _pts.lower():
    fails.append(("/points", "didn't report available points: %r" % _pts))
if "link" not in server.discord_command("points", {}, uid="999").lower():
    fails.append(("/points", "unlinked user got points"))
# /allbets lists everyone's open bets (the one Erol placed)
_ab = server.discord_command("allbets", {}, uid="123")
if "Erol" not in _ab or _t1 not in _ab:
    fails.append(("/allbets", "didn't list the open bet: %r" % _ab))
# /claim — today's free betting points. Unlinked can't claim; with a drop open it grants 5 points (a credit) once per drop.
if "link" not in server.discord_command("claim", {}, uid="999").lower():
    fails.append(("/claim", "an unlinked account could claim free points"))
_orig_drop = server._open_free_drop
server._open_free_drop = lambda now=None: {"id": "test-drop", "opens": 0, "closes": 9e18}
try:
    _free_before = sum(w.get("amount", 0) for w in server.load_wagers() if w.get("credit") and w["player"] == "Erol")
    _cc = server.discord_command("claim", {}, uid="123")
    _credits = [w for w in server.load_wagers() if w.get("credit") and w["player"] == "Erol"]
    if "free betting points added" not in _cc.lower() or len(_credits) != 1 or _credits[0]["amount"] != 5:
        fails.append(("/claim", "free points not granted: %r / credits=%r" % (_cc, _credits)))
    # the credit lifts available-to-stake (free_bonus grew by 5)
    if server.wager_mod.free_bonus("Erol", server.load_wagers()) != server.wager_mod.STARTING_BONUS + 5:
        fails.append(("/claim", "free_bonus didn't grow by 5 after claim"))
    _again = server.discord_command("claim", {}, uid="123")
    if "already claimed" not in _again.lower() or len([w for w in server.load_wagers() if w.get("credit") and w["player"] == "Erol"]) != 1:
        fails.append(("/claim twice", "a second claim on the same drop wasn't blocked: %r" % _again))
finally:
    server._open_free_drop = _orig_drop
# /scores works even with no results (recent finished game from the fixtures)
_sc = server.discord_command("scores", {}, uid="123")
if not isinstance(_sc, str) or not _sc:
    fails.append(("/scores", "no output: %r" % _sc))
# /mypin: a linked account can recover its own passcode
_mp = server.discord_command("mypin", {}, uid="123")
if "passcode" not in _mp.lower():
    fails.append(("/mypin", "linked account didn't get its passcode: %r" % _mp))
if "link" not in server.discord_command("mypin", {}, uid="999").lower():
    fails.append(("/mypin", "unlinked account was given a passcode"))
# /resetpin: a linked account can reset ITS OWN passcode (others untouched); an unlinked account cannot
_erol_before = server.load_config().get("wager_pins", {}).get("Erol")
_james_before = server.load_config().get("wager_pins", {}).get("James")
_rp = server.discord_command("resetpin", {}, uid="123")
_pins_after = server.load_config().get("wager_pins", {})
if _pins_after.get("Erol") == _erol_before or _pins_after.get("Erol") is None:
    fails.append(("/resetpin", "linked self-reset didn't change the passcode: %r" % _rp))
if _pins_after.get("James") != _james_before:
    fails.append(("/resetpin", "self-reset wrongly changed another player's passcode"))
if "link" not in server.discord_command("resetpin", {}, uid="999").lower():
    fails.append(("/resetpin", "an unlinked account could reset a passcode"))
# /unlink removes the betting link; afterwards /bet is blocked again
_un = server.discord_command("unlink", {}, uid="123")
if "unlink" not in _un.lower() or server.load_config().get("wager_links", {}).get("123"):
    fails.append(("/unlink", "did not remove the link: %r" % _un))
if "link" not in server.discord_command("bet", {"match": _t1, "team": _t1, "result": "win", "stake": 5}, uid="123").lower():
    fails.append(("/unlink", "could still bet after unlinking"))
if not [f for f in fails if "/bet" in f[0] or "/games" in f[0] or "/mybets" in f[0] or "/linkdiscord" in f[0]
        or "/points" in f[0] or "/allbets" in f[0] or "/scores" in f[0] or "/unlink" in f[0]]:
    print("[wager] Discord betting + /scores /points /allbets /unlink OK")

# ===== slash-command schema is valid (required options must precede optional — Discord rejects otherwise) =====
import urllib.request as _ur  # noqa: E402
_orig_urlopen = _ur.urlopen
_ur.urlopen = lambda *a, **k: type("R", (), {"read": lambda s: b"{}", "__enter__": lambda s: s, "__exit__": lambda s, *e: False})()
_cfgs = server.load_config(); _cfgs["discord_app_id"] = "111"; _cfgs["discord_bot_token"] = "tok"; _cfgs.pop("discord_guild_id", None); server.save_config(_cfgs)
_rok, _rerr = server.register_discord_commands()
_ur.urlopen = _orig_urlopen
if _rerr and "required option" in _rerr:
    fails.append(("slash-schema", _rerr))
elif not _rok:
    fails.append(("slash-schema", "register failed for another reason: %r" % _rerr))
else:
    print("[schema] every slash command lists required options before optional ones OK")

# ===== notifications: default-on for connected accounts, opt-out, per-game mute, bet reminders =====
_dms = []
_orig_botdm = server._bot_dm
server._bot_dm = lambda uid, text: (_dms.append((str(uid), text)) or (True, None))
_cfgn = server.load_config()
_cfgn["wager_links"] = {"900": "Erol"}; _cfgn["discord_subs"] = {"901": "James"}
_cfgn.pop("discord_dm_off", None); _cfgn.pop("discord_mutes", None); server.save_config(_cfgn)
# default-on: a betting-linked account (never ran /notifyme) is still a DM recipient
if set(server._uids_for_player("Erol")) != {"900"}:
    fails.append(("uids", "Erol's known accounts wrong: %r" % server._uids_for_player("Erol")))
_dms.clear(); server._bot_dm_player("Erol", "ping")
if ("900", "ping") not in _dms:
    fails.append(("default-dm", "betting-linked user not DM'd by default: %r" % _dms))
# opt-out (/stopnotify) silences default-on DMs
_so = server.discord_command("stopnotify", {}, uid="900")
_dms.clear(); server._bot_dm_player("Erol", "ping")
if _dms or "off" not in _so.lower():
    fails.append(("optout", "opted-out user still DM'd: %r / %r" % (_dms, _so)))
# /notifyme re-enables
server.discord_command("notifyme", {"player": "Erol"}, uid="900")
_dms.clear(); server._bot_dm_player("Erol", "ping")
if ("900", "ping") not in _dms:
    fails.append(("reenable", "/notifyme didn't re-enable DMs: %r" % _dms))
# per-game mute: the muted game is skipped; other games still DM
_cfgn = server.load_config(); _cfgn["discord_mutes"] = {"900": ["MID1"]}; server.save_config(_cfgn)
_dms.clear(); server._bot_dm_player("Erol", "ping", match_id="MID1")
if _dms:
    fails.append(("mute", "muted game still DM'd: %r" % _dms))
_dms.clear(); server._bot_dm_player("Erol", "ping", match_id="MID2")
if ("900", "ping") not in _dms:
    fails.append(("mute-other", "a non-muted game was not DM'd: %r" % _dms))
# the /mute + /unmute commands resolve a game and store/remove it
_cfgn = server.load_config(); _cfgn.pop("discord_mutes", None); server.save_config(_cfgn)
_td = json.load(open("tracker_data.json"))
_td.setdefault("fixtures", []).append({"matchId": "MUTEME", "home": "Brazil", "away": "Serbia", "status": "TIMED", "odds": {"HOME": {"frac": "1/2"}}})
json.dump(_td, open("tracker_data.json", "w"))
_mz = server.discord_command("mute", {"match": "Brazil"}, uid="900")
if "Muted" not in _mz or "MUTEME" not in (server.load_config().get("discord_mutes", {}).get("900") or []):
    fails.append(("/mute", "mute command didn't store: %r" % _mz))
_uz = server.discord_command("unmute", {"match": "Brazil"}, uid="900")
if "Unmuted" not in _uz or "MUTEME" in (server.load_config().get("discord_mutes", {}).get("900") or []):
    fails.append(("/unmute", "unmute command didn't remove: %r" % _uz))
# bettors-on-match: a player with an open bet on a game (even with no team in it) is found for reminders
server.save_wagers([{"player": "Louis", "matchId": "BMID", "status": "pending", "selection": "HOME", "stake": 2, "frac": "1/2", "return": 3}])
if "Louis" not in server._bettors_on_match("BMID") or server._bettors_on_match("NOPE"):
    fails.append(("bettors", "bettors-on-match wrong: %r" % server._bettors_on_match("BMID")))
if not [f for f in fails if f[0] in ("uids", "default-dm", "optout", "reenable", "mute", "mute-other", "/mute", "/unmute", "bettors")]:
    print("[notify] default-on DMs + opt-out + per-game mute + bet reminders OK")

# ===== overtake + leader-change post to the channel (the head-to-head 'global' messages) =====
_sent.clear()
json.dump({"leaderboards": {"points": [{"name": "Erol"}, {"name": "Louis"}, {"name": "James"}]},
           "stats": {"matches_played": 6}, "fixtures": []}, open("tracker_data.json", "w"))
server.notify_changes({"leaderboards": {"points": [{"name": "Erol"}, {"name": "James"}, {"name": "Louis"}]},
                       "stats": {"matches_played": 5}, "fixtures": []})
if not any("overtakes" in s and "Louis" in s and "James" in s for s in _sent):
    fails.append(("rivalry-channel", "overtake channel message missing: %r" % _sent))
_sent.clear()
json.dump({"leaderboards": {"points": [{"name": "James"}, {"name": "Erol"}]},
           "stats": {"matches_played": 7}, "fixtures": []}, open("tracker_data.json", "w"))
server.notify_changes({"leaderboards": {"points": [{"name": "Erol"}, {"name": "James"}]},
                       "stats": {"matches_played": 6}, "fixtures": []})
if not any("New leader" in s and "James" in s for s in _sent):
    fails.append(("leader-channel", "leader-change channel message missing: %r" % _sent))
if not [f for f in fails if f[0] in ("rivalry-channel", "leader-channel")]:
    print("[notify] overtake + leader-change channel messages OK")
server._bot_dm = _orig_botdm

shutil.rmtree(D2, ignore_errors=True)

if fails:
    print("\nFAILED:", fails)
    raise SystemExit(1)
print("\nAll bot tests passed.")
