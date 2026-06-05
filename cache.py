"""Кеш у RAM під скромні ресурси Render Free.
Зберігає file_id + meta (не байти, нічого на диск). Ключ — sha256(канонічний URL)."""
import hashlib
import time
from urllib.parse import urlparse, urlunparse

from cachetools import TTLCache
from loguru import logger

from config import CACHE_SIZE, MAX_URL_LEN

# --- TTL по типах контенту (секунди) ---
TTL_PHOTO = 3600
TTL_MEDIA_GROUP = 3600
TTL_TEXT_ONLY = 300       # скриншот не вийшов — даємо сайту шанс розблокуватись
TTL_HAS_PRICE = 900       # ціна/рейтинг можуть застаріти швидше
TTL_FAILURE = 180         # негативний кеш — короткий

# Один TTLCache із максимальним TTL; фактичний TTL — через cached_at у записі.
_store = TTLCache(maxsize=CACHE_SIZE, ttl=TTL_PHOTO)


def _canonical(url: str) -> str:
    # Нормалізуємо схему/хост (регістр), прибираємо фрагмент. Query НЕ чіпаємо —
    # у товарних URL він значущий (?id=...). Обрізаємо до MAX_URL_LEN.
    try:
        p = urlparse(url[:MAX_URL_LEN])
        return urlunparse((p.scheme.lower(), (p.netloc or "").lower(), p.path, p.params, p.query, ""))
    except Exception:
        return url[:MAX_URL_LEN]


def _key(url: str) -> str:
    return hashlib.sha256(_canonical(url).encode()).hexdigest()


def _effective_ttl(entry: dict) -> int:
    kind = entry.get("kind")
    if kind == "failure":
        return TTL_FAILURE
    if kind == "text":
        return TTL_TEXT_ONLY
    meta = entry.get("meta") or {}
    if meta.get("price") or meta.get("rating"):
        return TTL_HAS_PRICE
    if kind == "media_group":
        return TTL_MEDIA_GROUP
    return TTL_PHOTO


def get(url: str):
    entry = _store.get(_key(url))
    if entry is None:
        return None
    age = time.time() - entry.get("cached_at", 0)
    ttl = _effective_ttl(entry)
    if age > ttl:
        _store.pop(_key(url), None)
        logger.info(f"CACHE expired kind={entry.get('kind')} age={age:.0f}s ttl={ttl}s")
        return None
    logger.info(f"CACHE hit kind={entry.get('kind')} age={age:.0f}s")
    return entry


def save_photo(url: str, file_id: str, meta: dict):
    _store[_key(url)] = {"kind": "photo", "file_id": file_id, "meta": meta or {}, "cached_at": time.time()}
    logger.info("CACHE save photo")


def save_media_group(url: str, file_ids: list, meta: dict):
    _store[_key(url)] = {"kind": "media_group", "file_ids": file_ids, "meta": meta or {}, "cached_at": time.time()}
    logger.info(f"CACHE save media_group parts={len(file_ids)}")


def save_text_only(url: str, meta: dict):
    _store[_key(url)] = {"kind": "text", "meta": meta or {}, "cached_at": time.time()}
    logger.info("CACHE save text_only")


def save_failure(url: str, reason: str):
    _store[_key(url)] = {"kind": "failure", "failure_reason": reason, "cached_at": time.time()}
    logger.info(f"CACHE save failure reason={reason}")


def stats() -> dict:
    counts = {"photo": 0, "media_group": 0, "text": 0, "failure": 0}
    for entry in _store.values():
        kind = entry.get("kind", "unknown")
        if kind in counts:
            counts[kind] += 1
    return {"size": len(_store), "maxsize": CACHE_SIZE, **counts}
