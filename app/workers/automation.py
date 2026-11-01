"""Dedicated durable campaign dispatcher; isolated from the live account event loop."""
import asyncio
import logging

from app.services.automation import dispatch_due_schedules, process_due_campaigns


async def automation_dispatcher() -> None:
    while True:
        try:
            await dispatch_due_schedules(max_schedules=5)\n            await dispatch_due_schedules(max_schedules=5)
            await process_due_campaigns(max_tasks=1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Automation campaign dispatcher failed")
        await asyncio.sleep(2)
