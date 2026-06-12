#!/usr/bin/env python3
"""Resilience QA: prove the data layer survives corruption and bad writes.
- a corrupt wagers.json / config.json recovers from the last-good backup (bets/links never vanish)
- an empty write can never clobber a non-empty wager log
- the 6-hour snapshot rotates and keeps a bounded history
Runs in a throwaway temp dir; never touches the repo."""
import os, sys, json, shutil, tempfile

SRC = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="wc26_resil_")
for fn in os.listdir(SRC):
    if fn.endswith(".py"):
        try: shutil.copy(os.path.join(SRC, fn), TMP)
        except Exception: pass
os.environ["WC26_CONFIG"] = os.path.join(TMP, "config.json")
os.chdir(TMP); sys.path.insert(0, TMP)
import server as S

FAILS = []
def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or extra == "" else "  -> %r" % (extra,)))
    if not cond: FAILS.append(name)

def write(path, text):
    with open(path, "w") as f: f.write(text)

print("=== 1) corrupt wagers.json recovers from last_good (bets survive) ===")
good = [{"id": "a1", "player": "Erol", "stake": 10, "status": "pending"},
        {"id": "a2", "player": "James", "stake": 5, "status": "won", "return": 12}]
S.save_wagers(good)
S.backup_data()                                   # snapshot good state into backups/last_good
write("wagers.json", "{ this is corrupt json ][")  # simulate a torn write
loaded = S.load_wagers()
ck("corrupt wagers.json -> recovered 2 bets from backup", loaded == good, loaded)

print("\n=== 2) corrupt config.json recovers from backup (links/keys survive) ===")
cfg = {"configured": True, "admin_key": "SECRET", "wager_links": {"123": "Erol"}, "free_bet_claims": {"d1": {"Erol": "x"}}}
S.save_config(cfg)
os.makedirs("backups/last_good", exist_ok=True)
shutil.copy2(os.environ["WC26_CONFIG"], "backups/last_good/config.json")
write(os.environ["WC26_CONFIG"], "}}corrupt{{")
rc = S.load_config()
ck("corrupt config recovers admin_key + links from backup", rc.get("admin_key") == "SECRET" and rc.get("wager_links") == {"123": "Erol"}, rc)

print("\n=== 3) empty write can NEVER clobber a non-empty wager log ===")
S.save_wagers(good)                               # restore a healthy file
S.save_wagers([])                                 # attempt to wipe it
after = json.load(open("wagers.json"))
ck("save_wagers([]) refused -> the 2 bets are still on disk", after == good, after)

print("\n=== 4) both primary and backup corrupt -> safe empty default, no crash ===")
write("wagers.json", "broken")
shutil.rmtree("backups", ignore_errors=True)
write("wagers.json", "broken")                    # nothing to recover from now
ck("no usable copy -> returns [] without throwing", S.load_wagers() == [], "")

print("\n=== 5) 6-hour snapshot writes a timestamped copy and rotates ===")
S.save_wagers(good)
S._LAST_SNAPSHOT[0] = 0.0
S.backup_snapshot(every_seconds=0, keep=3)        # force a snapshot
snaps = sorted(os.listdir("backups/snapshots"))
ck("a snapshot directory was created", len(snaps) >= 1, snaps)
ck("snapshot contains wagers.json", os.path.exists(os.path.join("backups/snapshots", snaps[-1], "wagers.json")), "")
# force several more and confirm rotation keeps only `keep`
for i in range(6):
    S._LAST_SNAPSHOT[0] = 0.0
    os.makedirs("backups/snapshots/old-%02d" % i, exist_ok=True)  # seed extra dirs to rotate out
    S.backup_snapshot(every_seconds=0, keep=3)
remaining = [d for d in os.listdir("backups/snapshots") if os.path.isdir(os.path.join("backups/snapshots", d))]
ck("rotation keeps the history bounded (<= keep)", len(remaining) <= 3, len(remaining))

print("\n=== 6) snapshot does NOT fire again before the interval elapses ===")
n_before = len([d for d in os.listdir("backups/snapshots")])
S.backup_snapshot(every_seconds=6 * 3600, keep=3)  # just set _LAST_SNAPSHOT to ~now, should skip
n_after = len([d for d in os.listdir("backups/snapshots")])
ck("no extra snapshot within the interval", n_after == n_before, (n_before, n_after))

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("\nRESILIENCE QA FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("\nAll resilience QA passed — data survives corruption, bad writes, and restarts.")
