#!/usr/bin/env bash
# One-command setup for the Telegram bridge skill.
#
#   git clone <repo> ~/.claude/skills/telegram
#   ~/.claude/skills/telegram/install.sh
#
# Idempotent: safe to re-run. Wires the Stop / UserPromptSubmit / Notification
# hooks into ~/.claude/settings.json (without touching existing hooks), makes the
# scripts executable, and seeds config.json. Then put your bot token in config.json,
# message the bot once, and run `tg.sh setup`.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"

for bin in jq curl python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "✗ missing dependency: $bin" >&2; exit 1; }
done

chmod +x "$DIR"/*.sh

# Seed config.json from the template (kept out of git; holds your token).
if [[ ! -f "$DIR/config.json" ]]; then
  cp "$DIR/config.example.json" "$DIR/config.json"
  chmod 600 "$DIR/config.json"
  echo "• created config.json (chmod 600)"
else
  echo "• config.json already exists — left as is"
fi

# Wire the three hooks into ~/.claude/settings.json, idempotently (skip if a hook
# with the same command is already present; never disturb other hooks).
[[ -f "$SETTINGS" ]] || echo '{}' > "$SETTINGS"
tmp="$(mktemp)"
jq \
  --arg stop   "$DIR/hook-stop.sh" \
  --arg up     "$DIR/hook-userprompt.sh" \
  --arg notify "$DIR/hook-notify.sh" \
  --arg start  "$DIR/hook-sessionstart.sh" '
  def ensure(event; cmd; to):
    .hooks[event] = ((.hooks[event] // [])
      | if any(.[]?.hooks[]?; .command == cmd) then .
        else . + [ {hooks: [ {type: "command", command: cmd, timeout: to} ]} ] end);
  ensure("Stop";             $stop;   30)
  | ensure("UserPromptSubmit"; $up;     10)
  | ensure("Notification";     $notify; 15)
  | ensure("SessionStart";     $start;  10)
' "$SETTINGS" > "$tmp"
jq empty "$tmp" && mv "$tmp" "$SETTINGS" || { echo "✗ settings.json edit failed" >&2; rm -f "$tmp"; exit 1; }
echo "• hooks wired into $SETTINGS"

echo
echo "Next steps:"
echo "  1) Put your @BotFather token into $DIR/config.json"
echo "  2) Send any message (e.g. /start) to your bot in Telegram"
echo "  3) Run: $DIR/tg.sh setup    # detects chat_id + user_id"
echo
echo "Done. Then just say e.g. «ping me on telegram when done» or «let's discuss on telegram»."
