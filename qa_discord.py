#!/usr/bin/env python3
"""Discord QA across three areas, with NO hard dependency on the 'cryptography' package (so it runs on a
plain Python install — same as the server now does):
  A  discord_command() builds a reply for every command without crashing.
  A2 the server's pure-stdlib Ed25519 verifier is correct (golden vector + RFC 8032 vector + tamper/badlen).
  B  the /api/discord_interactions endpoint verifies signatures on the RAW bytes BEFORE parsing — golden
     signed PING / command / private command / autocomplete are accepted; missing/forged/tampered/wrong-key
     are rejected (401); fails closed with no pubkey. Driven by pre-computed golden vectors (no signing lib).
  C  (only if 'cryptography' is installed) extra dynamic round-trip signing, for belt-and-braces.
"""
import os, sys, json, time, shutil, tempfile, subprocess, socket
import urllib.request, urllib.error

REPO = os.path.dirname(os.path.abspath(__file__))
KEY = "QA_ADMIN_KEY_1234567"
FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

NM = [t["name"] for t in json.load(open(os.path.join(REPO, "teams.json")))["teams"]]

# --- golden signed interactions (fixed key seed bytes(range(32)), ts 1700000000); generated once ---
GP = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
GT = "1700000000"
G = {
    "ping":    ('{"type":1}',
                "c1098e97d711377f30225d53d94b89d43537f92e5b3afaddc590781b1f9f9d4b2eeab335d370be3b9a090bc61a85b86448bc140dcd195f1569c2bff181257607"),
    "cmd":     ('{"type":2,"id":"gv","data":{"name":"leaderboard","options":[]},"member":{"user":{"id":"111"}}}',
                "fc7e581b67023732490721b5322384a28968a2f0e0f1161e0ad317f7820449fed7e45819ef6aaec92fb8e0c3a4afa45fda53ae3ed19a363654e699b6f1ae8502"),
    "private": ('{"type":2,"id":"gv","data":{"name":"mybets","options":[]},"member":{"user":{"id":"111"}}}',
                "1c5802950a5e7c6dca8dc3841a31788fa5bc3e62d21830c6b303c0a3dc8abcbfe87be931ba590a31d3c91af709dc77c4259df776ad602a26fbd2c74c3cb4b80f"),
    "autocomp":('{"type":4,"id":"gv","data":{"name":"bet","options":[{"name":"match","value":"","focused":true}]}}',
                "bf68fa88410bd6a3f9ef450f392101a76dfa344cc93b7ac869e95e0869f912baee92180cd9888f45f0228a217dc6fb9fbc7da3e2db3535a86ca29164c8a92203"),
}
RFC_PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
RFC_SIG = "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"

# ============================================================== PART A — COMMAND DISPATCH
print("\n== A. discord_command dispatch (no crash) ==")
TMPA = tempfile.mkdtemp(prefix="wc26_dscA_")
for fn in os.listdir(REPO):
    if fn.endswith(".py"):
        try: shutil.copy(os.path.join(REPO, fn), TMPA)
        except Exception: pass
shutil.copy2(os.path.join(REPO, "teams.json"), os.path.join(TMPA, "teams.json"))
json.dump({"players": [{"name": "Erol", "teams": [{"name": NM[0], "tier": 1, "group": "A"}]},
                       {"name": "James", "teams": [{"name": NM[1], "tier": 1, "group": "A"}]}]},
          open(os.path.join(TMPA, "draw_result.json"), "w"))
json.dump({"matches": [{"id": "m1", "home": NM[0], "away": NM[1], "homeScore": 2, "awayScore": 1,
                        "status": "FINISHED", "stage": "GROUP_STAGE", "winner": "HOME",
                        "utcDate": "2026-06-12T18:00:00Z"}]}, open(os.path.join(TMPA, "results.json"), "w"))
json.dump({"configured": True, "wagering_enabled": True, "players": ["Erol", "James"], "scoring_mode": "hybrid"},
          open(os.path.join(TMPA, "config.json"), "w"))
os.environ["WC26_CONFIG"] = os.path.join(TMPA, "config.json")
_cwd = os.getcwd(); os.chdir(TMPA); sys.path.insert(0, TMPA)
import scoring as SC
SC.compute(out="tracker_data.json", default_mode="hybrid")
import server as S

def cmd(name, opts=None, uid=None):
    try:
        return S.discord_command(name, opts or {}, uid, "iid-1"), None
    except Exception as e:
        return None, repr(e)

for c in ("leaderboard", "odds", "fixtures", "scores", "groups", "stats", "players", "help", "summary"):
    out, err = cmd(c)
    ck("/%s returns text, never throws" % c, err is None and isinstance(out, str) and out, err or out)
lb = cmd("leaderboard")[0] or ""
ck("/leaderboard names a player", ("Erol" in lb or "James" in lb), lb[:60])
ck("/team <unknown> degrades gracefully", cmd("team", {"team": "Nowhere FC"})[1] is None, "")
ck("/notifyme with no uid handled", cmd("notifyme", {"player": "Erol"}, None)[1] is None, "")
ck("an unknown command doesn't throw", cmd("zzz_not_real", {})[1] is None, "")
ck("a None command name doesn't throw", cmd(None, {})[1] is None, "")
ck("/team with no option doesn't throw", cmd("team", {})[1] is None, "")
ck("a wrong-typed option doesn't throw", cmd("notifyme", {"player": 123}, "9")[1] is None, "")
ck("junk options ignored, no throw", cmd("leaderboard", {"x": {"n": [1]}})[1] is None, "")

# ============================================================== PART A2 — PURE VERIFIER CORRECTNESS
print("\n== A2. pure-stdlib Ed25519 verifier correctness ==")
pb = bytes.fromhex(G["ping"][1]); pmsg = (GT + G["ping"][0]).encode()
ck("pure verify accepts the golden PING signature", S._ed25519_verify_pure(bytes.fromhex(GP), pb, pmsg), "")
ck("pure verify rejects a tampered golden message", not S._ed25519_verify_pure(bytes.fromhex(GP), pb, pmsg + b"x"), "")
ck("pure verify accepts the RFC 8032 vector", S._ed25519_verify_pure(bytes.fromhex(RFC_PUB), bytes.fromhex(RFC_SIG), b""), "")
ck("pure verify rejects RFC vector on a changed message", not S._ed25519_verify_pure(bytes.fromhex(RFC_PUB), bytes.fromhex(RFC_SIG), b"x"), "")
ck("pure verify rejects a wrong-length signature", not S._ed25519_verify_pure(bytes.fromhex(GP), b"short", pmsg), "")
ck("pure verify rejects a wrong-length public key", not S._ed25519_verify_pure(b"short", pb, pmsg), "")
bad = bytearray(pb); bad[0] ^= 1
ck("pure verify rejects a flipped signature bit", not S._ed25519_verify_pure(bytes.fromhex(GP), bytes(bad), pmsg), "")
ck("dispatcher accepts the golden PING (hex args)", S._verify_ed25519(GP, G["ping"][1], pmsg), "")
ck("dispatcher rejects junk hex safely", not S._verify_ed25519("zzz", "qqq", pmsg), "")
os.chdir(_cwd)

# ============================================================== PART B — HTTP SIGNATURE BOUNDARY (golden)
print("\n== B. /api/discord_interactions boundary (golden vectors, no signing lib) ==")
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def boot(cfg_extra):
    tmp = tempfile.mkdtemp(prefix="wc26_dscB_")
    shutil.copy2(os.path.join(REPO, "teams.json"), os.path.join(tmp, "teams.json"))
    shutil.copy2(os.path.join(TMPA, "draw_result.json"), os.path.join(tmp, "draw_result.json"))
    shutil.copy2(os.path.join(TMPA, "results.json"), os.path.join(tmp, "results.json"))
    if os.path.exists(os.path.join(TMPA, "tracker_data.json")):
        shutil.copy2(os.path.join(TMPA, "tracker_data.json"), os.path.join(tmp, "tracker_data.json"))
    cfg = {"configured": True, "wagering_enabled": True, "players": ["Erol", "James"],
           "admin_key": KEY, "scoring_mode": "hybrid"}
    cfg.update(cfg_extra)
    json.dump(cfg, open(os.path.join(tmp, "config.json"), "w"))
    port = free_port()
    env = dict(os.environ, WC26_DATA=tmp, WC26_CONFIG=os.path.join(tmp, "config.json"),
               PORT=str(port), HOST="127.0.0.1", ADMIN_KEY=KEY)
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    for _ in range(80):
        try:
            if urllib.request.urlopen(base + "/api/status", timeout=5).status == 200: break
        except Exception: time.sleep(0.1)
    return proc, base, tmp

def post(base, body_str, sig_hex, ts):
    headers = {"Content-Type": "application/json"}
    if sig_hex is not None: headers["X-Signature-Ed25519"] = sig_hex
    if ts is not None: headers["X-Signature-Timestamp"] = ts
    r = urllib.request.Request(base + "/api/discord_interactions", data=body_str.encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=8) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def stop(proc, tmp):
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)

proc, base, tmp = boot({"discord_pubkey": GP})
try:
    st, b = post(base, G["ping"][0], G["ping"][1], GT)
    ck("golden signed PING accepted (200)", st == 200, (st, b[:60]))
    ck("PING reply is a PONG (type 1)", st == 200 and json.loads(b).get("type") == 1, b[:60])
    st, b = post(base, G["cmd"][0], G["cmd"][1], GT)
    ck("golden signed command accepted (200, type 4)", st == 200 and json.loads(b).get("type") == 4, b[:80])
    ck("command reply carries content", st == 200 and json.loads(b).get("data", {}).get("content"), b[:80])
    st, b = post(base, G["private"][0], G["private"][1], GT)
    ck("private command reply is ephemeral (flags 64)", st == 200 and json.loads(b).get("data", {}).get("flags") == 64, b[:80])
    st, b = post(base, G["autocomp"][0], G["autocomp"][1], GT)
    ck("golden signed autocomplete returns choices (type 8)", st == 200 and json.loads(b).get("type") == 8, b[:80])
    ck("autocomplete choices is a list", st == 200 and isinstance(json.loads(b).get("data", {}).get("choices"), list), b[:80])
    st, b = post(base, G["ping"][0], None, None)
    ck("missing signature rejected (401)", st == 401, st)
    st, b = post(base, G["ping"][0], "00" * 64, GT)
    ck("forged signature rejected (401)", st == 401, st)
    st, b = post(base, G["cmd"][0], G["ping"][1], GT)
    ck("a body that doesn't match the signature rejected (401)", st == 401, st)
    st, b = post(base, G["ping"][0], G["ping"][1], "1700000001")
    ck("a mismatched timestamp rejected (401)", st == 401, st)
    st, b = post(base, G["ping"][0], G["ping"][1][:-2] + "00", GT)
    ck("a corrupted signature rejected (401)", st == 401, st)
finally:
    stop(proc, tmp)

proc, base, tmp = boot({})
try:
    st, b = post(base, G["ping"][0], G["ping"][1], GT)
    ck("with no pubkey configured, interactions rejected (401, fail-closed)", st == 401, st)
finally:
    stop(proc, tmp)

proc, base, tmp = boot({"discord_pubkey": RFC_PUB})
try:
    st, b = post(base, G["ping"][0], G["ping"][1], GT)
    ck("golden sig rejected under a DIFFERENT configured pubkey (401)", st == 401, st)
finally:
    stop(proc, tmp)

# ============================================================== PART C — OPTIONAL DYNAMIC SIGNING
print("\n== C. dynamic signing round-trip (only if 'cryptography' is present) ==")
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    have = True
except Exception:
    have = False
if not have:
    print("  SKIP (cryptography not installed — pure path already proven above)")
else:
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    proc, base, tmp = boot({"discord_pubkey": pub_hex})
    try:
        ts = str(int(time.time())); body = '{"type":1}'
        sig = priv.sign(ts.encode() + body.encode()).hex()
        st, b = post(base, body, sig, ts)
        ck("a freshly-signed PING verifies via the server (200)", st == 200 and json.loads(b).get("type") == 1, (st, b[:50]))
        other = Ed25519PrivateKey.generate()
        sig2 = other.sign(ts.encode() + body.encode()).hex()
        st, b = post(base, body, sig2, ts)
        ck("a PING signed by the WRONG key is rejected (401)", st == 401, st)
    finally:
        stop(proc, tmp)

shutil.rmtree(TMPA, ignore_errors=True)
if FAILS:
    print("\nDISCORD QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll Discord QA passed.")
