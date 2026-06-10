# SafeShot Bot — состояние проекта (handoff / аудит)
_Снимок на 2026-06-10. Назначение: не потерять нить; точка входа для нового чата._

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
| `screenshot.py` | Playwright + Pillow: запуск Chromium с hardened/perf-флагами, route-handler (SSRF на каждый запрос + рез тяжёлого контента), clamp высоты, нарезка, рестарт/50, reconnect |
| `metadata.py` | httpx-метаданные с лимитом размера тела, ручной разбор редиректов с SSRF-проверкой каждого hop |
| `queue_manager.py` | Очередь: глубина + per-chat квота + RAM-watchdog + дедуп + таймаут; один воркер под супервизором |
| `cache.py` | RAM-кэш (file_id + meta), sha256-ключ, дифференцированный TTL, негативный кэш |
| `config.py` | Env-конфиг, fail-closed allow-list групп, лимиты ресурсов |
| `main.py` | FastAPI (`/`,`/ping`,`/health`) + aiogram polling + graceful shutdown + loguru |
| `Dockerfile` | `mcr.microsoft.com/playwright/python:v1.60.0-noble`, non-root pwuser, dumb-init |
| `render.yaml` | Blueprint: web/docker/free, healthCheckPath `/ping`, секреты `sync:false` |
| `docker-compose.yml` | Локальный запуск с усиленной изоляцией (cap_drop, read_only, лимиты) |
| `tests/test_security.py` | Smoke-тесты SSRF-фильтра (11 шт.) |

---

## 3. ЛЕДЖЕР: что применено / запушено
_Сессия 2026-06-09…10: применён P0-батч улучшений по аудиту (5 коммитов, по одному файлу на этап) + полностью переписан README. Всё PUSHED, проверено в логах Render (один полный скриншот-успех + штатный text-fallback OLX, без регрессий; сдвиг номеров строк в логах подтверждает деплой)._

| Файл | Последнее изменение | Статус |
|---|---|---|
| `bot.py` | **Этап 1 — T-3:** `_dup_seen.pop(...)` добавлен в ветку `if not security.is_safe(url)`. Повтор заблокированной ссылки больше НЕ копится как «дубликат» и НЕ ведёт легитимного юзера к mute. Анти-фишинг баннер (был ⚠ «push не подтверждён») запушен этим же целым файлом — расхождение закрыто. | **PUSHED** ✅ |
| `screenshot.py` | **Этап 2 — R-2 + P-1.** R-2: рестарт браузера срабатывает и при `_browser is None or not _browser.is_connected()` (аварийное самовосстановление при OOM-килле рендерера — без ожидания 50 отказов до планового рестарта). P-1: `parts = await asyncio.to_thread(_split_image, full_png)` — нарезка/encode Pillow больше не блокирует event loop. | **PUSHED** ✅ |
| `queue_manager.py` | **Этап 3 — R-1:** воркер обёрнут в `_worker_supervised` (перезапуск при краше, `CancelledError` пробрасывается). `start_worker` создаёт супервизорный таск. Раньше падение воркера = молчаливое вечное зависание очереди при «зелёном» health. | **PUSHED** ✅ |
| `metadata.py` | **Этап 4 — H-1:** `follow_redirects=False` + ручной цикл (`_MAX_HOPS=4`) с `is_safe` на КАЖДЫЙ hop ПЕРЕД запросом. Закрыт blind-SSRF: раньше httpx с `follow_redirects=True` коннектился к внутреннему хосту через цепочку 30x до проверки финального URL. Логика проверена на mock-транспорте (к IMDS запрос не уходит). | **PUSHED** ✅ |
| `main.py` | **Этап 5.** `/health`: +флаг `worker_ok` (R-1), `is_connected()` в `browser_ok`, кеш `getMe` 20 с (M-1, анти-амплификация). Graceful shutdown: единый обработчик `SIGTERM`+`SIGINT` + `server.install_signal_handlers = lambda: None` (пункт №3) — убрал косметический `RuntimeError: Event loop is closed` при редеплое; локальный Ctrl+C теперь идёт через graceful-путь. | **PUSHED** ✅ |
| `README.md` | Полностью переписан по аудиту: возможности, архитектура, ENV, лимиты-константы, security-модель (SSRF/изоляция/анти-DoS + честные known-limitations), deploy Render/VPS, troubleshooting, roadmap. | **PUSHED** ✅ |
| `config.py`, `security.py`, `cache.py` | без изменений в этой сессии | **PUSHED** ✅ |
| `Dockerfile`, `render.yaml`, `docker-compose.yml`, `tests/` | без изменений в этой сессии | **PUSHED** ✅ |

> Расхождений код/GitHub больше нет: бывший ⚠ по `bot.py` закрыт (этап 1 пушил весь файл с баннером).

---

## 4. Очередь «возможно позже» (код не трогаем, пока не скажешь)

**Закрыто в этой сессии:**
- ~~`main.py` graceful shutdown (№3 — отключение собственного SIGTERM-handler uvicorn)~~ → ✅ применено (этап 5).
- Добавлен супервизор воркера (R-1, этап 3). **Важно:** это НЕ то же, что guard'ы №5/№6 ниже — те остаются открытыми.

**Ещё открыто (точечное, осмысленно для Render Free):**
1. `queue_manager.py` — два guard'а (отдельно от супервизора):
   - №5: защита от повторного `start_worker()` (`if _worker_task and not _worker_task.done(): return`).
   - №6: `if _processor is None: raise RuntimeError(...)`.
2. Опционально: анти-фишинг баннер и на кэш-хитах (правка `_send_from_cache` в `bot.py`).

**Сознательно НЕ делаем (для Render Free смысла нет / не про надёжность одного инстанса):**
- `asyncio.Lock` вокруг `enqueue` (№1 — на single-thread asyncio не нужен).
- mid-task RAM-kill (№2 — при ~130 МБ из 512 смысла нет; вернуться при росте RAM).
- **H-2 pin-to-IP** против остаточного DNS-rebinding — требует архитектурного изменения (браузер на запрос либо egress-прокси с pinned-resolver). Текущие меры (двойная проверка `is_safe` + `AsyncDns` off) окно сужают. Known-limitation, закрыть только при выносе на k8s/VPS.
- **R-3** negative-cache отдельным кешем — при `maxsize=200` вытеснение полезных записей фактически не наступает.
- Redis / stateless-реплики / egress-прокси / разбиение `bot.py` / CI+тесты / `/metrics` — трек РОСТА, не для одного инстанса на Free (всё состояние in-RAM + один polling-токен = строго один инстанс).

---

## 5. Security-постура (что закрыто)
- **SSRF / DNS-rebinding / cloud-metadata / internal-IP**: `is_safe()` резолвит все A+AAAA, нормализует IPv4-mapped, блок приватных/loopback/link-local/0.0.0.0/8, только порты 80/443, блок внутренних суффиксов. Вызовы: перед очередью, повторно перед `goto`, на КАЖДЫЙ запрос Chromium (route handler), и **на КАЖДЫЙ hop редиректов httpx** (H-1: `follow_redirects=False` + ручной цикл — закрыт blind-SSRF через 30x). `AsyncDns` сближает резолв Chromium с системным (как в is_safe).
- **RCE контейнера**: свежий Chromium (Playwright 1.60) + non-root pwuser (+ настоящий sandbox локально через `CHROMIUM_SANDBOX=on`).
- **DoS/OOM**: per-chat квота, RAM-watchdog, лимит тела httpx (2 МБ), clamp высоты, SEMAPHORE=1, рестарт/50 + reconnect.
- **Secure-by-default**: fail-closed allow-list групп; секреты только через env; `/health` без раскрытия внутренностей (только булевы флаги browser/bot/worker).
- **Тяжёлый контент**: режутся media/websocket/fonts/реклама-трекеры; on-device AI-модель не качается.

Остаточные риски (Render Free, честно):
- `--no-sandbox` неизбежен (нет userns/seccomp) → компенсировано non-root.
- Egress-фильтра на Free нет → защита от SSRF на app-уровне (`is_safe`). Полная — только VPS/`nftables`.
- Остаточное окно DNS-rebinding (H-2) без pin-to-IP — сужено, не закрыто. См. §4.
- Сервис засыпает без внешнего входящего HTTP → нужен внешний пингер на `/ping` (UptimeRobot/cron-job.org, ~10 мин).

---

## 6. Известные поведения (НЕ баги)
- **OLX**: `Screenshot failed: TimeoutError` → текстовый фолбэк за ~85–90с. Тяжёлый/анти-бот сайт, так было и до оптимизаций. ELMIR/Hotline рендерятся полностью (4 части) — конвейер исправен.
- **Cloudflare-челлендж** (`title=Just a moment...`): бот может заскриншотить страницу-заглушку, а не контент. Анти-бот лимит, не регрессия.
- **TelegramConflictError** при редеплое: новый инстанс начинает polling, пока старый ещё жив пару секунд. Само проходит, когда старый получает SIGTERM. Тревога только если длится >1–2 мин → форс-рестарт пустым коммитом.
- **`RuntimeError: Event loop is closed`** при редеплое: ~~косметика~~ → ✅ **ИСПРАВЛЕНО** (этап 5: единый SIGTERM/SIGINT-handler + отключение signal-handlers uvicorn → наш `shutdown()` штатно закрывает браузер до сноса loop). Не должно появляться в логах нового инстанса; если появилось — проверить, что `main.py` соответствует леджеру.
- **Поток `GET /ping 200`** в логах: внутренний health-check Render (`healthCheckPath: /ping`). Не держит от засыпания — для этого внешний пингер.

---

## 7. Проверенные факты (не перепроверять в новом чате)
- Playwright `1.60.0` ↔ образ `mcr.microsoft.com/playwright/python:v1.60.0-noble` (версии должны совпадать; браузеры предустановлены, `playwright install` не нужен).
- `Pillow>=12.2` (закрыт CVE-2026-40192). CVE-2024-28219 был исправлен ещё в Pillow 10.3.0.
- `render.yaml`: `type: web`, `runtime: docker`, `plan: free`, `healthCheckPath`, секреты `sync: false`.
- Render Free спит после ~15 мин без ВНЕШНЕГО входящего HTTP; внутренние health-check не считаются.
- Managed policies (как в just-the-browser) Playwright-Chromium **НЕ читает** → перенесено launch-флагами.
- Невалидные имена в `--disable-features` Chromium молча игнорирует (не падает).
- `server.install_signal_handlers = lambda: None` корректно отключает собственные signal-handler'ы uvicorn (вызывается без аргументов внутри `serve()`), отдавая SIGTERM/SIGINT нашему `shutdown()`.

---

## 8. Как продолжить в новом чате
1. Запушить текущее (если что-то локально не закоммичено): `git add . && git commit -m "stable" && git push`
2. Свежий дамп (на своей машине, из папки репозитория):
   `{ echo '# Дамп'; for f in $(find . -name "*.py" -o -name "Dockerfile" -o -name "*.yml" -o -name "*.txt" | grep -v .git | sort); do echo "## $f"; echo '```'; cat "$f"; echo '```'; done; } > project_dump.md`
3. В новый чат принести: `HOW_TO_HELP_ME.md` + `PROJECT_STATE.md` + `project_dump.md`.

## 9. Памятка-команды
- Push: `git add . && git commit -m "что и зачем" && git push`
- Форс-рестарт (TelegramConflictError): `git commit --allow-empty -m "restart" && git push`
- Проверить health: открыть `https://safeshot-bot.onrender.com/health` → ждём `{"status":"ok","browser":true,"bot":true,"worker":true}`
