import asyncio
import signal
import sys

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
    browser_ok = screenshot._browser is not None
    bot_ok = False
    if _bot is not None:
        try:
            await asyncio.wait_for(_bot.get_me(), timeout=5)
            bot_ok = True
        except Exception:
            bot_ok = False
    status = "ok" if (browser_ok and bot_ok) else "degraded"
    code = 200 if status == "ok" else 503
    return JSONResponse(status_code=code, content={"status": status, "browser": browser_ok, "bot": bot_ok})


async def shutdown(dp: Dispatcher, server: uvicorn.Server):
    logger.info("SIGTERM received — starting graceful shutdown")
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

    # get_running_loop замість застарілого get_event_loop (ми вже всередині run()).
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(shutdown(dp, server)))

    await asyncio.gather(dp.start_polling(_bot), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
