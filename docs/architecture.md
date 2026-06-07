# Telegram Bridge Architecture

## Overview

`claude-telegram-skill` packages `telegram-bridge`, a Claude Code plugin that
lets a user receive Telegram notifications from Claude sessions and reply back
to a specific active session. It is intentionally small: plugin metadata and
hook declarations load one standard-library Python script, while durable bot
state lives under `~/.claude/telegram/` rather than inside the plugin checkout.

## Scope

In scope:

- Claude Code plugin metadata and marketplace packaging.
- A Telegram skill document that teaches Claude how to notify, listen, and
  reply safely.
- Session hooks for start, stop, prompt submit, and notification events.
- Standard-library Python Telegram bridge runtime with send, file, photo,
  setup, receive, listen, ask, drain, away, and hook subcommands.
- Multi-session inbound routing through reply-to message IDs, file locks, a
  shared inbox, and a sent-message map.
- Offline e2e tests that mock Telegram API calls.

Out of scope:

- A daemon or server process separate from Claude sessions.
- Storing bot tokens, chat IDs, offsets, inbox, or locks in the repository.
- Supporting arbitrary Telegram users; inbound control is allow-listed by the
  configured `user_id`.
- Non-Telegram notification providers.

## Quality Goals

- Keep terminal sessions non-blocking while still allowing away-from-terminal
  replies.
- Route inbound messages only by explicit Telegram reply-to relationship, never
  by guessing or broadcasting.
- Avoid losing Telegram updates by writing inbox state before advancing the
  offset.
- Keep installation/update safe by separating plugin code from user secrets and
  runtime state.
- Keep the runtime easy to audit by using a single Python file with no third
  party dependencies.

## System Context

Claude Code loads the plugin manifest and hook config from the repository.
Session hooks invoke `scripts/tg.py`, which talks to the Telegram Bot API over
HTTPS and stores runtime state under `~/.claude/telegram/` or `TG_STATE_DIR`.
The human user talks to the bot from Telegram. Multiple Claude sessions can use
one bot because `tg.py` coordinates `getUpdates` through `flock` and routes
claimed messages by session key.

## Core Structure

| Path | Responsibility |
|------|----------------|
| `.claude-plugin/plugin.json` | Plugin identity, description, version, repository, license, and keywords. |
| `.claude-plugin/marketplace.json` | Local marketplace entry for installing the plugin from this repo. |
| `hooks/hooks.json` | Claude hook declarations for `SessionStart`, `Stop`, `UserPromptSubmit`, and `Notification`. |
| `skills/telegram/SKILL.md` | Skill instructions for sending notifications, running background listeners, and handling replies. |
| `scripts/tg.py` | Standard-library Telegram bridge runtime and hook handler. |
| `config.example.json` | Template copied to `~/.claude/telegram/config.json`. |
| `tests/test_e2e.py` | Offline structure and routing tests with a fake Telegram API. |

## Runtime Flows

### Setup

1. The user installs the plugin and copies `config.example.json` to
   `~/.claude/telegram/config.json`.
2. The user fills the bot token, messages the bot once, and runs
   `~/.claude/telegram/tg setup`.
3. `setup` reads Telegram updates, stores `chat_id` and `user_id`, and keeps the
   config file mode restricted.

### Session Start and Listening

1. `SessionStart` invokes `tg.py hook sessionstart`.
2. The hook writes or refreshes the stable `~/.claude/telegram/tg` launcher.
3. When `always_listen` is enabled, a session can start `tg listen` in the
   background.
4. A per-session singleton lock prevents multiple listeners for the same
   session.

### Outbound Notification

1. `tg send`, `tg file`, or `tg photo` loads the config and sends through the
   Telegram Bot API.
2. Text messages are prefixed with the session label where available and split
   into Telegram-sized chunks.
3. Every outbound message ID is recorded in `sent.map`, including nudges and
   notifications, so later replies can be attributed.

### Inbound Routing

1. A listener or receive cycle obtains the shared lock.
2. `_pump()` calls Telegram `getUpdates`, rejects unapproved `user_id`s, and
   looks up each message's `reply_to_message.message_id` in `sent.map`.
3. Attributed messages are written to `inbox.jsonl` and fsync'd before the
   Telegram offset advances.
4. The addressed session claims only messages tagged with its session key.
5. Plain messages or replies to unknown/nudge IDs are dropped with a nudge
   rather than delivered to the wrong session.

### Idle Mirror

1. The `Stop` hook can arm a detached idle watcher.
2. If the terminal remains idle for `idle_mirror_secs`, the last message is
   mirrored to Telegram once.
3. User terminal activity cancels the mirror.

## Source of Truth

- Plugin packaging: `.claude-plugin/plugin.json` and
  `.claude-plugin/marketplace.json`.
- Hook behavior: `hooks/hooks.json` plus `scripts/tg.py hook ...`.
- Runtime behavior: `scripts/tg.py`.
- User-facing skill contract: `skills/telegram/SKILL.md` and `README.md`.
- Persistent runtime state: `~/.claude/telegram/` or `TG_STATE_DIR`, especially
  `config.json`, `state`, `sent.map`, `inbox.jsonl`, locks, and reply targets.
- Regression evidence: `tests/test_e2e.py`.

## Cross-cutting Concepts

- State/code separation: plugin files are reinstallable; bot credentials and
  offsets are outside the repo.
- Reply-to-only routing: inbound Telegram messages must reply to a bot message
  whose ID maps to a session.
- Single-consumer coordination: `getUpdates` is protected by `flock` so one
  session pumps updates for all sessions.
- Crash-safe inboxing: inbox writes happen before offset advancement, favoring
  duplicate handling over message loss.
- Session labels and keys: environment variables such as `TG_KEY`, `TG_CWD`,
  and `TG_LABEL` control routing identity and message headers.

## Deployment/Operations

- Install through the Claude plugin marketplace commands documented in
  `README.md`.
- Configure `~/.claude/telegram/config.json` from `config.example.json`; never
  commit real token or chat data.
- Run `python3 tests/test_e2e.py` for offline validation.
- Run `claude plugin validate . --strict` where Claude's plugin validator is
  available.
- The project has no build step and no runtime daemon; hooks and explicit skill
  commands execute `scripts/tg.py` directly.

## Known Risks/Gaps

- Telegram `getUpdates` has a destructive offset, so routing correctness depends
  on `sent.map`, `inbox.jsonl`, and fsync discipline.
- If the user sends a plain non-reply message, the bridge intentionally drops it
  because there is no safe session target.
- Markdown parse mode can fail for arbitrary text; the runtime retries without
  Markdown for plain sends.
- Background listening depends on Claude/session tooling correctly handling
  long-running background tasks and exit codes.
- The e2e suite mocks Telegram and does not prove live Bot API connectivity.

## ADR Links

No dedicated ADR files were found. The routing decisions are documented in
`README.md`, `skills/telegram/SKILL.md`, inline comments in `scripts/tg.py`, and
`tests/test_e2e.py`.

## Freshness

Reviewed on 2026-06-04 against `README.md`, `.claude-plugin/plugin.json`,
`hooks/hooks.json`, `skills/telegram/SKILL.md`, `scripts/tg.py`,
`config.example.json`, and `tests/test_e2e.py`.

Refresh reason: daily architecture workflow found this active software project
without either `docs/architecture.md` or `docs/ARCHITECTURE.md`, so the
canonical architecture document was created at `docs/architecture.md`.
