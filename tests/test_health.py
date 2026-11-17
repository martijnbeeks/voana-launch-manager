"""kill_sync.py health — the deadman that runs on GitHub Actions.

Pins the 15-17 Nov 2026 blind spot: five runs read `completed` in Multica while
doing nothing, because the only thing that proves a run finished is the
last_run.json that `process` writes and `push-state` pushes.

Run: python3 tests/test_health.py
"""
import datetime as dt, json, pathlib, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import kill_sync as ks

fails, checks = [], 0

def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        fails.append(f"{label}: got {got!r}, want {want!r}")

SAVED = {k: getattr(ks, k) for k in ("DURABLE", "discord")}
posts = []
ks.discord = lambda embeds, content, dry, mention_ids=(): posts.append((content, dry)) or True
NOW = dt.datetime(2026, 11, 18, 9, 30, tzinfo=dt.timezone.utc)
tmp = pathlib.Path(tempfile.mkdtemp())
ks.DURABLE = tmp

def run(last_run, max_age=26.0):
    posts.clear()
    f = tmp / "last_run.json"
    if last_run is None:
        f.unlink(missing_ok=True)
    else:
        f.write_text(json.dumps({"last_run": last_run, "kills": 0}))
    return ks.health(False, max_age, now=NOW)

try:
    check("a run a few hours ago is healthy and silent", (run("2026-11-18T07:12:00+01:00"), posts), (0, []))
    rc = run("2026-11-17T07:12:00+01:00")
    check("a missed day alerts", (rc, len(posts)), (1, 1))
    check("the alert says how to catch up", "multica autopilot trigger" in posts[0][0], True)
    check("the 15-17 Nov gap would have alerted", run("2026-11-14T20:49:03"), 1)
    check("an old file without an offset is read as Amsterdam time",
          run("2026-11-18T07:12:00"), 0)
    check("no last_run.json at all alerts", (run(None), "no last_run.json" in posts[0][0]), (1, True))
    check("an unreadable stamp alerts rather than passing", run("garbage"), 1)
finally:
    for k, v in SAVED.items():
        setattr(ks, k, v)


def test_health():
    assert not fails, fails


if __name__ == "__main__":
    print(f"{checks - len(fails)}/{checks} checks passed")
    for f in fails:
        print("FAIL", f)
    sys.exit(1 if fails else 0)
