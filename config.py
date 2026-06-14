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

# --- Довірені домени (whitelist) ---
# Бот НЕ обробляє посилання на ці домени — лише тиха реакція 👌 (емодзі в bot.py).
# Матчинг: точний hostname або субдомен по межі крапки ("m.youtube.com" → так,
# "youtube.com.evil.top" → ні). Налаштування: env TRUSTED_DOMAINS, через кому/пробіл.
# env НЕ задано → дефолтний список нижче; задано порожнім → whitelist ВИМКНЕНО.
# Свідомо НЕ включаємо google.com (Google Forms/Drive — живий фішинг-вектор)
# і маркетплейси OLX/Epicentr (превʼю магазинів і є робота бота).
# ПЕРЕГЛЯНУТО 2026-06-12: t.me/соцмережі ДОДАНО за рішенням власника — Telegram
# сам малює нативне превʼю, headless їх однаково не рендерить (логін-стіни);
# ризик прийнято: 👌 на скам-каналі t.me виглядає як схвалення бота.
# ПЕРЕГЛЯНУТО 2026-06-14: rozetka.com.ua ДОДАНО — сайт за Cloudflare-challenge,
# превʼю фізично не знімається (див. _is_cloudflare_challenge у screenshot.py),
# тож тиха реакція 👌 чистіша за картку-щит на кожне посилання. OLX/Epicentr
# (без challenge, превʼю працює) лишаються поза whitelist.
# Субдомени покриваються суфікс-матчем (auto.rozetka.com.ua, vm.tiktok.com тощо).
_DEFAULT_TRUSTED = (
    "youtube.com youtu.be wikipedia.org github.com "
    "t.me telegram.me telegram.org "
    "tiktok.com instagram.com facebook.com fb.com fb.watch "
    "x.com twitter.com whatsapp.com wa.me viber.com "
    "reddit.com twitch.tv pinterest.com linkedin.com "
    "spotify.com soundcloud.com vimeo.com discord.com discord.gg imdb.com "
    "rozetka.com.ua"
)


def _parse_trusted_domains(raw: str) -> frozenset[str]:
    domains: set[str] = set()
    for token in (raw or "").replace(",", " ").split():
        d = token.strip().lower().lstrip(".")
        if d.startswith("www."):
            d = d[4:]
        if "." in d:
            domains.add(d)
        else:
            print(f"[config] WARN: пропускаю некоректний trusted domain: {token!r}")
    return frozenset(domains)


TRUSTED_DOMAINS = _parse_trusted_domains(
    os.environ.get("TRUSTED_DOMAINS", _DEFAULT_TRUSTED)
)

# --- Playwright / рендер ---
# МОБІЛЬНИЙ UA (Chrome Android): viewport у screenshot.py давно мобільний
# (390×844), але з десктопним UA сайти з UA-сніфінгом віддавали десктопну
# верстку, втиснуту в 390px. Тепер UA узгоджений із viewport + is_mobile +
# has_touch → сайти віддають справжню мобільну версію. Версія Chrome/140
# збігається з рушієм образу Playwright.
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
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
MAX_INFLIGHT_PER_CHAT = 8     # бот живе в ОДНІЙ групі → per-chat квота ≈ уся ємність бота; 2 відбивало другого-третього юзера ("перевантажений")
RAM_LIMIT_MB = 430            # тепер лише поріг ЛОГУ "accept under pressure"; реальний захист у shoot: RESTART_AT_MB=360 / ABORT_AT_MB=470
RATE_LIMIT_SEC = 5            # пейсинг різних посилань від одного юзера
MAX_URL_LEN = 2048            # довші URL не обробляємо (cache/regex hygiene)
MAX_BODY_BYTES = 2_000_000    # ліміт тіла httpx-відповіді (анти-OOM / gzip-bomb)
