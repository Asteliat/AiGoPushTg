"""Minimal worker heartbeat; intentionally does not automate group messaging."""
import asyncio
import logging
from redis.asyncio import Redis
from app.settings import get_settings

async def main() -> None:
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level.upper())
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    try:
        while True:
            await redis.set("atcivperson:worker:last_seen", "ok", ex=60)
            await asyncio.sleep(20)
    finally:
        await redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())
