#!/usr/bin/env bash
# SessionStart hook: if the bot is configured and always_listen is on, tell the
# agent to start a background Telegram listener for this session. The listener is
# a harness-tracked background task (cheap: no model tokens while idle); the harness
# re-invokes the agent only when a message actually arrives. Coordination across
# sessions is handled by bridge.py's flock, so only one session calls getUpdates at
# a time. Output is additionalContext — it never blocks.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$DIR/config.json"
[[ -f "$CONFIG" ]] || exit 0

chat="$(jq -r '.chat_id // empty' "$CONFIG" 2>/dev/null)"
always="$(jq -r '.always_listen // false' "$CONFIG" 2>/dev/null)"
[[ -n "$chat" && "$always" == "true" ]] || exit 0

read -r -d '' CTX <<EOF || true
Telegram always-listen is ON for this session. As soon as convenient on your first
turn, start listening for Telegram messages and keep listening for the whole session:

1. Launch the listener as a background task (Bash tool, run_in_background: true):
   ${DIR}/tg.sh listen
2. When that task completes, it printed a Telegram message routed to this session.
   Read its output, treat it as a normal user message, act on it, and send your
   reply with: ${DIR}/tg.sh send 'your reply'  (single quotes — avoid backticks).
3. Then IMMEDIATELY relaunch the listener the same way (step 1). If the task exited
   with no output (idle timeout), just relaunch it.

This loop is cheap — the background task uses no model tokens while waiting; you only
wake when a message arrives. The user addresses a specific session by replying
(Telegram reply-to) to that session's message. Stop only if the user says to stop
listening (then don't relaunch). Never block the terminal; the user can keep typing
here the whole time.
EOF

jq -n --arg ctx "$CTX" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
exit 0
