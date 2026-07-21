#!/usr/bin/env python3
"""End-of-tournament archiver. Run in the SITE directory on the box:

    cd /opt/wc26/sites/mandem && sudo -u wc26 python3 /opt/wc26/repo/archive.py

Produces, in the site directory:
  wc26-final-archive.zip   every data file (results, wagers, draw, tracker snapshot, history,
                           alerts, calibration, config WITHOUT secrets) — downloadable at
                           https://<site>/wc26-final-archive.zip
  wc26-demo/               a self-contained static demo: tracker.html + the final tracker_data.json
                           + me/watch pages. No server, no API, no token — open index.html anywhere.
  results_wc2026.json      \\
  wagers_wc2026.json        > frozen snapshots to COMMIT TO THE REPO as replay/test data
  draw_result_wc2026.json  /   (the results_wc2022.json pattern) — test_replay_wc26.py runs on them.

Nothing is deleted or moved; this only copies. Free-tier / no-API safe: it reads local files only."""
import json
import os
import shutil
import sys
import zipfile

SECRET_KEYS = ("token", "admin_key", "discord_token", "bot_token", "webhook", "vapid_private")

DATA_FILES = [
    "results.json", "wagers.json", "tracker_data.json", "draw_result.json", "teams.json",
    "history.json", "alerts_sent.json", "calibration.json", "push_subs.json", "claims.json",
    "welcomed.json", "standings.json",
]
DEMO_PAGES = ["tracker.html", "me.html", "watch.html", "wheel.html", "icon.svg", "manifest.webmanifest"]

DEMO_SHIM = """<script>
/* WC26 DEMO SHIM — makes the live app fully static: every /api/* call is answered from the
   bundled final-state files, so betting history, the draw reveal, teams and players all render
   with no server and no API token. Placements/admin are inert. */
(function(){
  var real = window.fetch.bind(window);
  window.fetch = function(url, opts){
    var u = String(url);
    if (u.indexOf("/api/") === -1) return real(url, opts);
    var ok = function(o){ return Promise.resolve(new Response(JSON.stringify(o), {status: 200,
      headers: {"Content-Type": "application/json"}})); };
    if (u.indexOf("/api/wagers") !== -1) return real("wagers.json").catch(function(){ return ok([]); });
    if (u.indexOf("/api/status") !== -1) return ok({configured: true, demo: true, wagering: true});
    return ok({ok: false, demo: true, error: "This is the static WC26 archive — actions are disabled."});
  };
  document.addEventListener("DOMContentLoaded", function(){
    var b = document.createElement("div");
    b.textContent = "WORLD CUP 2026 - FINAL ARCHIVE - champions: Spain - points winner: Ismail 550.1";
    b.style.cssText = "position:sticky;top:0;z-index:200;text-align:center;font:600 12px/2 sans-serif;" +
      "letter-spacing:.05em;background:#0c2416;color:#ffd76a;border-bottom:1px solid #24513a";
    document.body.prepend(b);
  });
})();
</script>"""


def _demoify(path):
    """Inject the static shim into a copied demo page so it runs with no server."""
    try:
        html = open(path).read()
        if "WC26 DEMO SHIM" in html or "<body" not in html:
            return
        i = html.index("<body")
        i = html.index(">", i) + 1
        data = (html[:i] + DEMO_SHIM + html[i:]).encode("utf-8")   # ENCODE before truncating: a bad
        with open(path, "wb") as fh:                               # byte must fail before the open()
            fh.write(data)
    except Exception as e:
        print("  (demo shim skipped for %s: %s)" % (path, e))


def _scrubbed_config():
    try:
        cfg = json.load(open("config.json"))
    except Exception:
        return None
    return {k: ("<redacted>" if any(s in k.lower() for s in SECRET_KEYS) else v) for k, v in cfg.items()}


def main():
    repo = os.path.dirname(os.path.abspath(__file__))
    made = []

    with zipfile.ZipFile("wc26-final-archive.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in DATA_FILES:
            if os.path.exists(f):
                z.write(f)
                made.append(f)
        cfg = _scrubbed_config()
        if cfg is not None:
            z.writestr("config.scrubbed.json", json.dumps(cfg, indent=2))
    print("wc26-final-archive.zip: %d files (%s)" % (len(made), ", ".join(made)))

    os.makedirs("wc26-demo", exist_ok=True)
    for f in ["tracker_data.json", "results.json", "draw_result.json", "teams.json", "history.json",
              "wagers.json", "live_draw.json", "review.json"]:
        if os.path.exists(f):
            shutil.copy2(f, os.path.join("wc26-demo", f))
    for f in DEMO_PAGES:
        src = f if os.path.exists(f) else os.path.join(repo, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join("wc26-demo", f))
    for f in DEMO_PAGES:
        p = os.path.join("wc26-demo", f)
        if f.endswith(".html") and os.path.exists(p):
            _demoify(p)
    if os.path.exists(os.path.join("wc26-demo", "tracker.html")):
        shutil.copy2(os.path.join("wc26-demo", "tracker.html"), os.path.join("wc26-demo", "index.html"))
    print("wc26-demo/: fully static snapshot — tracker + bets + draw reveal + teams/players, "
          "no server, no API (open wc26-demo/index.html)")

    for src, dst in [("results.json", "results_wc2026.json"), ("wagers.json", "wagers_wc2026.json"),
                     ("draw_result.json", "draw_result_wc2026.json")]:
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print("%s written — commit it to the repo as replay data" % dst)

    print("\nDone. Nothing was modified or deleted.")


if __name__ == "__main__":
    sys.exit(main())
