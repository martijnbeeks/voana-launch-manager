"""tw_sync.py — Triple Whale numbers as ad subtasks under the batch's Media task.

Offline: Triple Whale rows and ClickUp tasks are in-memory. Pins what the user
asked for on 2026-11-18: the ad numbers live inside the batch task, each run
overwrites them in place, and nothing is appended.

Run: python3 tests/test_tw_sync.py
"""
import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import kill_sync as ks
import tw_sync as tw

fails, checks = [], 0

def check(label, got, want):
    global checks
    checks += 1
    if got != want:
        fails.append(f"{label}: got {got!r}, want {want!r}")

F = {k: f"f_{k}" for k, *_ in tw.FIELDS}
ASOF = 1795003200000

def ad(i, name, adset, spend=100.0, recent=10.0, status="ACTIVE", **kw):
    row = {"ad_id": i, "ad_name": name, "adset_name": adset, "status": status, "spend": spend,
           "impr": 10000, "clicks": 500, "ob": 300, "cv": 250.0, "ncp": 2, "ncv": 120.0,
           "ncv_lc": 90.0, "recent": recent}
    row.update(kw)
    return row

ADS = [
    ad("a1", "S457 - COMP - V1-V3 - Video - Stick_V1", "S457 - V1-V3 - Video - Stick"),
    ad("a2", "S457 - COMP - V1-V3 - Video - Stick_V2", "S457 - V1-V3 - Video - Stick", spend=300.0, ncp=6, ncv=280.0),
    ad("a3", "TA S457 - COMP - V1-V3 - Video - Stick_V2", "Week_46_winners_Stick", recent=500.0),
    ad("a4", "S300 - OG - C1-C2 - Image - Cream_C1", "S300 - C1-C2 - Image - Cream", recent=0.0, status="PAUSED"),
    ad("a5", "S272 - 7R - C1-C6 - Image - Cream_C1 - IMP", "S272 - C1-C6 - Image - Cream", recent=900.0),
    ad("a6", "S999 - OG - C1 - Image - Stick_C1", "S999 - C1 - Image - Stick"),
]
TASKS = [
    {"id": "t457", "name": "S457 - V1-V3 - Video - Stick", "custom_fields": []},
    {"id": "t300", "name": "S300 - C1-C2 - Image - Cream", "custom_fields": []},
    {"id": "t272", "name": "S272 - C1-C6 - Image - Cream", "custom_fields": []},
]

batches = tw.live_batches(ADS)
check("live batches: own ad set only, recent spend, busiest first", [c for c, _ in batches], ["S272", "S457", "S999"])
check("a copy in a scaling ad set is not part of the batch", [a["ad_id"] for a in dict(batches)["S457"]], ["a1", "a2"])

plan = tw.reconcile(batches, TASKS, F, ASOF, "complete")
creates = {c["fields"]["f_ad_id"]: c for c in plan.creates}
check("one subtask per ad, under the batch task, named exactly like the ad", sorted((c["parent"], c["name"]) for c in plan.creates),
      [("t272", "S272 - 7R - C1-C6 - Image - Cream_C1 - IMP"), ("t457", "S457 - COMP - V1-V3 - Video - Stick_V1"),
       ("t457", "S457 - COMP - V1-V3 - Video - Stick_V2")])
check("subtasks are created in the configured status", {c["status"] for c in plan.creates}, {"complete"})
check("the Meta Ad ID is set on create, so the next run finds the subtask", creates["a1"]["fields"]["f_ad_id"], "a1")
check("Meta delivery state goes in a field, not the status", creates["a1"]["fields"]["f_meta_status"], "ACTIVE")
check("a batch with no Media task is skipped and said", any("S999" in n for n in plan.notes), True)

totals = {fid: v for tid, _, fid, v in plan.writes if tid == "t457"}
check("batch total spend on the parent", totals["f_spend"], 400.0)
check("batch ratios are recomputed from the sums, not averaged", (totals["f_ncroas_ta"], totals["f_nccpa"]), (1.0, 50.0))
check("ad ratios: NC CPA and CTR %", (creates["a2"]["fields"]["f_nccpa"], creates["a2"]["fields"]["f_ctr"]), (50.0, 5.0))

# The run after: every subtask exists and holds today's values (ClickUp returns strings, and moves the date's hour).
def as_task(c, i):
    return {"id": f"s{i}", "name": c["name"], "parent": c["parent"],
            "custom_fields": [{"id": k, "value": str(v + 3600_000 if k == "f_asof" else v)} for k, v in c["fields"].items()]}
existing = [as_task(c, i) for i, c in enumerate(plan.creates)]
v1 = next(t["id"] for t in existing if t["name"].endswith("Stick_V1"))
parents = [{**t, "custom_fields": [{"id": fid, "value": str(v)} for tid, _, fid, v in plan.writes if tid == t["id"]]} for t in TASKS]
again = tw.reconcile(batches, parents + existing, F, ASOF, "complete")
check("a second run with nothing changed writes nothing", (again.creates, again.renames, again.writes), ([], [], []))

old = [dict(t, name="V1 · COMP") if t["id"] == v1 else t for t in existing]
check("a subtask with an old short name is renamed to the ad name, nothing else",
      tw.reconcile(batches, parents + old, F, ASOF, "complete").renames, [(v1, "S457 - COMP - V1-V3 - Video - Stick_V1")])

cut = ks.ClickUpTasks.__new__(ks.ClickUpTasks)
cut.tasks = TASKS + old
check("kill-sync still matches the batch task, not an ad subtask that starts with the same code",
      ks.ClickUpTasks.find(cut, "S457 - other name")[0]["id"], "t457")

# A day later: one ad spent more. Only its changed values are overwritten.
later = [dict(a, spend=a["spend"] + 50.0) if a["ad_id"] == "a1" else a for a in ADS]
next_day = tw.reconcile(tw.live_batches(later), parents + existing, F, ASOF, "complete")
check("next day: no new subtasks", next_day.creates, [])
check("next day: only the changed ad and its batch are written", sorted({tid for tid, *_ in next_day.writes}), sorted([v1, "t457"]))
check("next day: the new spend overwrites the old value", [v for tid, _, fid, v in next_day.writes if tid == v1 and fid == "f_spend"], [150.0])

dupe = existing + [dict(existing[0], id="sdup")]
check("two subtasks with one Meta Ad ID are reported", any("2 subtasks" in n for n in tw.reconcile(batches, parents + dupe, F, ASOF, "complete").notes), True)

two = TASKS + [{"id": "t457b", "name": "S457 - relaunch", "custom_fields": []}]
check("an ambiguous batch code is skipped, not guessed", "S457" in tw.reconcile(batches, two, F, ASOF, "complete").batches, False)

mixed = [ad("m1", "S421 - OG - C1-C6 - Image - Stick_C1", "S421 - C1-C6 - Image - Stick"),
         ad("m4", "S421 - OG - C1-C6 - Image - Cream_C2", "S421 - C1-C6 - Image - Cream", spend=0.4)]
mp = tw.reconcile(tw.live_batches(mixed), [{"id": "t421", "name": "S421 - C1-C6 - Image", "custom_fields": []}], F, ASOF, "complete")
check("an ad under $1 gets no subtask but counts in the batch total",
      ([c["fields"]["f_ad_id"] for c in mp.creates], [v for tid, _, fid, v in mp.writes if fid == "f_spend"]), (["m1"], [100.4]))
check("the as-of date sits on the batch task only", ("f_asof" in creates["a1"]["fields"], "f_asof" in totals), (False, True))

check("a field date within the same day counts as unchanged", tw.same("date", str(ASOF + 5 * 3600_000), ASOF), True)


seen, saved_cu = [], ks.cu
def flaky(method, path, body=None):
    seen.append((method, ks.CU_RETRIES)); raise ks.ClickUpError(method, path, None, "read timed out")
ks.cu = flaky
try:
    errs = tw.apply(tw.Plan(creates=[{"parent": "t457", "name": "x", "status": "complete", "fields": {}}]), "L")
finally:
    ks.cu = saved_cu
check("a create runs with no retries and a failure is reported, not repeated", (seen, len(errs)), ([("POST", 0)], 1))
check("the retry budget is restored after a create", ks.CU_RETRIES, 3)


def test_tw_sync():
    assert not fails, fails


if __name__ == "__main__":
    print(f"{checks - len(fails)}/{checks} checks passed")
    for f in fails:
        print("FAIL", f)
    sys.exit(1 if fails else 0)
