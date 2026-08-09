from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable


async def bounded_parallel(tasks: Iterable[Callable[[], Awaitable[object]]], max_concurrency: int = 2) -> list[object]:
    if max_concurrency < 1: raise ValueError("max_concurrency must be positive")
    semaphore = asyncio.Semaphore(max_concurrency)
    async def run(task):
        async with semaphore: return await task()
    return await asyncio.gather(*(run(task) for task in tasks))
