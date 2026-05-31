# telegram-bridge — a Claude Code plugin

Message Claude on Telegram when work finishes, and **chat with it two-way while away
from the terminal** — non-blocking, with **every active session always listening**.
Multiple concurrent sessions share one bot, coordinated by a file lock so only one
polls Telegram at a time. No daemon, no server — just a bot token.

## Install

```text
/plugin marketplace add azalio/claude-telegram-skill
/plugin install telegram-bridge@azalio
```

Then configure the bot (state lives in `~/.claude/telegram/`, never inside the plugin):

```bash
# 1) create a bot with @BotFather, copy the token
# 2) put it in ~/.claude/telegram/config.json  (copy config.example.json there)
# 3) send your bot any message (e.g. /start), then:
~/.claude/telegram/tg setup     # auto-detects chat_id + user_id
```

The SessionStart hook writes the `~/.claude/telegram/tg` launcher on first run.

## What it does

- **Notify on done** — "ping me on telegram when you finish" → a summary; also `file`/`photo`.
- **Two-way chat, always on** — every session keeps a cheap background listener
  (`tg listen`); it costs no model tokens while idle and wakes only when a message
  arrives. Reply in Telegram and Claude continues. The terminal is never blocked.
- **Auto-mirror** — after ~10 min with no terminal reply, the last message is pushed
  to Telegram once.
- **Multi-session routing** — address a specific session by *replying* (Telegram
  reply-to) to its message. Inbound is serialized with `flock`, written before the
  Telegram offset advances (crash-safe), de-duplicated by `update_id`, and accepts
  messages only from your `user_id`.

## How it works

`getUpdates` is single-consumer with one destructive offset, so the lock holder
**pumps** all updates into a shared inbox tagging each with its target session
(via `reply_to` → sent-message map); each session **claims** only its own (or
broadcast `*`) messages. A reply unclaimed for >10 min is downgraded to broadcast
(dead-session fallback) and dropped after 1 h.

## Layout

```
.claude-plugin/plugin.json        plugin manifest (hooks → hooks/hooks.json)
.claude-plugin/marketplace.json   this repo is its own marketplace
skills/telegram/SKILL.md          the skill Claude reads
scripts/tg.py                     all logic (stdlib only)
hooks/hooks.json                  Stop / UserPromptSubmit / Notification / SessionStart
tests/test_e2e.py                 e2e tests (run in CI, no token)
config.example.json               copy to ~/.claude/telegram/config.json
```

## Test

```bash
python3 tests/test_e2e.py                 # structure + routing on mocks, no token
claude plugin validate . --strict         # official schema validation
claude --plugin-dir . -p "..." --bare     # load locally, headless
```

CI (`.github/workflows/e2e`) runs the e2e tests on every push/PR.

## Security

Your bot token, chat_id, user_id and all runtime state live in `~/.claude/telegram/`,
outside the plugin and out of git. Only messages from your configured `user_id` are
accepted. Never commit your token.
