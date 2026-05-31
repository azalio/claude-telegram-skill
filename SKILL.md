---
name: telegram
description: >
  Send Telegram messages to the user and hold a two-way chat with them while away
  from the terminal — non-blocking, with every active session always listening.
  Uses: (1) notify when a task/research finishes ("напиши в тг как закончишь",
  "ping me on telegram when done"); (2) two-way chat any time — the user messages a
  session over Telegram and it replies, addressing a session by replying to its
  message; (3) auto-mirror the last message to Telegram after ~10 min of no terminal
  reply. Works across multiple concurrent sessions sharing one bot, coordinated by a
  file lock so only one polls Telegram at a time. Bot token only; no daemon.
---

# Telegram bridge

Talk to the user over Telegram via a bot. Outbound goes through `tg.sh`; inbound
routing/locking lives in `bridge.py`. Use absolute paths under
`~/.claude/skills/telegram/`. Token + chat_id + allowed user_id live in
`config.json` (chmod 600, never printed).

**Nothing here blocks the terminal.** Listening uses a background task that costs no
model tokens while idle — you only wake when a message actually arrives.

## Default — always listen (every active session)

When `always_listen` is on (the default; the SessionStart hook reminds you), keep a
listener running for the whole session:

1. Launch it as a **background task** (Bash tool, `run_in_background: true`):
   ```bash
   ~/.claude/skills/telegram/tg.sh listen
   ```
   It blocks cheaply (a flock-coordinated poll loop — only one session calls Telegram
   at a time) until a message routed to THIS session arrives, then prints it and exits.
2. When the task completes, the harness re-invokes you with its output: that text is
   the user's Telegram message. Treat it as a normal user turn — act on it and send
   your reply with (single quotes — never backticks, see below):
   ```bash
   ~/.claude/skills/telegram/tg.sh send 'your reply'
   ```
3. **Immediately relaunch** the listener (step 1) so you keep listening. If the task
   exited with no output (idle timeout), just relaunch.

The terminal stays fully usable the whole time. The user can talk to whichever
session they want by **replying (Telegram reply-to) to that session's message**;
non-reply messages go to whichever session grabs them first. Only messages from the
configured user_id are accepted. Stop only if the user says to stop listening (then
don't relaunch) — typing in the terminal does NOT stop listening.

## Mode 1 — notify when done

When the user says "ping me / write to telegram when you finish": do the work, then
send a concise summary at the end.

```bash
~/.claude/skills/telegram/tg.sh send "✅ Research done. Key finding: X. Details in research.md."
~/.claude/skills/telegram/tg.sh file research.md "Full notes"     # send a document
~/.claude/skills/telegram/tg.sh photo screenshot.png "QA result"  # send an image
```

Long text auto-splits (>4096). For big outputs prefer `file`. Messages are prefixed
with a project label so the user can tell which session is talking.

**Sending text safely:** if the message may contain backticks or `$(...)` (code
snippets, commands), do NOT pass it in double quotes — the shell will execute the
backticks/substitution. Use single quotes, or pipe via stdin:
`printf '%s' "$msg" | ~/.claude/skills/telegram/tg.sh send -`.

## Auto-mirror after ~10 min idle (proactive heads-up)

Separately from listening, the Stop hook arms a cheap detached watcher: if you finish
a turn and the user does NOT touch the terminal for `idle_mirror_secs` (default 600s),
your last message is mirrored to Telegram once. Cancelled the instant the user types
in the terminal. This pushes "here's what I last said" so the user sees it without
asking; they can then reply and the always-on listener continues the chat.

## First-time setup / install

```bash
git clone git@github.com:azalio/claude-telegram-skill.git ~/.claude/skills/telegram \
  && ~/.claude/skills/telegram/install.sh
# then: put @BotFather token in config.json, message the bot once, run setup:
~/.claude/skills/telegram/tg.sh setup        # detects chat_id + user_id
```

`install.sh` wires the Stop / UserPromptSubmit / Notification / SessionStart hooks
into `~/.claude/settings.json` idempotently. Claude Code auto-discovers the skill.

## Config (config.json)

| Key | Meaning |
|---|---|
| `token`, `chat_id`, `user_id` | Bot token; your chat; the only sender accepted |
| `always_listen` | `true` = every session auto-starts the background listener |
| `idle_mirror_secs` | Seconds of terminal idle before auto-mirroring (0 disables) |

## Command reference (tg.sh)

| Command | What it does |
|---|---|
| `send "text"` / `send -` | Send text (or stdin); auto-split; records message_id for reply-routing |
| `file <path> [cap]` / `photo <path> [cap]` | Send a document / image |
| `listen [maxsecs]` | Block (cheap) until a message for this session, print it, exit. Run in background. |
| `recv [timeout]` | One receive cycle: lock → pump → claim this session's messages; exit 3 if none |
| `ask "text" [budget]` | Send + wait for the reply inline (loops recv; default 120s) |
| `setup` / `drain` | Detect chat_id+user_id / reset offset+inbox |
| `away on\|off\|active <dir>` | Optional per-session marker gating the Notification-forward hook |

## How inbound works (bridge.py)

Telegram's `getUpdates` is single-consumer with one destructive offset, so one
session at a time (exclusive `flock`) **pumps** all updates into a shared
`inbox.jsonl`, tagging each with the target session (via `reply_to` → `sent.map`).
Each session **claims** only its own (or broadcast `*`) messages. Safety:
- inbox is written+fsync'd **before** the offset advances → crashes duplicate, never lose;
- updates de-duplicated by `update_id`;
- a reply unclaimed for >10min is downgraded to broadcast (dead-session fallback), dropped after 1h;
- `flock` auto-releases if a session dies — no stale-lock logic.

## Notes / limits

- Token only in `config.json` (chmod 600); never echo it.
- Listening is cheap while idle (background task, no model tokens); you spend a turn
  only when a message arrives and you respond.
- Many sessions all listening = many short `getUpdates` calls, serialized by the lock.
- Runtime state (gitignored): `.state` (offset), `sent.map`, `inbox.jsonl`,
  `away.d/`, `idle.d/`, `.lock`.
