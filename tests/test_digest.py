"""kill_sync.py digest — a zero-kill run must not re-announce its predecessor.

Offline: `ks.discord` is replaced by a recorder, `ks.DURABLE` by a temp dir.
Pins the 2026-09-20/21 repeat (the 20:45 run synced 15 kills; the two zero-kill
runs that followed each posted the same "15 kill(s) → ClickUp" card, because
`max(detected)` still pointed at the last run that ledgered anything).

Run: python3 tests/test_digest.py
"""
import json, pathlib, shutil, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import kill_sync as ks

fails, checks = [], 0

def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        fails.append(f"{label}: got {got!r}, want {want!r}")

posted = []
ks.discord = lambda embeds, content, dry: (posted.append(embeds), True)[1]

def state(rows, last_run, kills):
    """A durable dir as `process` leaves it: ledger + this run's last_run.json."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="kill-sync-digest-"))
    (d / "kills.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "last_run.json").write_text(json.dumps({"last_run": last_run, "kills": kills}))
    ks.DURABLE = d
    return d

def row(detected, name, task_id="t1"):
    return {"key": f"{name}|{detected}", "kind": "adset", "name": name,
            "task_id": task_id, "detected": detected}

KILLS = [row("2026-09-20T20:48:32.725509", "S002 - C1-C3"),
         row("2026-09-20T20:48:32.725509", "S071 - C1-C3", "t2")]
dirs = []

# ── the run that actually ledgered the kills posts them ──────────────────────
dirs.append(state(KILLS, "2026-09-20T20:48:32", 2))
posted.clear()
check("syncing run exits 0", ks.digest(dry=False), 0)
check("syncing run posts one embed", len(posted), 1)
check("embed counts both kills", posted[0][0]["title"], "✅ 2 kill(s) → ClickUp")

# ── THE REGRESSION: the next run found nothing, so it says nothing ───────────
dirs.append(state(KILLS, "2026-09-21T08:46:20", 0))
posted.clear()
check("zero-kill run exits 0", ks.digest(dry=False), 0)
check("zero-kill run posts nothing", posted, [])

# a second zero-kill run stays quiet too (Discord saw three cards, not two)
dirs.append(state(KILLS, "2026-09-21T20:46:18", 0))
posted.clear()
ks.digest(dry=False)
check("second zero-kill run posts nothing", posted, [])

# ── a missing last_run.json fails closed rather than re-announcing ───────────
d = state(KILLS, "2026-09-20T20:48:32", 2)
dirs.append(d)
(d / "last_run.json").unlink()
posted.clear()
check("no last_run.json exits 0", ks.digest(dry=False), 0)
check("no last_run.json posts nothing", posted, [])

# ── a run that killed but matched no ClickUp task still says nothing ─────────
dirs.append(state([row("2026-09-21T08:46:20.1", "#410 | 08-08-26", None)],
                  "2026-09-21T08:46:20", 1))
posted.clear()
check("unmapped-only run exits 0", ks.digest(dry=False), 0)
check("unmapped-only run posts nothing", posted, [])

# ── --dry-run still previews: it writes no last_run.json to match against ────
dirs.append(state(KILLS, "2026-09-21T08:46:20", 0))
posted.clear()
check("dry run exits 0", ks.digest(dry=True), 0)
check("dry run still previews the newest stamp", len(posted), 1)

for d in dirs:
    shutil.rmtree(d, ignore_errors=True)
if fails:
    print("FAILED:")
    for x in fails:
        print("  -", x)
    sys.exit(1)
print(f"ok — {checks} checks passed")
