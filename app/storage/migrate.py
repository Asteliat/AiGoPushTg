"""One-shot database migration entry point for Docker Compose."""
import asyncio
import logging

from app.settings import get_settings
from app.storage.db import engine, init_db


async def main() -> None:
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level.upper())
    await init_db()
    logging.info("Database schema is up to date.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
