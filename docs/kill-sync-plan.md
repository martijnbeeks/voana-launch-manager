# Kill-sync: Meta ad kills → ClickUp status + Discord alert

Status: **built 2026-09-07, runs locally** — Martijn chose this Mac over GitHub Actions, twice daily. Implementation notes at the bottom; §2–§3 describe the original GitHub-Actions design. Goal: when a media buyer pauses ("kills") a test
ad or ad set in Ads Manager, the matching ClickUp Launch Manager task gets updated
automatically and the team gets a Discord message with the numbers, so the learnings
(format, angle, avatar, awareness, page, lander) can be filled in by hand while the
context is fresh.

## 1. What the research showed

| Question | Finding |
|---|---|
| Can Meta push a status change to us? | **No.** Marketing API webhooks do not cover ad/ad-set status. Automated Rules can only e-mail. So: **poll**. |
| Where does Meta record a kill? | `GET /act_{id}/activities` (the Ads Manager "History" page). Each event has `event_type` (`update_ad_run_status`, `update_ad_set_run_status`), `actor_name` (e.g. "Kuresingh Wakhala", "Martijn Beeks", "Meta"), `application_name` ("Power Editor", "ads MCP server"), `object_id`, `object_name`, `event_time`, and `extra_data.run_status {old_value,new_value}`. Codes seen: 1 = active, 7 = paused/inactive, 9 = pending review, 17 = in process. Human-readable values are localized (Dutch on this account) → always use the numeric codes. |
| Simpler alternative? | Poll `/act_{id}/ads?fields=id,name,status,effective_status,adset_id,updated_time` and diff against the last snapshot. Deterministic, catches everything, but loses *who* did it. **Use both**: snapshot diff decides "killed", activity log adds the actor. |
| Which accounts? | Both. ClickUp S-batches run in GetVoana - 1 (`1247693024067639`, S002–S016, S066–S070) **and** in CLN_0034 (`758865990548177`, S065/S074/S075). Sheet `#nnn` batches only in CLN_0034. |
| How do we map an ad to a ClickUp task? | Ad set name == ClickUp task name for S-batches (exact string). Ad name = task name with `OG/7R/COMP/PG8` inserted + `_C<n>` / `_V<n>` suffix → gives creative number and lander. Fallback: match on the `S0nn` prefix. |
| ClickUp write targets | Task status (`ready for launch` → `launched` → `complete`); custom fields `Status` (dropdown: Not Tested / In Testing / Losing Ad / Has Potential / Winning Ad / Super Winner), `Result` (text), `📖 Learnings` (short text), `Launch Date` (date). Dropdown **options cannot be created via API** — any new option/field is added once by hand in ClickUp. |
| Discord | Plain **incoming webhook** on the channel (Server settings → Integrations → Webhooks). No bot needed. Composio `DISCORDBOT_EXECUTE_WEBHOOK` exists but adds nothing over a `POST` to the webhook URL. |
| Off-the-shelf triggers? | Composio has no Meta Ads triggers; ClickUp triggers exist but go the wrong direction. Make.com could do it but its Facebook Ads module has no activity-log/status-change trigger, so it would be an HTTP module + data store poller — same logic, harder to version. |
| Where to run it | This repo is private on GitHub → **GitHub Actions cron** is free (well under the 2,000 min/month on a 30-min schedule), versioned, and the launch logs already live here. |

## 2. Design (recommended)

```
every 30 min (GitHub Actions cron)
  └─ scripts/kill_sync.py
       ├─ for each account [1247693024067639, 758865990548177]:
       │    ├─ pull all ads in the test campaigns (status, effective_status, adset_id, name)
       │    ├─ diff vs state/ads_snapshot.json  → "killed" = ACTIVE → PAUSED (ad) or ad set ACTIVE → PAUSED
       │    ├─ pull /activities since last run    → actor + app for each killed object
       │    └─ pull /insights (date_preset=maximum) for each killed ad/ad set:
       │         spend, impressions, clicks, ctr, cpc, purchases, cost per purchase, days live
       ├─ ClickUp: find task by ad-set name (list 901819973363, fallback: S0nn prefix)
       │    ├─ AD killed   → comment on task: "C2 killed by <actor> after N days — $spend, X purchases, CPA $y, CTR z%"
       │    │                + append "C2" to custom field `Killed ads` (new short-text field)
       │    ├─ AD SET killed, or all ads in it paused
       │    │              → task status = complete
       │    │                `Status` = Losing Ad   (unless a rule below says otherwise)
       │    │                `Result` = one-line performance summary (spend / purchases / CPA / CTR / days)
       │    │                comment with the full per-ad table
       │    └─ never overwrite `📖 Learnings` — humans own it
       ├─ Discord webhook: one embed per kill (title = task name, fields = actor, days live, spend,
       │    purchases, CPA, CTR, lander, page; buttons: ClickUp task URL + Ads Manager URL)
       │    + a daily 09:00 ET digest: "N ads killed yesterday, M batches still without learnings"
       └─ write state/ads_snapshot.json + state/last_run.json, commit back to the repo
```

### Kill vs. winner detection
A media buyer also pauses a *winner* in the test campaign after duplicating it into a
scaling campaign. Rules, in order:
1. If an ad with the same creative (same `image_hash` / `video_id`) exists ACTIVE in any
   `Scaling campaign` or `GRAVEYARD` → mark `Status = Winning Ad`, not Losing.
2. If the ad set was paused by `actor_name = "Meta"` (review/policy) → `Status` unchanged,
   comment "paused by Meta: <old→new>", Discord message tagged ⚠️ instead of 💀.
3. Pauses by `application_name = "ads MCP server"` with `old_value = 17` (our own
   create-paused-then-activate flow) → ignored.
4. Everything else → `Losing Ad`.
A human can always flip `Status` afterwards; the sync never downgrades a `Winning Ad` /
`Super Winner` / `Has Potential` back to `Losing Ad`.

### What the Discord message looks like
```
💀 S068 — C3 killed by Kuresingh Wakhala (GetVoana - 1)
Live 4 days · $78.40 spent · 1 purchase · CPA $78.40 · CTR 1.9% · CPC $0.83
Lander: /comparison2 · Page: Woman Health Magazine · Format: Image · Angle: rash · Awareness: Problem Aware
→ ClickUp task · → Ads Manager
Still running in this batch: C1, C2, C4
```
Batch-level kill adds the per-ad table and "Fill in 📖 Learnings → <task link>".

### Learnings loop (manual, but nudged)
- Daily digest lists tasks that are `complete` with `📖 Learnings` empty, oldest first.
- Optional later: a weekly Claude routine that reads all `complete` tasks of the week and
  drafts a "what worked by format / angle / avatar / lander" table into the ClickUp
  *Creative Data Sheet* view or the sheet's 📊 Log weekly recap row. Out of scope for v1.

## 3. Implementation steps

| # | Step | Owner | Notes |
|---|---|---|---|
| 1 | Create a **Meta System User token** (Business `618398511980773`, assets: both ad accounts, permission `ads_read`; `ads_management` only if we ever want to write back) | Martijn | Business settings → System users → Generate token, never expires. Store as GitHub secret `META_ACCESS_TOKEN`. |
| 2 | Create a **Discord webhook** on the target channel | Martijn | Secret `DISCORD_WEBHOOK_URL`. |
| 3 | ClickUp: add custom fields `Killed ads` (short text) and `Kill date` (date) to the Launch Manager list; confirm `Status` options stay as they are | Martijn | 2 min in the UI; API cannot add fields. Existing API key → secret `CLICKUP_API_KEY`. |
| 4 | `scripts/kill_sync.py` (Python 3, `requests` only) + `scripts/kill_sync_config.json` (account IDs, test-campaign IDs, ClickUp list/field IDs, scaling-campaign IDs) | Claude | ~300 lines. Idempotent: every kill gets a key `<ad_id>:<paused_at>` stored in state so re-runs never double-post. |
| 5 | `.github/workflows/kill-sync.yml` — cron `*/30 * * * *` + `workflow_dispatch`, commits `state/` back with `[skip ci]` | Claude | Uses the repo's default `GITHUB_TOKEN` for the state commit. |
| 6 | Dry-run mode (`--dry-run`) that prints what it *would* post; run it against the last 7 days of history (there are real kills to replay) | Claude | Verify mapping + numbers before enabling the cron. |
| 7 | Seed the snapshot from the current state so the first live run reports nothing | Claude | |
| 8 | Daily digest job (same script, `--digest`) at 09:00 ET | Claude | |
| 9 | Document in CLAUDE.md (Guardrails: the sync is read-only on Meta) | Claude | |

Effort: steps 4–9 are one working session once 1–3 are in place.

## 4. Decisions needed from Martijn

1. **Scope v1**: S-batches in both accounts only (ClickUp), or also the sheet `#nnn`
   batches in CLN_0034 (write `RESULT = Loser` + a note into the Ad Roadmap row)?
   Recommendation: S-batches first, sheet in v2 — same detector, different writer.
2. **Winner rule**: OK to auto-set `Winning Ad` when the creative is found live in a
   scaling/graveyard campaign, or should the sync only ever set `Losing Ad` and leave
   winners to humans?
3. **Discord channel** and whether to @-mention a role on batch-level kills.
4. **Polling interval**: 30 min (recommended) vs 15 min (doubles Actions minutes; still free).
5. **Task status on batch kill**: `complete` (proposed) or a new `killed` status in ClickUp?

## 5. Risks / edge cases
- GitHub cron can lag 5–15 min under load; acceptable for this use.
- Ad set names must stay exactly equal to ClickUp task names — the S0nn-prefix fallback
  covers renames, but a duplicate batch number (S099 appears twice in ClickUp today, and
  S070's field said S067) will match the wrong task → the script logs ambiguous matches
  to Discord instead of guessing.
- Meta `/insights` attribution: use the account default (7d click / 1d view) and label it.
- Tokens: a System User token does not expire but is revoked if the system user is
  removed from the account; the workflow should post a Discord alert on any 4xx from Meta.
- The Meta MCP is not usable from GitHub Actions; the script talks to the Graph API directly.

## 6. As built (2026-09-07)

- Runs as LaunchAgent `com.voana.kill-sync` on Martijn's MacBook at 08:45 and 20:45 local
  (mirrors `voana-tools/scripts/run_daily_hooks.sh`): `scripts/run_kill_sync.sh` → headless
  `claude -p` with `scripts/kill_sync_prompt.md` → `scripts/kill_sync.py process`.
- **Why headless Claude for the Meta half:** the only Meta credential on this Mac is the
  `meta-ads` MCP (user-scope, `https://mcp.facebook.com/ads`); the System-User token lives in
  the retire-dip-alerts Key Vault, not locally. The Claude run only fetches and writes JSON;
  ClickUp + Discord writes are deterministic Python.
- **Detection is activity-log driven, not snapshot driven:** a kill = activity event with
  `run_status` 1→7 (or 9→7), actor ≠ Meta, not our own 17→7 launch flow. No full ad inventory
  has to pass through the model — only the handful of kill events plus metrics for the ad sets
  involved.
- **ClickUp target is the Media list** (`901819973378`), not Launch Manager: an automation
  moves tasks there on `launched` (status `learning`). Batch kill → status `killed`
  (Media has `killed` / `winner` / `has potential` / `rejected` / `complete`).
- Winner rule (§2) is NOT implemented in v1: the sync never overrides a human-set `Status`
  (Has Potential / Winning Ad / Super Winner) or task status (`winner` / `has potential` /
  `complete`); it only fills empty / Not Tested / In Testing.
- Discord: `KILL_SYNC_DISCORD_WEBHOOK_URL` in `.env`; until set, posts go to the ops webhook
  with a footnote. Sheet `#nnn` batches are not written back (v2).
