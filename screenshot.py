"""Playwright + Pillow: безпечний рендер недовіреного сайту в PNG-превʼю.
Ключове проти OOM: захоплюємо ЛИШЕ ПЕРШИЙ ЕКРАН (clip), viewport не розтягуємо.
Ключове проти SSRF: is_safe на КОЖЕН запит у route handler + повторно перед goto.
Концепт візуальний → images/CSS НЕ блокуємо; ріжемо лише те, що для статичного
скриншота не потрібне (відео/аудіо, websocket, шрифти, реклама/трекери)."""
import asyncio
import os
from io import BytesIO

from PIL import Image
from loguru import logger
from playwright.async_api import async_playwright

import ram
import security
from config import USER_AGENT, TIMEOUT_MS, PAUSE_MS, SEMAPHORE, CHROMIUM_SANDBOX
from metadata import parse_from_html

semaphore = asyncio.Semaphore(SEMAPHORE)
_browser = None
_pw = None  # держимо playwright-інстанс, щоб коректно перезапускати браузер

# Кожні RESTART_EVERY скриншотів перезапускаємо браузер — Playwright поступово
# роздуває V8 heap і internal page cache. ~50 запитів = кілька годин на Render Free.
_request_count = 0
RESTART_EVERY = 50
# Другий тригер — по памʼяті: used > RESTART_AT_MB перед задачею → рестарт
# браузера ЗАМІСТЬ відмови. Калібровка (логи 2026-06-11): заявка додає на піку
# +102..115 MB; 360 + 115 + запас < 512 → watchdog (430) до відмов не доходить.
# Ціна — +5-8 c до одного запиту зрідка. НЕ піднімай RAM_LIMIT "під 512":
# watchdog міряє ДО задачі, пік приходить ПІСЛЯ.
RESTART_AT_MB = 360
# RAM-guard УСЕРЕДИНІ задачі: усі зовнішні сторожі міряють МІЖ задачами, а
# вбиває ОДНА важка сторінка (+250-300MB за рендер). Guard щосекунди дивиться
# cgroup-used і при перевищенні закриває КОНТЕКСТ сторінки → задача акуратно
# падає в текст-фолбек, браузер рестартує, інстанс живе. Рвемо сторінку, не
# процес. 470 = 512 − запас на секундний крок семплінгу і ріст python.
# (Свідомий перегляд старого рішення "mid-task kill не робимо": воно
# приймалось ДО OOM-даних 2026-06-11.)
ABORT_AT_MB = 470

# DSF 1.5 замість 2: растеризація картинок у подвійному масштабі була головним
# джерелом RAM-піку ОДНІЄЇ важкої сторінки (+250-300MB, OOM-кілл інстанса на
# cudy 2026-06-11; OLX пік 481MB). 1.5 зрізає пік ~на третину; кадр 585×960.
# На телефоні (стрічка ~390 CSS px завширшки) це досі ≥1.5x щільності.
# Відкат якості = повернути 2 (і подивитись, чи тримає RAM-guard).
DEVICE_SCALE = 1.5
MOBILE_WIDTH = 390
MOBILE_HEIGHT = 844
PART_HEIGHT = 1280              # висота частини; Telegram ліміт ~10 МБ на фото
MAX_PARTS = 4                   # страховка нарізки в _split_image
MAX_HEIGHT = PART_HEIGHT * MAX_PARTS
# OOM-фікс (інстанс убило по памʼяті на OLX після мобільного UA): single-message
# flow шле ЛИШЕ перший кадр → знімаємо ЛИШЕ його. Розтягування viewport до
# 2560 CSS px змушувало Chromium растеризувати ВСЮ сторінку (5120 фіз.px) —
# на важких сторінках це >512MB. Кадр = 640 CSS px × DEVICE_SCALE.
# (Заглушка тепер компактна 780×320 і з кадром НЕ збігається — повідомлення
# свідомо росте при успіху.) Підняти до 844 (повний екран viewport) можна
# майже безкоштовно — все ≤844 вже відрендерено; відкрите UX-рішення.
CAPTURE_CSS = 640  # CSS px — перший екран (кадр = 640 × DEVICE_SCALE фіз. px)

# Мінімальний stealth без зовнішньої залежності (фрагментний playwright-stealth
# зі застарілим API прибрано). Ховає найочевидніший маркер автоматизації.
# navigator.plugins НЕ спуфимо: на мобільному Chrome плагінів немає — фейковий
# список суперечив би мобільному UA і сам ставав маркером бота.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['uk-UA','uk','ru','en']});
"""

COOKIE_SELECTORS = [
    "button[id*='accept']", "button[class*='accept']",
    "button[aria-label*='Accept']", "button[aria-label*='Agree']",
    "[id*='cookie'] button", "[class*='cookie'] button", "[class*='consent'] button",
]

# Типи ресурсів, не потрібні для статичного скриншота → ріжемо (швидкість + RAM).
# images/stylesheet/document/script НЕ чіпаємо — без них превʼю зламається.
_BLOCK_RESOURCE_TYPES = {"media", "websocket", "font"}

# Реклама/трекери: чистий шум для скриншота, з'їдають час і памʼять.
# Якщо на якомусь сайті через GTM раптом зникне ціна — прибери "googletagmanager.com".
AD_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "googletagmanager.com", "adnxs.com", "criteo.com", "taboola.com", "outbrain.com",
    "facebook.net", "google-analytics.com", "mc.yandex.ru", "counter.yadro.ru",
    "hotjar.com", "clarity.ms", "scorecardresearch.com", "quantserve.com",
    "amazon-adsystem.com", "pubmatic.com", "rubiconproject.com", "casalemedia.com",
    "adsrvr.org", "sentry.io",
)


def _launch_args() -> list[str]:
    args = [
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",                 # /dev/shm малий у контейнері
        "--disable-gpu",
        "--disable-software-rasterizer",           # без GPU не тримаємо ще й SW-растер
        "--disable-3d-apis",                        # вимикаємо WebGL → менше GPU/ANGLE surface і RAM
        "--no-first-run",
        "--no-default-browser-check",
        "--no-zygote",
        "--disable-extensions",
        "--disable-background-networking",          # жодних фонових з'єднань браузера
        "--disable-component-update",               # не тягнемо апдейти компонентів/моделей
        "--disable-domain-reliability",             # не шлемо телеметрію надійності в Google
        "--disable-breakpad",                       # без crash-дампів
        "--metrics-recording-only",                 # метрики не вивантажуються
        "--disable-default-apps",
        "--disable-sync",
        "--disable-translate",
        "--disable-hang-monitor",                   # без діалогів "сторінка не відповідає"
        "--disable-client-side-phishing-detection",
        "--disable-prompt-on-repost",
        "--disable-background-timer-throttling",    # таймери не тротляться → JS-ціна встигає відрендеритись
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--mute-audio",
        "--hide-scrollbars",
        "--disable-remote-fonts",
        # WebRtc → leak локального IP; OptimizationGuide* → не качаємо on-device
        # AI/optimization-модель (зайвий трафік/диск на Free); AsyncDns → системний
        # резолвер замість вбудованого (узгоджено з security.is_safe → менше зазору
        # для DNS-rebinding); решта — фонова мережа/синк/зайвий surface.
        "--disable-features=WebRtc,Translate,InterestCohort,AcceptCHFrame,"
        "MediaRouter,DialMediaRouteProvider,OptimizationHints,"
        "OptimizationGuideOnDeviceModel,OptimizationGuideModelDownloading,"
        "BackForwardCache,AsyncDns",
    ]
    # На Render Free sandbox недоступний (немає userns/seccomp) → --no-sandbox.
    # Контейнер працює від non-root pwuser, тож втеча рендера ≠ root.
    # Локально/VPS із seccomp: CHROMIUM_SANDBOX=on → справжня ізоляція.
    if not CHROMIUM_SANDBOX:
        args.insert(0, "--no-sandbox")
    # JITLESS=on вимикає V8 JIT — найбільше зрізає RCE-поверхню (так роблять
    # безпековики), АЛЕ суттєво сповільнює важкі JS-сторінки. У нас уже були
    # таймаути 90с (OLX) → за замовчуванням ВИМКНЕНО, щоб не плодити текстові
    # фолбеки. Вмикати свідомо, якщо ризик важливіший за швидкість/повноту.
    if os.environ.get("JITLESS", "").lower() == "on":
        args.append("--js-flags=--jitless")
    return args


def log_ram(label: str):
    """used = cgroup-метрика (як рахує OOM-кіллер Render); rss-розбивка поруч —
    діагностика, вона ЗАВИЩУЄ (shared-сторінки Chromium по кілька разів)."""
    used, src = ram.used_mb()
    own_mb, child_mb = ram.rss_breakdown_mb()
    logger.info(f"[RAM | {label}] used={used:.1f} MB ({src}; rss: python={own_mb:.1f} + chromium={child_mb:.1f})")


async def init():
    global _browser, _pw
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(headless=True, args=_launch_args())
    log_ram("Browser started")


async def _restart_browser():
    """Перезапуск для скидання памʼяті Playwright. Викликається між запитами
    (семафор уже захоплено) → безпечно."""
    global _browser, _pw, _request_count
    logger.info(f"[BROWSER RESTART] after {RESTART_EVERY} requests — clearing V8 heap")
    log_ram("Before restart")
    try:
        await _browser.close()
    except Exception as e:
        logger.warning(f"Browser close error (non-critical): {e}")
    try:
        await _pw.stop()
    except Exception as e:
        logger.warning(f"Playwright stop error (non-critical): {e}")
    await init()
    _request_count = 0
    log_ram("After restart")


async def _ram_guard(ctx, stop: asyncio.Event, state: dict):
    """Семплінг памʼяті під час рендеру сторінки. Перевищення → close контексту:
    goto/screenshot всередині shoot падають TargetClosedError → текст-фолбек."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
            return  # stop виставлено штатно
        except asyncio.TimeoutError:
            pass
        used, _src = ram.used_mb()
        if used > ABORT_AT_MB:
            state["fired"] = used
            logger.warning(f"[RAM-GUARD] used={used:.0f}MB > {ABORT_AT_MB}MB — closing page context")
            try:
                await ctx.close()
            except Exception:
                pass
            return


async def _route_handler(route):
    req = route.request
    url = req.url

    # 1) SSRF на КОЖЕН запит: subresource/iframe/fetch/редирект на внутрішній
    # адрес → abort. data:/blob: не мають host і не йдуть у мережу → пропускаємо.
    if url.startswith(("http://", "https://")) and not security.is_safe(url):
        await route.abort()
        return

    # 2) Важкий/непотрібний для статичного скриншота контент.
    if req.resource_type in _BLOCK_RESOURCE_TYPES:
        await route.abort()
        return

    # 3) Реклама/трекери — шум, що з'їдає час і RAM.
    if any(host in url for host in AD_HOSTS):
        await route.abort()
        return

    await route.continue_()


def _split_image(png_bytes: bytes) -> list[bytes]:
    """Нарізка через Pillow на частини по PART_HEIGHT. Після OOM-фікса захват =
    рівно один кадр (clip 640 CSS px) → тут завжди 1 частина; код лишається
    як страховка на випадок зміни логіки захвату."""
    img = Image.open(BytesIO(png_bytes))
    width, height = img.size

    if height > MAX_HEIGHT:
        img = img.crop((0, 0, width, MAX_HEIGHT))
        height = MAX_HEIGHT

    if height <= PART_HEIGHT:
        buf = BytesIO()
        img.save(buf, format="PNG")
        return [buf.getvalue()]

    parts = []
    top = 0
    while top < height:
        bottom = min(top + PART_HEIGHT, height)
        part = img.crop((0, top, width, bottom))
        buf = BytesIO()
        part.save(buf, format="PNG")
        parts.append(buf.getvalue())
        top = bottom

    logger.info(f"Split into {len(parts)} parts, total height={height}px")
    return parts


async def shoot(url: str) -> tuple[list[bytes], dict]:
    """Повертає (список частин скриншота, метадані).
    browser_meta["_truncated"]=True → сторінка довша за перший екран."""
    global _request_count

    # Друга перевірка SSRF безпосередньо перед навігацією — звужує вікно
    # DNS-rebinding між перевіркою в bot.py і реальним резолвом Chromium.
    if not security.is_safe(url):
        logger.warning("SSRF re-check failed before goto")
        return [], {}

    log_ram("Before screenshot")
    async with semaphore:
        _request_count += 1
        # Рестарт браузера: плановий (RESTART_EVERY, скид V8 heap), по памʼяті
        # (RESTART_AT_MB — самоочистка замість відмови watchdog'а) або аварійний —
        # браузер відсутній/відвалився (напр. OOM-кілл рендерера). Без перевірки
        # is_connected впав би new_context на кожному запиті аж до RESTART_EVERY
        # → до 50 відмов поспіль. Тут самовідновлення.
        used, _src = ram.used_mb()
        if used > RESTART_AT_MB:
            logger.info(f"[BROWSER RESTART trigger] used={used:.0f}MB > {RESTART_AT_MB}MB")
        if (_request_count >= RESTART_EVERY
                or used > RESTART_AT_MB
                or _browser is None
                or not _browser.is_connected()):
            await _restart_browser()

        # ПОВНА мобільна емуляція: viewport був мобільним давно, але без
        # is_mobile/has_touch + з десктопним UA сайти з UA-сніфінгом віддавали
        # десктопну верстку в 390px. Тепер UA (config) + touch + mobile узгоджені.
        ctx = await _browser.new_context(
            viewport={"width": MOBILE_WIDTH, "height": MOBILE_HEIGHT},
            user_agent=USER_AGENT,
            device_scale_factor=DEVICE_SCALE,
            is_mobile=True,            # мобільний layout/meta-viewport, як на телефоні
            has_touch=True,            # touch-events: десктоп-UA без touch = маркер бота
            accept_downloads=False,    # не зберігаємо завантаження зі сторінки
            service_workers="block",   # SW міг би слати запити у фоні (до C2)
            ignore_https_errors=False,
        )
        _guard_stop = asyncio.Event()
        _guard_state: dict = {}
        _guard_task = asyncio.create_task(_ram_guard(ctx, _guard_stop, _guard_state))
        try:
            await ctx.add_init_script(_STEALTH_JS)
            page = await ctx.new_page()
            await page.route("**/*", _route_handler)

            await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            await page.wait_for_timeout(PAUSE_MS)
            await _close_cookies(page)

            html = await page.content()
            browser_meta = parse_from_html(html, url)
            logger.info(f"Browser meta: title={browser_meta.get('title')} price={browser_meta.get('price')}")

            # OOM-фікс: viewport НЕ розтягуємо (раніше set_viewport_size до 2560
            # CSS px → Chromium растеризував усю сторінку → OOM-кілл інстанса на
            # важких сторінках, OLX після мобільного UA). Знімаємо clip'ом РІВНО
            # перший екран: 390×640 CSS (фіз. розмір = × DEVICE_SCALE).
            # doc_height рахуємо лише для чесної позначки «Показано перший екран».
            doc_height = await page.evaluate(
                "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight, "
                "document.body.offsetHeight, document.documentElement.offsetHeight)"
            )
            truncated = int(doc_height) > CAPTURE_CSS
            logger.info(f"Capture: doc={int(doc_height)} CSS px -> clip first screen {CAPTURE_CSS} truncated={truncated}")

            full_png = await page.screenshot(
                animations="disabled",
                timeout=20_000,
                clip={"x": 0, "y": 0, "width": MOBILE_WIDTH, "height": CAPTURE_CSS},
            )
            log_ram("After screenshot")

            # Pillow синхронний → виносимо нарізку/encode у тред, щоб не блокувати
            # event loop (інакше на час crop/PNG-encode стоять polling і httpx-task).
            parts = await asyncio.to_thread(_split_image, full_png)
            if truncated:
                browser_meta["_truncated"] = True  # merge_meta це не переносить — bot.py читає напряму
            return parts, browser_meta

        except Exception as e:
            if _guard_state.get("fired"):
                # Сторінку розірвав guard → памʼять Chromium може лишитись
                # роздутою: рестартуємо браузер одразу (семафор уже наш).
                logger.warning(f"Screenshot ABORTED by RAM-guard at {_guard_state['fired']:.0f}MB")
                await _restart_browser()
            else:
                logger.warning(f"Screenshot failed: {type(e).__name__}")
            return [], {}

        finally:
            _guard_stop.set()
            _guard_task.cancel()
            try:
                await ctx.close()  # ідемпотентно не гарантовано → ковтаємо
            except Exception:
                pass


async def _close_cookies(page):
    for sel in COOKIE_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=500):
                await btn.click()
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue
