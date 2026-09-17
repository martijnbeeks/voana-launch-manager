"""kill_sync.py process — one bad ClickUp write must not abort the run.

Offline: `ks.cu` is replaced by an in-memory ClickUp. Pins the 2026-09-10 failure
(a 404 on one checklist item raised out of the loop: four batches already
written were never ledgered, two were never reached, inbox and last_run stayed
put) and the per-lander ad identity that the same run exposed (one creative
runs on two landers, so 'C4' alone is two ads).

Run: python3 tests/test_process_isolation.py
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

ACCOUNT = next(iter(ks.CONFIG["accounts"]))
F = ks.CONFIG["clickup"]["fields"]

def adset_name(no, fmt="C1-C3"):
    return f"{no} - {fmt} - Image - Stick - Pers - Pers - smell - Avatar - cycle - UMS - Problem Aware - THAI - Ideation - None"

def ad(no, lander, code, adset_id, spend="$10.00 USD", status="PAUSED"):
    base = adset_name(no)
    parts = base.split(" - ")
    parts.insert(1, lander)
    return {"id": f"{adset_id}{lander}{code}", "name": " - ".join(parts) + f"_{code}", "adset_id": adset_id,
            "adset_name": base, "effective_status": status, "created_time": "2026-09-01T10:00:00+0000",
            "amount_spent": spend, "impressions": 1000, "clicks": 20, "ctr": "2.00%", "cpc": "$0.50 USD",
            "omni_purchase": 0}

class FakeClickUp:
    """Enough of the v2 API for process(). `fail_item_posts` = {checklist_id: n}
    makes the n-th item POST on that checklist 404 once (ClickUp flake)."""
    def __init__(self, tasks):
        self.tasks = {t["id"]: t for t in tasks}
        # the real lists' status sets (verified 2026-09-17)
        L = ks.CONFIG["clickup"]["list_ids"]
        self.lists = {
            L["media"]: {"name": "Media", "statuses": [{"status": s} for s in
                         ("to do", "learning", "rejected", "winner", "has potential", "killed", "complete")]},
            L["launch_manager"]: {"name": "Launch Manager", "statuses": [{"status": s} for s in
                                  ("ready for launch", "launched", "complete")]},
        }
        self.calls = []
        self.comments = {t["id"]: [] for t in tasks}
        self.fail_item_posts = {}
        self._item_posts = {}
        self._n = 0

    def _id(self):
        self._n += 1
        return f"id{self._n}"

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        seg = path.strip("/").split("/")
        if method == "GET" and seg[0] == "list":
            lid = seg[1]
            if len(seg) == 2:   # list metadata: its own statuses
                return self.lists.get(lid, {"name": lid, "statuses": []})
            return {"tasks": [t for t in self.tasks.values() if t["list"] == lid], "last_page": True}
        if seg[0] == "task":
            tid = seg[1]
            t = self.tasks[tid]
            if method == "GET" and len(seg) == 2:
                return json.loads(json.dumps(t))
            if method == "PUT" and len(seg) == 2:
                t["status"] = {"status": body["status"]}; return {}
            if seg[-1] == "comment":
                if method == "GET":
                    return {"comments": [{"comment_text": c} for c in self.comments[tid]]}
                self.comments[tid].append(body["comment_text"]); return {}
            if seg[2] == "field":
                if not any(f["id"] == seg[3] for f in t["custom_fields"]):
                    raise ks.ClickUpError(method, path, 400, '{"err":"Custom field does not exist in the task location hierarchy","ECODE":"FIELD_115"}')
                for f in t["custom_fields"]:
                    if f["id"] == seg[3]:
                        f["value"] = body["value"]
                return {}
            if seg[2] == "checklist":
                cl = {"id": self._id(), "name": body["name"], "items": []}
                t.setdefault("checklists", []).append(cl)
                return {"checklist": cl}
        if seg[0] == "checklist":
            cl = next((c for t in self.tasks.values() for c in t.get("checklists", []) if c["id"] == seg[1]), None)
            if cl is None:
                raise ks.ClickUpError(method, path, 404, "checklist gone")
            if method == "POST":
                n = self._item_posts[cl["id"]] = self._item_posts.get(cl["id"], 0) + 1
                if self.fail_item_posts.get(cl["id"]) == n:
                    self.fail_item_posts.pop(cl["id"])
                    raise ks.ClickUpError(method, path, 404, "flake")
                cl["items"].append({"id": self._id(), **body}); return {}
            item = next((i for i in cl["items"] if i["id"] == seg[3]), None)
            if item is None:
                raise ks.ClickUpError(method, path, 404, "item gone")
            if method == "DELETE":
                cl["items"].remove(item); return {}
            item.update(body); return {}
        raise AssertionError(f"unexpected {method} {path}")

def make_task(tid, name, status="learning", list_key="media"):
    return {"id": tid, "name": name, "list": ks.CONFIG["clickup"]["list_ids"][list_key],
            "status": {"status": status}, "checklists": [],
            "custom_fields": [{"id": F["result"], "type": "text", "value": None},
                              {"id": F["status"], "type": "drop_down", "value": None, "type_config": {"options": []}}]
                             + ([{"id": fid, "type": "number", "value": None}
                                 for fid in (ks.CONFIG["clickup"].get("metric_fields") or {}).values()]
                                if list_key == "media" else [])}

def fresh_state(events, metrics, adset_rows=None):
    d = pathlib.Path(tempfile.mkdtemp(prefix="killsync-"))
    (d / "inbox").mkdir()
    (d / "inbox" / f"kills_{ACCOUNT}.json").write_text(json.dumps(events))
    (d / "inbox" / f"metrics_{ACCOUNT}.json").write_text(json.dumps(metrics))
    (d / "inbox" / f"active_adsets_{ACCOUNT}.json").write_text("[]")
    if adset_rows is not None:
        (d / "inbox" / f"adset_metrics_{ACCOUNT}.json").write_text(json.dumps(adset_rows))
    ks.STATE, ks.INBOX, ks.DURABLE = d, d / "inbox", d
    return d

ks.CLICKUP_KEY = "pk_test"
ks.WEBHOOK = ""
posted = []                # discord() stubbed: record what would be posted
ks.discord = lambda embeds, content, dry: (posted.append((embeds, content)), True)[1]
ks.SKIP_CLICKUP = False
ks._SLEEP = lambda s: None

# ── ad identity ──────────────────────────────────────────────────────────────
a = ad("S014", "7R", "C4", "as1")
check("ad_key carries the lander", ks.ad_key(a["name"]), "C4/7R")
check("ad_key without lander", ks.ad_key("S003 - C1-C3 - Image_C2"), "C2")
line = ks.ad_line(a)
check("ad_line starts with the key", line.startswith("C4/7R ⏸ · "), True)
check("AD_LINE_RE parses the key back", ks.AD_LINE_RE.match(line).group(1), "C4/7R")
check("legacy line still parses", ks.AD_LINE_RE.match("C4 ✖09-09 · $1").group(1), "C4")
merged = ks.merge_result("KILLED x\nC4 ✖09-09 · old\nC1 ▶ · keep",
                         ["C4/7R ✖09-10 · a", "C4/OG ✖09-10 · b", "C10/OG ▶ · c"], None)
check("legacy collapsed line dropped, others kept, numeric order",
      merged.splitlines(), ["KILLED x", "C1 ▶ · keep", "C4/7R ✖09-10 · a", "C4/OG ✖09-10 · b", "C10/OG ▶ · c"])

# ── transport retry ──────────────────────────────────────────────────────────
import urllib.error, io
attempts = []
def flaky_open(req, timeout):
    attempts.append(req.full_url)
    if len(attempts) < 3:
        raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "1"}, io.BytesIO(b"rate"))
    return io.BytesIO(b'{"ok": true}')
real_open = ks.urllib.request.urlopen
ks.urllib.request.urlopen = flaky_open
check("429 retried then succeeds", ks.cu("GET", "/x"), {"ok": True})
check("three attempts made", len(attempts), 3)
attempts.clear()
def dead_open(req, timeout):
    attempts.append(1)
    raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, io.BytesIO(b"gone"))
ks.urllib.request.urlopen = dead_open
try:
    ks.cu("POST", "/y", {})
    check("404 raises ClickUpError", False, True)
except ks.ClickUpError as e:
    check("404 not retried", len(attempts), 1)
    check("404 code exposed", e.code, 404)
    check("404 body in message", "gone" in str(e), True)
ks.urllib.request.urlopen = real_open

# ── process: one checklist 404 must not take the run down ────────────────────
S3, S4, S5 = adset_name("S003"), adset_name("S004"), adset_name("S005")
tasks = [make_task("t3", S3), make_task("t4", S4), make_task("t5", S5)]
fake = FakeClickUp(tasks)
ks.cu = fake
ev = lambda no, aid, t="9-9-2026 om 13:40": {"object_type": "adset", "object_id": aid, "object_name": adset_name(no),
                                             "actor_name": "Martijn", "application_name": "Ads Manager",
                                             "datetime": t, "old_value": 1, "new_value": 7}
events = [ev("S003", "as3"), ev("S004", "as4"), ev("S005", "as5")]
metrics = [ad("S003", l, c, "as3") for l in ("OG", "7R") for c in ("C1", "C2")] \
        + [ad("S004", l, c, "as4") for l in ("OG", "7R") for c in ("C1", "C2")] \
        + [ad("S005", "OG", "C1", "as5")]
metrics.append(dict(metrics[0]))  # the bridge listed one ad twice
state = fresh_state(events, metrics)
# every checklist-item write on S004's checklist 404s, refetch or not — the
# permanent flavour of the 2026-09-10 flake
orig_call = fake.__call__
def sabotaged(method, path, body=None):
    t4_cls = {c["id"] for c in fake.tasks["t4"].get("checklists", [])}
    if path.startswith("/checklist/") and path.split("/")[2] in t4_cls:
        raise ks.ClickUpError(method, path, 404, "flake")
    return orig_call(method, path, body)
ks.cu = sabotaged
rc = ks.process(dry=False)
ks.cu = fake
rows = [json.loads(l) for l in (state / "kills.jsonl").read_text().splitlines()]
check("run completed with a ClickUp-failure exit code", rc, 4)
check("failure named in the Discord problems line", "S004" in (posted[-1][1] or "") and "ClickUp update failed" in posted[-1][1], True)
check("failed kill still gets its embed", len(posted[-1][0]), 3)
check("all three kills ledgered", sorted(r["object_id"] for r in rows), ["as3", "as4", "as5"])
check("only the failed one carries an error", [r["object_id"] for r in rows if r.get("error")], ["as4"])
check("last_run advanced", (state / "last_run.json").exists(), True)
check("inbox archived", list((state / "inbox").glob("*.json")), [])
check("S003 commented", len(fake.comments["t3"]), 1)
check("S005 (after the failure) commented", len(fake.comments["t5"]), 1)
check("S005 task status set", fake.tasks["t5"]["status"]["status"], ks.CONFIG["clickup"]["batch_kill_task_status"])
check("S004 comment landed before the checklist blew up", len(fake.comments["t4"]), 1)
check("S004 status still set (checklist is written last)", fake.tasks["t4"]["status"]["status"], "killed")
cl3 = fake.tasks["t3"]["checklists"][0]
check("one checklist item per AD, not per creative", len(cl3["items"]), 4)
check("items keyed per lander", sorted(ks.AD_LINE_RE.match(i["name"]).group(1) for i in cl3["items"]),
      ["C1/7R", "C1/OG", "C2/7R", "C2/OG"])
res3 = next(f["value"] for f in fake.tasks["t3"]["custom_fields"] if f["id"] == F["result"])
check("Result has header + 4 lines (duplicate metric row collapsed)", len(res3.splitlines()), 5)
check("batch spend not doubled by the duplicate row", "$40.00" in res3, True)

# ── re-run on the same inbox (crash recovery): no duplicate comments ─────────
state2 = fresh_state(events, metrics)
shutil.copy(state / "kills.jsonl", state2 / "kills.jsonl")
rc = ks.process(dry=False)
check("re-run with ledger: nothing re-reported", rc, 0)
check("re-run posted no new comment", len(fake.comments["t3"]), 1)
# ledger lost (or keys differ) but comments already there → still no repeat
state3 = fresh_state(events, metrics)
fake.tasks["t4"]["checklists"].clear()
ks.cu = fake
rc = ks.process(dry=False)
check("re-run without ledger completes", rc, 0)
check("comment idempotent without ledger", [len(fake.comments[t]) for t in ("t3", "t4", "t5")], [1, 1, 1])
check("S004 checklist repaired on the re-run", len(fake.tasks["t4"]["checklists"][0]["items"]), 4)
cl3 = fake.tasks["t3"]["checklists"][0]
check("re-run updated items instead of adding", len(cl3["items"]), 4)

# ── checklist: flake on one item POST → refetch + retry, no duplicates ───────
t = make_task("t9", adset_name("S009"))
fake = FakeClickUp([t]); ks.cu = fake
ads = [ad("S009", l, c, "as9") for l in ("OG", "7R") for c in ("C1", "C2")]
fake.fail_item_posts["id1"] = 3   # the 3rd item POST on the checklist 404s once
ks.upsert_checklist(t, ads, {a["id"] for a in ads}, ks.dt.datetime(2026, 9, 10, 8, 45), dry=False)
check("flaky 404 recovered", len(t["checklists"][0]["items"]), 4)
check("items after recovery are unique", len({i["name"] for i in t["checklists"][0]["items"]}), 4)
# legacy collapsed items are retired once per-lander items exist; human items stay
t["checklists"][0]["items"] += [{"id": "old1", "name": "C1 ✖09-09 · $1.00 · old", "resolved": True},
                                {"id": "hum", "name": "check the landing page", "resolved": False}]
ks.upsert_checklist(t, ads, {a["id"] for a in ads}, ks.dt.datetime(2026, 9, 10, 8, 45), dry=False)
names = [i["name"] for i in t["checklists"][0]["items"]]
check("legacy item retired", any(n.startswith("C1 ✖09-09") for n in names), False)
check("human item untouched", "check the landing page" in names, True)
check("still one item per ad", len(names), 5)
# item deleted in the UI → recreated, not a crash
t["checklists"][0]["items"].pop(0)
fake.tasks["t9"] = t
ks.upsert_checklist(t, ads, {a["id"] for a in ads}, ks.dt.datetime(2026, 9, 10, 8, 45), dry=False)
check("deleted item recreated", len(t["checklists"][0]["items"]), 5)

# ── Discord chunking: 6000 chars of embed text per message, not just 10 embeds
big = [{"title": f"t{i}", "description": "x" * 1500} for i in range(7)]
chunks = ks.chunk_embeds(big)
check("7×1500-char embeds need 2 messages", [len(c) for c in chunks], [3, 3, 1])
check("every chunk under the budget", all(sum(ks._embed_len(e) for e in c) <= 6000 for c in chunks), True)
small = [{"title": "t", "description": "s"} for _ in range(23)]
check("small embeds chunk by 10", [len(c) for c in ks.chunk_embeds(small)], [10, 10, 3])
check("empty", ks.chunk_embeds([]), [])

# ── replay: Discord only, no ClickUp writes, no state change
state4 = fresh_state(events, metrics)
before = len(fake.calls)
posted.clear()
ks.SKIP_CLICKUP = False
rc = ks.process(dry=False, inbox=state4 / "inbox", replay=True)
check("replay exits clean", rc, 0)
check("replay re-posted all three kills", len(posted[-1][0]), 3)
check("replay wrote no ledger", (state4 / "kills.jsonl").exists(), False)
check("replay left the inbox in place", len(list((state4 / "inbox").glob("*.json"))), 3)
check("replay made only ClickUp reads", all(m == "GET" for m, _ in fake.calls[before:]), True)
ks.SKIP_CLICKUP = False

# ── S182 (2026-09-17): a task still in Launch Manager has no "killed" status ──
posted.clear()
ks._LIST_CACHE.clear()
t182 = make_task("t182", adset_name("S182"), status="ready for launch", list_key="launch_manager")
ks.CONFIG["clickup"].setdefault("metric_fields", {})
fake = FakeClickUp([t182]); ks.cu = fake
m182 = [ad("S182", "OG", c, "as182", spend="$25.00 USD") for c in ("C1", "C2")]
state5 = fresh_state([ev("S182", "as182")], m182)
rc = ks.process(dry=False)
rows = [json.loads(l) for l in (state5 / "kills.jsonl").read_text().splitlines()]
check("S182: not a ClickUp failure", rc, 0)
check("S182: no PUT with a status the list lacks", [c for c in fake.calls if c[0] == "PUT"], [])
check("S182: task status left alone", fake.tasks["t182"]["status"]["status"], "ready for launch")
check("S182: comment still written", len(fake.comments["t182"]), 1)
check("S182: checklist still written", len(fake.tasks["t182"]["checklists"][0]["items"]), 2)
check("S182: no write to a field the list lacks",
      [c for c in fake.calls if c[0] == "POST" and "/field/" in c[1]
       and c[1].split("/")[-1] in (ks.CONFIG["clickup"].get("metric_fields") or {}).values()], [])
check("S182: missing fields explained in the note", "metric fields" in (posted[-1][1] or ""), True)
check("S182: ledger row carries no error", [r.get("error") for r in rows], [None])
check("S182: Discord explains it as a note, not a failure",
      ("ℹ️ S182" in (posted[-1][1] or ""), "ClickUp update failed" in (posted[-1][1] or "")), (True, False))
check("S182: embed not marked failed", "⚠️" in posted[-1][0][0]["description"], False)
check("list statuses read once per list", sum(1 for c in fake.calls if c == ("GET", f"/list/{t182['list']}")), 1)

# a task that IS in Media still gets "killed"
ks._LIST_CACHE.clear()
tm = make_task("tm", adset_name("S183"))
fake = FakeClickUp([tm]); ks.cu = fake
state6 = fresh_state([ev("S183", "as183")], [ad("S183", "OG", "C1", "as183")])
check("Media task: run clean", ks.process(dry=False), 0)
check("Media task: status set to killed", fake.tasks["tm"]["status"]["status"], "killed")
check("Media task: metric fields written",
      any(f["value"] is not None for f in fake.tasks["tm"]["custom_fields"]
          if f["id"] in (ks.CONFIG["clickup"].get("metric_fields") or {}).values()), True)

# ── rewrite: ClickUp only, filtered, ignores the ledger, touches no state ─────
ks._LIST_CACHE.clear()
tA, tB = make_task("tA", adset_name("S201")), make_task("tB", adset_name("S202"))
fake = FakeClickUp([tA, tB]); ks.cu = fake
evs = [ev("S201", "as201"), ev("S202", "as202")]
mets = [ad("S201", "OG", "C1", "as201"), ad("S202", "OG", "C1", "as202")]
state7 = fresh_state(evs, mets)
# pretend both kills were already ledgered by the run whose ClickUp half failed
(state7 / "kills.jsonl").write_text("".join(
    json.dumps({"key": f"{ACCOUNT}:adset:{e['object_id']}:{e['datetime']}"}) + "\n" for e in evs))
posted.clear()
rc = ks.process(dry=False, inbox=state7 / "inbox", rewrite=True, only={"S202"})
check("rewrite exits clean", rc, 0)
check("rewrite ignores the ledger and writes the chosen batch", len(fake.comments["tB"]), 1)
check("rewrite leaves other batches alone", len(fake.comments["tA"]), 0)
check("rewrite posts nothing to Discord", posted, [])
check("rewrite appends no ledger rows", len((state7 / "kills.jsonl").read_text().splitlines()), 2)
check("rewrite leaves last_run alone", (state7 / "last_run.json").exists(), False)
check("rewrite leaves the inbox in place", len(list((state7 / "inbox").glob("*.json"))), 3)
rc = ks.process(dry=False, inbox=state7 / "inbox", rewrite=True, only={"S202"})
check("rewrite twice: still one comment", len(fake.comments["tB"]), 1)

# ── archive: durable, pruned ─────────────────────────────────────────────────
check("archive written under the durable dir", len(list((state5 / "inbox-archive").iterdir())), 1)
arch = ks.archive_dir()
for i in range(ks.ARCHIVE_KEEP + 3):
    (arch / f"2026-09-{i:02d}_0845").mkdir(parents=True, exist_ok=True)
gone = ks.prune_archive()
check("prune keeps the newest ARCHIVE_KEEP", len([d for d in arch.iterdir() if d.is_dir()]), ks.ARCHIVE_KEEP)
check("prune drops the oldest first", gone[0] if gone else None, "2026-09-00_0845")

for d in (state, state2, state3, state4, state5, state6, state7):
    shutil.rmtree(d, ignore_errors=True)
if fails:
    print("\n".join("FAIL " + f for f in fails)); sys.exit(1)
print(f"ok — {checks} checks passed")
