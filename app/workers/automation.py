"""Dedicated durable campaign dispatcher; isolated from the live account event loop."""
import asyncio
import logging

from app.services.automation import dispatch_due_schedules, process_due_campaigns, process_due_reactions
from app.settings import get_settings


async def automation_dispatcher() -> None:
    while True:
        try:
            if get_settings().legacy_automation_enabled:
                await dispatch_due_schedules(max_schedules=5)
                await process_due_campaigns(max_tasks=1)
                await process_due_reactions(max_jobs=3)
            else:
                # Only campaigns created through the consent-validated Mini App API
                # are processed while legacy bulk automation remains disabled.
                await process_due_campaigns(max_tasks=1, safe_only=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Automation campaign dispatcher failed")
        await asyncio.sleep(2)
