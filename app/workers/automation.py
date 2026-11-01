"""Dedicated durable campaign dispatcher; isolated from the live account event loop."""
import asyncio
import logging

from app.services.automation import process_due_campaigns


async def automation_dispatcher() -> None:
    while True:
        try:
            await process_due_campaigns(max_tasks=1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Automation campaign dispatcher failed")
        await asyncio.sleep(2)
