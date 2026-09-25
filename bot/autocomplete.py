"""Time-boxing for slash-command autocomplete handlers that call UEX.

Discord discards an autocomplete response that arrives after ~3 seconds, and UexClient
allows a 15s timeout with retries, so a cold cache or a slow UEX response used to leave the
user with no suggestions and nothing logged. gather_within() stops waiting at a budget
but does NOT cancel the slow fetches: they keep running in the background and fill
UexClient's cache, so the very next keystroke gets an instant answer.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable

logger = logging.getLogger(__name__)

AUTOCOMPLETE_BUDGET_SECONDS = 2.5

# asyncio only keeps weak references to tasks, so a fetch we stopped waiting on needs a
# strong one until it finishes, or it can be garbage-collected mid-flight.
_background: set[asyncio.Task] = set()


def _finish_in_background(task: asyncio.Task) -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.info("Background autocomplete fetch failed after its deadline: %r", task.exception())


async def gather_within(*aws: Awaitable[Any], timeout: float | None = None) -> list[Any]:
    """Like asyncio.gather(..., return_exceptions=True), but returns after `timeout`
    seconds (default AUTOCOMPLETE_BUDGET_SECONDS). Each slot holds the result, the
    exception the awaitable raised, or a TimeoutError if it hadn't finished in time."""
    tasks = [asyncio.ensure_future(aw) for aw in aws]
    budget = AUTOCOMPLETE_BUDGET_SECONDS if timeout is None else timeout
    _, pending = await asyncio.wait(tasks, timeout=budget)
    for task in pending:
        _background.add(task)
        task.add_done_callback(_finish_in_background)

    outcomes: list[Any] = []
    for task in tasks:
        if task in pending:
            outcomes.append(TimeoutError(f"autocomplete fetch exceeded {budget}s"))
        elif task.cancelled():
            outcomes.append(asyncio.CancelledError())
        elif task.exception() is not None:
            outcomes.append(task.exception())
        else:
            outcomes.append(task.result())
    return outcomes
