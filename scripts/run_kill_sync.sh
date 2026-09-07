#!/bin/zsh
# Wrapper for the twice-daily kill-sync LaunchAgent (com.voana.kill-sync).
#
# Mirrors voana-tools/scripts/run_daily_hooks.sh: source nvm so `claude`
# resolves under launchd, pre-flight what silently breaks, and post the log
# tail to the ops Discord webhook on failure. Meta data is fetched by a
# headless Claude session (the only Meta credential on this Mac is the MCP);
# scripts/kill_sync.py does the ClickUp + Discord side deterministically.
#
#   KILL_SYNC_PREFLIGHT_ONLY=1   stop after the pre-flight checks
#   KILL_SYNC_DRY_RUN=1          fetch, but write nothing to ClickUp/Discord
#   KILL_SYNC_DIGEST=1           also post the "learnings owed" digest
#   KILL_SYNC_SKIP_CLICKUP=1     post Discord + record state, but leave ClickUp untouched
#   KILL_SYNC_WINDOW_HOURS=36    override the poll window (default: since last_run.json)
set -uo pipefail

REPO="/Users/martijnbeeks/Development/voana-project/voana-launch-manager"
TOOLS="/Users/martijnbeeks/Development/voana-project/voana-tools"
PROMPT="$REPO/scripts/kill_sync_prompt.md"
LOG="$REPO/state/kill-sync.log"
cd "$REPO" || exit 1
mkdir -p "$REPO/state/inbox"

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null 2>&1

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

print -r -- "preflight ok: claude $(claude --version 2>/dev/null | head -1) · python $("$KILL_SYNC_PYTHON" -V 2>&1)"
if [ "${KILL_SYNC_PREFLIGHT_ONLY:-}" = "1" ]; then print -r -- "KILL_SYNC_PREFLIGHT_ONLY=1 — stopping."; exit 0; fi

# --- the run -----------------------------------------------------------------
claude -p "$(cat "$PROMPT")" --dangerously-skip-permissions --max-turns 40
rc=$?
if [ $rc -ne 0 ]; then
  alert "🔴 **kill-sync failed** (exit $rc)
\`\`\`
$(tail -c 1200 "$LOG" 2>/dev/null)
\`\`\`"
  exit $rc
fi

if [ "${KILL_SYNC_DIGEST:-}" = "1" ] && [ "$(date +%H)" -lt 12 ]; then
  "$KILL_SYNC_PYTHON" scripts/kill_sync.py digest ${KILL_SYNC_DRY_RUN:+--dry-run} || alert "🟧 kill-sync digest failed"
fi

print -r -- "===== run finished ok $(date '+%H:%M:%S') ====="
