#!/usr/bin/env bash
# UserPromptSubmit hook: typing in the terminal means you're here.
#   1) record a prompt timestamp (per session) so an armed idle-mirror watcher cancels;
#   2) release this session's away marker.
# Silent, never blocks the prompt.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TG="$DIR/tg.sh"
IDLED="$DIR/idle.d"

INPUT="$(cat)"
CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty')"
SID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' | tr -c 'A-Za-z0-9' '_')"

if [[ -n "$SID" ]]; then
  mkdir -p "$IDLED" 2>/dev/null || true
  date +%s > "$IDLED/prompt-$SID" 2>/dev/null || true
fi
[[ -n "$CWD" ]] && "$TG" away clear "$CWD" >/dev/null 2>&1 || true
exit 0
