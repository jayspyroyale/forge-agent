"""Cancellation-safe blocking work: settle the worker before resources disappear."""

import asyncio
import contextlib


async def run_blocking(function, *args, on_cancel=None, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        # A thread is not cancelled by cancelling its awaiting coroutine.
        # Never remove its workspace/close its database while it can still write.
        with contextlib.suppress(Exception):
            await asyncio.shield(worker)
        raise
