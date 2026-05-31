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

## Getting the config values

`~/.claude/telegram/config.json` has five fields. Here is how to obtain each:

| Field | What it is | How to get it |
| --- | --- | --- |
| `token` | Bot API token | In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name and username → it replies with a token like `123456789:AA...`. Re-issue anytime with `/token`. |
| `chat_id` | Where messages are sent | Auto-filled by `tg setup` (see below). It is the chat between you and your bot. |
| `user_id` | Allow-listed sender | Auto-filled by `tg setup`. Only messages **from this user id** are accepted, so a stranger who finds your bot can't drive your sessions. For a 1:1 bot it equals your own Telegram user id. |
| `idle_mirror_secs` | Auto-mirror delay | Seconds with no terminal reply before the last message is pushed to Telegram once. Default `600` (10 min); set `0` to disable. |
| `always_listen` | Always-on listener | `true` makes every session keep a background listener so you can chat at any time. `false` = notify-only. |

### Step by step

1. **Create the bot and copy the token.** Open [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts, copy the token it gives you.
2. **Write the token into the config.**
   ```bash
   mkdir -p ~/.claude/telegram
   cp config.example.json ~/.claude/telegram/config.json   # from this repo
   # then edit ~/.claude/telegram/config.json and paste your token into "token"
   ```
3. **Say hi to your bot.** Open the chat with your new bot in Telegram and send it any message (e.g. `/start`). This is required — Telegram only reveals your `chat_id`/`user_id` after you message the bot first.
4. **Auto-detect `chat_id` and `user_id`.**
   ```bash
   ~/.claude/telegram/tg setup
   # -> chat_id set to <n>, user_id <n>
   ```
   `setup` reads the bot's pending updates, takes the chat and sender of your latest message, and writes both ids into the config.

> Prefer to fill the ids by hand? Send your bot a message, then open
> `https://api.telegram.org/bot<token>/getUpdates` in a browser — `result[].message.chat.id`
> is your `chat_id` and `result[].message.from.id` is your `user_id`. `tg setup` just
> automates this.

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
  messages only from your `user_id`. **Reply-id is the only routing signal — there
  is no guessing and no broadcast.** A message that isn't a reply, or replies to a
  message we can't attribute to a session, is dropped (with a one-line nudge to
  reply to a session) rather than delivered to the wrong one.

## How it works

`getUpdates` is single-consumer with one destructive offset, so the lock holder
**pumps** all updates into a shared inbox. Each message is routed **solely by its
`reply_to` id** looked up in the sent-message map (`sent.map`), which records
*every* outbound id (replies, nudges, notifications) so it has no holes. A reply to
a known session is tagged for that session and waits (up to 1 h) until it next
listens — it is never reassigned to another session. Anything we can't attribute is
dropped. Each `tg listen` holds a per-session singleton lock (so listeners can't
pile up) and pumps without holding the shared lock during the network poll (so many
sessions don't serialize behind one slow poll).

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
