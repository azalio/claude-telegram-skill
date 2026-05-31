#!/usr/bin/env bash
# Notification hook: when this session holds the away lock, forward Claude's
# notification (e.g. "needs your permission", idle) to Telegram. Side-effect only.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TG="$DIR/tg.sh"

INPUT="$(cat)"
CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty')"
MSG="$(printf '%s' "$INPUT" | jq -r '.message // empty')"

# Only forward when this session is in away mode.
"$TG" away active "$CWD" >/dev/null 2>&1 || exit 0

export TG_CWD="$CWD"
[[ -n "$MSG" ]] || MSG="Claude ждёт твоего ввода."
"$TG" send "🔔 $MSG" >/dev/null 2>&1 || true
exit 0
