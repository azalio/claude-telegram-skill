#!/usr/bin/env bash
# Stop hook — NON-BLOCKING (never freezes the terminal).
#
# Two cases:
#  * Telegram two-way mode is active for this session (away marker set): the agent
#    drives a CronCreate poll loop itself, so the hook does nothing and returns.
#  * Otherwise: arm a cheap DETACHED idle-mirror watcher that, after ~10 min of no
#    terminal activity, mirrors the assistant's last message to Telegram once
#    (a one-way "you've been away while a reply was expected" heads-up).
#
# Real two-way conversation is agent-driven (CronCreate + tg.sh recv/send), not a
# blocking hook — see SKILL.md.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TG="$DIR/tg.sh"
IDLED="$DIR/idle.d"

INPUT="$(cat)"
CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty')"
LAST="$(printf '%s' "$INPUT" | jq -r '.last_assistant_message // empty')"
SID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' | tr -c 'A-Za-z0-9' '_')"

# In two-way mode the agent's own cron poller handles replies — nothing to do here.
"$TG" away active "$CWD" >/dev/null 2>&1 && exit 0

# Arm the detached idle auto-mirror (returns immediately; never blocks the terminal).
SECS="$(jq -r '.idle_mirror_secs // 600' "$DIR/config.json" 2>/dev/null)"
if [[ "$SECS" =~ ^[0-9]+$ ]] && (( SECS > 0 )) && [[ -n "$SID" ]]; then
  mkdir -p "$IDLED"
  printf '%s' "$LAST" > "$IDLED/msg-$SID"
  nohup "$DIR/idle-mirror.sh" "$SID" "$(date +%s)" "$CWD" >/dev/null 2>&1 &
  disown 2>/dev/null || true
fi
exit 0
