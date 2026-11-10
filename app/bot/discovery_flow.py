"""Shared mechanics of the chat and channel discovery flows.

Both flows search Telegram through the connected accounts, query public search engines
in parallel, merge the results by link and then verify every link through Telegram. Only
the search and verification functions differ, so that part lives here once.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.bot.core import isolated_telegram_client
from app.storage.db import SessionFactory
from app.storage.models import Account

NATIVE_SEARCH_CONCURRENCY = 3
NATIVE_RESULT_LIMIT = 100
WEB_RESULT_LIMIT = 600
WEB_PAGES_PER_QUERY = 5
CANDIDATE_LIMIT = 600

NativeSearch = Callable[..., Awaitable[list[dict]]]
WebSearch = Callable[..., Awaitable[list[dict]]]


async def search_with_accounts(
    account_ids: list[int],
    query: str,
    *,
    native_search: NativeSearch,
    log_label: str,
) -> list[dict]:
    """Run the Telegram-native search on each account in its own isolated session."""
    semaphore = asyncio.Semaphore(NATIVE_SEARCH_CONCURRENCY)

    async def search_one_account(search_account_id: int) -> list[dict]:
        try:
            async with SessionFactory() as db:
                search_account = await db.get(Account, search_account_id)
            if not search_account or not search_account.enabled:
                return []
            async with isolated_telegram_client(search_account) as search_client:
                if not await search_client.is_user_authorized():
                    return []
                return await native_search(search_client, query, limit=NATIVE_RESULT_LIMIT)
        except Exception as exc:
            logging.warning("%s discovery failed for account %s: %s", log_label, search_account_id, exc)
            return []

    async def limited(search_account_id: int) -> list[dict]:
        async with semaphore:
            return await search_one_account(search_account_id)

    batches = await asyncio.gather(
        *(limited(search_account_id) for search_account_id in account_ids),
        return_exceptions=True,
    )
    return [item for batch in batches if isinstance(batch, list) for item in batch]


def merge_candidates(*batches: list[dict]) -> list[dict]:
    """Merge candidates by link, keeping the best score and every source."""
    combined: dict[str, dict] = {}
    for batch in batches:
        for item in batch:
            link = str(item.get("link") or "").strip()
            if not link:
                continue
            key = link.casefold()
            if key not in combined:
                combined[key] = item
                continue
            current = combined[key]
            current["score"] = max(
                int(current.get("score") or 0), int(item.get("score") or 0)
            )
            current["source"] = ", ".join(
                dict.fromkeys(
                    (current.get("source", "") + ", " + item.get("source", ""))
                    .strip(", ")
                    .split(", ")
                )
            )
    return list(combined.values())[:CANDIDATE_LIMIT]


async def collect_candidates(
    account_ids: list[int],
    query: str,
    *,
    native_search: NativeSearch,
    web_search: WebSearch,
    log_label: str,
) -> list[dict]:
    """Search Telegram and the web in parallel and merge the raw candidates."""
    native_task = asyncio.create_task(
        search_with_accounts(
            account_ids, query, native_search=native_search, log_label=log_label
        )
    )
    web_task = asyncio.create_task(
        web_search(query, limit=WEB_RESULT_LIMIT, pages_per_query=WEB_PAGES_PER_QUERY)
    )
    native_result, web_result = await asyncio.gather(native_task, web_task, return_exceptions=True)
    return merge_candidates(
        native_result if isinstance(native_result, list) else [],
        web_result if isinstance(web_result, list) else [],
    )
