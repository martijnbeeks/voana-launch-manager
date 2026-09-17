#!/bin/zsh
# Wrapper for the twice-daily kill-sync LaunchAgent (com.voana.kill-sync).
#
# Mirrors voana-tools/scripts/run_format_radar.sh: source nvm so `claude`
# resolves under launchd, pre-flight what silently breaks, wait for a FULL wake
# (a lid-closed MacBook fires a missed StartCalendarInterval inside a dark
# wake with no network — the 2026-09-09 20:45 run died there "mid-response"),
# retry the bridge on transient failures, and post THIS run's log tail to the
# ops Discord webhook on failure. Meta data is fetched by a headless Claude
# session (the only Meta credential on this Mac is the MCP);
# scripts/kill_sync.py does the ClickUp + Discord side deterministically.
#
#   KILL_SYNC_PREFLIGHT_ONLY=1   stop after the pre-flight checks
#   KILL_SYNC_DRY_RUN=1          fetch, but write nothing to ClickUp/Discord
#   KILL_SYNC_DIGEST=1           also post the "synced into ClickUp" digest
#   KILL_SYNC_SKIP_CLICKUP=1     post Discord + record state, but leave ClickUp untouched
#   KILL_SYNC_WINDOW_HOURS=36    override the poll window (default: since last_run.json)
#   KILL_SYNC_MAX_WAKE_WAIT=N    seconds to wait for a full wake (default 39600 = 11h,
#                                under the 12h gap to the next scheduled run)
set -uo pipefail

REPO="/Users/martijnbeeks/Development/voana-project/voana-launch-manager"
TOOLS="/Users/martijnbeeks/Development/voana-project/voana-tools"
PROMPT="$REPO/scripts/kill_sync_prompt.md"
LOG="$REPO/state/kill-sync.log"
ATTEMPTS=3
DISCORD_FAILED=0
cd "$REPO" || exit 1
mkdir -p "$REPO/state/inbox"

# Byte offset of the shared log at start, so an alert quotes THIS run only.
# Before 2026-09-10 the alert did `tail -c 1200 "$LOG"`, which on a short run
# showed the previous run's traceback and misattributed the failure.
LOG_START=$(stat -f %z "$LOG" 2>/dev/null || echo 0)
run_tail() { tail -c +$((LOG_START + 1)) "$LOG" 2>/dev/null | grep -v '^\s*$' | tail -n 6 | cut -c1-160; }

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null 2>&1

# Shared scheduler guard (dark-wake wait, subscription-only claude, transient
# regex). Fall back to no-ops if voana-tools is not checked out beside us.
GUARD="$TOOLS/scripts/lib/scheduler_guard.sh"
if [ -f "$GUARD" ]; then . "$GUARD"; fi
command -v await_full_wake >/dev/null 2>&1 || await_full_wake() { return 0; }
command -v claude_sub >/dev/null 2>&1 || claude_sub() { env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN claude "$@"; }
TRANSIENT="${SCHEDULER_GUARD_TRANSIENT:-Not logged in|API Error: (429|5[0-9][0-9])|Overloaded|ECONNRESET|ETIMEDOUT|timed out|fetch failed}|went to sleep"
MAX_WAKE_WAIT="${KILL_SYNC_MAX_WAKE_WAIT:-39600}"

webhook() {
  grep -E '^DISCORD_WEBHOOK_URL=' "$TOOLS/.env" 2>/dev/null | head -1 | cut -d= -f2-
}

alert() {
  local url
  url="$(webhook)"
  print -r -- "$1"
  [ -n "$url" ] || return 0
  python3 - "$url" "$1" <<'PY' 2>/dev/null || true
import json, sys, urllib.request
url, msg = sys.argv[1], sys.argv[2][:1900]
req = urllib.request.Request(url, data=json.dumps({"content": msg}).encode(),
    headers={"Content-Type": "application/json", "User-Agent": "voana-kill-sync/1.0"})
urllib.request.urlopen(req, timeout=20).read()
PY
}

print -r -- "===== kill-sync run $(date '+%Y-%m-%d %H:%M:%S') ====="

# --- preflight ---------------------------------------------------------------
[ -f "$PROMPT" ] || { alert "🔴 **kill-sync did not run** — prompt missing: $PROMPT"; exit 1; }
[ -f "$REPO/.env" ] || { alert "🔴 **kill-sync did not run** — $REPO/.env is missing (needs CLICKUP_API_KEY)."; exit 1; }
grep -qE '^CLICKUP_API_KEY=pk_' "$REPO/.env" || { alert "🔴 **kill-sync did not run** — CLICKUP_API_KEY not set in $REPO/.env"; exit 1; }
command -v claude >/dev/null 2>&1 || { alert "🔴 **kill-sync did not run** — \`claude\` not on PATH under launchd (nvm?)"; exit 127; }

KILL_SYNC_PYTHON=""
for candidate in "$HOME/.pyenv/shims/python3" "$HOME/.pyenv/versions/3.10.4/bin/python3" "$(command -v python3 2>/dev/null)" /usr/bin/python3; do
  [ -n "$candidate" ] && [ -x "$candidate" ] || continue
  if "$candidate" -c 'import json, urllib.request' >/dev/null 2>&1; then KILL_SYNC_PYTHON="$candidate"; break; fi
done
[ -n "$KILL_SYNC_PYTHON" ] || { alert "🔴 **kill-sync did not run** — no usable python3"; exit 1; }
export KILL_SYNC_PYTHON
export KILL_SYNC_DRY_RUN="${KILL_SYNC_DRY_RUN:-}"
export KILL_SYNC_SKIP_CLICKUP="${KILL_SYNC_SKIP_CLICKUP:-}"
export KILL_SYNC_WINDOW_HOURS="${KILL_SYNC_WINDOW_HOURS:-}"

print -r -- "preflight ok: claude $(claude --version 2>/dev/null | head -1) · python $("$KILL_SYNC_PYTHON" -V 2>&1) · guard $([ -f "$GUARD" ] && echo shared || echo fallback)"
if [ "${KILL_SYNC_PREFLIGHT_ONLY:-}" = "1" ]; then print -r -- "KILL_SYNC_PREFLIGHT_ONLY=1 — stopping."; exit 0; fi

# --- wait for a real wake before spending anything ---------------------------
if ! await_full_wake "$MAX_WAKE_WAIT"; then
  alert "🟡 **kill-sync skipped** — Mac not fully awake for ${MAX_WAKE_WAIT}s. Next run covers the window."
  exit 0
fi

# --- the run -----------------------------------------------------------------
# `kill_sync.py process` advances data/kill-sync/last_run.json as its final act, and only
# when it completes (guarded by `if not dry`). The bridge reports failures in
# prose and still exits 0, so that file — not $rc — is the only trustworthy
# signal that the work actually happened. A re-run is safe: the ledger dedupes
# kills, and the script's ClickUp writes are idempotent (comments are matched
# on their first line, fields and checklist items are upserted).
read_last_run() {
  "$KILL_SYNC_PYTHON" -c 'import json
try:
    print(json.load(open("data/kill-sync/last_run.json"))["last_run"])
except Exception:
    print("")' 2>/dev/null
}
LAST_RUN_BEFORE="$(read_last_run)"

attempt=1
rc=1
while [ $attempt -le $ATTEMPTS ]; do
  print -r -- "----- bridge attempt $attempt/$ATTEMPTS $(date '+%H:%M:%S') -----"
  out="$(mktemp -t kill-sync)"
  claude_sub -p "$(cat "$PROMPT")" --dangerously-skip-permissions --max-turns 40 >"$out" 2>&1
  rc=$?
  cat "$out"
  transient=0
  grep -qiE "$TRANSIENT" "$out" && transient=1
  DISCORD_FAILED=0
  grep -q "discord=FAILED" "$out" && DISCORD_FAILED=1
  rm -f "$out"
  if [ $rc -eq 0 ] && [ $transient -eq 0 ]; then break; fi
  if [ $rc -eq 0 ] && [ "$(read_last_run)" != "$LAST_RUN_BEFORE" ]; then break; fi   # finished despite the noise
  if [ $transient -eq 0 ]; then
    print -r -- "non-transient failure (exit $rc) — not retrying."
    break
  fi
  if [ $attempt -lt $ATTEMPTS ]; then
    backoff=$((attempt * 300))   # 5 min, then 10 min
    print -r -- "transient failure (exit $rc) — retrying in ${backoff}s."
    sleep $backoff
    await_full_wake "$MAX_WAKE_WAIT" || break
  fi
  attempt=$((attempt + 1))
done

if [ $rc -ne 0 ]; then
  alert "🔴 **kill-sync failed** (exit $rc, $attempt attempt(s)) — last lines:
\`\`\`
$(run_tail)
\`\`\`"
  exit $rc
fi

if [ -z "${KILL_SYNC_DRY_RUN:-}" ]; then
  LAST_RUN_AFTER="$(read_last_run)"
  if [ "$LAST_RUN_AFTER" = "$LAST_RUN_BEFORE" ]; then
    alert "🔴 **kill-sync failed** — \`process\` did not complete (last_run still \`${LAST_RUN_BEFORE:-unset}\`) — last lines:
\`\`\`
$(run_tail)
\`\`\`"
    exit 1
  fi
fi

if [ "${DISCORD_FAILED:-0}" = "1" ]; then
  alert "🟧 **kill-sync**: ClickUp synced, Discord kill post failed — \`kill_sync.py replay data/kill-sync/inbox-archive/<stamp>\`"
fi

if [ "${KILL_SYNC_DIGEST:-}" = "1" ]; then
  "$KILL_SYNC_PYTHON" scripts/kill_sync.py digest ${KILL_SYNC_DRY_RUN:+--dry-run} || alert "🟧 kill-sync digest failed"
fi

print -r -- "===== run finished ok $(date '+%H:%M:%S') ====="
