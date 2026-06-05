# SafeShot Bot

Telegram-бот безпечного перегляду посилань. Бере посилання з групового чату,
рендерить сторінку в ізольованому headless Chromium, робить скриншот і повертає
прев'ю + метадані + застереження — щоб користувачі **не переходили** за
підозрілими лінками напряму.

Це перероблена, посилена версія: усунено SSRF/DNS-rebinding/cloud-metadata
вектори, DoS/OOM, контейнер працює від non-root, конфіг fail-closed.

## Як працює

1. Користувач кидає посилання в дозволену групу.
2. `security.is_safe()` відсікає внутрішні/приватні адреси (перед чергою, на
   кожен підзапит Chromium і після редиректів httpx).
3. Чергою (один воркер, per-chat квота, RAM-watchdog) задача йде в `screenshot`.
4. Chromium рендерить, Pillow ріже на частини, бот постить картку із застереженням.
5. Результат кешується в RAM (нічого на диск).

## Файли

| Файл | Призначення |
|---|---|
| `bot.py` | Хендлери Telegram, анти-спам, mute, дедуп, картка |
| `security.py` | SSRF-фільтр (єдиний барʼєр) |
| `screenshot.py` | Playwright + Pillow |
| `metadata.py` | httpx-метадані з лімітом розміру тіла |
| `queue_manager.py` | Черга: глибина + per-chat квота + RAM-watchdog |
| `cache.py` | RAM-кеш (file_id + meta) |
| `config.py` | Env-конфіг, fail-closed |
| `main.py` | FastAPI health + aiogram polling + graceful shutdown |
| `tests/test_security.py` | Smoke-тести SSRF-фільтра |

## Змінні оточення

| Змінна | Обовʼязкова | Опис |
|---|---|---|
| `BOT_TOKEN` | так | Токен від @BotFather |
| `ALLOWED_GROUP_IDS` | так* | ID дозволених груп (через кому/пробіл). *Без неї бот не стартує, якщо `ALLOW_OPEN_MODE` != `true` |
| `DISABLED_THREADS` | ні | Топіки denylist: `group:thread`, `group:general` |
| `ALLOW_OPEN_MODE` | ні | `true` → працювати в будь-якій групі (небезпечно) |
| `LOG_LEVEL` | ні | `DEBUG`/`INFO`/`WARNING` (типово `INFO`) |
| `CHROMIUM_SANDBOX` | ні | `on` → справжній sandbox (лише локально/VPS із seccomp) |
| `PORT` | ні | Порт HTTP (Render задає сам; локально 8000) |

## Деплой на Render (Free)

1. Залити код у новий GitHub-репозиторій.
2. Render > **New +** > **Blueprint** > вибрати репозиторій (підхопить `render.yaml`).
3. У Dashboard ввести секрети: `BOT_TOKEN`, `ALLOWED_GROUP_IDS`.
4. Дочекатись білда й деплою. У логах має зʼявитись `Browser started` і `Queue worker started`.

### Важливо про Render Free

- **Засинання:** free web-сервіс зупиняється без вхідного HTTP. Бот на long-polling
  його не утримує. Рішення (безкоштовне): зовнішній пінгер
  (UptimeRobot / cron-job.org) на `https://<service>.onrender.com/ping` раз на ~10 хв.
- **Sandbox:** на Render Free user namespaces/seccomp недоступні → Chromium йде з
  `--no-sandbox`. Це частково компенсовано тим, що контейнер працює від **non-root**
  (`pwuser`) і Chromium свіжий. Повна ізоляція (sandbox + seccomp + cap-drop +
  egress-фільтр) можлива лише локально або на VPS — див. нижче.
- **Мережевий egress-фільтр** на Render Free налаштувати не можна, тож захист від
  SSRF тримається на app-рівні (`security.is_safe`). Для критичних сценаріїв —
  власний VPS з `nftables`.

## Локальний запуск (Pop!_OS, посилена ізоляція)

```bash
cp .env.example .env      # заповнити BOT_TOKEN, ALLOWED_GROUP_IDS
docker compose up --build
```

`docker-compose.yml` дає non-root, `cap_drop: ALL`, read-only FS, ліміти памʼяті.
Для **справжнього** Chromium sandbox: встановити `CHROMIUM_SANDBOX=on` у `.env`,
розкоментувати `seccomp=./seccomp_profile.json` у compose і покласти поряд
офіційний профіль Playwright (default Docker seccomp + дозвіл `clone/setns/unshare`):
https://playwright.dev/python/docs/docker

## Тести

```bash
pip install pytest
pytest -q
```

Перевіряють, що SSRF-фільтр блокує localhost, приватні мережі, cloud-metadata
(169.254.169.254), 0.0.0.0, не-http схеми, нестандартні порти, внутрішні суфікси
й dual-stack, і пропускає публічні хости.

## Безпека (що закрито)

- SSRF / DNS-rebinding / cloud-metadata / internal-IP — `security.is_safe` з
  резолвом усіх адрес, перевіркою на кожен підзапит Chromium і після редиректів.
- Контейнер від non-root (`pwuser`), свіжий Chromium, без зайвих capabilities.
- DoS/OOM — per-chat квота, RAM-watchdog, ліміт розміру httpx-тіла, clamp висоти захвату.
- Secure-by-default — fail-closed allow-list груп; секрети лише через env.
