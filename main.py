import asyncio
import signal
import sys
import time

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from aiogram import Bot, Dispatcher
from loguru import logger

from bot import router
import screenshot, queue_manager
from config import BOT_TOKEN, PORT, LOG_LEVEL

# Централізоване логування. Рівень — з env. Сирі винятки ми вже маскуємо в модулях
# (logging type(e).__name__), тут вимикаємо diagnose/backtrace, щоб у трейс не
# потрапляли значення змінних (зокрема токен/URL).
logger.remove()
logger.add(sys.stderr, level=LOG_LEVEL, backtrace=False, diagnose=False)

app = FastAPI()
_bot: Bot | None = None

# Кеш результату getMe для /health (M-1). /health відкритий → потік запитів інакше
# смикав би getMe до Telegram на КОЖЕН запит (rate-limit + звʼязування корутин по 5с,
# DoS-амплітфікація). Оновлюємо не частіше ніж раз на 20с.
_last_me = {"ts": 0.0, "ok": False}


@app.get("/")
@app.head("/")
async def root():
    return {"ok": True}


@app.get("/ping")
@app.head("/ping")
async def ping():
    return {"ok": True}


@app.get("/health")
async def health():
    # Мінімум розкриття: лише статус і булеві прапорці. Внутрішні лічильники
    # черги/кешу НЕ віддаємо назовні (recon для DoS) — вони лишаються в логах.
    # browser: жив І підключений (is_connected ловить мертвий процес Chromium).
    browser_ok = screenshot._browser is not None and screenshot._browser.is_connected()
    # worker: супервізорний таск живий. Ловить тиху смерть черги (R-1).
    wt = queue_manager._worker_task
    worker_ok = wt is not None and not wt.done()
    # bot: getMe з кешем на 20с (M-1).
    now = time.monotonic()
    if _bot is not None and now - _last_me["ts"] > 20:
        try:
            await asyncio.wait_for(_bot.get_me(), timeout=5)
            _last_me.update(ts=now, ok=True)
        except Exception:
            _last_me.update(ts=now, ok=False)
    bot_ok = _last_me["ok"]
    status = "ok" if (browser_ok and bot_ok and worker_ok) else "degraded"
    code = 200 if status == "ok" else 503
    return JSONResponse(status_code=code, content={
        "status": status, "browser": browser_ok, "bot": bot_ok, "worker": worker_ok,
    })


async def shutdown(dp: Dispatcher, server: uvicorn.Server):
    logger.info("Shutdown signal received — starting graceful shutdown")
    await dp.stop_polling()
    logger.info("Polling stopped")

    try:
        await asyncio.wait_for(screenshot.semaphore.acquire(), timeout=60)
        screenshot.semaphore.release()
        logger.info("Semaphore free — no active screenshot")
    except asyncio.TimeoutError:
        logger.warning("Semaphore timeout — forcing shutdown anyway")

    if screenshot._browser is not None:
        try:
            await screenshot._browser.close()
            logger.info("Browser closed cleanly")
        except Exception as e:
            logger.warning(f"Browser close error: {e}")

    if screenshot._pw is not None:
        try:
            await screenshot._pw.stop()
            logger.info("Playwright stopped")
        except Exception as e:
            logger.warning(f"Playwright stop error: {e}")

    server.should_exit = True
    logger.info("Shutdown complete")


async def main():
    global _bot

    await screenshot.init()

    queue_manager.register_processor(screenshot.shoot)
    queue_manager.start_worker()

    _bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await _bot.delete_webhook(drop_pending_updates=True)

    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower())
    )

    # Вимикаємо власні signal-handler'и uvicorn (№3): інакше він перехоплює
    # SIGTERM/SIGINT першим, рве свій loop, і наш shutdown() (штатне закриття
    # браузера) не встигає → косметичний RuntimeError: Event loop is closed від
    # фіналізатора subprocess Chromium. Реєструємо ЄДИНИЙ обробник на обидва
    # сигнали — наш shutdown().
    server.install_signal_handlers = lambda: None

    # get_running_loop замість застарілого get_event_loop (ми вже всередині run()).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(dp, server)))

    await asyncio.gather(dp.start_polling(_bot), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
