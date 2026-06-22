# 🔐 ПОЛНЫЙ АУДИТ БЕЗОПАСНОСТИ — SafeShot Bot
### Principal Application Security Engineer Report | 2026-06-22
### Стандарты: OWASP ASVS v4, OWASP Top 10 2021, CWE, CAPEC, MITRE ATT&CK, Docker Security, CIS Benchmarks

---

## 1. EXECUTIVE SUMMARY

**Проект:** SafeShot Bot — Telegram-бот безопасного предпросмотра ссылок для групп.  
**Репозиторий:** github.com/Tosik017/safeshot-bot  
**Платформа:** Render Free (512 МБ), Python 3.12, Playwright/Chromium, aiogram 3.x, FastAPI.  
**Дата аудита:** 2026-06-22  
**Ревизия кода:** `6a31456` (main)

### Общая оценка безопасности: 6.1 / 10

Проект написан **заметно лучше среднего уровня** для подобных Telegram-ботов: присутствует явный SSRF-фильтр, многоуровневая проверка, ограничения очереди, rate limiting, non-root контейнер, структурированное логирование без diagnose. Автор **осознанно** принял ряд архитектурных компромиссов (например, `--no-sandbox`) и задокументировал их.

**Тем не менее выявлен ряд проблем:**

| Severity | Кол-во | Коротко |
|----------|--------|---------|
| 🔴 HIGH | 2 | `--no-sandbox` без seccomp (ACCEPTED); log-файл с secrets в git (✅ FIXED) |
| 🟠 MEDIUM | 4 | Блокирующий DNS в async-loop; анонимные sender'ы обходят rate-limit; `ipc: host`; pip без хэш-верификации — все 4 ✅ FIXED |
| 🟡 LOW | 5 | Digest-pin для Docker image (PARTIAL); queue position leak (INFORMATIONAL); /health без auth (✅ FIXED); gitignore/dockerignore не покрывали LOG-файл (✅ FIXED) |
| ℹ️ INFO | 8 | ALLOW_OPEN_MODE, отсутствие CI/CD SAST, нет SBOM, pip as root при сборке, и др. — без изменений |

**Статус на 2026-06-22:** V-01, V-03..V-06, V-09..V-11 устранены в этой же сессии (см. таблицу §13). Открыты только V-02 (ACCEPTED, ограничение Render Free), V-07 (PARTIAL), V-08/V-12..V-20 (INFORMATIONAL/LOW RISK/BY DESIGN, без изменений).

**Исходная критичная находка (устранена):** В git-репозитории был зафиксирован файл `Web Service safeshot-bot LOG`, содержащий **внутренний ID сервиса Render** (`srv-d8hinff7f7vs73cjmpvg`), **ID сборки**, **внутренние IP-адреса** Render-инфраструктуры и **численный ID бота Telegram** (`8101806705`). Удалён из истории через `git-filter-repo` + `git push --force` (2026-06-22); `BOT_TOKEN` в логе не присутствовал — ротация не требовалась.

---

## 2. КАРТА ПРОЕКТА

### 2.1 Структура файлов

```
safeshot-bot/
├── main.py              # Точка входа: FastAPI + aiogram Dispatcher + uvicorn
├── bot.py               # Handlers, single-message flow, anti-spam, rate limiting
├── config.py            # Конфиг из ENV, fail-fast BOT_TOKEN, SSRF-константы
├── security.py          # SSRF-фильтр: DNS resolution + IP blocklist + port whitelist
├── screenshot.py        # Playwright рендер: clip 390×640, RAM-guard, restart, stealth
├── metadata.py          # httpx meta-fetch: OG/JSON-LD, ручные редиректы, SSRF-check
├── cache.py             # RAM TTLCache: file_id + meta, SHA256-ключ, TTL по типу
├── queue_manager.py     # AsyncQueue: dedup, per-chat quota, timeout, supervisor
├── ram.py               # cgroup2/cgroup1/rss-sum — реальная RAM как OOM-killer
│
├── Dockerfile           # mcr.microsoft.com/playwright/python:v1.60.0-noble, non-root pwuser
├── docker-compose.yml   # Локал: cap_drop ALL, read_only, tmpfs, mem_limit=512m
├── render.yaml          # Render Blueprint: BOT_TOKEN sync:false, autoDeploy:true
├── requirements.txt     # Зависимости с диапазонами версий (без lock-файла)
├── .env.example         # Пример ENV (без секретов)
├── .gitignore           # .env, __pycache__, project_dump.md, *.log
├── .dockerignore        # .git, __pycache__, tests/, *.md, docker-compose.yml
│
├── placeholder.png      # 780×320 антифишинг-баннер (старт flow)
├── banner_text.png      # 780×320 банер (скрин недоступен, метаданные есть)
├── banner_fail.png      # 780×320 банер (превью не удалось)
├── banner_protected.png # 780×320 банер (Cloudflare challenge)
│
├── tests/
│   └── test_security.py # 10 smoke-тестов SSRF-фильтра
│
├── PROJECT_STATE.md     # Внутренняя документация (в репо и в Docker-образе!)
└── Web Service safeshot-bot LOG  # ❗ Render build log — секреты в git!
```

### 2.2 Схема взаимодействия компонентов

```
Telegram User
    │ (HTTPS long-polling)
    ▼
aiogram Dispatcher (bot.py → router.message())
    │
    ├─[1]  URL extraction: URL_RE regex
    ├─[2]  Message age check (MAX_MSG_AGE=60s)
    ├─[3]  Allowed group check (ALLOWED_GROUP_IDS)
    ├─[4]  Thread denylist check (DISABLED_THREADS)
    ├─[5]  Admin/trusted sender check (_is_trusted_sender)
    ├─[6]  URL length check (MAX_URL_LEN=2048)
    ├─[7]  Trusted domain whitelist (_trusted_domain → 👌 react)
    ├─[8]  Duplicate detection (_dup_seen TTLCache)
    ├─[9]  Rate limiting (_rate_cooldown TTLCache)
    ├─[10] SSRF check (security.is_safe)
    ├─[11] Cache lookup (cache.get)
    └─[12] Queue enqueue (queue_manager.enqueue)
                │
                ▼
        queue_manager._worker (asyncio)
                │
                ├─── screenshot.shoot(url)
                │       ├─ security.is_safe (повторная проверка)
                │       ├─ Playwright/Chromium (headless, --no-sandbox)
                │       │   ├─ _STEALTH_JS (navigator.webdriver=undefined)
                │       │   ├─ _route_handler (SSRF per-request + block resources)
                │       │   ├─ page.goto + wait_for_timeout
                │       │   ├─ _close_cookies
                │       │   ├─ _is_cloudflare_challenge
                │       │   └─ page.screenshot(clip=390×640)
                │       └─ _split_image (Pillow)
                │
                └─── metadata.fetch(url) [параллельно через asyncio.create_task]
                        ├─ httpx (follow_redirects=False)
                        ├─ security.is_safe на каждый hop
                        └─ selectolax HTML parser (OG + JSON-LD)

FastAPI (main.py)
    ├─ GET / HEAD /       → {"ok": true}
    ├─ GET HEAD /ping     → {"ok": true}
    └─ GET /health        → browser/bot/worker status (без auth)
```

### 2.3 Поток данных

```
ВХОД: Telegram Message → URL строка (недоверенная)
  │
  ├─ bot.py: извлечение URL + первичная фильтрация
  ├─ security.py: DNS resolution + IP check
  ├─ cache.py: проверка SHA256(canonical_url)
  ├─ queue_manager.py: постановка в очередь
  ├─ screenshot.py: Playwright render → PNG bytes
  ├─ metadata.py: httpx fetch → dict{title, price, ...}
  └─ bot.py: editMessageMedia → caption_entities → Telegram API

ВНЕШНИЕ ВЗАИМОДЕЙСТВИЯ:
  1. Telegram Bot API (api.telegram.org:443) ← polling + send
  2. Целевой сайт (любой публичный хост) ← Chromium + httpx
  3. Render Platform APIs ← healthcheck
```

---

## 3. АРХИТЕКТУРНЫЙ АУДИТ

### 3.1 Сильные стороны архитектуры

| Аспект | Оценка | Детали |
|--------|--------|--------|
| SSRF protection | ✅ Хорошо | Многоуровневая: before queue + route handler + httpx hops |
| Rate limiting | ✅ Хорошо | TTLCache per user_id, anti-duplicate escalation |
| Resource limits | ✅ Хорошо | Queue depth=10, per-chat=8, browser restart, RAM guard |
| Secrets | ✅ Хорошо | BOT_TOKEN только из ENV, sync:false в render.yaml |
| Non-root container | ✅ Хорошо | pwuser в Dockerfile |
| Error handling | ✅ Хорошо | Graceful fallback на всех уровнях |
| Logging | ✅ Хорошо | diagnose=False, backtrace=False — токены не попадают в трейсы |

### 3.2 Архитектурные недостатки

#### A-1 [HIGH]: Chromium без seccomp в production (Render)

**Описание:** `--no-sandbox` отключает Chromium Namespace Sandbox. На Render Free seccomp-профиль недоступен, пользовательские namespace тоже. Это означает, что renderer-процесс Chromium работает **без изоляции ОС** — только в рамках прав pwuser.

**Сценарий атаки (MITRE ATT&CK: T1203 — Exploitation for Client Execution):**
1. Атакующий создаёт специально сконструированную страницу с 0-day в V8/Blink.
2. Пользователь (или бот-спаммер) отправляет URL этой страницы в бот.
3. Chromium рендерит страницу → 0-day эксплуатирует renderer.
4. Без sandbox/seccomp: RCE в container namespace от имени pwuser.
5. Возможен доступ к ENV (BOT_TOKEN), к файловой системе приложения.

**CWE-693:** Protection Mechanism Failure  
**Уровень риска:** HIGH (с оговоркой: 0-day в V8 — редкое событие; принято как осознанный компромисс для Render Free)

**Исправление:**
```yaml
# docker-compose.yml — для local/VPS: добавить seccomp-профиль Playwright
security_opt:
  - no-new-privileges:true
  - seccomp=./playwright-seccomp.json
```
```python
# config.py: включить CHROMIUM_SANDBOX=on на VPS
CHROMIUM_SANDBOX = os.environ.get("CHROMIUM_SANDBOX", "").lower() == "on"
```
Для Render: добавить `JITLESS=on` снижает RCE-поверхность V8 за счёт скорости.

---

#### A-2 [MEDIUM]: Синхронный DNS в asyncio event loop (CWE-1069)

**Файл:** `security.py:71`, вызывается из `bot.py:611`, `screenshot.py:229`, `metadata.py:48`

**Код:**
```python
infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
```

**Проблема:** `socket.getaddrinfo()` — **синхронный блокирующий syscall**. При каждом вызове `is_safe()` весь asyncio event loop замирает на время DNS-запроса (30ms–5000ms). `_route_handler` вызывается для **каждого subresource** страницы — это 10–50+ DNS-запросов per page, каждый блокирует loop.

**Сценарий DoS (CAPEC-469):**
1. Атакующий отправляет URL на сайт с медленным DNS (TTL=0, slow resolver).
2. Каждый subresource вызывает `is_safe()` → DNS lookup ~5s.
3. Event loop заморожен → бот не отвечает на другие сообщения.
4. Telegram считает бота недоступным.

**CWE-1069:** Improper Handling of Blocking I/O in Asynchronous Context

**Исправление:**
```python
# security.py — async-версия:
import asyncio

async def is_safe_async(url: str) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        host = p.hostname
        if not host:
            return False
        host = host.rstrip(".")
        if host == "localhost" or any(host.endswith(s) for s in BLOCKED_HOST_SUFFIXES):
            return False
        port = p.port or (443 if p.scheme == "https" else 80)
        if port not in ALLOWED_PORTS:
            return False
        try:
            ipaddress.ip_address(host)
            return not _ip_blocked(host)
        except ValueError:
            pass
        # Выносим блокирующий DNS в тред-пул
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, type=socket.SOCK_STREAM
        )
        ips = {info[4][0] for info in infos}
        if not ips:
            return False
        return all(not _ip_blocked(ip) for ip in ips)
    except Exception:
        return False
```

---

#### A-3 [MEDIUM]: Анонимные отправители полностью обходят rate limiting

**Файл:** `bot.py:580-607`

**Код:**
```python
user_id = msg.from_user.id if msg.from_user else None

if user_id is not None:
    if (chat_id, user_id, url) in _dup_seen:
        await _handle_duplicate_spam(...)
        return
    cooldown = _rate_cooldown(user_id)
    ...
    _dup_seen[(chat_id, user_id, url)] = True
```

**Проблема:** Когда `msg.from_user` is None (Anonymous Admin, сообщение от другого бота, linked channel posting), `user_id = None`. Все блоки rate-limit и dedup внутри `if user_id is not None:` **пропускаются**.

**Сценарий атаки (CWE-770):**
```
Telegram: Анонимный admin пишет в группу 50 ссылок подряд
    → user_id = None для каждого
    → _rate_cooldown никогда не вызывается
    → _dup_seen никогда не проверяется
    → каждая ссылка (даже одинаковая) идёт в очередь
    → очередь заполняется за секунды
    → другие пользователи получают QueueFull
```

**CWE-770:** Allocation of Resources Without Limits or Throttling

**Исправление:**
```python
# bot.py — использовать fallback ключ вместо None:
if msg.sender_chat:
    rate_key = f"sender_chat:{msg.sender_chat.id}"
elif user_id is not None:
    rate_key = user_id
else:
    rate_key = f"anon:{chat_id}"  # per-chat anonymous bucket

# Применять rate_key везде вместо user_id для rate limit и dedup
```

---

## 4. АУДИТ ИСХОДНОГО КОДА

### 4.1 SSRF (Server-Side Request Forgery) — `security.py`

#### 4.1.1 Анализ защиты

**Реализованная защита:**
- Проверка схемы (`http://`, `https://` only)
- Allowlist портов (`{80, 443}`)
- Блокировка internal DNS суффиксов (`.internal`, `.local`, `.cluster.local` и др.)
- Резолвинг всех A+AAAA записей, проверка каждого IP
- Нормализация IPv4-mapped IPv6 (`::ffff:127.0.0.1` → `127.0.0.1`)
- Блокировка CGNAT (100.64.0.0/10), NAT64 (64:ff9b::/96)
- Повторная проверка в `_route_handler` на каждый subrequest
- Повторная проверка перед `page.goto()` в `screenshot.py:283`
- Ручные редиректы в `metadata.py` с проверкой каждого hop

**Оценка SSRF-защиты:** ✅ Хорошая, один из лучших примеров в open-source Telegram-ботах.

#### 4.1.2 Остаточный риск: DNS Rebinding Window [MEDIUM]

**Описание:** Между вызовом `security.is_safe()` (который резолвит DNS) и фактическим TCP-соединением Chromium существует временно́е окно. DNS-запись с TTL=0 может смениться за это время.

**Сценарий (CAPEC-275 DNS Rebinding):**
```
1. Атакующий регистрирует rebind.attacker.com с TTL=1
2. Первый запрос → 8.8.8.8 (публичный) → is_safe() = True
3. TTL истекает, DNS меняет запись на 169.254.169.254
4. Chromium делает реальный HTTP-запрос → идёт на IMDS
5. Содержимое cloud metadata попадает в screenshot или JS
```

**Снижение риска:** Route handler проверяет каждый subrequest. Но checks синхронные — OS-кэш может вернуть старый ответ.

**Рекомендация:** Добавить `--host-resolver-rules` в Chromium launch args:
```python
# screenshot.py → _launch_args():
"--host-resolver-rules=MAP 169.254.0.0/16 ~NOTFOUND, "
"MAP 100.64.0.0/10 ~NOTFOUND, "
"MAP 10.0.0.0/8 ~NOTFOUND, "
"MAP 172.16.0.0/12 ~NOTFOUND, "
"MAP 192.168.0.0/16 ~NOTFOUND, "
"MAP ::1 ~NOTFOUND, "
"MAP fc00::/7 ~NOTFOUND",
```

---

### 4.2 HTML Injection / XSS в Telegram-сообщениях

#### 4.2.1 Места с parse_mode="HTML" — анализ

Все вызовы с `parse_mode="HTML"` в `bot.py`:

```python
await msg.reply_photo(caption=_warning_caption(position), parse_mode="HTML")
await status.edit_media(media=InputMediaPhoto(caption=FAIL_CAPTION, parse_mode="HTML"))
await status.edit_caption(caption=FAIL_CAPTION, parse_mode="HTML")
await status.edit_media(media=InputMediaPhoto(caption=_protected_caption(url), parse_mode="HTML"))
```

- **`_warning_caption(position)`:** `position` — целое число из `_queue.qsize()+1`. Не управляется пользователем. ✅ Safe.
- **`FAIL_CAPTION`:** Статическая строка. ✅ Safe.
- **`_protected_caption(url)`:** Содержит `html.escape(urlsplit(url).netloc or url)`. ✅ Safe.

#### 4.2.2 Метаданные сайтов в caption — анализ

```python
# bot.py — используется caption_entities БЕЗ parse_mode
sent = await status.edit_media(media=InputMediaPhoto(
    media=...,
    caption=cap_text,
    caption_entities=cap_entities,
))
```

При использовании `caption_entities` без `parse_mode`, Telegram **не интерпретирует HTML-тэги** — весь текст отображается как literal plain text. Заголовок страницы `<script>alert(1)</script>` отобразится буквально.

✅ **XSS невозможен** через метаданные сайта.

---

### 4.3 Path Traversal — анализ

**Файл:** `bot.py:335-340`

```python
def _media(name: str):
    st = _media_state[name]
    ...
    if os.path.exists(st["file"]):
        with open(st["file"], "rb") as f:
            st["png"] = f.read()
```

`_media_state` — хардкодированный dict с именами файлов. Пути не управляются пользователем. ✅ Path Traversal невозможен.

---

### 4.4 Race Conditions — анализ

**`_rate_cooldown()` в `bot.py:50-58`:** В asyncio single-thread нет вытесняющего планирования между `.get()` и `[user_id] = ...` без `await`. ✅ Гонки нет.

**`enqueue()` в `queue_manager.py:41-76`:** Нет `await` между проверкой `_inflight` и добавлением в него. ✅ Атомарно в asyncio.

---

### 4.5 Business Logic — whitelist bypass [LOW/INFO]

**Файл:** `config.py:81-89`

Домен `fb.watch` включён в whitelist. Сокращатели ссылок Facebook (fb.watch) могут редиректить на произвольные URL, включая фишинговые. Бот поставит 👌 на fb.watch-ссылку без рендера, давая ложное ощущение безопасности.

**Рекомендация:** Рассмотреть удаление `fb.watch` из whitelist или добавить пометку о сокращателях.

---

### 4.6 Information Disclosure в логах

**Файл:** `bot.py:194`
```python
logger.warning(f"LEAVE non-allowed chat_id={chat.id} (allowed={sorted(ALLOWED_GROUP_IDS)})")
```
Логируется полный список allowed group IDs. При компрометации логов — атакующий узнаёт ID всех групп. **LOW** severity.

---

### 4.7 Cookie auto-click abuse [LOW]

**Файл:** `screenshot.py:96-100, 386-394`

```python
COOKIE_SELECTORS = [
    "button[id*='accept']", "button[class*='accept']",
    ...
]
```

**Сценарий (CAPEC-62):** Вредоносная страница создаёт кнопку с `id="accept-download"`, которая при клике инициирует вредоносное действие. `accept_downloads=False` предотвращает загрузку файлов. Навигация — будет обработана route handler'ом с SSRF-проверкой. Реальный риск минимален.

---

### 4.8 Recursive JSON-LD parsing [INFO]

**Файл:** `metadata.py:92-99`

Функция `_walk_jsonld` рекурсивная. Глубоко вложенный JSON-LD (1000+ уровней) может вызвать `RecursionError`. Исключение поймано в `except Exception: continue`. Не критично.

---

## 5. АУДИТ ЗАВИСИМОСТЕЙ И SUPPLY CHAIN SECURITY

### 5.1 Прямые зависимости

| Пакет | Версия (ranges) | Актуальная | CVE | Риск |
|-------|-----------------|------------|-----|------|
| `aiogram` | >=3.13,<4 | 3.28.2 | Нет известных критических | LOW |
| `playwright` | ==1.60.0 | 1.60.0 (pinned) | — | INFO |
| `fastapi` | >=0.115,<1 | 0.136.3 | Нет известных | LOW |
| `uvicorn` | >=0.30,<1 | 0.49.0 | Нет известных критических | LOW |
| `httpx` | >=0.27,<1 | 0.28.1 | Нет известных | LOW |
| `selectolax` | >=0.3.21,<0.4 | 0.3.34 | Нет CVE в NVD | LOW |
| `cachetools` | >=5.3,<7 | 6.2.6 | Нет известных | LOW |
| `loguru` | >=0.7,<0.8 | 0.7.3 | Нет известных | LOW |
| `psutil` | >=5.9,<8 | 7.2.2 | Нет критических | LOW |
| `Pillow` | >=12.2,<13 | 12.2.0 | CVE-2023-44271 (<=10.0.0) — **текущая версия закрыта** | LOW |

### 5.2 Транзитивные зависимости

| Пакет | Версия | Замечание |
|-------|--------|-----------|
| `pydantic` | 2.13.4 | Актуальная, без критических CVE |
| `pydantic-core` | 2.46.4 | Rust core |
| `aiohttp` | 3.13.5 | Актуальная |
| `certifi` | 2026.5.20 | Актуальная |
| `h11` | 0.16.0 | Актуальная |
| `starlette` | 1.2.1 | Актуальная |
| `greenlet` | 3.5.1 | Нативный C-модуль |

### 5.3 Критические проблемы Supply Chain

#### SC-1 [MEDIUM]: Отсутствие pip hash verification

**Проблема:** `pip install --no-cache-dir -r requirements.txt` без `--require-hashes`.

Если PyPI подвергнется атаке (BGP hijack, mirror compromise, package hijacking), установятся пакеты с изменённым кодом без предупреждения.

**CWE-494:** Download of Code Without Integrity Check

**Сценарий (MITRE ATT&CK T1195.001):**
1. Злоумышленник компрометирует PyPI mirror.
2. Новая сборка на Render загружает trojaned `aiogram`.
3. Trojaned пакет читает BOT_TOKEN из env и отправляет на C2.

**Исправление:**
```bash
# Генерация lock с хэшами:
pip-compile --generate-hashes requirements.txt > requirements.lock

# В Dockerfile:
COPY --chown=pwuser:pwuser requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
```

#### SC-2 [LOW]: Нет digest-pin для базового Docker-образа

**Dockerfile:**
```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble
# ↑ без digest
```

Из log-файла известен фактический digest, который Render использует:
```dockerfile
# Исправление:
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble@sha256:8ff591d613b01c884cc488339ed4318b4513eaf0c57a164a878ba49e70e3f384
```

#### SC-3 [INFO]: Нет lock-файла для Python зависимостей

Диапазоны версий означают, что разные сборки могут использовать разные версии пакетов.

**Исправление:** Добавить `requirements.lock` с точными версиями и хэшами.

#### SC-4 [INFO]: pip запускается от root при сборке

**Из log-файла:**
```
WARNING: Running pip as the 'root' user can result in broken permissions
```

**Исправление в Dockerfile:**
```dockerfile
RUN python -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.lock
```

---

## 6. АУДИТ СЕКРЕТОВ И КОНФИДЕНЦИАЛЬНЫХ ДАННЫХ

### 6.1 ❗ КРИТИЧЕСКАЯ НАХОДКА: Секреты в git-репозитории [HIGH]

**Файл:** `Web Service safeshot-bot LOG` (в git, публично доступен)

**Обнаруженные секреты/чувствительные данные:**

```
# Render internal registry (строка 14):
image-registry-v2.aws-us-west-2-7.internal.render.com/srv-d8hinff7f7vs73cjmpvg:bld-d8k8hg9o3t8c73aq2jo0@sha256:7ea...

# Telegram bot numeric ID (строка 251):
bot id = 8101806705

# Внутренние IP Render (строки 239-265):
10.234.24.243 — IP Render load balancer/healthcheck
10.28.44.1    — IP Render internal gateway

# Production URL (строка 247):
https://safeshot-bot.onrender.com

# Render service ID:
srv-d8hinff7f7vs73cjmpvg

# Render build ID:
bld-d8k8hg9o3t8c73aq2jo0
```

**Риски:**
- **Bot numeric ID (8101806705):** Может использоваться для social engineering против администраторов.
- **Render Service/Build IDs:** Потенциальный вектор для Render API abuse при компрометации API-ключа.
- **Внутренние IP Render:** Помогают атакующему картировать инфраструктуру.
- **Сам факт публичного лога в git:** Прецедент — возможны другие чувствительные логи в будущих коммитах.

**CWE-312:** Cleartext Storage of Sensitive Information  
**CWE-540:** Inclusion of Sensitive Information in Source Code

**Немедленные действия:**
```bash
# 1. Удалить файл из git-истории:
pip install git-filter-repo
git filter-repo --path "Web Service safeshot-bot LOG" --invert-paths

# 2. Обновить .gitignore:
echo '*LOG' >> .gitignore
echo '* LOG' >> .gitignore

# 3. Force-push:
git push --force-with-lease origin main
```

### 6.2 BOT_TOKEN Management — ✅ Хорошо

```python
# config.py:6
BOT_TOKEN = os.environ["BOT_TOKEN"]  # fail-fast, из ENV только
```
```yaml
# render.yaml
- key: BOT_TOKEN
  sync: false  # вводится вручную, НЕ в git
```
✅ Токен никогда не попадает в репозиторий.

### 6.3 Логирование секретов — ✅ Защищено

**`main.py:19-20`:**
```python
logger.remove()
logger.add(sys.stderr, level=LOG_LEVEL, backtrace=False, diagnose=False)
```

`diagnose=False` — значения локальных переменных (включая URL и токены) не попадают в трейсы. ✅

---

## 7. DOCKER SECURITY AUDIT

### 7.1 Dockerfile — детальный анализ

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble    # ← нет digest pin

RUN apt-get update \
 && apt-get install -y --no-install-recommends dumb-init \ # ✅ только нужные пакеты
 && rm -rf /var/lib/apt/lists/*                            # ✅ очистка кэша

WORKDIR /app

COPY --chown=pwuser:pwuser requirements.txt .              # ✅ ownership
RUN pip install --no-cache-dir -r requirements.txt         # ← root pip, нет хэшей

COPY --chown=pwuser:pwuser . .                             # ← анализ .dockerignore ниже

USER pwuser                                                # ✅ non-root
ENTRYPOINT ["dumb-init", "--"]                             # ✅ правильный PID 1
```

### 7.2 Что попадает в Docker-образ

`.dockerignore` содержит `*.md` — исключает PROJECT_STATE.md ✅

Но `Web Service safeshot-bot LOG` (без `.md` расширения) **попадает в образ** через `COPY . .` — не покрыт `.dockerignore`.

**Исправление:**
```
# Добавить в .dockerignore:
*LOG
*.log
```

### 7.3 [MEDIUM]: `ipc: host` в docker-compose.yml

**Проблема:** `ipc: host` предоставляет контейнеру доступ к IPC namespace хоста — shared memory, семафоры, очереди сообщений.

**Цель:** Избежать краша Chromium из-за маленького `/dev/shm` в контейнере.

**Риск (MITRE ATT&CK T1610):** При компрометации Chromium-renderer — потенциальный доступ к shared memory других процессов на хосте.

**Исправление:**
```yaml
services:
  bot:
    shm_size: '256m'    # Замена ipc: host
    # УБРАТЬ строку: ipc: host
```

### 7.4 Проверка seccomp

```yaml
# Закомментировано в docker-compose.yml:
# - seccomp=./seccomp_profile.json
```

Seccomp-профиль Playwright не добавлен в репозиторий.

**Рекомендация:**
```bash
# Скачать официальный seccomp профиль:
curl -O https://raw.githubusercontent.com/microsoft/playwright/main/browser_patches/chromium/seccomp_profile.json

# docker-compose.yml:
security_opt:
  - no-new-privileges:true
  - seccomp=./seccomp_profile.json
```

---

## 8. INFRASTRUCTURE SECURITY AUDIT

### 8.1 Render Free — текущая инфраструктура

**Достоинства:**
- Автодеплой из git
- BOT_TOKEN хранится в Render Secrets
- HTTPS по умолчанию
- Non-root контейнер
- Memory limit 512MB enforced

**Недостатки:**
- Single instance — нет HA, downtime при рестарте
- Без `--no-sandbox` нет OS-level изоляции Chromium
- Sleep режим без HTTP трафика (нужен внешний пингер)
- Нет persistent storage (кэш сбрасывается при рестарте)
- Нет seccomp профиля
- Нет network egress filtering
- Render знает BOT_TOKEN — компрометация Render = компрометация токена

### 8.2 /health endpoint [LOW]

**`main.py:43-65`:**
```python
@app.get("/health")
async def health():
    browser_ok = screenshot._browser is not None and screenshot._browser.is_connected()
    worker_ok = wt is not None and not wt.done()
    ...
    return JSONResponse(content={"status": status, "browser": browser_ok, ...})
```

Эндпоинт **публично доступен** без аутентификации. Раскрывает статус компонентов (browser down, worker down).

**Рекомендация:**
```python
HEALTH_TOKEN = os.environ.get("HEALTH_CHECK_TOKEN", "")

@app.get("/health")
async def health(x_health_token: str = Header(default="")):
    if HEALTH_TOKEN and x_health_token != HEALTH_TOKEN:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    ...
```

---

## 9. DEPLOYMENT COMPARISON

| Аспект | Render Free | Docker локально | VPS Ubuntu/Hetzner | Fly.io | Railway | Coolify/Self-hosted |
|--------|-------------|-----------------|---------------------|--------|---------|---------------------|
| **Безопасность** | ★★★☆☆ | ★★★★☆ | ★★★★★ | ★★★★☆ | ★★★☆☆ | ★★★★★ |
| **seccomp** | ✗ | ✅ (docker-compose) | ✅ | ✅ | ✗ | ✅ |
| **--no-sandbox** | Требуется | Опционально | Не нужен | Может не нужен | Требуется | Не нужен |
| **Secrets Management** | Dashboard UI | .env файл | vault/env | Fly secrets | Dashboard | Vault/env |
| **Network egress** | Нет | Cap + iptables | iptables | Нет | Нет | iptables |
| **HA/Failover** | ✗ | ✗ | Ручной | ✅ | ✅ | ✅ |
| **Playwright поддержка** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Sleep режим** | Да (Free) | Нет | Нет | Нет (paid) | Да (Free) | Нет |
| **Persistent cache** | ✗ | ✅ volume | ✅ | ✅ volume | ✗ | ✅ |
| **Мониторинг** | Базовый | Ручной | Grafana+Prometheus | Fly metrics | Базовый | Prometheus |
| **Стоимость** | $0 | $0 | ~$5/мес | ~$1-5/мес | $0-5/мес | VPS+время |
| **Сложность** | Низкая | Средняя | Высокая | Средняя | Низкая | Высокая |

**Рекомендация по безопасности (от лучшего к приемлемому):**
1. VPS (Hetzner/Oracle) + Docker + seccomp + CHROMIUM_SANDBOX=on
2. Fly.io или Railway (не засыпает, лучше изоляция)
3. Render Free — текущий выбор, приемлем при осознанных компромиссах

---

## 10. OPERATIONAL SECURITY AUDIT

### 10.1 Логирование и мониторинг

| Аспект | Текущее состояние | Риск | Рекомендация |
|--------|------------------|------|--------------|
| Уровень логирования | INFO (конфигурируемый) | Низкий | ✅ Хорошо |
| Трейсбэки с данными | `diagnose=False` ✅ | Устранён | Сохранить |
| Структурированные логи | Текстовые через loguru | Средний | Добавить JSON-формат |
| Метрики | Нет | Средний | Добавить Prometheus `/metrics` |
| Алерты | Нет | Средний | UptimeRobot на /health |

### 10.2 Rate Limiting

| Защита | Реализация | Оценка |
|--------|-----------|--------|
| Per-user cooldown | TTLCache RATE_LIMIT_SEC=5 | ✅ |
| Duplicate detection | TTLCache DUP_WINDOW_SEC=120 | ✅ |
| Duplicate escalation | strike→warn→mute | ✅ |
| Queue depth | MAX_QUEUE_SIZE=10 | ✅ |
| Per-chat quota | MAX_INFLIGHT_PER_CHAT=8 | ✅ |
| URL length | MAX_URL_LEN=2048 | ✅ |
| Anonymous bypass | **НЕТ** | ⚠️ Issue A-3 |
| /health /ping rate limit | Нет | Низкий риск |

### 10.3 Health Checks

**Реализовано:**
- `/ping` → `{"ok": true}` — для Render healthcheck ✅
- `/health` → `{status, browser, bot, worker}` — для мониторинга ✅
- Chromium heartbeat через `_browser.is_connected()` ✅
- Worker supervisor (`_worker_supervised`) — перезапуск при crash ✅

**Отсутствует:**
- Нет алерта при degraded статусе
- Нет метрик времени ответа

### 10.4 Browser Hang Protection

**Реализовано:**
- `TASK_TIMEOUT_SEC = 90` — глобальный таймаут задачи ✅
- `TIMEOUT_MS = 20_000` — goto timeout ✅
- RAM-guard `ABORT_AT_MB=470` — рвёт страницу при превышении памяти ✅
- Browser restart `RESTART_EVERY=50` — сброс V8 heap ✅
- Browser restart при `RESTART_AT_MB=360` — проактивная очистка ✅
- `asyncio.wait_for` в shutdown — ждёт завершения скриншота до 60s ✅

---

## 11. THREAT MODELING (STRIDE)

### 11.1 Активы

| Актив | Ценность | Угрозы |
|-------|----------|--------|
| BOT_TOKEN | Критический | Компрометация = полный захват бота |
| Группы (ALLOWED_GROUP_IDS) | Высокий | Спам, информация о пользователях |
| Chromium renderer | Высокий | RCE через 0-day при --no-sandbox |
| Render контейнер | Высокий | Lateral movement при компрометации |
| Очередь задач | Средний | DoS, исчерпание ресурсов |
| Кэш в RAM | Низкий | Инвалидация, утечка URLs |

### 11.2 Доверенные зоны

```
[UNTRUSTED]           [SEMI-TRUSTED]        [TRUSTED]
Telegram Users   →    Telegram Bot API   →  BOT_TOKEN
Malicious URLs   →    SSRF Filter        →  Playwright
                       Queue              →  FastAPI /health
                                         →  Render ENV
```

### 11.3 STRIDE Analysis

**Spoofing:**
| Угроза | Вектор | Контроль | Риск |
|--------|--------|---------|------|
| Подмена администратора | Telegram user ID spoofing | Telegram API гарантирует user_id | LOW |
| Anonymous admin bypass | msg.from_user=None | **ОТСУТСТВУЕТ** (Issue A-3) | MEDIUM |

**Tampering:**
| Угроза | Вектор | Контроль | Риск |
|--------|--------|---------|------|
| Инъекция в метаданные | Вредоносный og:title | caption_entities без parse_mode | MITIGATED |
| Модификация cache | Нет внешнего доступа | RAM cache изолирован в процессе | LOW |

**Information Disclosure:**
| Угроза | Вектор | Контроль | Риск |
|--------|--------|---------|------|
| BOT_TOKEN leak via log | diagnose=False | ✅ MITIGATED | LOW |
| Render internals в git | LOG файл | **НЕТ** | HIGH |
| SSRF → internal metadata | IMDS access | SSRF filter | MITIGATED |

**Denial of Service:**
| Угроза | Вектор | Контроль | Риск |
|--------|--------|---------|------|
| Queue flood | Много пользователей | QueueFull exception | LOW |
| Anonymous spam | user_id=None bypass | **ОТСУТСТВУЕТ** | MEDIUM |
| DNS slow flood | Медленный DNS resolver | Блокирует event loop | MEDIUM |
| OOM via heavy page | +300MB spike | RAM guard + DSF 1.5 | LOW |

**Elevation of Privilege:**
| Угроза | Вектор | Контроль | Риск |
|--------|--------|---------|------|
| Chromium RCE → pwuser | 0-day в V8/Blink | --no-sandbox без seccomp | HIGH |
| Container escape | Kernel exploit от pwuser | OS-level контроль (Render) | LOW (rare) |

### 11.4 Наиболее вероятные сценарии атак

**AS-1: Anonymous Bot Spam DoS [MEDIUM]**
```
1. Attacker использует бота или anonymized admin mode в группе
2. Бот-спаммер отправляет 50 одинаковых URLs/минуту
3. msg.from_user = None → user_id = None → обход rate limit
4. Очередь заполняется → другие пользователи получают QueueFull
5. Бот недоступен для легитимных пользователей
Вероятность: Medium | Ущерб: Availability
```

**AS-2: Chromium 0-day RCE [HIGH impact, LOW probability]**
```
1. Attacker создаёт HTML-страницу, эксплуатирующую уязвимость V8
2. Chromium рендерит страницу → exploit в renderer context
3. Без seccomp: renderer пишет в файловую систему /app
4. Attacker читает /proc/1/environ → получает BOT_TOKEN
Вероятность: Low | Ущерб: Full compromise
Снижение: JITLESS=on уменьшает RCE-поверхность V8
```

**AS-3: Supply Chain via pip [MEDIUM impact, LOW probability]**
```
1. Compromised PyPI package в диапазоне >=3.13 (aiogram)
2. Render делает rebuild → устанавливает trojaned версию
3. Trojaned aiogram читает BOT_TOKEN при старте → C2
Снижение: --require-hashes + pip-audit в CI
```

**AS-4: DNS Rebinding [MEDIUM impact, VERY LOW probability]**
```
Практически устранён многоуровневой SSRF-проверкой.
Route handler проверяет каждый subrequest.
Реальный риск минимален.
```

---

## 12. SECURITY HARDENING PLAN (ПОШАГОВЫЙ)

### 12.1 КРИТИЧЕСКИЕ (немедленно, до следующего деплоя)

#### H-CRIT-1: Удалить LOG-файл из git-истории

**Проблема:** Render build log с внутренними ID в публичном репозитории.  
**Сложность внедрения:** Низкая (30 мин)

```bash
pip install git-filter-repo
git filter-repo --path "Web Service safeshot-bot LOG" --invert-paths
echo '*LOG' >> .gitignore
echo '* LOG' >> .gitignore
git add .gitignore
git commit -m "security: remove render log, update gitignore"
git push --force-with-lease origin main
```

---

### 12.2 ВЫСОКОПРИОРИТЕТНЫЕ (в течение 1 недели)

#### H-HIGH-1: Async DNS resolution

**Проблема:** Блокирующий `socket.getaddrinfo()` замораживает event loop.  
**Сложность:** Средняя (2-3 часа)

```python
# security.py — обернуть в asyncio.to_thread:
infos = await asyncio.to_thread(
    socket.getaddrinfo, host, None, type=socket.SOCK_STREAM
)

# Все вызовы is_safe() сделать async:
async def is_safe_async(url: str) -> bool:
    ...
```

#### H-HIGH-2: Anonymous sender rate limiting

**Проблема:** Бот-отправители обходят rate limit.  
**Сложность:** Низкая (1 час)

```python
# bot.py — заменить user_id на rate_key:
if msg.sender_chat:
    rate_key = f"sender_chat:{msg.sender_chat.id}"
elif user_id is not None:
    rate_key = user_id
else:
    rate_key = f"anon:{chat_id}"

# Применять rate_key в _rate_cooldown и _dup_seen
```

#### H-HIGH-3: pip hash verification

**Сложность:** Средняя (2-3 часа)

```bash
pip install pip-tools
pip-compile --generate-hashes requirements.txt -o requirements.lock
# Dockerfile: pip install --no-cache-dir --require-hashes -r requirements.lock
```

#### H-HIGH-4: Docker image digest pinning

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble@sha256:8ff591d613b01c884cc488339ed4318b4513eaf0c57a164a878ba49e70e3f384
```

---

### 12.3 СРЕДНИЙ ПРИОРИТЕТ (в течение 2 недель)

#### H-MED-1: Исправить `ipc: host` → `shm_size`

```yaml
# docker-compose.yml:
services:
  bot:
    shm_size: '256m'   # Вместо ipc: host
    # УБРАТЬ строку: ipc: host
```

#### H-MED-2: Chromium `--host-resolver-rules`

```python
# screenshot.py → _launch_args():
"--host-resolver-rules=MAP 169.254.0.0/16 ~NOTFOUND, "
"MAP 100.64.0.0/10 ~NOTFOUND, "
"MAP 10.0.0.0/8 ~NOTFOUND, "
"MAP 172.16.0.0/12 ~NOTFOUND, "
"MAP 192.168.0.0/16 ~NOTFOUND",
```

#### H-MED-3: /health аутентификация

```python
# main.py:
HEALTH_TOKEN = os.environ.get("HEALTH_CHECK_TOKEN", "")

@app.get("/health")
async def health(x_health_token: str = Header(default="")):
    if HEALTH_TOKEN and x_health_token != HEALTH_TOKEN:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    ...
```

#### H-MED-4: Обновить .dockerignore

```
# Добавить в .dockerignore:
*LOG
*.log
```

#### H-MED-5: Пересмотреть fb.watch в whitelist

```python
# config.py — fb.watch — URL shortener, может вести на фишинг.
# Рекомендуется удалить из whitelist или добавить явный комментарий о риске.
```

---

### 12.4 ДОЛГОСРОЧНЫЕ (1 месяц+)

#### H-LONG-1: CI/CD Security Pipeline

```yaml
# .github/workflows/security.yml:
name: Security Scan
on: [push, pull_request]
jobs:
  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: pip-audit
        run: pip-audit -r requirements.txt
      - name: Bandit SAST
        run: bandit -r . -x tests/
      - name: Secrets scan
        uses: gitleaks/gitleaks-action@v2
      - name: Trivy container scan
        uses: aquasecurity/trivy-action@master
```

#### H-LONG-2: VPS + seccomp для максимальной изоляции

На Hetzner CX11 (~€5/мес) или Oracle Cloud Free Tier:
```bash
CHROMIUM_SANDBOX=on  # + seccomp.json из Playwright репозитория
# JITLESS=on (компромисс скорость vs RCE surface)
```

#### H-LONG-3: Structured Logging (JSON)

```python
# main.py:
logger.add(sys.stderr, level=LOG_LEVEL,
           backtrace=False, diagnose=False,
           serialize=True)  # JSON output для SIEM
```

#### H-LONG-4: SBOM (Software Bill of Materials)

```yaml
- name: Generate SBOM
  uses: anchore/sbom-action@v0
  with:
    image: ghcr.io/tosik017/safeshot-bot:latest
    artifact-name: sbom.spdx
```

---

## 13. ПОЛНАЯ ТАБЛИЦА УЯЗВИМОСТЕЙ

| ID | Severity | Файл / Строка | Тип | Описание | CWE | Статус |
|----|----------|---------------|-----|----------|-----|--------|
| V-01 | 🔴 HIGH | `Web Service safeshot-bot LOG` | Information Disclosure | Render internal IDs, bot numeric ID, internal IPs в публичном git-репо | CWE-312, CWE-540 | FIXED 2026-06-22 (git-filter-repo + force-push) |
| V-02 | 🔴 HIGH | `screenshot.py:158-159` | Container Escape Risk | Chromium без seccomp — 0-day renderer = RCE в контейнере | CWE-693 | ACCEPTED (Render Free) |
| V-03 | 🟠 MEDIUM | `security.py:71` | DoS / Event Loop Block | Синхронный `socket.getaddrinfo()` блокирует asyncio event loop | CWE-1069 | FIXED 2026-06-22 (`is_safe` → async, `loop.getaddrinfo`) |
| V-04 | 🟠 MEDIUM | `bot.py:580-607` | Auth Bypass / DoS | Anonymous sender (user_id=None) обходит rate limit и dedup | CWE-770 | FIXED 2026-06-22 (sender_key fallback на sender_chat.id) |
| V-05 | 🟠 MEDIUM | `docker-compose.yml:14` | Container Isolation | `ipc: host` — доступ к IPC namespace хоста | CWE-269 | FIXED 2026-06-22 (убран — не нужен, Chromium уже --disable-dev-shm-usage) |
| V-06 | 🟠 MEDIUM | `Dockerfile:13` | Supply Chain | pip install без `--require-hashes`, нет lock-файла | CWE-494 | FIXED 2026-06-22 (uv pip compile --generate-hashes, requirements.in добавлен как source) |
| V-07 | 🟡 LOW | `Dockerfile:3` | Supply Chain | Базовый образ не закреплён по digest | CWE-494 | PARTIAL (Render фиксирует) |
| V-08 | 🟡 LOW | `bot.py:350-356` | Information Disclosure | Позиция в очереди раскрывается пользователям | CWE-200 | INFORMATIONAL |
| V-09 | 🟡 LOW | `main.py:43-65` | Information Disclosure | `/health` без аутентификации раскрывает статус компонентов | CWE-200 | FIXED 2026-06-22 (опциональный HEALTH_TOKEN, открыт по умолчанию) |
| V-10 | 🟡 LOW | `.gitignore` | Configuration | Файл `Web Service safeshot-bot LOG` не покрыт gitignore | CWE-312 | FIXED 2026-06-22 (`*LOG*` добавлено) |
| V-11 | 🟡 LOW | `.dockerignore` | Configuration | `Web Service safeshot-bot LOG` попадает в Docker-образ | CWE-200 | FIXED 2026-06-22 (`*LOG*` добавлено) |
| V-12 | 🟡 LOW | `config.py:81-89` | Business Logic | `fb.watch` в whitelist — URL shortener, может вести на фишинг | CWE-183 | INFORMATIONAL |
| V-13 | ℹ️ INFO | `bot.py:194` | Information Disclosure | ALLOWED_GROUP_IDS в логах при выходе из чата | CWE-200 | LOW RISK |
| V-14 | ℹ️ INFO | `Dockerfile:13` | Best Practice | pip запускается от root при сборке | — | INFORMATIONAL |
| V-15 | ℹ️ INFO | — | Supply Chain | Нет автоматического SAST, dependency audit, secrets scanning в CI | — | INFORMATIONAL |
| V-16 | ℹ️ INFO | `screenshot.py:96-100` | Browser Abuse | Cookie auto-click может взаимодействовать с вредоносными кнопками | CWE-1021 | LOW RISK |
| V-17 | ℹ️ INFO | `screenshot.py:66-70` | Anti-Detection | `_STEALTH_JS` минимален — Chromium может быть обнаружен | — | BY DESIGN |
| V-18 | ℹ️ INFO | `metadata.py:92-99` | DoS | Рекурсивный `_walk_jsonld` без ограничения глубины | CWE-674 | LOW RISK (caught) |
| V-19 | ℹ️ INFO | `config.py:30-35` | Misconfiguration Risk | `ALLOW_OPEN_MODE=true` превращает бот в открытый SSRF-прокси | CWE-306 | PROTECTED (fail-fast) |
| V-20 | ℹ️ INFO | — | Supply Chain | Нет Software Bill of Materials (SBOM) | — | INFORMATIONAL |

---

## 14. SECURITY SCORE

### 14.1 Оценки по разделам

| Раздел | Оценка /10 | Обоснование |
|--------|-----------|-------------|
| **Архитектура** | 6.5 | Хорошая структура, осознанные компромиссы задокументированы. Минус: анон bypass, синхронный DNS |
| **Код** | 7.5 | Нет Command Injection, XSS заблокирован caption_entities, SSRF многоуровневый. Минус: анон bypass |
| **Зависимости** | 5.5 | Актуальные версии, нет критических CVE. Минус: нет lock-файла, нет хэш-верификации |
| **Docker** | 6.0 | Non-root, dumb-init, read_only в compose. Минус: --no-sandbox без seccomp, ipc:host |
| **Инфраструктура** | 5.0 | Render Free — ограниченные гарантии изоляции. Нет seccomp, нет egress filtering |
| **Secrets Management** | 6.0 | BOT_TOKEN в ENV, sync:false, diagnose=False. Минус: LOG-файл с internals в git |
| **Monitoring** | 4.5 | Базовые health-check эндпоинты, loguru. Нет метрик, нет алертов |
| **Operational Security** | 6.5 | TTLCache rate limits, RAM-guard, worker supervisor. Минус: анон bypass |
| **Supply Chain Security** | 4.0 | Нет lock-файла, нет хэшей, нет CI-pipeline, нет SBOM |

### 14.2 Итоговый Security Score

```
╔══════════════════════════════════════════════╗
║       ОБЩИЙ SECURITY SCORE: 6.1 / 10        ║
║                                              ║
║  Выше среднего для публичных Telegram-ботов  ║
║  Требует устранения выявленных проблем       ║
║  перед широким production-использованием     ║
╚══════════════════════════════════════════════╝
```

### 14.3 Critical Findings (немедленные действия)

1. **[HIGH] V-01** — Render build LOG с internal IDs в публичном git. Удалить через `git filter-repo` НЕМЕДЛЕННО.

### 14.4 High Findings

1. **[HIGH] V-02** — `--no-sandbox` Chromium без seccomp: единственный барьер при 0-day — непривилегированный пользователь.

### 14.5 Quick Wins (низкая сложность, высокий эффект)

| # | Действие | Время |
|---|----------|-------|
| 1 | Добавить `*LOG` в `.gitignore` и `.dockerignore` | 5 мин |
| 2 | Закрепить Docker image по digest | 5 мин |
| 3 | Добавить анон rate-limit fallback в `bot.py` | 30 мин |
| 4 | Добавить `--dns-prefetch-disable`, `--host-resolver-rules` | 15 мин |
| 5 | Заменить `ipc: host` на `shm_size: '256m'` | 5 мин |

### 14.6 Обязательные исправления перед production-запуском в публичном/широком доступе

| # | Действие | Файл | Приоритет |
|---|----------|------|-----------|
| 1 | Удалить LOG из git истории | `Web Service safeshot-bot LOG` | НЕМЕДЛЕННО |
| 2 | Anonymous sender rate limiting | `bot.py:580-607` | ВЫСОКИЙ |
| 3 | Async DNS (asyncio.to_thread) | `security.py:71` | ВЫСОКИЙ |
| 4 | pip --require-hashes + lock-файл | `Dockerfile`, `requirements.txt` | ВЫСОКИЙ |
| 5 | Убрать ipc:host, добавить shm_size | `docker-compose.yml` | СРЕДНИЙ |
| 6 | Digest pin для базового образа | `Dockerfile:3` | СРЕДНИЙ |

---

## 15. ФИНАЛЬНЫЕ ВЫВОДЫ

### 15.1 Общая оценка

SafeShot Bot — **технически грамотно написанный проект** с явным пониманием угроз безопасности. Автор:
- Реализовал SSRF-защиту на нескольких уровнях с документированием edge-cases
- Осознанно принял риск `--no-sandbox` и задокументировал его
- Использовал `diagnose=False` для защиты секретов в трейсах
- Написал тесты для SSRF-фильтра
- Корректно экранировал HTML при вставке в Telegram-сообщения (`html.escape`, `caption_entities`)
- Реализовал resource limiting и RAM-guard

**Главный выявленный недостаток** — это не уязвимость в коде, а **операционная ошибка**: Render build log попал в git-репозиторий, раскрыв внутренние идентификаторы инфраструктуры и численный ID бота. Это нужно исправить в первую очередь.

### 15.2 Что хорошо сделано (не трогать)

- Многоуровневая SSRF-проверка (до очереди + route handler + httpx hops)
- `diagnose=False, backtrace=False` в loguru
- `caption_entities` без `parse_mode` для метаданных сайтов
- RAM-guard с `ctx.close()` вместо kill-процесса
- `service_workers="block"` и `accept_downloads=False`
- `html.escape()` в `_protected_caption()`
- `sync: false` для BOT_TOKEN в render.yaml
- Worker supervisor с автоперезапуском

### 15.3 Матрица рисков

```
   ВЕРОЯТНОСТЬ
   Высокая  │ V-04 (anon bypass)    │ V-03 (DNS block)      │
            │                       │                       │
   Средняя  │ V-10,11 (LOG gitignore)│ V-01 (LOG in git)*   │
            │ V-06 (pip hashes)     │                       │
   Низкая   │ V-12 (fb.watch)       │ V-02 (Chromium 0day)  │
            │ V-09 (/health auth)   │                       │
            └───────────────────────┴───────────────────────┤
                    НИЗКОЕ ВЛИЯНИЕ        ВЫСОКОЕ ВЛИЯНИЕ

  *V-01 уже реализован (файл в репо), вероятность = факт
```

### 15.4 Приоритизированный план действий

```
Сегодня (< 30 мин):
  □ git filter-repo → удалить LOG из истории
  □ Обновить .gitignore: добавить *LOG
  □ Обновить .dockerignore: добавить *LOG

Эта неделя (< 8 часов):
  □ Anonymous rate limiting в bot.py
  □ asyncio.to_thread для DNS в security.py
  □ pip-compile --generate-hashes → requirements.lock
  □ ipc: host → shm_size: '256m' в docker-compose.yml
  □ Digest pin в Dockerfile

Этот месяц:
  □ GitHub Actions: gitleaks + pip-audit + bandit
  □ --host-resolver-rules в Chromium launch args
  □ /health token auth
  □ Рассмотреть VPS + seccomp (CHROMIUM_SANDBOX=on)
```

---

*Аудит проведён на основе полного исследования исходного кода, конфигурационных файлов, docker-конфигурации, render.yaml, всех зависимостей и log-файлов репозитория. Все выводы основаны на реальных артефактах кода с указанием файлов и строк.*

*Методологическая база: OWASP ASVS v4, OWASP Top 10 2021 (A01-Broken Access Control, A02-Cryptographic Failures, A05-Security Misconfiguration, A06-Vulnerable Components, A08-Software and Data Integrity Failures, A09-Security Logging), CWE Top 25, MITRE ATT&CK for Containers, Docker Security Benchmarks, CIS Docker Benchmark v1.6.*
