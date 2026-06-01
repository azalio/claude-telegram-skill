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

Talk to the user over Telegram via a bot. All logic is one script, `tg.py`
(standard library only). State (token, offset, inbox, locks) lives in a stable dir
`~/.claude/telegram/`, never inside the plugin — so updates never touch your token.

The SessionStart hook writes a stable launcher and tells you its path; use that
launcher for every command:

```
~/.claude/telegram/tg <subcommand> ...
```

**Nothing here blocks the terminal.** Listening uses a background task that costs no
model tokens while idle — you wake only when a message actually arrives.

## Default — always listen (every active session)

When `always_listen` is on (default), the SessionStart hook reminds you to keep a
listener running for the whole session:

1. Launch it as a **background task** (Bash tool, `run_in_background: true`):
   ```bash
   ~/.claude/telegram/tg listen
   ```
   It blocks cheaply (a flock-coordinated poll loop — only one session calls Telegram
   at a time) until a message routed to THIS session arrives, then prints it and exits.
   A per-session singleton lock guarantees at most one listener per session.
2. When the task completes, **branch on its exit code** — don't blindly relaunch:
   - **exit 0** — it printed a message. Your **first** action is to reply in Telegram
     to acknowledge (single quotes — never backticks), *then* act on it:
     ```bash
     ~/.claude/telegram/tg send 'your reply'
     ```
     Then relaunch the listener (step 1).
   - **exit 3** — timed out, no message. Relaunch once, quietly (this is normal idle).
   - **exit 4** — a listener for this session is already running. Do **nothing** — do
     not relaunch (this is the singleton lock preventing pile-up).

Keep **exactly one** listener per session. The terminal stays usable throughout. The
user targets a session by **replying (Telegram reply-to) to that session's message**.
Routing is by reply-id only: a message that isn't a reply, or replies to something we
can't attribute to a session, is **dropped** (with a one-line nudge) — never guessed
or broadcast. Only messages from the configured user_id are accepted. Stop only if the
user says to stop listening (don't relaunch).

## Mode 1 — notify when done

```bash
~/.claude/telegram/tg send "✅ Done. Key result: X. Details in research.md."
~/.claude/telegram/tg file research.md "Full notes"
~/.claude/telegram/tg photo screenshot.png "QA result"
```

Long text auto-splits (>4096); for big outputs prefer `file`. Messages are prefixed
with a project label.

**Sending text safely:** if the message may contain backticks or `$(...)`, don't pass
it in double quotes (the shell would run it). Use single quotes, or stdin:
`printf '%s' "$msg" | ~/.claude/telegram/tg send -`.

## Auto-mirror after ~10 min idle

The Stop hook arms a cheap detached watcher: if you finish a turn and the user
doesn't touch the terminal for `idle_mirror_secs` (default 600s), your last message
is mirrored to Telegram once. Cancelled the instant the user types in the terminal.

## Setup (after installing the plugin)

```bash
# put your @BotFather token in ~/.claude/telegram/config.json (copy config.example.json),
# message the bot once, then:
~/.claude/telegram/tg setup     # detects chat_id + user_id
```

## Config (~/.claude/telegram/config.json)

| Key | Meaning |
|---|---|
| `token`, `chat_id`, `user_id` | Bot token; your chat; the only sender accepted |
| `always_listen` | `true` = every session auto-starts the background listener |
| `idle_mirror_secs` | Seconds of terminal idle before auto-mirroring (0 disables) |

## Commands (~/.claude/telegram/tg ...)

`send "text"` / `send -` · `file <path> [cap]` · `photo <path> [cap]` ·
`listen [maxsecs]` · `recv [timeout]` · `ask "text" [budget]` · `setup` · `drain` ·
`away on|off|active|clear|list [dir]`

## How inbound works

Telegram's `getUpdates` is single-consumer with one destructive offset, so one
session at a time (exclusive `flock`) **pumps** all updates into a shared inbox,
tagging each with the target session via its `reply_to` id looked up in the
sent-message map (`sent.map`, which records **every** outbound id — replies, nudges,
notifications — so it has no holes). Each session **claims** only messages tagged for
it. **Routing is by reply-id alone — no guessing, no broadcast:** a message that isn't
a reply, or replies to an id we can't attribute, is dropped (the user gets a one-line
nudge to reply to a session). Inbox is written+fsync'd before the offset advances
(crashes duplicate, never lose); updates de-duped by `update_id`; unclaimed messages
expire after 1h; `flock` auto-releases on death.
