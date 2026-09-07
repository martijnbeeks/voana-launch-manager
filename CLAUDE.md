# Voana Launch Manager

This repo is the operating manual + tooling for launching new Voana ads on Meta.
Claude acts as the **launch manager**: it prepares and launches new creative
batches, following the system described below.

**Scope: launching only.** Media buying — budget changes, pausing losers,
promoting winners to scaling campaigns, graveyard imports — is handled by other
people. Claude never touches existing ad sets/ads; it only creates new ones.

**Product context:** Voana anti-chafing stick. Core audience is women 45+.
Proven angles: rash/chafing relief and GLP-1 loose-skin chafing. Markets: US, CA, UK.

---

## Ad account map (Meta)

| Account | ID | Role | Currency |
|---|---|---|---|
| CLN_0034_Kandy_Voana (CLNG4) | `758865990548177` | **Main media-buying account.** All testing + scaling happens here. | USD |
| GetVoana - 1 (Voana) | `1247693024067639` | **Second live test account** (since 2026-09-06). Runs `ABO: Test Campaign USA V2` (`120247451142810591`, the S-series batches from ClickUp, $20/day per ad set), the `KATECHON X VOANA \| $10 \| ABO` campaign (active, ad set `KAT020` at $60/day) and mirrored `14-07-26 \| Scaling campaign US/CA/UK` CBOs. Same pixel as the main account. **Image-upload MCP tools are not enabled here** — see "Launching into GetVoana - 1". | USD |
| [NOVA] getvoana.com (InovaInteraktif) | `901583842399904` | Engagement/social-proof farming ("NEW PPE - US", ~€1/day PPE ad set with duplicated post ads). | EUR |
| CLA_0011_Kandy_Voana (Clikim Apex) | `814663791667175` | Idle — no active campaigns. | USD |

Always use the Meta Ads MCP (`mcp__meta-ads__*`) for reads and writes. Reuse one
`client_conversation_id` per session. Filter with `effective_status IN [ACTIVE]`
when asking "what's running".

---

## Campaign architecture (main account)

The account runs a three-stage **test → scale → graveyard** system. All
campaigns optimize for OFFSITE_CONVERSIONS (purchases), Highest Volume bidding.

1. **Testing — `ABO: Test Campaign USA`** (`120254492561360562`)
   - One ad set per creative batch, budget at ad set level ($20–$130/day, typical $20–$60).
   - 3–6 numbered creative variants per ad set (ads named `1`, `2`, `3`, …).
   - New batches launch almost daily, usually starting at midnight account time (ET).

2. **Scaling — one CBO campaign per geo** (winners get duplicated in):
   - `14-07-26 | Scaling campaign US` (`120254415421110562`) — ~$175/day
   - `14-07-26 | Scaling campaign CA` (`120254426770060562`) — ~$180/day
   - `14-07-26 | Scaling campaign UK` (`120254427096840562`) — ~$390/day
   - Weekly winner roll-ups are launched **simultaneously in all three geos**
     under a shared name, e.g. `Week_33_Winners` (launched 2026-08-15).

3. **Graveyard — `GRAVEYARD - ALL`** (`120254596528760562`) — ~$1,250/day
   - Single broad ad set (`GRAVEYARD_OG_ADV_SP_COC_CA-US-UK`, CA/US/UK combined).
   - Proven winners are *copied* (never moved) into it and accumulate there.

Promotion flow: test batch → geo scaling campaigns → graveyard. Ads are always
duplicated ("- Copy", "imp"/"imported" suffixes), never moved, so history stays intact.

---

## Naming conventions

### Test ad sets
`#<batch> - <creator> | <DD-MM-YY>` or `#<batch> (Assignment N - <initials>) | <DD-MM-YY>`
- Examples: `#563 - Wasif | 15-08-26`, `#527 (Assignment 1 - JLS) | 13-08-26`.
- Batch numbers are globally incrementing (currently in the ~#400–580 range).
- Known creators/sources: Wasif, AMV, DLS, JS, JLS, DM.

### Creative code (full descriptor)
`B150_V3_VID_Stick_W26_GL_AGN_RASH_Woman45+_Chafing_UMS_Unaware_SpeechAI_L2_QA_Imitation_None_Kandy`

Decoded (best current understanding — confirm/extend as we learn):
| Segment | Meaning | Observed values |
|---|---|---|
| `B150` | Creative batch ID | B43, B99, B145, B150, B151 |
| `V3` / `C1` | Variant / cut | V1–V3 |
| `VID` / `LFS` | Format | VID (video), LFS (long-form static) |
| `Stick` | Product | Stick |
| `W26` | Production week | W18–W27 |
| `GL`, `AM`, `AGN` | angle/source codes (TBD — confirm) | GL, AM, AGN, GL_GL |
| `RASH` | Hook/problem | RASH |
| `Woman45+` | Audience | Woman45+ |
| `Chafing`, `UMP` | Pain point | Chafing, UMP, LooseSkinGLP |
| `UMS` | TBD — confirm | UMS |
| `Unaware` … | Awareness stage | Unaware, Problem Aware, Solution Aware |
| `SpeechAI` … | Production method | SpeechAI, SpeechDF, AiAvatar, StoryVSL, LFStatic |
| `L2` | Landing page / level | L2 |
| `QA`, `Authority` | Persuasion mechanism | QA, Authority, rankingauthorityFigure, TooExpensive |
| `Imitation` | Style | Imitation |
| `Kandy` | Agency/partner | Kandy |

### Scaling ad sets
`SCALE_<asset or page>` (e.g. `SCALE_7_REASONS_WHY`, `SCALE_COMPARISON_V2`,
`SCALE_OG_V1`, `SCALE_PAGE18_SMOOCHE`, `SCALE_LEAD_7`) — these test landing
pages/advertorials as much as creatives. Winner roll-ups: `Week_<n>_Winners`.

Current winning creative families: **"7R" (7 Reasons Why)**, **"Comp"
(comparison)**, and the **B150/B151 video** series.

---

## Launch template

**Two launch queues exist — check which one the request refers to:**
1. **The launch Google Sheet** (below) → `BATCH #nnn` batches → main account `758865990548177`.
2. **ClickUp "Launch Manager"** → `S0nn` batches → GetVoana - 1 `1247693024067639`
   (see "ClickUp pipeline (S-series)" further down).

**Source of truth for sheet batches: the launch Google Sheet**
<https://docs.google.com/spreadsheets/d/1P2ILJvzxSBUx5aeqEQJ2-xFyVAkeJkzDP7QcIkqDvg0/>
Every launch is driven by rows in that sheet; Claude reads the sheet, launches
what it specifies, and writes launch results back. Cadence: a target of X ad
sets per day (X set per week by Martijn), launched in weekly planning cycles.
Readable without auth via `export?format=xlsx` (or `format=csv` per tab).
Read/write cell access via Composio: `composio execute GOOGLESHEETS_BATCH_GET`
/ `GOOGLESHEETS_BATCH_UPDATE` (googlesheets connection is linked). After each
launch: set the Ad Roadmap row's STATUS to "Working", fill PAGE and AD ACCOUNT,
and write a launch note in the 📊 Log tab on today's date row (column B).

### Sheet structure (key tabs)
- **`Ad Roadmap`** — the launch queue. One row per batch. Key columns:
  `STATUS` (launch only rows marked **"Ready for Launch"**; other values:
  Not started / Working / Learning / Done), batch label (col C, e.g.
  `BATCH #443`), `DATE ADDED`, `AUTHOR` (Aris/Jurre = strategists),
  `EDITOR`/creator, `FILE TYPE` (IMG / VID / LFS / CAR / UGC),
  `LINK TO BRIEF`, `LINK TO AD` (the creative, e.g. "BATCH #443 - REN"),
  `URL`, `PAGE`, `AD ACCOUNT`, `RESULT` (KPI Winner / Spend Winner / Loser /
  Retest), `LEARNINGS`. Sheet batch numbers match the `#<batch>` ad set names
  in the ad account.
- **`Info`** — Triple Whale UTM template + page→account mapping (below).
- **`📊 Log`** — daily Meta ads log with weekly recaps; write launch notes here.

### URL rules (per Ad Roadmap row)
1. **URL empty** (the common case): launch the ad to BOTH defaults —
   `https://offer.getvoana.com/comparison2` and
   `https://offer.getvoana.com/7-reasons` — i.e. generate **2 versions of the
   same ad**, one per URL, named `<n> - Comp` and `<n> - 7R`.
2. **URL filled with a link**: use only that URL (single version).
3. **`HOLD UMS: <product>`** (e.g. Voana 3-shield / Seal / Bare / Renew /
   Armor / Defense): do NOT launch — held for a new-product funnel that isn't
   live yet.
4. **Any other note** (e.g. "put in CBO!", "Ask Aris/Jurre…"): follow the
   instruction or ask before launching.

### Tracking (required on every link URL)
Append the Triple Whale UTM parameters:
`?tw_source={{site_source_name}}&tw_adid={{ad.id}}`

All new ads are uploaded to **`ABO: Test Campaign USA`** (`120254492561360562`)
in the main account. One new ad set per creative batch, created just before
midnight ET with `start_time` at 00:00.

**Per-launch inputs** (everything else is fixed):
- Batch number (globally incrementing, ~#400–580 range as of Aug 2026)
- Creator (Wasif, AMV, DLS, REN, JS, JLS, DM, Peejay, Patrick, ML, Pitah, …)
- 3–6 video creative variants
- Facebook page from the roster below (matched to the creative's persona)
- **Advertorial URL(s)** from the landing-page roster below — set as `link_url`
  on `ads_create_creative`, per ad. Baked into the post at creation; immutable
  afterwards. Optionally a `display_link` (see display domains below).

**Ad set spec (fixed):**
- Budget: $20/day default ($25 occasionally; never higher at launch)
- Optimization: OFFSITE_CONVERSIONS, Highest Volume bidding
- Targeting: US only, age 18–65, all genders, no interests/custom audiences,
  Advantage+ audience ON (age + gender expansion enabled)
- Placements: Advantage+ (all platforms/positions, mobile + desktop)
- Brand safety: relaxed (Facebook / Audience Network / Feed)

**Copy pairing rule:** the copy doc is always paired 1:1 with its same-numbered
image — image 1 gets "ad copy 1", image 2 gets "ad copy 2". Never cross-combine
images and copies into a test matrix.
**Standing override (Martijn, 2026-08-23 and again 2026-09-07):** when a folder
has fewer copy docs than images (typically "AD COPY 1" long + "AD COPY 2" short),
give every ad **both** texts as Meta "multiple primary text" options:
`creative.asset_feed_spec = {"optimization_type":"DEGREES_OF_FREEDOM","bodies":[{"text":…},{"text":…}]}`
next to a normal `object_story_spec.link_data` (pass the whole creative JSON to
`ads_create_ad`; `ads_create_creative` cannot express this). Body = the full doc
including its bold title line, paragraphs separated by a `⠀` spacer line;
headline = the title line of AD COPY 1.

**Ad spec (fixed):**
- Video page-post ads with linked Instagram media; ads named `1`, `2`, `3`, …
- When the default two-URL rule applies, name the versions with the landing-page
  suffix, matching existing account convention: `<n> - Comp` (→ /comparison2)
  and `<n> - 7R` (→ /7-reasons)
- Headline = the hook (e.g. "Skin Fold Rash Keeps Coming Back")
- Primary text: long-form DR copy — problem → mechanism → product → proof block
  (21,000+ customers · 4.9/5 · free shipping · 90-day money-back guarantee) → "Tap below"
- CTA button: Learn More
- Display link (`link_data.caption`): **WWW.NO-SKIN-RASH.COM** (always — masks
  the offer.getvoana.com destination). Immutable after publish, so set it at
  creation.
- Creative library name: `<Hook Title> <YYYY-MM-DD>-<hash>` (auto-generated)

**Pixel & attribution** (confirmed via first launch, batch #415):
promoted_object = English Health Pixel `642125018433390`, custom_event_type
PURCHASE; attribution 7-day click + 1-day view (account default). Launch flow:
entities are created PAUSED → activate ad set + every ad right after creation;
the scheduled midnight start_time gates actual delivery.

**Image hosting for uploads:** Meta's image upload only accepts public URLs
(Drive links fail). Stage creatives in github.com/martijnbeeks/voana-ad-assets
(`batch-<n>/ad<i>.jpg`, converted PNG→JPEG q92 via sips) and pass the
raw.githubusercontent.com URL; append `?v=2` to bust Meta's fetch cache if the
first attempt 404s.
- **Getting the PNGs out of Drive:** do NOT use the Drive MCP `download_file_content`
  (base64 through the model, ~3 MB per image). Use Composio:
  `composio execute GOOGLEDRIVE_DOWNLOAD_FILE -d '{"fileId":"…"}'` returns a
  short-lived `downloaded_file_content.s3url`; `curl` that to disk. Copy docs are
  fine via the Drive MCP `read_file_content`.
- **If `ads_creative_upload_media` / `ads_creative_upload_image` answer "gradually
  rolled out"** (true for GetVoana - 1), skip the library upload and put
  `"image_url": "<raw.githubusercontent URL>"` at the **top level** of the
  `creative` JSON in `ads_create_ad`; Meta fetches it at creation.

### Workflow
1. **Gather** the per-launch inputs above; default to next batch number,
   $20/day, next midnight ET start.
2. **Name** ad set and ads per the conventions above.
3. **Confirm with the user before any write.** Show a launch summary first.
4. **Launch** via MCP (`ads_create_ad_set`, `ads_create_creative`, `ads_create_ad`).
5. **Log** the launch in `launches/` so this repo becomes the source of truth
   for launch history.

### Guardrails
- Launching only: **never** pause, delete, or change budgets on existing
  campaigns/ad sets — that is the media buyer's territory.
- Test ad sets launch at $20–$25/day; flag any request outside it.
- All writes go to the main account (`758865990548177`) unless told otherwise.
- Surface anomalies proactively (e.g. dormant campaigns spending nothing,
  future-scheduled batches, budget outliers).

---

## Landing pages (advertorials)

All on `offer.getvoana.com`. Confirmed roster (from Martijn, 2026-08-17):

| URL | Codename in account |
|---|---|
| `https://offer.getvoana.com/intertigo-stick-og/lander` | OG (`SCALE_OG_V1`, "Batch 356/360 OG") |
| `https://offer.getvoana.com/7-reasons` | 7R (`SCALE_7_REASONS_WHY`) |
| `https://offer.getvoana.com/comparison2` | Comp (`SCALE_COMPARISON_V2`) |
| `https://offer.getvoana.com/comparison-listicle-v2-page13` | Page 13 (see also "Page 14" ad sets) |
| `https://offer.getvoana.com/lead7` | LEAD7 (`SCALE_LEAD_7`, `CRO - PAGE TEST - LEAD7`) |
| `https://offer.getvoana.com/5-possible-fixes/page-18` | Page 18 / SMOOCHE (`SCALE_PAGE18_SMOOCHE`) |

Also observed in the account (not in the confirmed roster — ask before using):
`/BWADV` (test-campaign advertorial), `/directSP` (direct sales page, graveyard).

**Display domains** (shown on the ad instead of the real destination):
`WWW.NO-SKIN-RASH.COM` and `www.skin-fold-issues.com` — both used on the same
landers as an apparent A/B test.

---

## Facebook pages (ad publishing roster)

**Page selection rule (current policy, Martijn 2026-08-17):**
- **BOF ads (bottom of funnel — product visible/branded) → launch from the
  `GetVoana` (brand) page.**
- **All TOF and MOF ads → launch from the persona pages**, picked by ad style
  using the style mapping below.

Older per-style mapping from the sheet's `Info` tab (still used to pick WHICH
persona page for non-TOP ads; the account column is outdated — see note):

| Page | Style fit |
|---|---|
| Linda Ramos | All other / catch-all |
| Woman Health Magazine | Branded, magazine style |
| Skin Health with Dr. Carter | Curiosity, TOF ads |
| Daisy Cottons | Native |

Note: the Info-tab account mapping predates the move to the Kandy accounts —
recent sheet batches (#415+) run on `CLA_011_Kandy_Voana` / `CLN_0034_Kandy_Voana`,
and all pages below are linked to the main Kandy account. When in doubt, the
Ad Roadmap `AD ACCOUNT` column wins; default to the main account.
The sheet also references "Dermatology with Debra" — not in the roster below;
ask before using.

Ads publish through persona/advertorial pages, chosen per launch:

| Page | ID |
|---|---|
| GetVoana (brand) | 1043046762230248 |
| Skin Health with Dr. Carter | 969944376210163 |
| Daisy Cottons | 981390358400642 |
| Linda Ramos | 1053824494477778 |
| Woman Health Magazine | 882932054906497 |
| Dr. Daniel Hart | 1239878865878372 |
| Ask Dr. Claire | 1064169246783168 |
| Glenda Ford | 1137359786138189 |
| ClikGlobal | 1128763686997383 |
| FinnDore | 1153248104536863 |

### Known open items (as of 2026-09-07)
- ~~`KATECHON X VOANA | $10 | ABO` has zero active ad sets~~ — resolved: ad set
  `KAT020 … Katechon Engine v2` runs at $60/day.
- `Batch 399 - 7R, Comp` is scheduled to start 2026-09-08 ($20/day) — verify
  this is intentional.
- Named-brand comparative imagery (Johnson's, Vaseline, Gold Bond, Desitin,
  Sudocrem, Boots, Superdrug…) keeps shipping without legal review — flagged in
  every launch log since 2026-08-23; still Martijn's call each time.
- ClickUp S063 is parked: its Landing Page URL field lists two pages and the
  copy docs point to a third (`/intertigo-stick-og/sp`). Needs a decision.

---

## ClickUp pipeline (S-series) — added 2026-09-07

Since early September the creative team plans in ClickUp, not the sheet. Martijn
supplies a personal API key per session (header `Authorization: <key>`, plain
REST, `https://api.clickup.com/api/v2/…`); never commit the key.

| Thing | Where |
|---|---|
| Workspace | Syndesmos — team `90182798127` |
| Space | VOANA (Brand) — `901811531882` |
| Folder → list | `[VN] - Tasks` → **Launch Manager** — list `901819973363`; only view is "Channel Tasks" (`2kzn0jtf-3038`). Sibling lists: Creative Team, Media. |
| Statuses | `ready for launch` → `launched` → `complete` |
| Task name | `S0nn - C1-Cn - <Image\|Video\|LFS\|GIF> - Stick - <Creative Director> - <Editor> - <angle> - <avatar> - <UMP> - <UMS> - <Awareness> - <Static\|…> - <Iteration\|Ideation\|Imitation> - <Variation ID>` |
| Key custom fields | `Landing Page URL` (short text — can hold 2 URLs or be empty), `Google Drive Folder Link`, `Page` (dropdown of the FB pages), `Launch Date`, `Batch number *`, `Ad Format *`, `Number of Ads in Batch` |
| Drive folder contents | `…_C<n>.png` per creative (1080×1080) + Google Docs `AD COPY 1` (long) / `AD COPY 2` (short) — usually only two docs regardless of image count |

**How to read it:** `GET /list/901819973363/task?include_closed=true` (38 tasks,
single page) — dropdown values come back as `orderindex`, resolve them against
`GET /list/{id}/field`. "First N in the view" = `GET /view/2kzn0jtf-3038/task`
order, which is by batch number, not the list API's `orderindex`.

**Launch spec for S-batches** (differences from the sheet template):
- Account GetVoana - 1, campaign `ABO: Test Campaign USA V2` (`120247451142810591`) —
  Martijn chose to keep using it rather than open a new ABO (2026-09-07).
- Ad set name = the full ClickUp task name. Ad name = task name with the lander
  code inserted after the batch id and `_C<n>` appended:
  `S070 - COMP - C1-C3 - … - None_C1` (codes seen: `OG`, `7R`, `COMP`, `PG8`).
- Single-URL rule: if `Landing Page URL` has one link, launch one version per
  image to that link (no default two-URL split). Empty field → ask. Two links
  in the field, or links that contradict the copy docs → **skip the batch** and
  take the next one (S063 precedent).
- Page: the ClickUp `Page` field is usually empty. Pick by style and by precedent
  in the same campaign: magazine/"independent review"/"tested" statics →
  **Woman Health Magazine**; curiosity statics (Aris `the_cycle` iterations) →
  **Skin Health with Dr. Carter**; long-form story LFS → Linda Ramos / Glenda Ford.
  Write the choice back into the `Page` field.
- Everything else as the fixed ad-set/ad spec above ($20/day, midnight ET
  start, pixel `642125018433390` PURCHASE, WWW.NO-SKIN-RASH.COM, UTMs).
- Previous S-batches in that campaign (S002–S014, 2026-09-06) were launched with
  **immediate** start; check `start_time` on existing sets before assuming the
  midnight slot is free — on 2026-09-07 it already held S016 from someone else.

**Lists and the auto-move:** `Launch Manager` (`901819973363`, statuses ready for
launch → launched → complete) is only the queue. A ClickUp automation moves a task to
the **Media** list (`901819973378`) the moment it becomes `launched`, where it lands as
`learning`; Media's outcome statuses are `killed` / `winner` / `has potential` /
`rejected` / `complete`. So after launch, look for the task in Media, not Launch Manager.
Custom fields (`Status`, `Result`, `📖 Learnings`, `Launch Date`, `Page`) are shared
across both lists with identical IDs (see `scripts/kill_sync_config.json`).

**Write-back after launch:** `PUT /task/{id}` `{"status":"launched"}`, then
`POST /task/{id}/field/{field_id}` for `Launch Date` (ms epoch), `Page`
(option UUID) and any corrected `Batch number *` (S070's said "S067"). Also add
the note to the sheet's 📊 Log row and the `launches/` file as usual.

---

## Required connections

Every launch uses these four connections — verify all are live before launching:

| Connection | Used for | Quick test |
|---|---|---|
| **Meta Ads MCP** (`mcp__meta-ads__*`) | All ad-account reads/writes: image upload, ad set + ad creation, activate/pause | `ads_get_ad_accounts` should list CLN_0034_Kandy_Voana (`758865990548177`) |
| **Google Drive MCP** (claude.ai connector) | Reading creative folders, downloading images + copy docs (files are private to the team) | Drive search for "VOANA - Growth Guide" |
| **Composio → Google Sheets** (`googlesheets`) | Reading Ad Roadmap rows, writing status + 📊 Log updates | `composio execute GOOGLESHEETS_BATCH_GET` on the sheet |
| **GitHub CLI** (`gh`) | Pushing creatives to `martijnbeeks/voana-ad-assets` (public) — Meta image upload needs a public URL, Drive links fail | `gh auth status` |
| **Composio → Google Drive** (`googledrive`, linked) | Downloading creative PNGs without base64 through the model | `composio execute GOOGLEDRIVE_DOWNLOAD_FILE -d '{"fileId":"…"}'` |
| **ClickUp REST API** (personal key from Martijn, per session) | S-series launch queue reads + status/field write-back | `curl -H "Authorization: $CU" https://api.clickup.com/api/v2/team` lists "Syndesmos" |

Local (no auth): macOS `sips` converts PNG→JPEG q92 before staging.

### Setup for a new operator
1. **Access first** (ask Martijn): Meta Business access to the ad account(s) and
   Facebook pages; the Growth Guide sheet + creative Drive folders shared to your
   Google account; write access to `martijnbeeks/voana-ad-assets` (or create your
   own public assets repo and update this doc).
2. **Meta Ads MCP**: add Meta's ads MCP server to Claude Code and complete its
   OAuth with a Meta login that has advertiser access to the account.
3. **Google Drive**: connect the Google Drive connector (claude.ai connectors)
   with the Google account the sheet/folders are shared with.
4. **Composio**: `composio login`, then `composio link googlesheets` — on
   Google's consent screen, tick the spreadsheet permission (missing scope is
   the common failure; fix with `composio connections remove googlesheets` and
   re-link). Optional: `composio link googledrive` to make image downloads
   lighter than the Drive MCP route.
5. **GitHub**: `gh auth login` with an account that can push to the assets repo.
6. Verify with the quick tests above, then follow the Launch template.

---

## Kill-sync (Meta pause → ClickUp + Discord) — runs on Martijn's Mac

Twice daily (08:45 / 20:45 local, LaunchAgent `com.voana.kill-sync`) a headless
Claude run follows `scripts/kill_sync_prompt.md`: it reads both test accounts'
activity logs via the Meta MCP, writes the human pause events (run_status 1→7,
not by "Meta", not our own 17→7 launch flow) plus lifetime metrics into
`state/inbox/`, then `scripts/kill_sync.py process` does the deterministic part:
- ad killed → comment on the Media task with actor, days live, spend, purchases,
  CPA, CTR, CPC and what is still running; Discord embed.
- whole ad set killed → task status `killed`, `Status` = Losing Ad (only if it
  was empty / Not Tested / In Testing), `Result` = one-line summary, comment with
  the per-ad table, Discord embed asking for 📖 Learnings. Never touches Learnings
  and never downgrades winner / has potential / complete.
- morning run also posts a digest of killed batches still without learnings.
Files: `scripts/run_kill_sync.sh` (wrapper, alerts the ops webhook on failure),
`scripts/com.voana.kill-sync.plist`, `state/kills.jsonl` (append-only ledger,
dedupes re-runs), `state/last_run.json` (poll window), `state/kill-sync.log`.
Secrets in `.env` (gitignored): `CLICKUP_API_KEY`, `KILL_SYNC_DISCORD_WEBHOOK_URL`
(falls back to the ops webhook in `../voana-tools/.env` until set). Test with
`KILL_SYNC_DRY_RUN=1 zsh scripts/run_kill_sync.sh`. Full design: `docs/kill-sync-plan.md`.
Guardrail: the job is **read-only on Meta**.

## Repo layout (target)

```
CLAUDE.md            # this file — the operating manual
launches/            # one file per launch: YYYY-MM-DD-batch-<n>.md (batch, creator,
                     #   creatives, budget, ad set / ad IDs, links)
docs/                # design docs (kill-sync-plan.md)
scripts/             # kill_sync.py + prompt + launchd wrapper/plist
state/               # kill-sync ledger + poll window (inbox/ and log are gitignored)
creatives/           # briefs & metadata for creative batches (not raw video files)
scripts/             # helper scripts (batch launch, winner promotion, reporting)
reports/             # generated performance snapshots
```

Conventions for this repo: keep launch logs append-only; record Meta entity IDs
in every log so performance can be pulled later without searching.
