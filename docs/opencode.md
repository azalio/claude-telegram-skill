# telegram-bridge для opencode

У opencode другая модель, чем у Claude Code / Codex: хуки — это TypeScript-плагин под
Bun, а не внешние команды, и нет инъекции контекста на старте сессии. Поэтому интеграция
состоит из двух частей:

- тонкий **TS-плагин**, который транслирует события opencode в те же обработчики
  `scripts/tg.py` (announce, idle-mirror, notification);
- always-listen инструкции в **`AGENTS.md`** (раз нет session-start инъекции, инструкции
  должны быть статическим правилом, которое opencode подхватывает на старте).

## Установка

### 1. Положи репозиторий куда удобно

```bash
git clone https://github.com/azalio/claude-telegram-skill
cd claude-telegram-skill
```

### 2. Настрой бота (общий конфиг)

Конфиг общий со всеми агентами — `~/.claude/telegram/config.json`. Если ещё не
настраивал:

```bash
mkdir -p ~/.claude/telegram
cp config.example.json ~/.claude/telegram/config.json
# впиши token от @BotFather, напиши боту любое сообщение, затем:
python3 scripts/tg.py setup
```

### 3. Поставь плагин в opencode

```bash
python3 scripts/tg.py install opencode
```

Что делает install:

- копирует `opencode/plugin/telegram-bridge.ts` в `$OPENCODE_CONFIG_DIR/plugin/`
  (по умолчанию `~/.config/opencode/plugin/`), подставляя абсолютный путь к `tg.py`;
- дописывает always-listen инструкции в `~/.config/opencode/AGENTS.md` между маркерами
  `<!-- telegram-bridge:begin -->` / `:end` (идемпотентно, твой текст не трогается).

### 4. Проверь

```bash
~/.claude/telegram/tg send "test from opencode"
```

Запусти opencode — на старте сессии плагин пришлёт «Сессия на связи».

## Что подключается

| Событие opencode | Обработчик `tg.py` | Что делает |
| --- | --- | --- |
| `session.created` | `hook sessionstart` | announce сессии в Telegram |
| `session.idle` | `hook stop` + inbound | arming idle-mirror + доставка входящих сообщений |
| `session.status` / `session.deleted` | — | трекинг busy/idle, очистка |
| `chat.message` | `hook userprompt` | отмена idle-mirror, когда ты пишешь в opencode |
| `permission.ask` | `hook notification` | уведомление, что агент ждёт подтверждения (только в режиме `away`) |

### Inbound: как ответы из Telegram попадают в сессию

Shell-инструмент opencode **не имеет** фонового режима — блокирующий `tg listen` заморозил
бы ход. Поэтому inbound — plugin-driven, без участия агента:

- Плагин держит один **poll-loop на весь процесс** opencode (top-level функция плагина
  живёт всё время процесса).
- Цикл дёргает `tg.py recv` (неблокирующий pump+claim) и, когда приходит сообщение,
  инжектит его в сессию через `client.session.promptAsync` — это запускает новый ход, не
  блокируя ничего.
- Инъекция делается только когда сессия **idle** (в busy опрокинется `BusyError`); сообщения
  буферизуются и доставляются на следующем `session.idle`. Дедуп по Telegram `update_id` —
  в `tg.py`.

Агент по инструкции из `AGENTS.md` сначала отвечает в Telegram (`tg send`), потом действует.

## Особенности и ограничения

- **Нужен Bun.** opencode исполняет плагин через Bun. `import type { Plugin }` стирается
  на рантайме, поэтому ставить `@opencode-ai/plugin` не обязательно.
- **Нет session-start инъекции контекста.** Инструкции (отвечать в Telegram, не запускать
  listener) живут в `AGENTS.md`, а не инжектятся динамически. Правишь — переустанови
  (`install opencode`) или поправь блок в `AGENTS.md` руками.
- **Блокирующий shell.** У opencode нет фонового режима shell — поэтому inbound вынесен в
  плагин (см. выше), а агент НЕ запускает `tg listen`.
- **`session.idle` ≠ строго конец хода.** opencode иногда помечает idle в середине потока;
  инъекция защищена `BusyError`-catch и ре-очередью, idle-mirror — таймаутом тишины.
- **Плагин загружен дважды** (если копия и в глобальном, и в проектном `plugin/`) → два
  poll-loop. `install` кладёт в одно место (глобальный конфиг); есть и `globalThis`-guard.
- **Последнее сообщение для mirror** тянется из opencode SDK best-effort; если не удалось
  — mirror отправит generic-строку вместо текста ответа.

## Удаление

```bash
rm ~/.config/opencode/plugin/telegram-bridge.ts
```

И убери блок `telegram-bridge` из `~/.config/opencode/AGENTS.md`.
