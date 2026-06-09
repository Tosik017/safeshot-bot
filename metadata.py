"""Швидкі метадані через httpx (без браузера). Ліміт розміру тіла й перевірка
фінального хоста ДО читання тіла — анти-SSRF і анти-OOM."""
import json

import httpx
from selectolax.parser import HTMLParser
from loguru import logger

import security
from config import MAX_BODY_BYTES

USER_AGENTS = [
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",  # Cloudflare пропускає ботів соцмереж
    "Twitterbot/1.0",
    "facebookexternalhit/1.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
]

# Стандартні redirect-статуси, які проходимо ВРУЧНУ (a не follow_redirects=True).
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_MAX_HOPS = 4  # вихідний запит + до 3 редиректів (як було max_redirects=3)


async def fetch(url: str) -> dict:
    """Один запит — перший успішний. Стрімимо тіло з лімітом розміру.

    SSRF: follow_redirects ВИМКНЕНО. httpx із follow_redirects=True сам фізично
    конектиться до КОЖНОГО проміжного хоста цепочки 30x ДО того, як ми перевіримо
    фінальний URL — це blind-SSRF (IMDS, внутрішні сервіси, порт-скан). Тому
    проходимо редиректи вручну й перевіряємо is_safe на КОЖЕН хоп ПЕРЕД запитом —
    до небезпечного хоста запит не йде взагалі (та сама модель, що в screenshot)."""
    for ua in USER_AGENTS:
        try:
            headers = {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "uk,ru;q=0.9,en;q=0.8",
            }
            body = None
            encoding = "utf-8"
            async with httpx.AsyncClient(follow_redirects=False, timeout=8) as client:
                current = url
                for _ in range(_MAX_HOPS):
                    # Перевірка ПЕРЕД запитом: небезпечний хоп → нічого не конектимо.
                    if not security.is_safe(current):
                        logger.warning("SSRF blocked metadata hop (host not safe)")
                        return {}
                    async with client.stream("GET", current, headers=headers) as r:
                        if r.status_code in _REDIRECT_CODES:
                            loc = r.headers.get("location")
                            if not loc:
                                return {}
                            # Відносний Location резолвимо відносно поточного URL.
                            current = str(httpx.URL(current).join(loc))
                            continue
                        # Фінальна відповідь — current уже перевірено is_safe вище.
                        total = 0
                        chunks = []
                        async for chunk in r.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_BODY_BYTES:  # анти-OOM / gzip-bomb
                                logger.warning(f"Metadata body too large (> {MAX_BODY_BYTES} B)")
                                return {}
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        encoding = r.encoding or "utf-8"
                        break
                else:
                    logger.warning("Metadata too many redirects")
                    return {}
            if body is None:
                continue
            text = body.decode(encoding, errors="replace")
            result = _parse(text, url)
            if result.get("title") and result["title"] not in ("", url):
                logger.info(f"Metadata OK ua={ua[:24]}")
                return result
        except Exception as e:
            # Не логуємо сирий URL/виняток — може містити токени/PII.
            logger.warning(f"Metadata attempt failed: {type(e).__name__}")
            continue
    logger.warning("All httpx metadata attempts failed")
    return {}


def parse_from_html(html: str, url: str) -> dict:
    """Парсимо HTML із Playwright після page.content()."""
    return _parse(html, url)


def _walk_jsonld(data):
    """Обходимо всі JSON-LD обʼєкти включно з @graph — фікс для Elmir/Rozetka/Comfy."""
    if isinstance(data, dict):
        yield data
        if "@graph" in data:
            for item in data["@graph"]:
                yield from _walk_jsonld(item)
    elif isinstance(data, list):
        for item in data:
            yield from _walk_jsonld(item)


def _parse(html: str, url: str) -> dict:
    tree = HTMLParser(html)
    result = {}

    for tag in tree.css("meta"):
        prop = tag.attributes.get("property", "")
        name = tag.attributes.get("name", "")
        content = tag.attributes.get("content", "")
        if not content:
            continue
        if prop == "og:title":
            result["title"] = content
        elif prop == "og:description":
            result["description"] = content
        elif prop == "og:image":
            result["image"] = content
        elif prop == "og:site_name":
            result["site_name"] = content
        elif name == "twitter:title" and "title" not in result:
            result["title"] = content
        elif name == "twitter:description" and "description" not in result:
            result["description"] = content
        elif name == "twitter:image" and "image" not in result:
            result["image"] = content
        elif name == "description" and "description" not in result:
            result["description"] = content

    if "title" not in result:
        node = tree.css_first("title")
        if node:
            result["title"] = node.text(strip=True)

    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
            for item in _walk_jsonld(data):
                if item.get("@type") in ("Product", "product"):
                    if "name" in item and "title" not in result:
                        result["title"] = item["name"]
                    offers = item.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price")
                    currency = offers.get("priceCurrency", "")
                    if price:
                        result["price"] = f"{price} {currency}".strip()
                    brand = item.get("brand", {})
                    if isinstance(brand, dict):
                        result["brand"] = brand.get("name", "")
                    rating = item.get("aggregateRating", {})
                    if rating:
                        rv = rating.get("ratingValue")
                        rc = rating.get("reviewCount")
                        if rv:
                            result["rating"] = f"⭐ {rv}"
                            if rc:
                                result["rating"] += f" ({rc} відгуків)"
                    break
        except Exception:
            continue

    return result
