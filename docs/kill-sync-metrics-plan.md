# Plan — full metrics in ClickUp when a batch is killed

Status: **all four phases done 2026-09-09** (see git log). Phase 3 (real ClickUp
number fields) and phase 4 (backfill) still need the decisions in section 4.
Goal: when kill-sync kills a batch, ClickUp should hold every number the team
needs to judge that batch — and hold it in a form you can sort and filter.

## 1. What happens today

When a batch is killed, `kill_sync.py` writes three things to the ClickUp task:

| Where | What | Type |
|---|---|---|
| `Result` custom field | `KILLED <date> after N days · $ · purchases · CPA · by <actor>` plus one line per creative | **text** |
| Comment | a table with one row per ad | text |
| Checklist "Ad performance" | one item per creative with its numbers | text |

The numbers used are: **spend, purchases, CPA, CTR, CPC, impressions**
(`ad_summary`, `ad_line`).

## 2. Problems found

### P1 — purchases are wrong, and you cannot see it (critical)

Meta returns the purchase fields as `omni_purchase` and `cost_per_omni_purchase`.
`kill_sync.py` reads `purchases` and `cost_per_purchase`.

In the 2026-09-09 test run the file happened to contain both names, but only
because the bridge model noticed the problem and added aliases by itself. The
prompt does not ask for that, and `kill_sync.py` does not do it. So on a normal
run:

- every killed ad reports **0 purchases**
- CPA falls back to `spend / purchases` → division by zero → `n/a`

This is the worst kind of bug: the number is **wrong, not missing**, so nothing
looks broken. Fix this before anything else on this list.

### P2 — no ROAS and no revenue

Meta is never asked for purchase value, so ClickUp never sees revenue or ROAS.
ROAS is the number the team actually judges creative on (the blog winners job
uses `ROAS >= 2`). Right now you cannot answer "was this batch profitable?"
from the ClickUp task.

### P3 — metrics are text, so you cannot sort or filter

Both lists have 34–38 custom fields, but **none** is a number field for
performance. There is no Spend, Purchases, CPA or ROAS field. Everything sits
inside the `Result` text field.

That means you cannot sort the Media list by spend, filter for "CPA under $30",
or group by result. The data is in ClickUp but not usable as data.

### P4 — no video metrics

For video creative the team needs hook rate (3s views ÷ impressions) and hold
rate (thruplay ÷ impressions). Neither is requested or written. For a video
batch, CTR and CPC do not explain why it failed.

## 3. Plan

### Phase 1 — fix the purchase field names — ✅ DONE 2026-09-09

In `kill_sync.py`, read the Meta name first and keep the old one as a fallback:

```python
purch = count(m.get("omni_purchase") or m.get("purchases"))
cpa   = m.get("cost_per_omni_purchase") or m.get("cost_per_purchase")
```

Touches `ad_summary`, `ad_line`, and the ad-set totals near line 481.
Add a test with a real Meta row so a future rename is caught.

**Do this first.** Every other phase produces wrong numbers until it is done.

### Phase 2 — ask Meta for more metrics — ✅ DONE 2026-09-09

Extend the `fields` list in `scripts/kill_sync_prompt.md` step 2:

| Metric | Why |
|---|---|
| purchase value / revenue | needed for ROAS |
| `purchase_roas` | the main decision number |
| `reach`, `frequency` | shows fatigue |
| `cpm` | shows auction cost, separates "bad creative" from "expensive audience" |
| `inline_link_clicks` | real link clicks, not all clicks |
| video: 3s views, thruplay, p25/50/75/100 | hook rate and hold rate |

**Research step first:** confirm which of these `ads_get_ad_entities` really
supports on this account. Do not assume. Ask for a small set, print what comes
back, then add the ones that work. Metrics that Meta does not return must print
`n/a`, never `0` — that is the P1 lesson.

### Phase 3 — real number fields in ClickUp — ✅ DONE 2026-09-09

Create number fields so the data can be sorted and filtered. The v2 API can do
this: `POST /api/v2/list/{list_id}/field` with `{"name","type","type_config"}`.

Suggested fields, all at **batch level** (the whole ad set, summed):

| Field | Type |
|---|---|
| Spend | number (currency) |
| Purchases | number |
| Revenue | number (currency) |
| ROAS | number |
| CPA | number (currency) |
| CTR | number |
| Days Live | number |

Per-creative detail stays where it is now, in the checklist and the comment.
A number field holds one value, and a batch has many ads, so only the batch
total belongs there.

Then extend the kill path to fill these fields next to the `Result` text.

**Two things to check before building:**
1. Most of the 26 brief fields on this folder are **folder-level**, shared with
   the Creative Team's other lists. A new field must not disturb their views.
   Decide list-level or folder-level with the team first.
2. Dropdown options cannot be edited after creation (`PUT /field/{id}` → 405).
   Number fields have no options, so this is safe here — but get the **names**
   right the first time, because renaming is the easy part and re-typing values
   is not.

### Phase 4 — backfill — ✅ DONE 2026-09-09

The 25 batches already `killed` in ClickUp have no numbers in the new fields.
A one-off script can read their metrics from Meta and fill them. Only worth
doing if the team wants history in the new views.

## 4. Decisions needed from Martijn

1. Which metrics become real ClickUp fields? (my suggestion is the 7 above)
2. List-level or folder-level fields? (folder-level = the Creative Team sees
   them too)
3. Backfill the 25 old batches, or start from today?

## 5. Order of work

```
Phase 1  fix purchases        ← do this now, it is small and it is wrong today
Phase 2  more metrics from Meta  (research first, then build)
Phase 3  ClickUp number fields   (needs decisions 1 and 2)
Phase 4  backfill                (optional)
```


## 6. Added after the first build

**P5 — archived ads return no metrics.** On 2026-09-09 the 12 metric rows for
S002/S003 came back `ARCHIVED` with empty spend and purchases: Meta serves no
lifetime metrics for archived ads. It did not matter there (both were suppressed
by the replacement guard), but a batch that is killed *and* archived before the
next run lands in ClickUp with `n/a` everywhere. Honest, but empty.

Worth considering: capture metrics at kill time rather than at report time, or
shorten the gap by running more often. Not urgent — a kill is normally reported
within hours, well before anyone archives it.

**Field names confirmed 2026-09-09** via `ads_get_field_context`:

| Metric | Meta field | Note |
|---|---|---|
| CPA | `cost_per_omni_purchase` | alias `cost_per_purchase` |
| Outbound CTR | `outbound_clicks_ctr` | |
| Adds to cart | `omni_add_to_cart` | alias `adds_to_cart` |
| CPM / CPC | `cpm` / `cpc` | |
| ROAS | `purchase_roas` | `website_purchase_roas` as fallback |
| Conversion value | `omni_purchase_values` | alias `purchases_conversion_value` |
| Average conversion value | *(none)* | computed: value ÷ purchases |

`conversion_value`, `purchase_value`, `roas`, `aov` and `outbound_ctr` do **not**
exist — do not guess field names, ask `ads_get_field_context`.


## 7. Phase 3 as built (2026-09-09)

9 **folder-level** fields on `[VN] - Tasks` (901814791421): Ad Spend, Purchases,
CPA, Outbound CTR, Adds to Cart, ROAS, CPM, CPC, AOV. Ids in
`kill_sync_config.json` under `clickup.metric_fields`.

Three things learned doing it:

1. **`POST /api/v2/folder/{id}/field` works** and is undocumented. Folder-level
   was necessary: an automation moves batches from Launch Manager into Media, and
   list-level fields are per-list, so the values would be lost on the move.
2. **`DELETE /api/v2/field/{id}` returns 405** — a custom field cannot be removed
   through the API. Name it right the first time. (A probe field named
   `ZZ Kill-sync probe` was created during this work and has to be deleted in the
   UI.) Field *values* on a task DO delete fine:
   `DELETE /task/{id}/field/{field_id}` → 200.
3. **New fields do not appear in existing saved views** (checked on By Department
   and Launch It), and each view can hide columns individually, so adding
   folder-level fields does not disturb the Creative Team's boards.

## 8. Phase 4 as built (2026-09-09)

**25 of the 28 `killed` tasks were backfilled** from Meta ad-set rows
(`level: adset`, `date_preset: maximum`, matched to ClickUp by `S###` prefix).
Meta returned `omni_purchase` / `cost_per_omni_purchase`, confirming the P1 fix
was needed — without it every backfilled CPA would have been blank.

Only the metrics Meta actually reported were written, so most rows got 4-8 of the
9 fields rather than all 9. Those gaps are real: these batches were killed
early, and 15 of the 25 recorded **no purchase at all**, so CPA / ROAS / AOV do
not exist for them. Writing 0 there would have invented a result.

Three were skipped:

| Batch | Why |
|---|---|
| S001 | not in Meta any more — matches the `[AI 2026-08-25]` "Not attributable" note already on the task |
| S055 | in Meta but **`ACTIVE`** with no spend |
| S058 | in Meta but **`ACTIVE`** with no spend |

**S055 and S058 are worth a human look**: they are `killed` in ClickUp but still
`ACTIVE` in Meta. That is the opposite of the mismatch this tool looks for — the
board says dead, the account says running — and kill-sync will never catch it,
because it only reads *pause* events. A "ClickUp says killed but Meta says
active" check would be a separate, useful job.


## 9. Correction, same day: the fields moved to Media only

Phase 3 shipped the nine fields **folder-level** on `[VN] - Tasks`, reasoning that
values had to survive the automation that moves a batch from Launch Manager into
Media. Checked afterwards: **all 28 killed tasks were already in Media**. The move
happens at launch, long before any kill, so that risk was theoretical.

The cost was not. A folder-level field shows in the task detail panel of *every*
task in the folder, so all 34 Creative Team tasks and all 14 Format Radar tasks
grew nine permanently-empty metric fields. That cannot be hidden: the fields
appeared as columns in **zero** of the folder's 24 views, so view settings had
nothing to switch off — the detail panel lists every field the task's list has.

There is no per-list opt-out for a folder-level field, and the API cannot rename,
re-scope or delete a field (all four endpoints 405). The only route was a human
deleting the ten fields in the UI, then recreating them list-level on Media and
writing the values again — which is what happened.

**Lesson: pick a custom field's scope as carefully as its name. Both are
permanent as far as the API is concerned.**
