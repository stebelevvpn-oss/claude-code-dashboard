# Claude Code Registry — пакет для переноса

Дашборд агентов Claude Code с веб-интерфейсом для запуска, прогресс-баром, экспортом результатов в TXT/PDF, и двумя готовыми Wildberries-агентами:

- **wb-photo-downloader** — скачивает все фото товара по артикулу/URL.
- **wb-review-analyst** — скачивает отзывы по конкретному артикулу (с фильтрацией от соседних вариаций в imt-группе) и выдаёт структурированный анализ + план действий.

Целевая ОС: **Windows 10/11**.

---

## Что нужно установить заранее

1. **Python 3.10+** — скачать https://www.python.org/downloads/ Обязательно отметить «Add Python to PATH» при установке. Проверка: `python --version`.

2. **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code` (нужен Node.js). После установки авторизоваться: `claude` → пройти OAuth в браузере. Проверка: `claude --version`.

3. **Microsoft Edge или Google Chrome** — нужны для генерации PDF. На Windows обычно есть по умолчанию.

4. **(Опционально) Pillow** — для конвертации фото из webp в jpg в `wb-photo-downloader`. Установится автоматически инсталлером, если есть pip.

---

## Установка

1. Распакуйте zip в любую папку (например, `C:\temp\claude-registry-bundle\`).

2. Откройте PowerShell в этой папке (Shift+правый клик → «Открыть окно PowerShell здесь»).

3. Запустите:
   ```
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   Скрипт:
   - Скопирует файлы в `~/.claude/registry/`, `~/.claude/agents/` и в домашнюю папку (`~/wb_download.py`, `~/wb_reviews.py`)
   - Установит Pillow через pip (если pip есть)
   - Добавит хуки в `~/.claude/settings.json` (с merge, если файл уже существует)
   - Сгенерирует начальный дашборд
   - Создаст ярлык **«Мои агенты»** на рабочем столе с кастомной иконкой
   - Запустит сервер и откроет дашборд в браузере

4. Готово. В будущем для запуска используйте ярлык на рабочем столе.

---

## Что куда устанавливается

```
%USERPROFILE%\.claude\registry\         — UI-сервер, build-скрипт, переводы, иконка
%USERPROFILE%\.claude\agents\           — определения агентов (.md с YAML frontmatter)
%USERPROFILE%\.claude\settings.json     — добавляются 2 хука: PostToolUse + SessionStart
%USERPROFILE%\wb_download.py            — скрипт скачивания фото WB
%USERPROFILE%\wb_reviews.py             — скрипт скачивания отзывов WB
%USERPROFILE%\Desktop\Мои агенты.lnk    — ярлык-лаунчер
%USERPROFILE%\Desktop\wb_<артикул>\     — папка с фото после запуска агента
%USERPROFILE%\Desktop\wb_<артикул>_reviews.json — JSON с отзывами
```

---

## Как пользоваться

1. Двойной клик по ярлыку **«Мои агенты»** на рабочем столе.
2. Если сервер не запущен — он стартует автоматически (без чёрного окна), потом откроется браузер на `http://127.0.0.1:8765/`.
3. На вкладке **«Мои агенты»** найдёте `Wildberries: фото товара` и `Wildberries: анализ отзывов`. Нажмите **▶ Запустить**, введите артикул или URL, дождитесь окончания (прогресс-бар покажет ожидаемое время).
4. После результата — кнопки **📄 Скачать TXT**, **📕 Скачать PDF**, и автодетект пути к папке с результатом (**📂 Открыть**).

---

## Часто задаваемые

**Кнопки задизейблены / «Сервер выключен»**
Откройте через ярлык, не через двойной клик по `dashboard.html`. С `file://` браузер блокирует запросы к localhost. Если открыто правильно — сделайте Ctrl+F5.

**Агент работает, но команды не выполняет, спрашивает подтверждение**
Сервер уже запускает `claude` с флагом `--dangerously-skip-permissions`. Если убрать его — каждый шаг агента будет требовать ручного OK, а headless-режим не интерактивный.

**Сервер не стартует / порт занят**
В Диспетчере задач завершите все `python.exe` и `pythonw.exe`, потом снова откройте ярлык.

**PDF-кнопка ничего не делает**
Скрипт ищет Edge/Chrome в стандартных путях и через `where`. Если ни один не нашёлся — установите Edge или Chrome.

**Хочу добавить своего агента**
Создайте файл `~/.claude/agents/<name>.md` с YAML-frontmatter (`name:`, `description:`, `tools:`, `model:`). Хук автоматически пересоберёт дашборд. Чтобы сразу был с русским именем — добавьте запись в `~/.claude/registry/translations.json`.

**Как остановить сервер**
В PowerShell: `Get-Process python, pythonw | Stop-Process -Force`. Или просто закройте процесс через Диспетчер задач.

---

## Архитектура (вкратце)

- **`server.py`** — локальный HTTP сервер на 127.0.0.1:8765. Эндпоинты:
  - `GET /` — отдаёт дашборд
  - `GET /api/health` — статус
  - `GET /api/run-stats?name=X` — медиана прошлых длительностей запусков
  - `POST /api/run-agent {name, prompt}` — запускает `claude -p --agent <name> --output-format json --dangerously-skip-permissions`
  - `POST /api/open-path {path}` — открывает папку/файл в Проводнике
  - `POST /api/export-pdf {content, title}` — генерирует PDF из markdown через headless Edge `--print-to-pdf`
  - `POST /api/rebuild` — перегенерирует dashboard.html

- **`build_registry.py`** — сканирует `~/.claude/agents/`, `~/.claude/skills/` и плагины из `~/.claude/plugins/marketplaces/`, применяет переводы из `translations.json`, генерирует `dashboard.html`.

- **Хуки в settings.json:**
  - `PostToolUse` (Edit/Write/MultiEdit) → `rebuild_hook.ps1` — пересобирает дашборд при изменении агентов/скиллов/настроек
  - `SessionStart` → пересобирает дашборд при запуске Claude Code

- **`launch.ps1`** — умный лаунчер для ярлыка. Проверяет `/api/health`. Если сервер мёртв — запускает `pythonw server.py` тихо, ждёт готовности, открывает браузер.

---

## Удаление

Запустите `uninstall.ps1` в той же папке. Удалит файлы реестра и хуки из settings.json. Остальные ваши данные не тронет.
