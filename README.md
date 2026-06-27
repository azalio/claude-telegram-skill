# telegram-bridge for Claude Code, Codex & opencode

Telegram-мост для AI-агентов в терминале: агент пишет тебе в Telegram, когда задача закончена, и принимает ответы из Telegram, пока ты отошел от терминала. Один бот, одна логика (`scripts/tg.py`), три агента: **Claude Code**, **OpenAI Codex CLI** и **opencode**.

Что получится после установки:

- Агент сможет отправлять тебе сообщения, файлы и скриншоты в Telegram.
- Ты сможешь отвечать агенту прямо в Telegram.
- Несколько сессий (в т.ч. разных агентов) смогут пользоваться одним ботом.
- Никакого сервера, демона и внешней базы. Только Telegram bot token и один Python-скрипт.

Установка ниже расписана для Claude Code. Для Codex и opencode — отдельные короткие гайды: [docs/codex.md](docs/codex.md), [docs/opencode.md](docs/opencode.md). Конфиг бота (`~/.claude/telegram/config.json`) общий для всех трёх.

## Быстрый старт

Нужно примерно 5 минут.

### 1. Установи плагин

В Claude Code выполни:

```text
/plugin marketplace add azalio/claude-telegram-skill
/plugin install telegram-bridge@azalio
```

### 2. Создай Telegram-бота

Открой [@BotFather](https://t.me/BotFather) в Telegram.

Отправь:

```text
/newbot
```

BotFather попросит имя и username бота, потом выдаст token вида:

```text
123456789:AA...
```

### 3. Создай конфиг

В терминале выполни:

```bash
mkdir -p ~/.claude/telegram
nano ~/.claude/telegram/config.json
```

Вставь туда JSON и замени `PUT_YOUR_BOT_TOKEN_HERE` на token от BotFather:

```json
{
  "token": "PUT_YOUR_BOT_TOKEN_HERE",
  "chat_id": null,
  "user_id": null,
  "idle_mirror_secs": 600,
  "always_listen": true
}
```

Сохрани файл.

### 4. Перезапусти Claude Code сессию

Закрой текущую Claude Code сессию и открой новую.

После старта плагин создаст удобную команду:

```bash
~/.claude/telegram/tg
```

### 5. Напиши боту первым

Открой своего нового бота в Telegram и отправь ему любое сообщение, например:

```text
/start
```

Это обязательно: Telegram не отдаст `chat_id` и `user_id`, пока ты сам не написал боту.

### 6. Заверши настройку

В терминале выполни:

```bash
~/.claude/telegram/tg setup
```

Должно появиться что-то вроде:

```text
chat_id set to 123456789, user_id 123456789
```

Готово.

Проверь отправку:

```bash
~/.claude/telegram/tg send "test from Claude Code"
```

## Как пользоваться

Самый простой способ: просто проси Claude писать тебе в Telegram.

Примеры промптов:

```text
Сделай задачу и напиши мне в Telegram, когда закончишь.
```

```text
Запусти тесты, исправь ошибки и пришли результат в Telegram.
```

```text
Если понадобится мой ответ, спроси меня в Telegram.
```

```text
Сделай исследование, а итоговый markdown-файл отправь мне в Telegram.
```

## Как писать Claude из Telegram

Когда новая Claude Code сессия стартует, бот пришлет сообщение вида:

```text
Сессия на связи: project-name
```

Чтобы написать именно этой сессии, отвечай в Telegram реплаем на это сообщение или на любое сообщение этой сессии.

Важно: обычное сообщение без reply не будет доставлено ни в какую сессию. Это сделано специально, чтобы один бот не отправил команду не тому Claude-процессу.

## Что умеет команда `tg`

После установки доступна команда:

```bash
~/.claude/telegram/tg <command>
```

Основные команды:

| Команда | Что делает |
| --- | --- |
| `send "text"` | Отправить текст в Telegram |
| `send -` | Прочитать текст из stdin и отправить |
| `file <path> [caption]` | Отправить файл |
| `photo <path> [caption]` | Отправить картинку |
| `setup` | Автоматически записать `chat_id` и `user_id` в конфиг |
| `listen [seconds]` | Ждать сообщение из Telegram для текущей сессии |
| `ask "text" [seconds]` | Отправить вопрос и ждать ответ |
| `drain` | Сбросить offset и очистить inbox |

Примеры:

```bash
~/.claude/telegram/tg send "Готово, тесты прошли"
~/.claude/telegram/tg file report.md "Отчет"
~/.claude/telegram/tg photo screenshot.png "Скриншот"
```

## Как это работает

Вся логика — один Python-скрипт `scripts/tg.py` (только стандартная библиотека). Агенты подключаются к нему через свои механизмы хуков; обработчики хуков в `tg.py` одни и те же для всех трёх.

Hooks подключены на события (имена событий у Claude Code и Codex совпадают):

- `SessionStart`: создает launcher `~/.claude/telegram/tg` и объявляет сессию в Telegram.
- `Stop`: может отправить последнее сообщение в Telegram, если ты долго не отвечаешь в терминале.
- `UserPromptSubmit`: отменяет idle-mirror, когда ты вернулся в терминал.
- `Notification` (у Codex — `PermissionRequest`): отправляет Telegram-уведомление, если агент ждет твоего ввода (только в режиме `away`).

Как именно подключается каждый агент:

- **Claude Code** — плагин из этого репозитория, hooks из `hooks/hooks.json` (см. установку выше).
- **Codex** — `python3 scripts/tg.py install codex` мёрджит хуки в `~/.codex/hooks.json`. Те же четыре события, та же логика. [docs/codex.md](docs/codex.md).
- **opencode** — `python3 scripts/tg.py install opencode` ставит тонкий TS-плагин в `~/.config/opencode/plugin/`, который транслирует события opencode в те же обработчики `tg.py`. Always-listen инструкции пишутся в `AGENTS.md` (у opencode нет инъекции контекста на старте сессии). [docs/opencode.md](docs/opencode.md).

Состояние хранится здесь:

```text
~/.claude/telegram/
```

Там лежат config, offset, inbox, locks и routing map. В репозитории токен не хранится.

## Безопасность

Плагин принимает входящие Telegram-сообщения только от `user_id`, который записан в `~/.claude/telegram/config.json`.

Это значит:

- Если кто-то найдет твоего бота, он не сможет управлять Claude без твоего Telegram user id.
- Если у тебя несколько Claude Code сессий, сообщение доставляется только той сессии, на сообщение которой ты ответил reply.
- Сообщения без reply не угадываются и не рассылаются всем сессиям.

Никогда не коммить свой `~/.claude/telegram/config.json` и не публикуй bot token.

## Настройки

Файл настроек:

```text
~/.claude/telegram/config.json
```

Поля:

| Поле | Значение |
| --- | --- |
| `token` | Token от BotFather |
| `chat_id` | Telegram chat, куда отправлять сообщения. Заполняется через `tg setup` |
| `user_id` | Единственный Telegram user, от которого принимаются команды. Заполняется через `tg setup` |
| `idle_mirror_secs` | Через сколько секунд без ответа в терминале отправить последнее сообщение в Telegram. `600` = 10 минут, `0` = выключить |
| `always_listen` | `true` = каждая Claude Code сессия может слушать Telegram |

## Если что-то не работает

Попроси claude починить и сделай PR ;).

## Требования

- Один из агентов: Claude Code (plugins), Codex CLI (hooks) или opencode (плагины + Bun).
- Python 3.
- macOS или Linux. На Windows используй WSL.
- Telegram account и bot token от BotFather.

Python-зависимостей нет: используется только standard library. У opencode-плагина рантайм-зависимостей тоже нет (`import type` стирается Bun на старте).

## Структура репозитория

```text
.claude-plugin/plugin.json        Plugin manifest (Claude Code)
.claude-plugin/marketplace.json   Marketplace entry (Claude Code)
hooks/hooks.json                  Claude Code hooks
skills/telegram/SKILL.md          Skill-инструкция для Claude
codex/hooks.json                  Codex hooks (шаблон, ставится через install codex)
opencode/plugin/telegram-bridge.ts  opencode TS-плагин (ставится через install opencode)
scripts/tg.py                     Вся логика Telegram bridge + install
config.example.json               Пример config.json
tests/test_e2e.py                 E2E-тесты без token и сети
docs/architecture.md              Техническая архитектура
docs/codex.md                     Установка для Codex
docs/opencode.md                  Установка для opencode
```

## Проверка для разработчиков

```bash
python3 tests/test_e2e.py
claude plugin validate . --strict
claude --plugin-dir . -p "ping me on telegram when done" --bare
```

CI запускает `tests/test_e2e.py` на push и pull request. Telegram API в тестах замокан, настоящий token не нужен.

## Лицензия

MIT.
