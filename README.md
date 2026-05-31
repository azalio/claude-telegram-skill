# Telegram bridge — a Claude Code skill

Lets Claude Code message you on Telegram when work finishes, and have a **two-way
chat** with you while you're away from the terminal — with **no daemon and no
blocking hook**. Works across multiple concurrent Claude sessions sharing one bot.

## What it does

- **Notify when done** — `tg.sh send "..."`, plus `file`/`photo` for documents and images.
- **Two-way chat** — driven by a non-blocking `CronCreate` poll loop; you reply in
  Telegram and Claude continues. The terminal is never frozen.
- **Auto-mirror** — after ~10 min of no terminal reply, the assistant's last message
  is mirrored to Telegram once (a detached, zero-cost heads-up).
- **Multi-session routing** — several Claude sessions share one bot; you address a
  specific session by *replying* (Telegram reply-to) to its message. Inbound is
  serialized with `flock`, written before the Telegram offset advances (crash-safe),
  de-duplicated by `update_id`, and accepts messages only from your user_id.

See [`SKILL.md`](./SKILL.md) for the full behavior and command reference.

## Install (one command)

```bash
git clone git@github.com:azalio/claude-telegram-skill.git ~/.claude/skills/telegram \
  && ~/.claude/skills/telegram/install.sh
```

`install.sh` is idempotent: it makes the scripts executable, seeds `config.json`,
and wires the Stop / UserPromptSubmit / Notification hooks into
`~/.claude/settings.json` without touching your other hooks. Then:

```bash
# 1) put your @BotFather token in ~/.claude/skills/telegram/config.json
# 2) send any message (e.g. /start) to your bot in Telegram
~/.claude/skills/telegram/tg.sh setup     # auto-detects chat_id + user_id
```

Claude Code auto-discovers the skill (no restart needed). Now say e.g. "ping me on
telegram when done" or "let's discuss on telegram".

Requires `jq`, `curl`, `python3`.

## Files

| File | Role |
|---|---|
| `SKILL.md` | Skill definition + usage the agent reads |
| `tg.sh` | Outbound + CLI: `send`/`file`/`photo`/`ask`/`recv`/`away`/`setup` |
| `bridge.py` | Inbound routing/locking: pump → inbox → claim (flock, dedup, fsync-before-offset) |
| `hook-stop.sh` | Non-blocking Stop hook: arms the idle auto-mirror |
| `hook-notify.sh` | Forwards "needs permission"/idle notifications while in Telegram mode |
| `hook-userprompt.sh` | Terminal input = you're back: cancels watcher, clears mode |
| `idle-mirror.sh` | Detached 10-min idle auto-mirror watcher |
| `config.example.json` | Template — copy to `config.json` (gitignored, holds your token) |

## Security

`config.json` (bot token, chat_id, user_id) and all runtime state
(`.state`, `sent.map`, `inbox.jsonl`, `away.d/`, `idle.d/`, `.lock`) are gitignored.
Never commit your token.
