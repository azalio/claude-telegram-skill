---
name: telegram
description: >
  Send Telegram messages to the user and have a two-way chat with them while away
  from the terminal — without any blocking hook. Three uses: (1) notify when a
  task/research finishes ("напиши в тг как закончишь", "ping me on telegram when
  done"); (2) two-way chat over Telegram, driven by a non-blocking cron poll loop
  ("обсудим в тг", "let's discuss on telegram", "I'm stepping away"); (3) end it
  ("я вернулась", "я в терминале", "I'm back", "/stop"). Also auto-mirrors the last
  message to Telegram after ~10 min of no terminal reply. Works across multiple
  concurrent Claude sessions sharing one bot. Uses a bot token; no daemon.
---

# Telegram bridge

Talk to the user over Telegram via a bot. Outbound goes through `tg.sh`; inbound
routing/locking lives in `bridge.py`. Use absolute paths under
`~/.claude/skills/telegram/`. Token + chat_id + allowed user_id live in
`config.json` (chmod 600, never printed).

**Nothing here ever blocks the terminal.** Hooks (Stop / Notification /
UserPromptSubmit, already wired in `~/.claude/settings.json`) only do cheap,
non-blocking side effects. Two-way conversation is driven by *you* (the agent) with
a `CronCreate` poll loop — see Mode 2.

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

## Mode 2 — two-way chat over Telegram (non-blocking, cron-driven)

When the user says "обсудим в тг" / "let's continue on telegram" / "I'm stepping
away", DO THIS (it never blocks the terminal):

1. Mark this session as in Telegram mode (gates hooks):
   ```bash
   ~/.claude/skills/telegram/tg.sh away on
   ```
2. Send your opening message / question with `tg.sh send`.
3. Start a **non-blocking poll loop** with `CronCreate` (session-only, recurring,
   every ~2 min). Use this exact prompt so each fire re-enters the loop, and
   **remember the returned job id**:
   > `[tg-poll] Run ~/.claude/skills/telegram/tg.sh recv 3. If it prints text, that is the user's Telegram reply — act on it and send your response with tg.sh send, then stay in Telegram mode. If the text is a return phrase (вернул / в терминал / I'm back / /stop), run CronDelete on this job and tg.sh away off and tell the user you're back. If it prints nothing, end the turn silently. Do not narrate empty polls.`

Then on every cron fire you'll check Telegram, reply over Telegram, and continue —
all while the terminal stays free. Reply latency is ~the cron interval (1–2 min);
each fire costs one model turn. Tune the interval for less cost (slower) or lower
latency (faster, min 1 min: `*/1 * * * *`).

**End the loop** (CronDelete the job + `tg.sh away off`) when:
- the user sends a return phrase over Telegram (вернулась / в терминале / I'm back / /stop), or
- the user types ANYTHING in the terminal (they're back — do this on that turn).

While in Telegram mode, keep messages chat-sized; the user is on their phone.

### Quick one-off question (single turn, may briefly wait)

For a single question where a short inline wait is fine:
```bash
~/.claude/skills/telegram/tg.sh ask "Approach A or B?"   # prints the reply (loops recv, ~120s)
```

### Replying / routing (multiple sessions)

Several sessions can use one bot. The user routes a reply to a specific session by
**replying (Telegram reply-to) to that session's message**. Non-reply messages are
broadcast and claimed by whichever session polls first. Only messages from the
configured user_id are accepted.

## Default — auto-mirror after ~10 min idle (one-way)

Even without Mode 2, the Stop hook arms a cheap detached watcher: if you finish a
turn and the user does NOT interact in the terminal for `idle_mirror_secs` (default
600s = 10 min), the watcher mirrors your last message to Telegram once. Cancelled
the instant the user types in the terminal. This is a one-way heads-up; to actually
converse, switch to Mode 2 (start the cron poll loop).

## Mode 3 — back at the terminal

The user is back when they type in the **terminal** (UserPromptSubmit hook clears
this session's marker and cancels the idle watcher) or say a return phrase in
**Telegram**. On that turn: CronDelete any running tg-poll job and run
`tg.sh away off`. Then continue normally; don't restart Mode 2 unless asked.

## First-time setup (only if config is missing/unconfigured)

1. Create a bot via [@BotFather](https://t.me/BotFather), get the token.
2. Put it in `config.json` (copy `config.example.json`), `chmod 600`.
3. Send any message (e.g. `/start`) to the bot.
4. `~/.claude/skills/telegram/tg.sh setup` — auto-detects & saves chat_id + user_id.

Check before doing this: `test -f config.json` and the token isn't the placeholder.

## Command reference (tg.sh)

| Command | What it does |
|---|---|
| `send "text"` / `send -` | Send text (or stdin); auto-split; records message_id for reply-routing |
| `file <path> [cap]` / `photo <path> [cap]` | Send a document / image |
| `recv [timeout]` | One receive cycle: lock → pump Telegram → claim this session's messages; exit 3 if none |
| `ask "text" [budget]` | Send + wait for the reply inline (loops recv; default 120s) |
| `away on\|off\|active <dir>\|clear <dir>\|list` | Per-session Telegram-mode marker (keyed by cwd) |
| `setup` / `drain` | Detect chat_id+user_id / reset offset+inbox |

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
- Two-way costs one model turn per poll interval while waiting (no cheap polling
  primitive exists). Use a 1–2 min interval and CronDelete promptly when done.
- Telegram is only checked while a session is polling (Mode 2) or via the 10-min
  one-way idle-mirror. A reply sent when nobody is polling waits in the inbox.
- Runtime state (gitignored): `.state` (offset), `sent.map`, `inbox.jsonl`,
  `away.d/`, `idle.d/`, `.lock`.
