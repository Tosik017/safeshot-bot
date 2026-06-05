"""Черга на скриншот: ліміт глибини + per-chat квота + RAM-watchdog +
дедуплікація + глобальний таймаут. Один воркер (відповідає SEMAPHORE=1)."""
import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass, field

import psutil
from loguru import logger

from config import (
    MAX_QUEUE_SIZE, TASK_TIMEOUT_SEC, MAX_INFLIGHT_PER_CHAT, RAM_LIMIT_MB,
)


@dataclass
class QueueTask:
    key: tuple
    url: str
    future: asyncio.Future = field(default_factory=asyncio.Future)
    position: int = 0


class QueueFull(Exception):
    """Черга заповнена / ліміт ресурсів — бот перевантажений."""
    pass


_queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
_inflight: dict = {}                        # key -> Future
_inflight_by_chat: dict = defaultdict(int)  # chat_id -> count
_worker_task = None
_processor = None


def register_processor(processor):
    global _processor
    _processor = processor


async def enqueue(key, url):
    """Повертає (future, position, is_duplicate). Кидає QueueFull при перевантаженні."""
    # Дедуп: той самий (chat,thread,url) уже в роботі → підчіпляємось.
    if key in _inflight:
        logger.info("QUEUE dedup — already in-flight")
        return _inflight[key], 0, True

    # RAM-watchdog: біля межі 512MB не беремо нову задачу (анти-OOM).
    rss_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    if rss_mb > RAM_LIMIT_MB:
        logger.warning(f"QUEUE reject — RAM {rss_mb:.0f}MB > {RAM_LIMIT_MB}MB")
        raise QueueFull()

    # Per-chat квота: один чат не може зайняти всю чергу (анти-DoS).
    chat_id = key[0]
    if _inflight_by_chat[chat_id] >= MAX_INFLIGHT_PER_CHAT:
        logger.warning(f"QUEUE reject — chat {chat_id} over per-chat limit")
        raise QueueFull()

    if _queue.qsize() >= MAX_QUEUE_SIZE:
        logger.warning(f"QUEUE full ({_queue.qsize()}/{MAX_QUEUE_SIZE})")
        raise QueueFull()

    task = QueueTask(key=key, url=url)
    _inflight[key] = task.future
    _inflight_by_chat[chat_id] += 1
    position = _queue.qsize() + 1
    task.position = position
    await _queue.put(task)
    logger.info(f"QUEUE enqueued position={position} qsize={_queue.qsize()}")
    return task.future, position, False


async def _worker():
    logger.info("Queue worker started")
    while True:
        task = await _queue.get()
        try:
            try:
                result = await asyncio.wait_for(_processor(task.url), timeout=TASK_TIMEOUT_SEC)
                if not task.future.done():
                    task.future.set_result(result)
            except asyncio.TimeoutError:
                logger.warning(f"QUEUE task timeout after {TASK_TIMEOUT_SEC}s")
                if not task.future.done():
                    task.future.set_result(([], {}))
            except Exception as e:
                logger.error(f"QUEUE task failed: {type(e).__name__}")
                if not task.future.done():
                    task.future.set_exception(e)
        finally:
            _inflight.pop(task.key, None)
            chat_id = task.key[0]
            _inflight_by_chat[chat_id] -= 1
            if _inflight_by_chat[chat_id] <= 0:
                _inflight_by_chat.pop(chat_id, None)
            _queue.task_done()


def start_worker():
    global _worker_task
    _worker_task = asyncio.create_task(_worker())


def get_stats() -> dict:
    return {"queue_size": _queue.qsize(), "queue_max": MAX_QUEUE_SIZE, "inflight_urls": len(_inflight)}
