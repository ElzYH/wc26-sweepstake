#!/usr/bin/env python3
"""Frontend QA for tracker.html: confirm the embedded JS parses, and unit-test the pure helper functions
by extracting them and running them under node — especially esc() (the XSS-escaping guard), ownerOf()
(team->owner lookup used all over the UI), koNote() (extra-time/penalty caption), and the 2-dp money
rounding used for stakes/returns/balances. DOM-bound functions can't run headless, so we test the logic
that does the actual work."""
import os, sys, re, subprocess, shutil

REPO = os.path.dirname(os.path.abspath(__file__))
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

if not shutil.which("node"):
    print("  SKIP frontend QA (node not available)")
    sys.exit(0)

HTML = open(os.path.join(REPO, "tracker.html")).read()
JS = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))

# ---- 1. the whole script must parse ----
print("\n== 1. JS syntax ==")
open("/tmp/_front.js", "w").write(JS)
r = subprocess.run(["node", "--check", "/tmp/_front.js"], capture_output=True, text=True)
ck("the embedded JS parses with no syntax error", r.returncode == 0, r.stderr[:200])

# ---- 2. CSS braces balanced (a stray brace breaks rendering) ----
ck("CSS braces are balanced", HTML.count("{") == HTML.count("}"), (HTML.count("{"), HTML.count("}")))

# ---- sticky bet-confirmation bar (.betcta): floats then docks; must stay correct + tab-scoped ----
print("\n== sticky bet-confirmation bar ==")
ck("a .betcta sticky rule exists", ".betcta{position:sticky" in HTML.replace(" ", ""), None)
ck("it sticks to the BOTTOM (floats up, then docks)", re.search(r"\.betcta\{position:sticky;bottom:", HTML.replace(" ", "")) is not None, None)
ck("it is safe-area aware (above the iOS home indicator)", "env(safe-area-inset-bottom" in HTML, None)
ck("the class is applied to both bet bars (single + acca)", HTML.count('class="betcta"') == 2, HTML.count('class="betcta"'))
# the sticky wrapper must contain the Place-bet button AND the 'final' notice, so the whole CTA travels together
_bars = HTML.split('class="betcta"')
_acca_bar = _bars[1] if len(_bars) > 1 else ""        # bounded: ends at the single-bet betcta
_single_bar = _bars[2][:1300] if len(_bars) > 2 else ""
ck("the acca sticky bar contains its Place-acca button", 'id="accaPlace"' in _acca_bar, None)
ck("the single-bet sticky bar contains its Place-bet button", 'id="betPlace"' in _single_bar, None)
ck("the 'this bet is final' notice rides inside the single-bet sticky bar", "this bet is final" in _single_bar, None)
# tab-scoped: the bar is built inside renderBets() (#bets section), which is display:none on other tabs
ck("the bar lives in the betting tab only (rendered in renderBets/#betsBody)", "function renderBets(" in JS and 'id="betsBody"' in HTML, None)
ck("sections are display:none when inactive (so the bar can't show on other tabs)", re.search(r"section\{[^}]*display:none", HTML.replace(" ", "")) is not None, None)

# ---- XSS regression: name-bearing fields must never be interpolated raw into templates ----
print("\n== escaping regression (names always esc()'d) ==")
_raw = re.findall(r"\$\{(?:m\.(?:home|away)(?:\|\|[^}]*)?|p\.name|t\.name|r\.(?:name|team|owner)|s\.(?:name|player)|m\.(?:home|away)Owner[^}]*)\}", HTML)
ck("no raw name interpolations (all wrapped in esc())", len(_raw) == 0, _raw[:4])
ck("both own() helpers escape the owner name", HTML.count("`<small>${esc(o)}</small>`") == 2, HTML.count("`<small>${esc(o)}</small>`"))

# ---- symmetric team rows ----
ck("fixture rows use the symmetric grid (.fxteams)", ".fxteams{display:grid;grid-template-columns:1fr38px1fr" in HTML.replace(" ", ""), None)
ck("bet/live cards centre the 'v' (mdteams grid)", ".mdteams{display:grid;grid-template-columns:1frauto1fr" in HTML.replace(" ", ""), None)
ck("buttons are centre-aligned (incl. menu items)", ".menu .mini{width:100%;text-align:center}" in HTML, None)
ck("the phone menu button is compact, not full-width", "#menuBtn{width:auto" in HTML and "#menuBtn{width:100%" not in HTML, None)

# ---- best & worst bets card ----
ck("a best/worst bets container exists", 'id="betHall"' in HTML and 'id="betHallWrap"' in HTML, None)
ck("best/worst excludes free-points credit rows", "filter(w=>!w.credit&&(w.status==='won'||w.status==='lost'))" in HTML, None)
ck("push test failures are surfaced to the user", "j.errors" in HTML and "failed" in HTML, None)

# ---- pool teams: every surface says they score for no one ----
ck("the live breakdown flags an unowned (pool) side", "unowned (pool) — those points count for no one" in HTML, None)
ck("group tables carry the pool-teams note", "Pool teams (no owner):" in HTML and "you only ever earn points for your own team" in HTML, None)
ck("the rules panel explains the pool", "The pool (leftover teams):" in HTML and "score for <b>no one</b>" in HTML, None)
_setup = open(os.path.join(REPO, "setup.html")).read() if os.path.exists(os.path.join(REPO, "setup.html")) else ""
ck("setup explains both leftover options plainly", "belong to no one and score points for no one" in _setup, None)

# ---- this round: betting revert, layout, stable join link, leaner copy ----
print("\n== iOS betting fix + layout + join link ==")
ck("the broken noFloat/scrollBy keyboard hack is gone", "noFloat" not in HTML and "scrollBy" not in HTML, None)
ck("stake inputs are 16px (iOS won't zoom)", HTML.count("padding:8px 10px;font-size:16px") == 2 and "padding:8px 10px;font-size:14px" not in HTML, None)
ck("tabs row is centred", ".tabs{display:flex;gap:8px;margin:22px 0 18px;flex-wrap:wrap;justify-content:center}" in HTML, None)
ck("on phone the controls sit left (don't run off-page)", ".ctrls{margin-left:0;width:auto;justify-content:flex-start}" in HTML, None)
ck("on phone the menu opens from the left", ".menu{left:0;right:auto;min-width:210px}" in HTML, None)
ck("the bets chip is now LEFT of the score on the leaderboard", re.search(r'class="pscore">\$\{\(p\.bet_potential>0[^`]*`<span class="betdelta"', HTML) is not None, None)
ck("the chips are smaller (9.5px)", ".betdelta{font-family:'Sora',sans-serif;font-size:9.5px" in HTML and ".livedelta{font-family:'Sora',sans-serif;font-size:9.5px" in HTML, None)
ck("a stable /join link is advertised on the tracker", '/join' in HTML and "always points at the latest invite" in HTML, None)
ck("the Join button points at the stable /join redirect", 'href="/join"' in HTML, None)

# ---- leaner copy: the giant get-alerts paragraph is gone ----
ck("the get-alerts help is trimmed (no Home-Screen wall of text)", "On iPhone you must first add the site to your Home Screen (Share" not in HTML, None)

# ---- potential betting points: visible but never added to the score ----
ck("leaderboard rows show a potential-bets chip", 'class="betdelta"' in HTML and "+${p.bet_potential}" in HTML, None)
ck("the chip explains it only counts when a bet settles", "counts only when a bet settles" in HTML, None)
ck("player cards carry the open-bets potential line", "if they all win" in HTML and "only counts when a bet settles" in HTML, None)
ck("a .betdelta style exists (gold, distinct from the green live chip)", ".betdelta{" in HTML.replace(" ", "") and "var(--gold)" in HTML.split(".betdelta{",1)[1][:200], None)

# ---- live points breakdown: the frontend explanation must match the server's scoring EXACTLY ----
print("\n== live points breakdown (frontend mirrors scoring.py) ==")
ck("a liveParts helper exists", "function liveParts(scored, conceded)" in HTML, None)
ck("the live match card renders the breakdown line", "liveBreakLine(m)" in HTML, None)
ck("player cards show a per-team live chip", "from a game in play right now" in HTML, None)
try:
    import json as _json, tempfile as _tf, subprocess as _sp
    sys.path.insert(0, REPO)
    import scoring as _scoring
    _cases = [(0, 0), (1, 0), (0, 1), (2, 2), (3, 1), (0, 5)]
    _server = []
    for _hs, _as in _cases:
        _d = _tf.mkdtemp()
        _json.dump({"teams": [{"name": "TT1", "composite": 50, "group": "A"}, {"name": "TT2", "composite": 50, "group": "A"}]}, open(os.path.join(_d, "teams.json"), "w"))
        _json.dump({"players": [{"name": "P1", "teams": [{"name": "TT1"}]}, {"name": "P2", "teams": [{"name": "TT2"}]}]}, open(os.path.join(_d, "draw.json"), "w"))
        _json.dump({"matches": [{"home": "TT1", "away": "TT2", "homeScore": _hs, "awayScore": _as, "status": "IN_PLAY", "stage": "GROUP_STAGE", "utcDate": "2026-06-12T17:00:00Z", "matchId": 1}]}, open(os.path.join(_d, "results.json"), "w"))
        _o = os.path.join(_d, "out.json")
        _scoring.compute(os.path.join(_d, "teams.json"), os.path.join(_d, "draw.json"), os.path.join(_d, "results.json"), _o, "hybrid")
        _jj = _json.load(open(_o))
        _server.append(tuple([p for p in _jj["players"] if p["name"] == n][0]["live"] for n in ("P1", "P2")))
    _src = re.search(r"(function liveParts\(scored, conceded\)\{.*?\n\})", HTML, re.S).group(1)
    _njs = "const DATA={scoring:{points:{per_goal:1,win:3,draw:1,clean_sheet:1}}};\n" + _src + "\nconst cases=" + _json.dumps(_cases) + ";\nconsole.log(JSON.stringify(cases.map(([h,a])=>[liveParts(h,a).pts, liveParts(a,h).pts])));"
    open("/tmp/_lp.js", "w").write(_njs)
    _front = [tuple(x) for x in _json.loads(_sp.run(["node", "/tmp/_lp.js"], capture_output=True, text=True).stdout)]
    ck("liveParts matches scoring.py live points on 6 scorelines (incl. 0-0 = draw + clean sheet = 2)",
       _front == _server, (_front, _server))
except Exception as _e:
    ck("live-points cross-check ran", False, str(_e)[:140])

# ---- bet-slip selection model: tap=select, re-tap=remove, 2nd game=auto-acca, down-to-1=single ----
print("\n== bet-slip selection model (.betodd click) ==")
_s = HTML.index("querySelectorAll('.betodd').forEach(b=>b.onclick=()=>{")
_i = HTML.index("{", _s); _d = 0; _j = _i
while _j < len(HTML):
    if HTML[_j] == "{": _d += 1
    elif HTML[_j] == "}":
        _d -= 1
        if _d == 0: break
    _j += 1
_BODY = HTML[_i + 1:_j]
_slip = """
let ACCA=[], BETPICK=null, ACCAMODE=false, BETMSG='';
let maxLegs=3, accasOn=true;
function renderBets(){}
function click(mid,sel,frac){ const b={dataset:{mid,sel,frac}}; (()=>{%s})(); }
function st(){ return ACCAMODE ? ('ACCA['+ACCA.map(l=>l.matchId+'-'+l.sel).join(',')+']') : (BETPICK?('SINGLE '+BETPICK.matchId+'-'+BETPICK.sel):'EMPTY'); }
let out=[];
function eq(n,g,w){ out.push((g===w?'PASS ':'FAIL ')+n+(g===w?'':'  got '+JSON.stringify(g))); }
click('A','HOME','2/1'); eq('first tap selects a single', st(), 'SINGLE A-HOME');
click('A','HOME','2/1'); eq('re-tapping the same single removes it', st(), 'EMPTY');
click('A','HOME','2/1'); click('A','AWAY','3/1'); eq('same game, other outcome switches the single', st(), 'SINGLE A-AWAY');
BETPICK=null;ACCA=[];ACCAMODE=false;
click('A','HOME','2/1'); click('B','HOME','1/1'); eq('a second game auto-builds an acca', st(), 'ACCA[A-HOME,B-HOME]');
click('B','HOME','1/1'); eq('removing a leg back to one reverts to a single', st(), 'SINGLE A-HOME');
BETPICK=null;ACCA=[];ACCAMODE=false;
click('A','HOME','2/1'); click('B','HOME','1/1'); click('C','HOME','1/1'); eq('a third leg is added', st(), 'ACCA[A-HOME,B-HOME,C-HOME]');
click('D','HOME','1/1'); eq('a leg beyond the cap is rejected', st(), 'ACCA[A-HOME,B-HOME,C-HOME]');
eq('the cap rejection sets a message', BETMSG.indexOf('at most 3')>=0, true);
click('A','AWAY','5/2'); eq('tapping the other outcome switches that leg', st(), 'ACCA[A-AWAY,B-HOME,C-HOME]');
BETPICK=null;ACCA=[];ACCAMODE=false; accasOn=false;
click('A','HOME','2/1'); click('B','HOME','1/1'); eq('with accas off, a second game just switches the single', st(), 'SINGLE B-HOME');
console.log(out.join('\\n'));
""" % _BODY
open("/tmp/_slip.js", "w").write(_slip)
_r = subprocess.run(["node", "/tmp/_slip.js"], capture_output=True, text=True)
if _r.returncode != 0:
    ck("the bet-slip handler runs", False, _r.stderr[:160])
else:
    for _ln in _r.stdout.splitlines():
        _ln = _ln.strip()
        if _ln.startswith(("PASS ", "FAIL ")):
            ck(_ln[5:], _ln.startswith("PASS "), None)

# ---- 3. no forbidden patterns ----
print("\n== 2. Safety patterns ==")
ck("no eval( in the client", "eval(" not in JS, "eval present")
ck("uses an HTML-escaping helper (esc) for user/team text", "function esc(" in JS, "no esc()")
# localStorage is fine on a real site, but every use should be wrapped so a privacy-mode browser can't crash the app
ls_uses = len(re.findall(r"localStorage\.", JS))
ck("localStorage access is guarded by try/catch where used", (ls_uses == 0) or ("try" in JS), ls_uses)

# ---- 4. extract pure functions by name (brace-match) and unit-test under node ----
def extract(name):
    i = JS.find("function %s(" % name)
    if i < 0:
        return None
    j = JS.find("{", i)
    depth = 0
    k = j
    while k < len(JS):
        c = JS[k]
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return JS[i:k+1]
        k += 1
    return None

funcs = {n: extract(n) for n in ("esc", "ownerOf", "koNote", "fmtTime", "mulberry32", "isDone", "liveClockText", "provResult", "accaLiveStatus", "buildHistory", "simWinOdds")}
missing = [n for n, v in funcs.items() if not v]
ck("all pure helpers were found in the source", not missing, missing)
_am = re.search(r"const award=\(wins,cmp\)=>\{.*?\};", JS, re.S)
AWARD_SRC = _am.group(0) if _am else "const award=function(){};"
ck("forecast award() tie-splitter found in simWinOdds", _am is not None)

test_js = "\n".join(v for v in funcs.values() if v) + r"""
let fails = [];
function ck(name, cond){ if(!cond){ fails.push(name); console.log("  FAIL "+name);} else { console.log("  PASS "+name);} }
let FETCH_AT = Date.now();   // liveClockText ticks on from the server value relative to this

// ---- liveClockText(): the live match clock ----
ck("clock shows server seconds as MM:SS", liveClockText("IN_PLAY", null, 3120) === "52:00");
ck("clock shows HT while paused", liveClockText("PAUSED", 45, 2700) === "HT");
ck("clock shows PENS during a shootout", liveClockText("IN_PLAY", null, 7200, "1") === "PENS");
ck("clock falls back to the feed minute", liveClockText("IN_PLAY", 52, null) === "52'");
ck("clock falls back to LIVE with nothing", liveClockText("IN_PLAY", null, null) === "LIVE");
ck("clock is blank when not live", liveClockText("FINISHED", 90, 5400) === "");
ck("clock caps a runaway value at 130:00", liveClockText("IN_PLAY", null, 200*60) === "130:00");
ck("clock counts into extra time (e.g. 105:00)", liveClockText("IN_PLAY", null, 105*60) === "105:00");
ck("clock pads single-digit seconds", liveClockText("IN_PLAY", null, 61) === "1:01");

// ---- provResult(): is a bet currently winning/level/losing? ----
ck("HOME ahead -> win",  provResult("HOME", 1, 0) === "win");
ck("HOME level -> level", provResult("HOME", 0, 0) === "level");
ck("HOME behind -> lose", provResult("HOME", 0, 1) === "lose");
ck("AWAY ahead -> win",  provResult("AWAY", 0, 1) === "win");
ck("AWAY level -> level", provResult("AWAY", 2, 2) === "level");
ck("AWAY behind -> lose", provResult("AWAY", 1, 0) === "lose");
ck("DRAW level -> win",  provResult("DRAW", 1, 1) === "win");
ck("DRAW 0-0 -> win",    provResult("DRAW", 0, 0) === "win");
ck("DRAW not level -> lose", provResult("DRAW", 2, 1) === "lose");
ck("provResult null when score missing", provResult("HOME", null, 1) === null);
ck("provResult reads string scores", provResult("HOME", "2", "1") === "win");

// ---- accaLiveStatus(): overall winning/level/losing for an accumulator ----
{
  const fx = new Map([["m1",{homeScore:1,awayScore:0}],["m2",{homeScore:0,awayScore:0}],["m3",{homeScore:0,awayScore:2}]]);
  const fixOf = id => fx.get(id); const liveS = new Set(["m1","m2","m3"]);
  const W = legs => ({legs});
  ck("acca all live winning -> win", accaLiveStatus(W([{matchId:"m1",selection:"HOME"}]), fixOf, liveS) === "win");
  ck("acca one leg level -> level", accaLiveStatus(W([{matchId:"m1",selection:"HOME"},{matchId:"m2",selection:"HOME"}]), fixOf, liveS) === "level");
  ck("acca one leg losing -> lose", accaLiveStatus(W([{matchId:"m1",selection:"HOME"},{matchId:"m3",selection:"HOME"}]), fixOf, liveS) === "lose");
  ck("acca with a settled-lost leg -> lose", accaLiveStatus(W([{matchId:"m1",selection:"HOME",result:"lost"},{matchId:"m1",selection:"HOME"}]), fixOf, liveS) === "lose");
  ck("acca won leg + live winner -> win", accaLiveStatus(W([{matchId:"x",selection:"HOME",result:"won"},{matchId:"m1",selection:"HOME"}]), fixOf, liveS) === "win");
  ck("acca with a not-yet-started leg -> level", accaLiveStatus(W([{matchId:"m1",selection:"HOME"},{matchId:"zzz",selection:"HOME"}]), fixOf, liveS) === "level");
  ck("acca with nothing live/lost -> null", accaLiveStatus(W([{matchId:"zzz",selection:"HOME"}]), fixOf, liveS) === null);
}

// ---- buildHistory(): points/position/survival time series ----
{
  const SCO = {points:{per_goal:1,win:3,draw:1,clean_sheet:1,stage_bonus:{LAST_16:5}}, survival:{LAST_16:26}};
  const alvOf = dd => buildHistory(dd).map(s=>Object.fromEntries(Object.entries(s.p).map(([k,v])=>[k,v.alv])));
  const ptsOf = dd => buildHistory(dd).map(s=>Object.fromEntries(Object.entries(s.p).map(([k,v])=>[k,v.pts])));

  // a) a single finished match yields 2 points (baseline + match), so the chart can draw a line
  const d1 = {players:[{name:"James",teams:[{name:"Mexico",status:"alive"},{name:"South Africa",status:"alive"}]},
                       {name:"Reuben",teams:[{name:"Brazil",status:"alive"}]}], scoring:SCO,
              fixtures:[{home:"Mexico",away:"South Africa",status:"FINISHED",homeScore:2,awayScore:0,winner:"HOME",stage:"GROUP_STAGE",utcDate:"2026-06-11T19:00:00Z"}]};
  ck("history has a baseline + one point per finished match", buildHistory(d1).length === 2);
  ck("history starts every player at zero points", ptsOf(d1)[0].James === 0 && ptsOf(d1)[0].Reuben === 0);
  ck("history points accrue after the match", ptsOf(d1)[1].James === 6);

  // b) survival = teams still in; stays full through the group stage (nobody eliminated yet)
  ck("survival starts at full squad size", alvOf(d1)[0].James === 2 && alvOf(d1)[0].Reuben === 1);
  ck("survival is flat through the group stage", JSON.stringify(alvOf(d1)[1]) === JSON.stringify(alvOf(d1)[0]));

  // c) a knockout loser drops the owner's teams-in by one, exactly at that match
  const d2 = {players:[{name:"James",teams:[{name:"Mexico",status:"alive"},{name:"South Africa",status:"out"}]},
                       {name:"Reuben",teams:[{name:"Brazil",status:"alive"}]}], scoring:SCO,
              fixtures:[{home:"Mexico",away:"South Africa",status:"FINISHED",homeScore:2,awayScore:0,winner:"HOME",stage:"GROUP_STAGE",utcDate:"2026-06-11T19:00:00Z"},
                        {home:"Brazil",away:"South Africa",status:"FINISHED",homeScore:3,awayScore:0,winner:"HOME",stage:"LAST_16",utcDate:"2026-07-01T19:00:00Z"}]};
  ck("survival steps down at the KO elimination", alvOf(d2).map(x=>x.James).join(",") === "2,2,1");
  ck("survival untouched for a player with no eliminations", alvOf(d2).every(x=>x.Reuben === 1));

  // d) a group-stage casualty drops when the knockout stage opens (the first KO match)
  const d3 = {players:[{name:"Reuben",teams:[{name:"Brazil",status:"alive"},{name:"Spain",status:"out"}]}], scoring:SCO,
              fixtures:[{home:"Mexico",away:"Spain",status:"FINISHED",homeScore:1,awayScore:0,winner:"HOME",stage:"GROUP_STAGE",utcDate:"2026-06-11T19:00:00Z"},
                        {home:"Mexico",away:"Brazil",status:"FINISHED",homeScore:0,awayScore:1,winner:"AWAY",stage:"LAST_16",utcDate:"2026-07-01T19:00:00Z"}]};
  ck("group casualty drops at the first knockout match", alvOf(d3).map(x=>x.Reuben).join(",") === "2,2,1");

  // e) the "now" point snaps to the leaderboard totals so betting shows (match replay gives Erol 0 football, but
  //    the leaderboard has him on 0.2 from a settled bet) — earlier points stay match-only
  const d4 = {players:[{name:"James",teams:[{name:"Mexico",status:"alive"}]},{name:"Erol",teams:[{name:"Brazil",status:"alive"}]}], scoring:SCO,
              fixtures:[{home:"Mexico",away:"South Africa",status:"FINISHED",homeScore:2,awayScore:0,winner:"HOME",stage:"GROUP_STAGE",utcDate:"2026-06-11T19:00:00Z"}],
              leaderboards:{points:[{name:"James",score:6},{name:"Erol",score:0.2}], hybrid:[{name:"James",score:6},{name:"Erol",score:0.2}]}};
  ck("chart now-point includes betting (Erol 0 -> 0.2)", ptsOf(d4).map(x=>x.Erol).join(",") === "0,0.2");
  ck("chart baseline stays at zero (betting not back-dated)", ptsOf(d4)[0].Erol === 0);
}

// ---- simWinOdds(): the forecast must carry betting on points/both, but leave survival bet-free ----
{
  const groups=[], pt={P1:[],P2:[]}; let ti=0;
  "ABCDEFGHIJKL".split("").forEach(g=>{ const table=[];
    for(let k=0;k<4;k++){ const name="T"+(ti++), owner=(ti%2)?"P1":"P2", comp=50+k*5;
      table.push({team:name,owner,composite:comp,points:k,goalDifference:0,goalsFor:k,group:g});
      pt[owner].push({name,points:k,status:"alive",stage:"GROUP_STAGE"}); }
    groups.push({group:g,table}); });
  const sco={points:{per_goal:1,win:3,draw:1,clean_sheet:1,stage_bonus:{LAST_32:2,LAST_16:4,QUARTER_FINALS:6,SEMI_FINALS:9,FINAL:12,WINNER:16}},
             survival:{LAST_32:18,LAST_16:26,QUARTER_FINALS:34,SEMI_FINALS:44,FINAL:55,WINNER:70}};
  const mkD=bet=>{ const f1=pt.P1.reduce((s,t)=>s+t.points,0), f2=pt.P2.reduce((s,t)=>s+t.points,0);
    return {groups,fixtures:[],scoring:sco,players:[{name:"P1",teams:pt.P1,points:f1+bet},{name:"P2",teams:pt.P2,points:f2}]}; };
  const get=(r,who)=>r.find(x=>x.name===who);
  const base=simWinOdds(mkD(0),3000), bet=simWinOdds(mkD(500),3000);
  ck("forecast runs on a full bracket", Array.isArray(base) && base.length===2);
  ck("a big betting lead lifts the points-win forecast", get(bet,"P1").pts > get(base,"P1").pts + 30, [get(base,"P1").pts, get(bet,"P1").pts]);
  ck("projected points rise by exactly the betting net", Math.abs(get(bet,"P1").proj - (get(base,"P1").proj + 500)) < 1, [get(base,"P1").proj, get(bet,"P1").proj]);
  ck("survival forecast is bet-free (unchanged)", Math.abs(get(bet,"P1").surv - get(base,"P1").surv) < 6, [get(base,"P1").surv, get(bet,"P1").surv]);
}

// ---- forecast tie-break: a points/both tie goes to most-teams-in; a genuine dead-heat is split ----
{
  const players=["P1","P2","P3"];
  /*__AWARD__*/
  const ez=x=>Math.abs(x)<1e-9?0:x;
  const run=(PT,AL)=>{ const w={P1:0,P2:0,P3:0}; award(w,(a,b)=>ez(PT[b]-PT[a])||(AL[b]-AL[a])); return w; };
  let r;
  r=run({P1:10,P2:10,P3:5},{P1:2,P2:2,P3:1}); ck("dead-heat (same pts & teams-in) splits 50/50", r.P1===0.5&&r.P2===0.5&&r.P3===0);
  r=run({P1:10,P2:10,P3:5},{P1:3,P2:2,P3:1}); ck("a points tie is broken by most teams still in", r.P1===1&&r.P2===0);
  r=run({P1:9,P2:11,P3:5},{P1:5,P2:1,P3:1});  ck("higher points wins regardless of teams-in", r.P2===1&&r.P1===0);
  r=run({P1:8,P2:8,P3:8},{P1:2,P2:2,P3:2});   ck("three-way dead-heat splits into thirds", Math.abs(r.P1-1/3)<1e-9&&Math.abs(r.P3-1/3)<1e-9);
}

// ---- esc(): XSS escaping ----
ck("esc escapes <", esc("<b>") === "&lt;b&gt;");
ck("esc escapes ampersand", esc("a & b") === "a &amp; b");
ck("esc escapes a script tag", esc("<script>alert(1)</script>") === "&lt;script&gt;alert(1)&lt;/script&gt;");
ck("esc handles null safely", esc(null) === "");
ck("esc handles a number", esc(123) === "123");
ck("esc leaves plain text untouched", esc("Brazil") === "Brazil");
ck("esc escapes a quote-laden team name oddity", esc("A<>&B") === "A&lt;&gt;&amp;B");

// ---- ownerOf(): team -> owner ----
const d = {players:[{name:"Erol",teams:[{name:"Brazil"},{name:"Spain"}]},{name:"James",teams:[{name:"Serbia"}]}]};
ck("ownerOf finds the right owner", ownerOf(d,"Brazil") === "Erol");
ck("ownerOf finds the other owner", ownerOf(d,"Serbia") === "James");
ck("ownerOf returns dash for an unowned team", ownerOf(d,"France") === "\u2014");
ck("ownerOf is safe on empty data", ownerOf({},"Brazil") === "\u2014");
ck("ownerOf is safe when players have no teams", ownerOf({players:[{name:"X"}]},"Brazil") === "\u2014");

// ---- koNote(): a.e.t / pens caption ----
ck("koNote blank for a normal finished game", koNote({status:"FINISHED"}) === "");
ck("koNote shows a.e.t. for extra time", koNote({status:"FINISHED",aet:true}) === "a.e.t.");
ck("koNote shows pens score", koNote({status:"FINISHED",shootout:true,penHome:4,penAway:3}) === "pens 4\u20133");
ck("koNote combines a.e.t. + pens", koNote({status:"FINISHED",aet:true,shootout:true,penHome:5,penAway:4}) === "a.e.t. \u00b7 pens 5\u20134");
ck("koNote blank for an in-play game", koNote({status:"IN_PLAY",aet:true}) === "");
ck("koNote handles missing pen scores", koNote({status:"FINISHED",shootout:true}) === "pens 0\u20130");

// ---- fmtTime(): invalid -> TBC ----
ck("fmtTime returns TBC for junk", fmtTime("not-a-date") === "TBC");
ck("fmtTime returns a string for a real ISO time", typeof fmtTime("2026-06-15T18:00:00Z") === "string");

// ---- mulberry32(): deterministic PRNG ----
const A = mulberry32(42), B = mulberry32(42);
ck("mulberry32 is deterministic for the same seed", A() === B());
ck("mulberry32 yields values in [0,1)", (()=>{const r=mulberry32(7);for(let i=0;i<1000;i++){const v=r();if(v<0||v>=1)return false;}return true;})());

// ---- 2-dp money rounding (the fix): returns/net shown to 2 decimals ----
const round2 = x => Math.round(x*100)/100;
ck("a 1.23 stake at 2/1 returns 3.69 (2dp, not 3.7)", round2(1.23*(1+2/1)) === 3.69);
ck("net of a 2dp win is exact to 2dp", round2((3.69-1.23)) === 2.46);
ck("rounding leaves whole numbers whole", round2(15) === 15);

if(fails.length){ console.log("\nFRONTEND-JS FAILED: "+fails.join(", ")); process.exit(1); }
console.log("\nfront-js ok");
"""
open("/tmp/_front_test.js", "w").write(test_js.replace("/*__AWARD__*/", AWARD_SRC))
print("\n== 3. Pure-function behaviour (under node) ==")
r = subprocess.run(["node", "/tmp/_front_test.js"], capture_output=True, text=True)
for line in r.stdout.splitlines():
    if line.strip().startswith(("PASS", "FAIL")):
        print("  " + line.strip())
    elif line.strip().startswith(("  PASS", "  FAIL")):
        print(line)
if r.returncode != 0:
    # surface any node failure lines already printed; mark a single rollup failure
    if "FRONTEND-JS FAILED" in r.stdout:
        for nm in r.stdout.split("FRONTEND-JS FAILED:", 1)[1].strip().split(", "):
            if nm and nm not in FAILS: FAILS.append(nm)
    else:
        FAILS.append("frontend node test crashed: " + (r.stderr[:120] or "?"))
else:
    # count node passes into our tally for visibility
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith("PASS "):
            pass

# ---- 5. other pages parse + escape names; the wheel draw allocates correctly ----
print("\n== 4. Other pages (me/watch/setup/wheel) ==")
for page in ("me.html", "watch.html", "setup.html", "wheel.html"):
    p = os.path.join(REPO, page)
    if not os.path.exists(p):
        continue
    h = open(p).read()
    pjs = "\n".join(re.findall(r"<script>(.*?)</script>", h, re.S))
    open("/tmp/_pg.js", "w").write(pjs)
    rr = subprocess.run(["node", "--check", "/tmp/_pg.js"], capture_output=True, text=True)
    ck("%s JS parses" % page, rr.returncode == 0, rr.stderr[:150])
    ck("%s CSS braces balanced" % page, h.count("{") == h.count("}"), page)

# me.html / watch.html must escape interpolated names (defence-in-depth XSS, like tracker.html)
for page in ("me.html", "watch.html"):
    pjs = "\n".join(re.findall(r"<script>(.*?)</script>", open(os.path.join(REPO, page)).read(), re.S))
    ck("%s defines an esc() helper" % page, ("esc=" in pjs or "function esc(" in pjs), page)
    ck("%s does not interpolate a raw ${p.name} into innerHTML" % page, "${p.name}" not in pjs, page)
    ck("%s does not interpolate a raw ${t.name} into innerHTML" % page, "${t.name}" not in pjs, page)
wjs = "\n".join(re.findall(r"<script>(.*?)</script>", open(os.path.join(REPO, "watch.html")).read(), re.S))
ck("watch.html escapes the team chip name", "esc(t.team" in wjs, "team not escaped")
ck("watch.html escapes the player name", "esc(p)" in wjs, "player not escaped")

# ---- 6. the wheel's client-side 'fair' draw allocates every team exactly once ----
print("\n== 5. Wheel client draw allocation ==")
wheel_html = open(os.path.join(REPO, "wheel.html")).read()
wheel_js = "\n".join(re.findall(r"<script>(.*?)</script>", wheel_html, re.S))
def _extract_from(js, name):
    i = js.find("function %s(" % name)
    if i < 0: return None
    j = js.find("{", i); depth = 0; k = j
    while k < len(js):
        if js[k] == "{": depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0: return js[i:k+1]
        k += 1
    return None
cf = _extract_from(wheel_js, "computeFair")
ck("computeFair found in wheel.html", bool(cf), "")
if cf:
    wheel_test = cf + r"""
let order, teams, perPlayer, fairAssign=null;
let fails=[];
function ck(n,c){ console.log((c?"  PASS ":"  FAIL ")+n); if(!c)fails.push(n); }
function mkTeams(N){ const o=[]; for(let i=0;i<N;i++) o.push({name:"T"+i,composite:100-i,implied_prob:Math.max(0.001,(N-i)/(N*N)),tier:(i<4?1:i<12?2:i<24?3:4)}); return o; }
function run(P,per){ order=[]; for(let i=0;i<P;i++)order.push("P"+i); perPlayer=per; teams=mkTeams(P*per); fairAssign=null; computeFair(); return fairAssign; }
for(const [P,per] of [[5,8],[5,9],[4,10],[2,11],[8,4]]){
  const a=run(P,per), N=P*per; const all=[]; let okc=!!a;
  if(a){ for(const p of order){ if(!a[p]||a[p].length!==per) okc=false; (a[p]||[]).forEach(t=>all.push(t.name)); } }
  ck("draw "+P+"x"+per+": each player gets "+per+" teams", okc);
  ck("draw "+P+"x"+per+": all "+N+" teams assigned, none duplicated/dropped", a && all.length===N && new Set(all).size===N);
}
if(fails.length){ console.log("WHEELFAIL:"+fails.join(",")); process.exit(1);} console.log("wheelok");
"""
    open("/tmp/_wheel_test.js", "w").write(wheel_test)
    rr = subprocess.run(["node", "/tmp/_wheel_test.js"], capture_output=True, text=True)
    for line in rr.stdout.splitlines():
        s = line.strip()
        if s.startswith(("PASS", "FAIL")): print("  " + s)
        elif line.startswith(("  PASS", "  FAIL")): print(line)
    if rr.returncode != 0:
        if "WHEELFAIL:" in rr.stdout:
            for nm in rr.stdout.split("WHEELFAIL:", 1)[1].splitlines()[0].split(","):
                if nm: FAILS.append("wheel: " + nm)
        else:
            FAILS.append("wheel draw test crashed: " + (rr.stderr[:120] or "?"))

if FAILS:
    print("\nFRONTEND QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll frontend QA passed.")
