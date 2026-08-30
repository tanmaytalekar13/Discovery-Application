import asyncio

import pytest

from app.search.concurrency import gather_source_outcomes


@pytest.mark.asyncio
async def test_empty_tasks_returns_empty_list():
    assert await gather_source_outcomes({}) == []


@pytest.mark.asyncio
async def test_sources_run_concurrently_not_sequentially():
    order: list[str] = []

    async def slow():
        await asyncio.sleep(0.05)
        order.append("slow")
        return []

    async def fast():
        order.append("fast")
        return []

    await gather_source_outcomes({"slow": slow(), "fast": fast()})

    # `fast` finishes first only if both ran concurrently rather than
    # `slow` being awaited to completion before `fast` ever started.
    assert order == ["fast", "slow"]


@pytest.mark.asyncio
async def test_one_failure_is_isolated_from_the_rest():
    async def ok():
        return ["candidate"]

    async def broken():
        raise ValueError("boom")

    outcomes = await gather_source_outcomes({"ok": ok(), "broken": broken()})

    by_source = {outcome.source: outcome for outcome in outcomes}

    assert by_source["ok"].succeeded is True
    assert by_source["ok"].candidates == ("candidate",)

    assert by_source["broken"].succeeded is False
    assert by_source["broken"].candidates == ()
    assert "boom" in by_source["broken"].error
