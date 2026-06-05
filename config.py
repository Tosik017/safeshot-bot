"""Конфіг через environment variables. Secure-by-default, fail-closed."""
import os

# Токен обовʼязковий — без нього бот не має сенсу. Падаємо одразу (fail-fast),
# щоб не стартувати в зламаному стані.
BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 8000))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def _parse_group_ids(*raw_values: str) -> frozenset[int]:
    ids: set[int] = set()
    for raw in raw_values:
        for token in (raw or "").replace(",", " ").split():
            try:
                ids.add(int(token))
            except ValueError:
                print(f"[config] WARN: пропускаю некоректний group id: {token!r}")
    return frozenset(ids)


ALLOWED_GROUP_IDS = _parse_group_ids(
    os.environ.get("ALLOWED_GROUP_IDS", ""),
    os.environ.get("ALLOWED_GROUP_ID", ""),  # сумісність зі старим однина-варіантом
)

# Secure-by-default: порожній allow-list = бот мовчить скрізь, ОКРІМ коли явно
# увімкнено ALLOW_OPEN_MODE=true. Інакше забутий env робив би бота відкритим
# проксі для будь-кого (SSRF / DoS / abuse).
ALLOW_OPEN_MODE = os.environ.get("ALLOW_OPEN_MODE", "").lower() == "true"
if not ALLOWED_GROUP_IDS and not ALLOW_OPEN_MODE:
    raise SystemExit(
        "[config] ALLOWED_GROUP_IDS не задано і ALLOW_OPEN_MODE!=true. "
        "Вкажіть ID дозволених груп або свідомо увімкніть відкритий режим."
    )


def _parse_disabled_threads(raw: str):
    """Denylist топіків group:thread (group:general для General)."""
    pairs: set[tuple[int, int]] = set()
    general_chats: set[int] = set()
    for token in (raw or "").replace(",", " ").split():
        if ":" not in token:
            print(f"[config] WARN: токен без ':' (треба group:thread): {token!r}")
            continue
        gid_s, thr_s = token.rsplit(":", 1)
        try:
            gid = int(gid_s)
        except ValueError:
            print(f"[config] WARN: некоректний group id у токені {token!r}")
            continue
        if thr_s.lower() in ("general", "gen", "none"):
            general_chats.add(gid)
            continue
        try:
            pairs.add((gid, int(thr_s)))
        except ValueError:
            print(f"[config] WARN: некоректний thread id у токені {token!r}")
    return frozenset(pairs), frozenset(general_chats)


DISABLED_THREADS, DISABLED_GENERAL_CHATS = _parse_disabled_threads(
    os.environ.get("DISABLED_THREADS", "")
)

# --- Playwright / рендер ---
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
TIMEOUT_MS = 20_000        # goto: баланс швидкість/надійність
PAUSE_MS = 3_000           # чекаємо JS-рендер ціни після domcontentloaded
SEMAPHORE = 1              # один скриншот за раз — два Chromium = OOM на 512MB

# Chromium sandbox. На Render Free user namespaces недоступні → лишаємо OFF
# (--no-sandbox), АЛЕ контейнер працює від non-root pwuser (Dockerfile), тож
# втеча рендера = непривілейований pwuser, а не root. Локально/на VPS із
# seccomp-профілем можна CHROMIUM_SANDBOX=on і отримати справжню ізоляцію.
CHROMIUM_SANDBOX = os.environ.get("CHROMIUM_SANDBOX", "").lower() == "on"

# --- Ліміти ресурсів (захист від DoS / OOM) ---
CACHE_SIZE = 200
MAX_QUEUE_SIZE = 10            # глибина черги
TASK_TIMEOUT_SEC = 90         # 20(goto)+3(pause)+20(screenshot)+запас
MAX_INFLIGHT_PER_CHAT = 2     # один чат не забиває всю чергу (анти-DoS)
RAM_LIMIT_MB = 430            # відсікаємо нові задачі біля межі 512MB Render Free
RATE_LIMIT_SEC = 5            # пейсинг різних посилань від одного юзера
MAX_URL_LEN = 2048            # довші URL не обробляємо (cache/regex hygiene)
MAX_BODY_BYTES = 2_000_000    # ліміт тіла httpx-відповіді (анти-OOM / gzip-bomb)
