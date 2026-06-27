# Telegram Bridge Architecture

## Overview

`claude-telegram-skill` packages `telegram-bridge`, which lets a user receive
Telegram notifications from terminal AI-agent sessions and reply back to a
specific active session. It serves three agents from one runtime: **Claude Code**
(plugin), **OpenAI Codex CLI** (hooks merged into `~/.codex/hooks.json`), and
**opencode** (a thin TS plugin). It is intentionally small: each agent's hook
mechanism loads one standard-library Python script (`scripts/tg.py`), while
durable bot state lives under `~/.claude/telegram/` rather than inside any
checkout, so one bot and one state dir serve all three agents at once.

The runtime is agent-agnostic by design: routing identity and labels come from
environment variables (`TG_CWD`/`TG_KEY`/`TG_LABEL`/`TG_AGENT`), and the same
four hook handlers (`sessionstart`/`stop`/`userprompt`/`notification`) are driven
by each agent's native hook system. Only thin per-agent adapters differ.

## Scope

In scope:

- Claude Code plugin metadata and marketplace packaging.
- A Telegram skill document that teaches Claude how to notify, listen, and
  reply safely.
- Per-agent adapters: Claude Code hooks (`hooks/hooks.json`), a Codex hooks
  template merged into `~/.codex/hooks.json`, and an opencode TS plugin — all
  installed via `tg.py install codex|opencode` and routed into the same handlers.
- Session hooks for start, stop, prompt submit, and notification events.
- Standard-library Python Telegram bridge runtime with send, file, photo,
  setup, receive, listen, ask, drain, away, install, and hook subcommands.
- Multi-session, multi-agent inbound routing through reply-to message IDs, file
  locks, a shared inbox, and a sent-message map.
- Offline e2e tests that mock Telegram API calls.

Out of scope:

- A daemon or server process separate from the agent sessions.
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

Each agent loads its hook config and invokes `scripts/tg.py`: Claude Code from
the plugin manifest and `hooks/hooks.json`, Codex from `~/.codex/hooks.json`,
opencode through its TS plugin (which spawns `tg.py hook ...`). `tg.py` talks to
the Telegram Bot API over HTTPS and stores runtime state under
`~/.claude/telegram/` or `TG_STATE_DIR`. The human user talks to the bot from
Telegram. Multiple sessions across all three agents can share one bot because
`tg.py` coordinates `getUpdates` through `flock` and routes claimed messages by
session key.

## Core Structure

| Path | Responsibility |
|------|----------------|
| `.claude-plugin/plugin.json` | Claude Code plugin identity, description, version, repository, license, and keywords. |
| `.claude-plugin/marketplace.json` | Local marketplace entry for installing the Claude Code plugin from this repo. |
| `hooks/hooks.json` | Claude Code hook declarations for `SessionStart`, `Stop`, `UserPromptSubmit`, and `Notification`. |
| `codex/hooks.json` | Codex hooks template (`__TG_PY__` placeholder); merged into `~/.codex/hooks.json` by `tg.py install codex`. `PermissionRequest` maps to the notification handler. |
| `opencode/plugin/telegram-bridge.ts` | opencode TS plugin: maps `session.created`/`session.idle`/`chat.message`/`permission.ask` to `tg.py hook ...`; installed by `tg.py install opencode`. |
| `skills/telegram/SKILL.md` | Skill instructions for sending notifications, running background listeners, and handling replies. |
| `scripts/tg.py` | Standard-library Telegram bridge runtime, hook handlers, and the `install` subcommand. |
| `config.example.json` | Template copied to `~/.claude/telegram/config.json` (shared by all agents). |
| `tests/test_e2e.py` | Offline structure, install, helper, and routing tests with a fake Telegram API. |
| `docs/codex.md`, `docs/opencode.md` | Per-agent setup guides. |

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

- Agent-agnostic core + thin adapters: the four hook handlers and the routing
  runtime are shared; each agent supplies only an adapter that feeds the same
  stdin-JSON contract (`cwd`, `session_id`, `last_assistant_message`, etc.) and,
  for Codex, consumes the same `hookSpecificOutput.additionalContext` envelope.
  `TG_AGENT` tailors only the always-listen phrasing (true background task for
  Claude Code vs bounded `listen 600` for the blocking shells of Codex/opencode).
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
- Background listening depends on agent/session tooling correctly handling
  long-running background tasks and exit codes. Only Claude Code has a true
  non-blocking background task; Codex/opencode shells block the turn, so they use
  a bounded `listen 600` loop instead of a single long poll.
- opencode has no session-start context injection, so its always-listen
  instructions are a static `AGENTS.md` block (rewritten on reinstall), not a
  dynamic per-session injection like Claude Code's and Codex's `additionalContext`.
- Codex gates non-managed hooks behind a per-hash trust prompt; a changed `tg.py`
  must be re-trusted, and Codex's hooks subsystem is relatively new/version-sensitive.
- The e2e suite mocks Telegram and does not prove live Bot API connectivity, nor
  does it run the real Codex/opencode hook runtimes (it tests install + handlers).

## ADR Links

No dedicated ADR files were found. The routing decisions are documented in
`README.md`, `skills/telegram/SKILL.md`, inline comments in `scripts/tg.py`, and
`tests/test_e2e.py`.

## Freshness

Reviewed on 2026-06-27 against `README.md`, `.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json`, `hooks/hooks.json`, `codex/hooks.json`,
`opencode/plugin/telegram-bridge.ts`, `skills/telegram/SKILL.md`,
`scripts/tg.py`, `config.example.json`, `tests/test_e2e.py`, `docs/codex.md`,
and `docs/opencode.md`.

Refresh reason: added Codex and opencode adapters. The runtime was generalized to
be agent-agnostic (shared hook handlers + `install` subcommand + `TG_AGENT`
phrasing), and per-agent adapters (Codex hooks template, opencode TS plugin) were
introduced. The core shape is unchanged: a single standard-library Telegram bridge
script serves all three agents, while bot credentials and runtime state remain
outside any checkout.
