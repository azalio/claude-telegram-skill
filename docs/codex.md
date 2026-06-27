# telegram-bridge для Codex CLI

Codex CLI имеет hooks-систему, скопированную с Claude Code (`SessionStart`, `Stop`,
`UserPromptSubmit`, `PermissionRequest` и др.), поэтому интеграция почти один-в-один:
те же обработчики `scripts/tg.py`, что и у Claude Code.

## Установка

### 1. Положи репозиторий куда удобно

```bash
git clone https://github.com/azalio/claude-telegram-skill
cd claude-telegram-skill
```

`scripts/tg.py` может лежать где угодно — путь к нему зашивается в хуки при установке.

### 2. Настрой бота (общий конфиг)

Конфиг бота общий со всеми агентами — `~/.claude/telegram/config.json`. Если ты ещё
не настраивал его для Claude Code:

```bash
mkdir -p ~/.claude/telegram
cp config.example.json ~/.claude/telegram/config.json
# впиши token от @BotFather, напиши боту любое сообщение, затем:
python3 scripts/tg.py setup     # запишет chat_id и user_id
```

### 3. Подключи хуки в Codex

```bash
python3 scripts/tg.py install codex
```

Это мёрджит наши хуки в `$CODEX_HOME/hooks.json` (по умолчанию `~/.codex/hooks.json`),
подставляя абсолютный путь к `tg.py`. Идемпотентно — повторный запуск не плодит дубли
и сохраняет твои чужие хуки.

### 4. Доверь хуки

Codex прячет недоверенные хуки за trust-гейтом. При первом запуске подтверди их:

```text
/hooks
```

(или запусти Codex с `--dangerously-bypass-hook-trust` для автоматизации). При изменении
`tg.py` Codex попросит передоверить — это нормально.

### 5. Проверь

```bash
~/.claude/telegram/tg send "test from Codex"
```

Запусти Codex — на старте сессии бот пришлёт «Сессия на связи».

## Что подключается

| Событие Codex | Обработчик `tg.py` | Что делает |
| --- | --- | --- |
| `SessionStart` | `hook sessionstart` | announce + инъекция always-listen инструкций через `additionalContext` |
| `Stop` | `hook stop` | arming idle-mirror последнего сообщения |
| `UserPromptSubmit` | `hook userprompt` | отмена idle-mirror, когда ты в терминале |
| `PermissionRequest` | `hook notification` | Telegram-уведомление, что агент ждёт подтверждения (только в режиме `away`) |

`SessionStart` инжектит always-listen инструкции тем же форматом, что у Claude Code
(`hookSpecificOutput.additionalContext`), поэтому Codex сам крутит `tg listen` и
обрабатывает exit codes.

## Особенности и ограничения

- **Trust-гейт.** Каждый недоверенный command-hook надо подтвердить (по hash). Меняешь
  `tg.py` — подтверждаешь заново.
- **`notify` не используется.** У Codex есть упрощённый `notify` (только
  `agent-turn-complete`), но хуки покрывают больше, поэтому интеграция идёт через хуки.
- **Блокирующий shell.** В отличие от Claude Code, shell-инструмент Codex блокирует ход,
  пока команда не вернётся, поэтому инструкции советуют `tg listen 600` (ограниченный
  таймаут) с пере-запуском, а не бесконечный поллинг.
- **Версии.** Hooks в Codex относительно новые; был регресс с несрабатыванием
  `SessionStart`/`PreToolUse` в некоторых alpha-сборках. Если announce не приходит —
  проверь версию Codex и `/hooks`.

## Удаление

Открой `~/.codex/hooks.json` и убери записи, в command которых встречается путь к
`scripts/tg.py`.
