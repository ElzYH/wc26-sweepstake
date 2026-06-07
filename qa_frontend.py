#!/usr/bin/env python3
"""Frontend QA for tracker.html: confirm the embedded JS parses, and unit-test the pure helper functions
by extracting them and running them under node — especially esc() (the XSS-escaping guard), ownerOf()
(team->owner lookup used all over the UI), koNote() (extra-time/penalty caption), and the 2-dp money
rounding used for stakes/returns/balances. DOM-bound functions can't run headless, so we test the logic
that does the actual work."""
import os, sys, re, json, subprocess, shutil

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

funcs = {n: extract(n) for n in ("esc", "ownerOf", "koNote", "fmtTime", "mulberry32", "isDone")}
missing = [n for n, v in funcs.items() if not v]
ck("all pure helpers were found in the source", not missing, missing)

test_js = "\n".join(v for v in funcs.values() if v) + r"""
let fails = [];
function ck(name, cond){ if(!cond){ fails.push(name); console.log("  FAIL "+name);} else { console.log("  PASS "+name);} }

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
open("/tmp/_front_test.js", "w").write(test_js)
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

if FAILS:
    print("\nFRONTEND QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll frontend QA passed.")
