"""kill_sync.py audit-view / audit — killed batches that are live again.

Offline: ClickUp and Discord are in-memory fakes. Pins the 2026-11-17 finding:
ad sets kill-sync had recorded as killed were switched on again, and killed
batches were copied into scaling CBOs under generic ad-set names. Claude judges
which live ads are a batch; these tests pin what the script does with that
verdict: check it, report it once, and leave the task status alone.

Run: python3 tests/test_comeback_audit.py
"""
import datetime as dt, json, pathlib, shutil, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import kill_sync as ks

fails, checks = [], 0

def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        fails.append(f"{label}: got {got!r}, want {want!r}")

NOW = dt.datetime(2026, 11, 17, 9, 0)
ACC = "1247693024067639"

def ad(i, name, adset_id, adset_name, campaign, spend="10.00"):
    return {"id": i, "name": name, "adset_id": adset_id, "adset_name": adset_name, "campaign_id": "c" + adset_id,
            "campaign_name": campaign, "created_time": "2026-11-14T10:00:00+0000",
            "amount_spent": {"value": spend, "unit": "USD"}}

LIVE = [
    ad("a1", "S247 - COMP - C1-C4 - Image - Cream_C1", "as247", "LC S247 - C1-C4 - Image - Cream", "ABO: Cream Test"),
    ad("a2", "S247 - COMP - C1-C4 - Image - Cream_C2", "as247", "LC S247 - C1-C4 - Image - Cream", "ABO: Cream Test"),
    ad("a3", "TA S361 - COMP - V1-V4 - AI UGC - Stick_V2", "asw46", "Week_46_winners_Stick", "Scaling STICK"),
    ad("a4", "FC S078 - 7R - C1-C5 - LFS - Stick_C3", "asw46", "Week_46_winners_Stick", "Scaling STICK"),
    ad("a5", "S123 - OG - C1-C3 - Image - Stick_C1", "asgy", "Graveyard - Stick", "Evergreen - Graveyard - STICK"),
]

LEDGER = [
    {"key": "k1", "kind": "adset", "object_id": "as247", "name": "S247 - C1-C4 - Image - Cream", "at": "2026-10-03T08:00:00",
     "task_id": "t247", "account": ACC},
    {"key": "k2", "kind": "adset", "object_id": "as361", "name": "S361 - V1-V4 - AI UGC - Stick", "at": "2026-11-13T08:00:00",
     "task_id": "t361", "account": ACC},
    {"key": "k3", "kind": "adset", "object_id": "as123", "name": "S123 - C1-C3 - Image - Stick", "at": "2026-11-10T08:00:00",
     "task_id": "t123", "account": ACC},
    {"key": "k4", "kind": "adset", "object_id": "as001", "name": "S001 - old", "at": "2026-07-01T08:00:00",
     "task_id": "t001", "account": ACC},
    {"key": "k5", "skipped": "replaced by x", "at": "2026-11-12T08:00:00"},
]

VERDICTS = [
    {"batch": "S247", "kind": "reactivated", "adset_id": "as247", "reason": "killed ad set id is live"},
    {"batch": "S361", "kind": "copied", "adset_id": "asw46", "ad_ids": ["a3"], "reason": "TA copy of the S361 ad"},
    {"batch": "S123", "kind": "graveyard", "adset_id": "asgy", "reason": "graveyard copy"},
    {"batch": "S999", "kind": "copied", "adset_id": "asw46", "reason": "not a killed batch"},
    {"batch": "S361", "kind": "copied", "ad_ids": ["gone"], "reason": "id not live"},
]

class Fake:
    def __init__(self):
        self.comments, self.status_writes, self.posts = {}, [], []
    def cu(self, method, path, body=None):
        seg = path.strip("/").split("/")
        if seg[0] == "task" and seg[-1] == "comment":
            got = self.comments.setdefault(seg[1], [])
            if method == "GET":
                return {"comments": [{"comment_text": c} for c in got]}
            got.append(body["comment_text"]); return {}
        self.status_writes.append((method, path))
        return {}
    def discord(self, embeds, content, dry, mention_ids=()):
        self.posts.append(content); return True

def setup():
    tmp = pathlib.Path(tempfile.mkdtemp())
    durable, inbox = tmp / "durable", tmp / "inbox"
    durable.mkdir(); inbox.mkdir()
    (durable / "kills.jsonl").write_text("\n".join(json.dumps(r) for r in LEDGER) + "\n")
    (inbox / "accounts.json").write_text(json.dumps([{"id": ACC, "name": "GetVoana - 1"},
                                                     {"id": "1782681832393562", "name": "Pinnacle"}]))
    (inbox / f"active_ads_{ACC}.json").write_text(json.dumps(LIVE))
    (inbox / "active_ads_1782681832393562.json").write_text("[]")
    (inbox / "comebacks.json").write_text(json.dumps(VERDICTS))
    ks.DURABLE = durable
    fake = Fake()
    ks.cu, ks.discord, ks.WEBHOOK, ks.SKIP_CLICKUP = fake.cu, fake.discord, "https://example.invalid/hook", False
    return tmp, inbox, fake

SAVED = {k: getattr(ks, k) for k in ("DURABLE", "cu", "discord", "WEBHOOK", "SKIP_CLICKUP")}
tmp, inbox, fake = setup()
try:
    check("accounts come from the bridge, not the config", list(ks.run_accounts(inbox)),
          [ACC, "1782681832393562"])
    check("an older inbox falls back to the config list", ks.run_accounts(tmp / "missing"), ks.CONFIG["accounts"])

    batches = ks.killed_batches(now=NOW)
    check("killed batches = ledger kills in the window, skips and old kills out", sorted(batches), ["S123", "S247", "S361"])

    view = ks.audit_view(inbox, now=NOW)
    check("view lists the killed ad set id Claude matches a reactivation on", "as247" in view.split("# Live ads")[0], True)
    check("view folds creative suffixes into one line", view.count("S247 - COMP - C1-C4 - Image - Cream ·"), 1)
    check("view shows ad ids in a mixed winners ad set", "a3" in view and "a4" in view, True)

    rc = ks.audit(False, inbox, now=NOW)
    check("audit exits 0", rc, 0)
    posted = "\n".join(p for p in fake.posts if p)
    check("reactivated batch alerted", "**S247**" in posted and "switched on again" in posted, True)
    check("copied batch alerted", "**S361**" in posted and "copied" in posted, True)
    check("the uncopied winner in the same ad set is not blamed", "S078" in posted, False)
    check("graveyard copy is counted, not alerted", ("**S123**" in posted, "1 live ad(s)" in posted), (False, True))
    check("a verdict for a batch that was never killed is dropped and said", "S999 · not a killed batch" in posted, True)
    check("a verdict whose ids are not live is dropped and said", "S361 · no live ads behind the verdict" in posted, True)
    check("one comment per comeback, on the batch's task", (len(fake.comments.get("t247", [])), len(fake.comments.get("t361", []))), (1, 1))
    check("reactivation comment lists both live ads", all(x in fake.comments["t247"][0] for x in ("a1", "a2")), True)
    check("graveyard gets no comment", fake.comments.get("t123"), None)
    check("no task status or field is ever written", fake.status_writes, [])

    led = [json.loads(l) for l in (ks.DURABLE / ks.COMEBACKS).read_text().splitlines()]
    check("every reported live ad is remembered", sorted(r["key"] for r in led), ["S123:a5", "S247:a1", "S247:a2", "S361:a3"])

    fake.posts.clear()
    ks.audit(False, inbox, now=NOW)
    again = "\n".join(p for p in fake.posts if p)
    check("a second run does not alert the same comeback again", "**S247**" in again or "**S361**" in again, False)
    check("a second run does not comment again", (len(fake.comments["t247"]), len(fake.comments["t361"])), (1, 1))

    shutil.rmtree(tmp)
    tmp, inbox, fake = setup()
    ks.audit(True, inbox, now=NOW)
    check("dry run remembers nothing", (ks.DURABLE / ks.COMEBACKS).exists(), False)
    (inbox / "comebacks.json").unlink()
    fake.posts.clear()
    check("no verdict file = nothing reported", (ks.audit(False, inbox, now=NOW), fake.posts), (0, []))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    for k, v in SAVED.items():   # other test modules import the same module object
        setattr(ks, k, v)


def test_comeback_audit():
    assert not fails, fails


if __name__ == "__main__":
    print(f"{checks - len(fails)}/{checks} checks passed")
    for f in fails:
        print("FAIL", f)
    sys.exit(1 if fails else 0)
