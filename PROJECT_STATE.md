# SafeShot Bot — состояние проекта (handoff / аудит)
_Снимок на 2026-06-06. Назначение: не потерять нить; точка входа для нового чата._

Переносить в новый чат: **HOW_TO_HELP_ME.md + этот PROJECT_STATE.md + project_dump.md**.

---

## 1. Что это
Telegram-бот безопасного предпросмотра ссылок. По ссылке из группы открывает страницу в изолированном headless-Chromium (Playwright), делает скриншот, режет на части (Pillow), тянет метаданные (httpx + OG/JSON-LD) и постит карточку с анти-фишинг предупреждением — чтобы человек не переходил по ссылке сам.

- Репозиторий: `github.com/Tosik017/safeshot-bot`
- Хостинг: Render Free, `https://safeshot-bot.onrender.com`, режим polling, 512 МБ RAM, non-root (`pwuser`).
- Язык интерфейса бота: украинский. Язык работы со мной: русский, термины — English.

---

## 2. Карта файлов
| Файл | Назначение |
|---|---|
| `bot.py` | Хендлеры Telegram: фильтр групп, denylist топиков, извлечение URL, trusted-sender (пропуск админов), анти-спам+mute, rate-limit, кэш, очередь, сборка карточки, анти-фишинг баннер |
| `security.py` | SSRF-фильтр `is_safe()` — единственный барьер; вызывается на 4 рубежах |
| `screenshot.py` | Playwright + Pillow: запуск Chromium с hardened/perf-флагами, route-handler (SSRF на каждый запрос + рез тяжёлого контента), clamp высоты, нарезка, рестарт/50 |
| `metadata.py` | httpx-метаданные с лимитом размера тела и проверкой финального хоста |
| `queue_manager.py` | Очередь: глубина + per-chat квота + RAM-watchdog + дедуп + таймаут; один воркер |
| `cache.py` | RAM-кэш (file_id + meta), sha256-ключ, дифференцированный TTL, негативный кэш |
| `config.py` | Env-конфиг, fail-closed allow-list групп, лимиты ресурсов |
| `main.py` | FastAPI (`/`,`/ping`,`/health`) + aiogram polling + graceful shutdown + loguru |
| `Dockerfile` | `mcr.microsoft.com/playwright/python:v1.60.0-noble`, non-root pwuser, dumb-init |
| `render.yaml` | Blueprint: web/docker/free, healthCheckPath `/ping`, секреты `sync:false` |
| `docker-compose.yml` | Локальный запуск с усиленной изоляцией (cap_drop, read_only, лимиты) |
| `tests/test_security.py` | Smoke-тесты SSRF-фильтра (11 шт.) |

---

## 3. ЛЕДЖЕР: что применено / запушено
| Файл | Последнее изменение | Статус |
|---|---|---|
| `screenshot.py` | hardened+perf launch args; route-handler рез media/ws/fonts/трекеры; just-the-browser флаги (`OptimizationGuideOnDeviceModel,OptimizationGuideModelDownloading,AsyncDns`); `JITLESS` env-выключатель (off) | **PUSHED, живой** ✅ |
| `bot.py` | анти-фишинг баннер: статус «Готую перегляд» не удаляется, а редактируется в стойкое предупреждение; анти-фишинг тон в ошибках | **ВЫДАН; push не подтверждён** ⚠ — сверь с GitHub, при необходимости запушь, чтобы совпадало с этим дампом |
| `config.py`, `security.py`, `metadata.py`, `cache.py`, `queue_manager.py`, `main.py`, инфра | как при первичной сборке | **PUSHED** ✅ |

> В дампе `project_dump.md` — версия из контейнера: `screenshot.py` и `bot.py` уже с патчами. Если `bot.py` на GitHub старый — это единственное расхождение, закрывается одним push.

---

## 4. Очередь «возможно позже» (код не трогаем, пока не скажешь)
1. `queue_manager.py` — два guard'а:
   - №5: защита от повторного `start_worker()` (`if _worker_task and not _worker_task.done(): return`).
   - №6: `if _processor is None: raise RuntimeError(...)`.
2. `main.py` — graceful shutdown (№3): `server.install_signal_handlers = lambda: None`, чтобы отрабатывал наш `shutdown()` (закрывал браузер штатно, убрал косметический `RuntimeError: Event loop is closed` при редеплое).
3. Опционально: анти-фишинг баннер и на кэш-хитах (правка `_send_from_cache` в `bot.py`).

Сознательно НЕ делаем: `asyncio.Lock` вокруг `enqueue` (№1 — на single-thread asyncio не нужен); mid-task RAM-kill (№2 — при текущих ~130 МБ из 512 смысла нет, вернуться при росте RAM).

---

## 5. Security-постура (что закрыто)
- **SSRF / DNS-rebinding / cloud-metadata / internal-IP**: `is_safe()` резолвит все A+AAAA, нормализует IPv4-mapped, блок приватных/loopback/link-local/0.0.0.0/8, только порты 80/443, блок внутренних суффиксов. Вызовы: перед очередью, повторно перед `goto`, на КАЖДЫЙ запрос Chromium (route handler), после редиректов httpx. `AsyncDns` сближает резолв Chromium с системным (как в is_safe).
- **RCE контейнера**: свежий Chromium (Playwright 1.60) + non-root pwuser (+ настоящий sandbox локально через `CHROMIUM_SANDBOX=on`).
- **DoS/OOM**: per-chat квота, RAM-watchdog, лимит тела httpx (2 МБ), clamp высоты, SEMAPHORE=1, рестарт/50.
- **Secure-by-default**: fail-closed allow-list групп; секреты только через env; `/health` без раскрытия внутренностей.
- **Тяжёлый контент**: режутся media/websocket/fonts/реклама-трекеры; on-device AI-модель не качается.

Остаточные риски (Render Free, честно):
- `--no-sandbox` неизбежен (нет userns/seccomp) → компенсировано non-root.
- Egress-фильтра на Free нет → защита от SSRF на app-уровне (`is_safe`). Полная — только VPS/`nftables`.
- Сервис засыпает без внешнего входящего HTTP → нужен внешний пингер на `/ping` (UptimeRobot/cron-job.org, ~10 мин).

---

## 6. Известные поведения (НЕ баги)
- **OLX**: `Screenshot failed: TimeoutError` → текстовый фолбэк за ~85–90с. Тяжёлый/анти-бот сайт, так было и до оптимизаций. ELMIR рендерится полностью за ~21с (4 части) — конвейер исправен.
- **TelegramConflictError** при редеплое: новый инстанс начинает polling, пока старый ещё жив пару секунд. Само проходит, когда старый получает SIGTERM. Тревога только если длится >1–2 мин → форс-рестарт пустым коммитом.
- **`RuntimeError: Event loop is closed`** при редеплое: косметика (финализатор подпроцесса Chromium на закрытом loop). Чинится пунктом 4.2 (main.py graceful shutdown).
- **Поток `GET /ping 200`** в логах: внутренний health-check Render (`healthCheckPath: /ping`). Не держит от засыпания — для этого внешний пингер.

---

## 7. Проверенные факты (не перепроверять в новом чате)
- Playwright `1.60.0` ↔ образ `mcr.microsoft.com/playwright/python:v1.60.0-noble` (версии должны совпадать; браузеры предустановлены, `playwright install` не нужен).
- `Pillow>=12.2` (закрыт CVE-2026-40192). CVE-2024-28219 был исправлен ещё в Pillow 10.3.0.
- `render.yaml`: `type: web`, `runtime: docker`, `plan: free`, `healthCheckPath`, секреты `sync: false`.
- Render Free спит после ~15 мин без ВНЕШНЕГО входящего HTTP; внутренние health-check не считаются.
- Managed policies (как в just-the-browser) Playwright-Chromium **НЕ читает** → перенесено launch-флагами.
- Невалидные имена в `--disable-features` Chromium молча игнорирует (не падает).

---

## 8. Как продолжить в новом чате
1. Запушить текущее: `git add . && git commit -m "stable" && git push`
2. Свежий дамп (на своей машине):
   `{ echo '# Дамп'; for f in $(find . -name "*.py" -o -name "Dockerfile" -o -name "*.yml" -o -name "*.txt" | grep -v .git | sort); do echo "## $f"; echo '```'; cat "$f"; echo '```'; done; } > project_dump.md`
3. В новый чат принести: `HOW_TO_HELP_ME.md` + `PROJECT_STATE.md` + `project_dump.md`.

## 9. Памятка-команды
- Push: `git add . && git commit -m "что и зачем" && git push`
- Форс-рестарт (TelegramConflictError): `git commit --allow-empty -m "restart" && git push`
