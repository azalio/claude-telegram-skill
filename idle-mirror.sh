#!/usr/bin/env bash
# Detached idle watcher, armed by the Stop hook at the end of a normal (non-away)
# turn. Waits up to idle_mirror_secs (default 600 = 10 min). If the user interacts
# in the terminal first (UserPromptSubmit records a newer prompt timestamp), it
# exits silently. Otherwise it mirrors the assistant's last message to Telegram
# ONCE — a heads-up that you've been away while a reply was expected.
#
# Args: <session_key> <armed_ts> <cwd>
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TG="$DIR/tg.sh"
IDLED="$DIR/idle.d"
SID="${1:-}"; ARMED="${2:-0}"; CWD="${3:-}"
PROMPTF="$IDLED/prompt-$SID"
MSGF="$IDLED/msg-$SID"

cleanup() { rm -f "$MSGF" 2>/dev/null || true; }
trap cleanup EXIT

SECS="$(jq -r '.idle_mirror_secs // 600' "$DIR/config.json" 2>/dev/null)"
[[ "$SECS" =~ ^[0-9]+$ ]] || SECS=600
(( SECS > 0 )) || exit 0

# Did the user submit a terminal prompt after we armed? -> they're here, cancel.
user_returned() {
  [[ -f "$PROMPTF" ]] || return 1
  local pts; pts="$(cat "$PROMPTF" 2>/dev/null || echo 0)"
  [[ "$pts" =~ ^[0-9]+$ ]] || pts=0
  (( pts >= ARMED ))
}

waited=0
while (( waited < SECS )); do
  user_returned && exit 0
  # also bail if this session entered explicit away mode (handled by the Stop hook)
  "$TG" away active "$CWD" >/dev/null 2>&1 && exit 0
  sleep 15
  waited=$(( waited + 15 ))
done
user_returned && exit 0

msg=""; [[ -f "$MSGF" ]] && msg="$(cat "$MSGF")"
[[ -n "$msg" ]] || msg="Жду твоего ответа."
export TG_CWD="$CWD"
printf '💤 %d мин без ответа:\n\n%s' "$(( SECS / 60 ))" "$msg" | "$TG" send - >/dev/null 2>&1 || true
exit 0
