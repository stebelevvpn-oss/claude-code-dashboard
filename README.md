# Claude Code Dashboard

> Веб-дашборд для запуска агентов [Claude Code](https://github.com/anthropics/claude-code) одной кнопкой — с прогресс-баром, экспортом результатов в TXT/PDF и тремя готовыми Wildberries-агентами «из коробки».

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D4)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)](https://www.python.org/)

---

## Что это

Локальный сервер на 127.0.0.1, который сканирует ваши агенты и скиллы Claude Code, собирает из них красивый дашборд и позволяет запускать их в headless-режиме без копания в терминале. Удобно, когда хочется быстро дёрнуть агента из браузера, увидеть прогресс и забрать результат.

В комплекте — три готовых агента под Wildberries:

- 📸 **wb-photo-downloader** — скачивает все фото товара по артикулу или URL.
- 📊 **wb-review-analyst** — собирает отзывы с фильтрацией по `nm_id` (без шума от соседних вариаций imt-группы) и выдаёт структурированный анализ + план действий.
- 🚚 **wb-supply-planner** — тянет продажи и остатки из Seller API, считает скорость продаж и формирует CSV-план распределения поставки по кластерам и складам WB (с учётом целевой оборачиваемости, недоступных складов, акций и индекса локализации). Требует WB API-токен с правом «Статистика».

> **Целевая ОС:** Windows 10 / 11

---

## ⚡ Быстрый старт

```powershell
# 1. Распакуйте проект в любую папку
# 2. Откройте PowerShell в этой папке
# 3. Запустите инсталлер
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

После установки — на рабочем столе появится ярлык **«Мои агенты»**. Двойной клик — и дашборд откроется в браузере.

---

## 🧰 Что нужно установить заранее

| # | Инструмент | Зачем | Проверка |
|---|---|---|---|
| 1 | [Python 3.10+](https://www.python.org/downloads/) — обязательно с галкой *«Add Python to PATH»* | Запуск сервера и WB-скриптов | `python --version` |
| 2 | [Claude Code CLI](https://docs.claude.com/en/docs/claude-code/overview) — `npm install -g @anthropic-ai/claude-code` | Само ядро агентов | `claude --version` |
| 3 | Microsoft Edge **или** Google Chrome | Генерация PDF (headless `--print-to-pdf`) | Уже есть на Windows |
| 4 | Pillow *(опционально)* | Конвертация webp → jpg в фото-агенте | Поставится автоматически инсталлером |

После установки Claude Code один раз авторизуйтесь: запустите `claude` в терминале и пройдите OAuth.

---

## 📦 Что делает инсталлер

`install.ps1`:

- Копирует файлы:
  - `~/.claude/registry/` — UI-сервер, build-скрипт, переводы, иконка
  - `~/.claude/agents/` — определения агентов (`.md` с YAML-frontmatter)
  - `~/wb_download.py`, `~/wb_reviews.py`, `~/wb_supply_planner.py` — WB-скрипты в домашней папке
- Устанавливает `Pillow` через pip (если pip есть)
- Добавляет 2 хука в `~/.claude/settings.json` (с merge, если файл уже существует):
  - `PostToolUse` для Edit/Write/MultiEdit → пересборка дашборда при изменении агентов
  - `SessionStart` → пересборка при старте Claude Code
- Генерирует начальный `dashboard.html`
- Создаёт ярлык **«Мои агенты»** на рабочем столе с кастомной иконкой
- Запускает сервер и открывает дашборд

---

## 🗂 Что куда устанавливается

```
%USERPROFILE%\.claude\registry\         UI-сервер, build-скрипт, переводы, иконка
%USERPROFILE%\.claude\agents\           Определения агентов (.md с YAML)
%USERPROFILE%\.claude\settings.json     +2 хука: PostToolUse, SessionStart
%USERPROFILE%\wb_download.py            Скрипт скачивания фото WB
%USERPROFILE%\wb_reviews.py             Скрипт скачивания отзывов WB
%USERPROFILE%\wb_supply_planner.py      Скрипт планировщика поставок WB
%USERPROFILE%\.wb_token                 (опц.) WB API-токен для wb-supply-planner
%USERPROFILE%\Desktop\Мои агенты.lnk    Ярлык-лаунчер
%USERPROFILE%\Desktop\wb_<артикул>\     Папка с фото после запуска агента
%USERPROFILE%\Desktop\wb_<артикул>_reviews.json   JSON с отзывами
%USERPROFILE%\Desktop\wb_supply_plan_<YYYY-MM-DD>.csv  CSV-план поставки
```

---

## 🚀 Как пользоваться

1. Двойной клик по ярлыку **«Мои агенты»** на рабочем столе.
2. Если сервер не запущен — он стартует автоматически (без чёрного окна), браузер откроется на `http://127.0.0.1:8765/`.
3. На вкладке **«Мои агенты»** найдёте `Wildberries: фото товара` и `Wildberries: анализ отзывов`. Нажмите **▶ Запустить**, введите артикул или URL, дождитесь окончания (прогресс-бар покажет ожидаемое время на основе медианы прошлых запусков).
4. По итогу — кнопки **📄 Скачать TXT**, **📕 Скачать PDF**, и **📂 Открыть** (автодетект пути к папке с результатом).

---

## 🏗 Архитектура

### `server.py` — локальный HTTP сервер на `127.0.0.1:8765`

| Метод | Эндпоинт | Что делает |
|---|---|---|
| GET | `/` | Отдаёт дашборд (HTML) |
| GET | `/api/health` | Статус сервера |
| GET | `/api/run-stats?name=X` | Медиана прошлых длительностей запусков |
| POST | `/api/run-agent` | Запускает `claude -p --agent <name> --output-format json --dangerously-skip-permissions` |
| POST | `/api/open-path` | Открывает папку/файл в Проводнике |
| POST | `/api/export-pdf` | Генерирует PDF из markdown через headless Edge `--print-to-pdf` |
| POST | `/api/rebuild` | Перегенерирует `dashboard.html` |

### `build_registry.py`
Сканирует `~/.claude/agents/`, `~/.claude/skills/` и плагины из `~/.claude/plugins/marketplaces/`, применяет переводы из `translations.json`, генерирует `dashboard.html`.

### `launch.ps1`
Умный лаунчер для ярлыка. Сначала проверяет `/api/health`. Если сервер мёртв — тихо запускает `pythonw server.py`, ждёт готовности, открывает браузер.

### Хуки в `settings.json`
- `PostToolUse` (Edit/Write/MultiEdit) → `rebuild_hook.ps1` — пересобирает дашборд при изменении агентов / скиллов / настроек
- `SessionStart` → пересборка при запуске Claude Code

---

## ❓ FAQ

<details>
<summary><b>Кнопки задизейблены / «Сервер выключен»</b></summary>

Откройте через ярлык, не двойным кликом по `dashboard.html`. С `file://` браузер блокирует запросы к localhost. Если открыто правильно — Ctrl+F5.
</details>

<details>
<summary><b>Агент работает, но ничего не делает — спрашивает подтверждения</b></summary>

Сервер запускает `claude` с флагом `--dangerously-skip-permissions`. Если убрать его — каждый шаг будет требовать ручного OK, а headless-режим не интерактивный.
</details>

<details>
<summary><b>Сервер не стартует / порт занят</b></summary>

В Диспетчере задач завершите все `python.exe` и `pythonw.exe`, потом снова откройте ярлык.
</details>

<details>
<summary><b>PDF-кнопка ничего не делает</b></summary>

Скрипт ищет Edge/Chrome в стандартных путях и через `where`. Если ни один не нашёлся — установите Edge или Chrome.
</details>

<details>
<summary><b>Хочу добавить своего агента</b></summary>

Создайте файл `~/.claude/agents/<name>.md` с YAML-frontmatter (`name:`, `description:`, `tools:`, `model:`). Хук автоматически пересоберёт дашборд. Чтобы сразу был с русским именем — добавьте запись в `~/.claude/registry/translations.json`.
</details>

<details>
<summary><b>Как остановить сервер</b></summary>

В PowerShell: `Get-Process python, pythonw | Stop-Process -Force`. Или просто закройте процесс через Диспетчер задач.
</details>

---

## 🗑 Удаление

Запустите `uninstall.ps1` из той же папки. Удалит файлы реестра и хуки из `settings.json`. Остальные ваши данные не тронет.

---

## 📄 Лицензия

[MIT](LICENSE) — делайте что хотите, только сохраните авторские права.
