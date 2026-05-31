#!/usr/bin/env bash
# Telegram bridge for Claude Code.
# Reads bot token + chat_id from config.json next to this script.
#
# Messaging:
#   tg.sh send "text"          Send a message (auto-splits >4096, falls back to plain on bad Markdown)
#   tg.sh send -               Send message read from stdin (use for long/multiline/special text)
#   tg.sh file <path> [cap]    Send a file as a document
#   tg.sh photo <path> [cap]   Send an image
#   tg.sh ask "text" [timeout] Send, then block until you reply; prints your reply
#   tg.sh poll [timeout]       Block until the next incoming message; prints its text (default 50s)
#   tg.sh drain                Discard pending updates (resync offset to "now")
#
# Setup:
#   tg.sh setup                Detect chat_id from the latest message you sent the bot
#
# Away mode (single-owner, keyed by project dir so only one session polls):
#   tg.sh away on              Mark current $PWD as the away owner
#   tg.sh away off             Clear away mode (any owner)
#   tg.sh away owner           Print the owning dir (empty if off)
#   tg.sh away clear <dir>     Clear ONLY if <dir> is the current owner (used by hooks)
#
# Env:
#   TG_LABEL    Optional prefix shown before sent text, e.g. "[llm-memory]". If unset,
#               and TG_CWD is set, the label is derived from basename(TG_CWD).
#   TG_CWD      Working dir to derive a label from (hooks pass the session cwd here).
#
# Exit codes: 0 ok, 2 config missing/invalid, 3 timeout (poll/ask), 1 other error.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$DIR/config.json"
STATE="$DIR/.state"
BRIDGE="$DIR/bridge.py"

# Identity of this session for reply-routing & away markers (sanitized cwd).
session_key() { printf '%s' "${TG_KEY:-${TG_CWD:-$PWD}}" | tr -c 'A-Za-z0-9' '_'; }

die() { echo "tg: $*" >&2; exit "${2:-1}"; }

[[ -f "$CONFIG" ]] || die "no config.json — copy config.example.json to config.json and set token" 2
TOKEN="$(jq -r '.token // empty' "$CONFIG")"
CHAT_ID="$(jq -r '.chat_id // empty' "$CONFIG")"
[[ -n "$TOKEN" && "$TOKEN" != "PUT_YOUR_BOT_TOKEN_HERE" ]] || die "token not set in config.json" 2

API="https://api.telegram.org/bot${TOKEN}"

api() { local method="$1"; shift; curl -fsS "$@" "${API}/${method}"; }

get_offset() { [[ -f "$STATE" ]] && cat "$STATE" || echo 0; }
set_offset() { echo "$1" > "$STATE"; }

# Optional "[label] " prefix for outgoing text.
label_prefix() {
  local l="${TG_LABEL:-}"
  if [[ -z "$l" && -n "${TG_CWD:-}" ]]; then l="[$(basename "$TG_CWD")]"; fi
  [[ -n "$l" ]] && printf '%s ' "$l"
  return 0
}

# Send one chunk (<=4096). Tries Markdown, falls back to plain. Echoes message_id.
send_chunk() {
  local text="$1" resp
  resp="$(api sendMessage \
            --data-urlencode "chat_id=${CHAT_ID}" \
            --data-urlencode "text=${text}" \
            --data-urlencode "parse_mode=Markdown" 2>/dev/null)" \
  || resp="$(api sendMessage \
            --data-urlencode "chat_id=${CHAT_ID}" \
            --data-urlencode "text=${text}")"
  printf '%s' "$resp" | jq -r '.result.message_id // empty'
}

# Record message_ids as sent by this session, so your reply-to routes back here.
record_mids() {
  (( $# > 0 )) && python3 "$BRIDGE" record "$(session_key)" "$@" || true
}

cmd_send() {
  [[ -n "$CHAT_ID" ]] || die "chat_id not set — run: tg.sh setup" 2
  local text
  if [[ "${1:-}" == "-" ]]; then text="$(cat)"; else text="${1:-}"; fi
  [[ -n "$text" ]] || die "usage: tg.sh send \"text\"  (or: ... | tg.sh send -)"
  text="$(label_prefix)$text"
  local mids=() mid
  # Split into <=4000-char chunks (codepoints; safe under Telegram's 4096 limit).
  if [[ "${#text}" -le 4000 ]]; then
    mid="$(send_chunk "$text")"; [[ -n "$mid" ]] && mids+=("$mid")
  else
    while IFS= read -r -d '' part; do
      mid="$(send_chunk "$part")"; [[ -n "$mid" ]] && mids+=("$mid")
    done < <(printf '%s' "$text" | python3 -c '
import sys
t=sys.stdin.read(); n=4000
parts=[t[i:i+n] for i in range(0,len(t),n)]
sys.stdout.write("\x00".join(parts))')
  fi
  record_mids "${mids[@]}"
  echo "sent"
}

cmd_file() {
  [[ -n "${1:-}" ]] || die "usage: tg.sh file <path> [caption]"
  [[ -f "$1" ]] || die "no such file: $1"
  [[ -n "$CHAT_ID" ]] || die "chat_id not set — run: tg.sh setup" 2
  local cap mid; cap="$(label_prefix)${2:-}"
  mid="$(api sendDocument -F "chat_id=${CHAT_ID}" -F "document=@$1" -F "caption=${cap}" | jq -r '.result.message_id // empty')"
  [[ -n "$mid" ]] && record_mids "$mid"
  echo "sent"
}

cmd_photo() {
  [[ -n "${1:-}" ]] || die "usage: tg.sh photo <path> [caption]"
  [[ -f "$1" ]] || die "no such file: $1"
  [[ -n "$CHAT_ID" ]] || die "chat_id not set — run: tg.sh setup" 2
  local cap mid; cap="$(label_prefix)${2:-}"
  mid="$(api sendPhoto -F "chat_id=${CHAT_ID}" -F "photo=@$1" -F "caption=${cap}" | jq -r '.result.message_id // empty')"
  [[ -n "$mid" ]] && record_mids "$mid"
  echo "sent"
}

cmd_setup() {
  local resp; resp="$(api getUpdates)" || die "API call failed (bad token?)"
  local id; id="$(echo "$resp" | jq -r '[.result[].message.chat.id] | last // empty')"
  local uid; uid="$(echo "$resp" | jq -r '[.result[].message.from.id] | last // empty')"
  [[ -n "$id" ]] || die "no messages found. Send any message to your bot first, then rerun setup." 1
  local tmp; tmp="$(mktemp)"
  jq --arg id "$id" --arg uid "$uid" \
    '.chat_id = ($id|tonumber) | (if $uid != "" then .user_id = ($uid|tonumber) else . end)' \
    "$CONFIG" > "$tmp" && mv "$tmp" "$CONFIG"
  chmod 600 "$CONFIG"
  local lastu; lastu="$(echo "$resp" | jq -r '[.result[].update_id] | last // 0')"
  [[ "$lastu" -gt 0 ]] && set_offset "$((lastu + 1))"
  echo "chat_id set to $id, user_id ${uid:-unchanged}"
}

cmd_drain() {
  # Reset: consume all pending Telegram updates and clear the local inbox.
  python3 "$BRIDGE" reset >/dev/null 2>&1 || true
  echo "drained"
}

# One receive cycle (lock + pump + claim live in bridge.py, guarded by flock):
# prints messages routed to THIS session; exit 3 if nothing yet.
cmd_recv() {
  [[ -n "$CHAT_ID" ]] || die "chat_id not set — run: tg.sh setup" 2
  python3 "$BRIDGE" recv "$(session_key)" "${1:-5}"
}

# Backward-compatible alias.
cmd_poll() { cmd_recv "${1:-5}"; }

# Block (cheaply, in a background task) until a message routed to THIS session
# arrives, then print it and exit 0. Loops recv (flock-coordinated, so sessions
# share Telegram's single getUpdates fairly) with a small backoff. Exits 3 after
# <maxsecs> (default ~6h) so the caller can relaunch. Meant to be run via the Bash
# tool with run_in_background:true — the harness re-invokes the agent when it exits.
cmd_listen() {
  local maxsecs="${1:-21600}"
  [[ -n "$CHAT_ID" ]] || die "chat_id not set — run: tg.sh setup" 2
  while (( SECONDS < maxsecs )); do
    if out="$(cmd_recv 5 2>/dev/null)"; then printf '%s\n' "$out"; return 0; fi
    sleep "$(( (RANDOM % 4) + 2 ))"
  done
  exit 3
}

# Send a question, then wait (cooperatively) for YOUR reply. Loops recv with a
# short backoff until a reply arrives or the total timeout (default 120s) passes.
cmd_ask() {
  [[ -n "${1:-}" ]] || die "usage: tg.sh ask \"text\""
  cmd_send "$1" >/dev/null
  echo "waiting for reply..." >&2
  local budget="${2:-120}" out
  while (( SECONDS < budget )); do
    if out="$(cmd_recv 10)"; then printf '%s\n' "$out"; return 0; fi
    sleep "$(( (RANDOM % 5) + 2 ))"
  done
  exit 3
}

# Away mode is per-session (keyed by dir): multiple sessions may listen at once.
# Markers live in away.d/, one file per owning dir.
AWAYD="$DIR/away.d"
marker_path() { printf '%s/%s' "$AWAYD" "$(printf '%s' "${1:-$PWD}" | tr -c 'A-Za-z0-9' '_')"; }

cmd_away() {
  local me="${2:-$PWD}"
  case "${1:-}" in
    on)     mkdir -p "$AWAYD"; printf '%s' "$me" > "$(marker_path "$me")"; echo "away on: $me" ;;
    off)    rm -f "$(marker_path "$me")"; echo "away off: $me" ;;
    clear)  rm -f "$(marker_path "$me")"; echo "cleared: $me" ;;       # alias of off, used by hooks
    active) [[ -f "$(marker_path "$me")" ]] && exit 0 || exit 1 ;;      # is <dir> listening?
    list)   [[ -d "$AWAYD" ]] && cat "$AWAYD"/* 2>/dev/null | sed 's/$/\n/' || true ;;
    *) die "usage: tg.sh away {on|off|clear <dir>|active <dir>|list}" ;;
  esac
}

case "${1:-}" in
  setup) cmd_setup ;;
  send)  shift; cmd_send "${1:-}" ;;
  file)  shift; cmd_file "${1:-}" "${2:-}" ;;
  photo) shift; cmd_photo "${1:-}" "${2:-}" ;;
  ask)   shift; cmd_ask "${1:-}" "${2:-}" ;;
  recv)  shift; cmd_recv "${1:-}" ;;
  listen) shift; cmd_listen "${1:-}" ;;
  poll)  shift; cmd_poll "${1:-}" ;;
  drain) cmd_drain ;;
  away)  shift; cmd_away "${1:-}" "${2:-}" ;;
  *) die "usage: tg.sh {setup|send|file|photo|ask|recv|poll|drain|away}" ;;
esac
