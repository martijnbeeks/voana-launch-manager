# Plan — full metrics in ClickUp when a batch is killed

Status: **phases 1 and 2 built 2026-09-09** (see git log). Phase 3 (real ClickUp
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

### Phase 3 — real number fields in ClickUp

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

### Phase 4 — backfill (optional)

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
