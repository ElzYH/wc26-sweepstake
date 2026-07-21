#!/usr/bin/env python3
"""Demo generator — builds a demos/ folder of fully static, GitHub-Pages-ready scenarios:

  demos/real/            the IRL WC26: real results, real bets (Erol, Ismail, James, Louis, Reuben)
  demos/real-nobets/     the same tournament with betting stripped (pure sweepstake)
  demos/random-crew/     real tournament, anonymised random crew + their bets (shareable)
  demos/fictional/       an invented tournament (simulate) — same draw, alternate universe
  demos/index.html       a landing page linking them all

Every scenario is a REPLAY: a control bar lets you scrub the tournament day by day or press play —
speeds: step / as-it-happened (1 day = 60s) / matchday (5s) / fast (2s) / jump to end. Each day is a
real scoring.compute() snapshot, and the bet ledger is re-settled per day, so the leaderboard, bracket,
odds, and every bet move exactly as they did live. No server, no API, no token.

Run from the REPO ROOT after committing the archive snapshots:
    python3 tools/make_demos.py             # needs results_wc2026.json etc. (from archive.py)
    python3 tools/make_demos.py --fictional-seed 7
"""
import copy
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import scoring  # noqa: E402
import wager    # noqa: E402

PAGES = ["tracker.html", "me.html", "watch.html", "wheel.html", "icon.svg", "manifest.webmanifest"]
RANDOM_NAMES = ["Ava", "Zed", "Kofi", "Mika", "Ines", "Theo", "Nadia", "Ravi"]

REPLAY_SHIM = """<script>
/* WC26 REPLAY SHIM — static time-travel. Snapshots per tournament day; fetch() is rerouted so the
   live app renders day N; the control bar scrubs or plays through the tournament. */
(function(){
  var META=null, DAY=0, TIMER=null;
  function snap(){ return "snapshots/day-" + DAY + ".json"; }
  function wsnap(){ return "snapshots/wagers-day-" + DAY + ".json"; }
  var real = window.fetch.bind(window);
  window.fetch = function(url, opts){
    var u = String(url);
    if (u.indexOf("tracker_data.json") !== -1) return real(snap()+"?v="+DAY);
    if (u.indexOf("/api/wagers") !== -1) return real(wsnap()+"?v="+DAY).catch(function(){
      return new Response("[]", {headers:{"Content-Type":"application/json"}}); });
    if (u.indexOf("/api/status") !== -1) return Promise.resolve(new Response(
      JSON.stringify({configured:true, demo:true, wagering:true}), {headers:{"Content-Type":"application/json"}}));
    if (u.indexOf("/api/") !== -1) return Promise.resolve(new Response(
      JSON.stringify({ok:false, demo:true, error:"Static replay — actions are disabled."}),
      {headers:{"Content-Type":"application/json"}}));
    return real(url, opts);
  };
  function rerender(){ if (typeof window.refresh === "function") { window.refresh(); }
    else { location.hash = "#d" + DAY; location.reload(); } }
  function setDay(d){ DAY = Math.max(0, Math.min(META.days.length - 1, d));
    document.getElementById("rpDay").textContent = META.days[DAY] || "pre-tournament";
    document.getElementById("rpSlider").value = DAY; rerender(); }
  function play(ms){ stop(); if (ms) TIMER = setInterval(function(){
    if (DAY >= META.days.length - 1) { stop(); return; } setDay(DAY + 1); }, ms); }
  function stop(){ if (TIMER) clearInterval(TIMER); TIMER = null; }
  document.addEventListener("DOMContentLoaded", function(){
    real("snapshots/meta.json").then(function(r){ return r.json(); }).then(function(m){
      META = m;
      var bar = document.createElement("div");
      bar.style.cssText = "position:sticky;top:0;z-index:220;display:flex;gap:8px;align-items:center;" +
        "flex-wrap:wrap;padding:7px 12px;background:#0c2416;color:#ffd76a;border-bottom:1px solid #24513a;" +
        "font:600 12px sans-serif";
      bar.innerHTML = '<span>REPLAY \\u00b7 ' + (m.title || "WC26") + '</span>' +
        '<button id="rpPrev">\\u23ee</button><button id="rpPlay">\\u25b6</button>' +
        '<button id="rpNext">\\u23ed</button>' +
        '<input id="rpSlider" type="range" min="0" max="' + (m.days.length - 1) + '" value="0" style="flex:1;min-width:120px">' +
        '<span id="rpDay">pre-tournament</span>' +
        '<select id="rpSpeed"><option value="0">step</option><option value="60000">as-it-happened (1d/min)</option>' +
        '<option value="5000" selected>matchday (1d/5s)</option><option value="2000">fast (1d/2s)</option></select>' +
        '<button id="rpEnd">\\u23e9 end</button>';
      document.body.prepend(bar);
      var sp = function(){ return parseInt(document.getElementById("rpSpeed").value, 10); };
      document.getElementById("rpPrev").onclick = function(){ stop(); setDay(DAY - 1); };
      document.getElementById("rpNext").onclick = function(){ stop(); setDay(DAY + 1); };
      document.getElementById("rpEnd").onclick = function(){ stop(); setDay(META.days.length - 1); };
      document.getElementById("rpPlay").onclick = function(){ TIMER ? stop() : play(sp() || 0); };
      document.getElementById("rpSlider").oninput = function(e){ stop(); setDay(parseInt(e.target.value, 10)); };
      var h = (location.hash.match(/#d(\\d+)/) || [])[1];
      setDay(h ? parseInt(h, 10) : 0);
    });
  });
})();
</script>"""


def _inject(path, shim):
    html = open(path).read()
    if "REPLAY SHIM" in html or "<body" not in html:
        return
    i = html.index(">", html.index("<body")) + 1
    data = (html[:i] + shim + html[i:]).encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)


def _day_of(m):
    return (m.get("utcDate") or "")[:10]


def _reset_wagers(ws):
    ws = copy.deepcopy(ws)
    for w in ws:
        if not isinstance(w, dict) or w.get("credit"):
            continue
        for l in (w.get("legs") or []):
            l.pop("result", None)
        if w.get("status") in ("won", "lost", "void"):
            w["status"] = "pending"
            w.pop("result", None)
            w.pop("settled_at", None)
            den, num = w.get("den"), w.get("num")
            if not w.get("legs") and den:
                w["return"] = round((w.get("stake") or 0) * (1 + (num or 0) / den), 2)
            elif w.get("legs") and w.get("decimal"):
                w["return"] = round((w.get("stake") or 0) * w["decimal"], 2)
    return ws


def build_scenario(name, results, draw, wagers, teams_path, title):
    out = os.path.join("demos", name)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(os.path.join(out, "snapshots"))

    matches = results.get("matches", [])
    days = sorted({_day_of(m) for m in matches
                   if m.get("status") in ("FINISHED", "AWARDED") and _day_of(m)})
    day_ends = []
    with tempfile.TemporaryDirectory() as td:
        dpath = os.path.join(td, "draw_result.json")
        json.dump(draw, open(dpath, "w"))
        for k in range(len(days) + 1):                       # k=0 is pre-tournament
            cutoff = days[k - 1] if k else ""
            part = copy.deepcopy(results)
            for m in part["matches"]:
                if m.get("status") in ("FINISHED", "AWARDED") and _day_of(m) > cutoff:
                    m["status"] = "TIMED"
                    for f in ("homeScore", "awayScore", "winner", "penHome", "penAway", "aet",
                              "shootout", "scorers", "cardsHome", "cardsAway", "cardEvents",
                              "homeLineup", "awayLineup"):
                        m.pop(f, None)
            rpath = os.path.join(td, "results.json")
            json.dump(part, open(rpath, "w"))
            wk = [w for w in _reset_wagers(wagers)
                  if not (isinstance(w, dict) and not w.get("credit") and cutoff
                          and False)]                        # all bets exist; settlement below dates them
            fin = [m for m in part["matches"] if m.get("status") in ("FINISHED", "AWARDED")]
            for m in fin:
                wager.settle(wk, m)
            json.dump(wk, open(os.path.join(out, "snapshots", "wagers-day-%d.json" % k), "w"))
            scoring.compute(teams_path=teams_path, draw_path=dpath, results_path=rpath,
                            out=os.path.join(out, "snapshots", "day-%d.json" % k), wagers=wk)
            day_ends.append(cutoff or "pre")
    json.dump({"title": title, "days": ["pre-tournament"] + days},
              open(os.path.join(out, "snapshots", "meta.json"), "w"))

    json.dump(results, open(os.path.join(out, "results.json"), "w"))
    json.dump(draw, open(os.path.join(out, "draw_result.json"), "w"))
    shutil.copy2(teams_path, os.path.join(out, "teams.json"))
    for f in PAGES:
        src = f if os.path.exists(f) else os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(out, f))
    for f in PAGES:
        p = os.path.join(out, f)
        if f.endswith(".html") and os.path.exists(p):
            _inject(p, REPLAY_SHIM)
    shutil.copy2(os.path.join(out, "tracker.html"), os.path.join(out, "index.html"))
    print("demos/%s: %d day snapshots" % (name, len(days) + 1))


def _rename_crew(obj, mapping):
    s = json.dumps(obj)
    for old, new in mapping.items():
        s = s.replace('"%s"' % old, '"%s"' % new)
    return json.loads(s)


def _fictional(seed):
    """Run the simulator in a temp dir against the real teams/draw -> an invented results.json."""
    with tempfile.TemporaryDirectory() as td:
        for f in ("teams.json", "draw_result.json"):
            src = f if os.path.exists(f) else None
            if src is None and os.path.exists(f + ".demo"):
                src = f + ".demo"
            if src is None and f == "draw_result.json" and os.path.exists("draw_result_wc2026.json"):
                src = "draw_result_wc2026.json"
            shutil.copy2(src, os.path.join(td, f))
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "simulate_2026.py"), str(seed)],
                       cwd=td, check=True, stdout=subprocess.DEVNULL)
        return json.load(open(os.path.join(td, "results.json")))


def main():
    seed = 7
    if "--fictional-seed" in sys.argv:
        seed = int(sys.argv[sys.argv.index("--fictional-seed") + 1])
    for f in ("results_wc2026.json", "draw_result_wc2026.json"):
        if not os.path.exists(f):
            sys.exit("%s missing — run archive.py on the box and commit the snapshots first." % f)
    results = json.load(open("results_wc2026.json"))
    draw = json.load(open("draw_result_wc2026.json"))
    wagers = json.load(open("wagers_wc2026.json")) if os.path.exists("wagers_wc2026.json") else []
    os.makedirs("demos", exist_ok=True)

    build_scenario("real", results, draw, wagers, "teams.json", "WC26 — as it happened")
    build_scenario("real-nobets", results, draw, [], "teams.json", "WC26 — sweepstake only (no betting)")

    names = sorted({p.get("name") for p in (draw.get("players") or []) if p.get("name")})
    rng = random.Random(42)
    picks = rng.sample(RANDOM_NAMES, len(names))
    mapping = dict(zip(names, picks))
    build_scenario("random-crew", results, _rename_crew(draw, mapping), _rename_crew(wagers, mapping),
                   "teams.json", "WC26 — anonymised crew")

    build_scenario("fictional", _fictional(seed), draw, [], "teams.json",
                   "Alternate universe (simulated, seed %d)" % seed)

    links = "".join('<li><a href="%s/index.html">%s</a></li>' % (n, t) for n, t in [
        ("real", "The real WC26 — full replay with every bet"),
        ("real-nobets", "WC26 sweepstake only — betting stripped"),
        ("random-crew", "WC26 with an anonymised crew"),
        ("fictional", "Alternate-universe tournament (simulated)")])
    open("demos/index.html", "w").write(
        "<!doctype html><meta charset=utf-8><title>WC26 sweepstake demos</title>"
        "<body style='font-family:sans-serif;background:#07130c;color:#e7f3ea;padding:40px'>"
        "<h1>WC26 sweepstake — demos</h1><p>Each demo is a static time-travel replay: scrub or play "
        "through the tournament day by day. No server needed.</p><ul>%s</ul>" % links)
    print("demos/index.html written — commit demos/ (or publish via GitHub Pages)")


if __name__ == "__main__":
    main()
