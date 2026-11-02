import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import MenuButtonWebApp, WebAppInfo

from app.bot.handlers import router
from app.settings import get_settings
from app.storage.db import engine


async def main() -> None:
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level.upper())
    if not cfg.bot_token:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "sessions").mkdir(parents=True, exist_ok=True)
    bot = Bot(token=cfg.bot_token)
    if cfg.mini_app_url:
        if not cfg.mini_app_url.startswith("https://"):
            logging.error("MINI_APP_URL must use HTTPS; the Mini App menu button was not configured.")
        else:
            try:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="Открыть панель",
                        web_app=WebAppInfo(url=cfg.mini_app_url),
                    )
                )
            except Exception:
                logging.exception("Could not configure the Telegram Mini App menu button.")
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
