"""Scheduler event-loop hygiene tests (C-3, FR-12).

``BriefingService.warm_and_prune`` / ``refresh_active`` are synchronous, network/GRIB-heavy
calls that can run for minutes on a cold cycle. Run inline in :func:`run_scheduler` they
starved the asyncio event loop — ``/v1/health`` and every briefing request froze for the
whole pass. These tests pin the fix (each pass dispatched via ``asyncio.to_thread``), the
loop's survive-a-bad-cycle semantics, and the lifespan shutdown timeout that keeps a
blocked pass from hanging process exit.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time
from types import SimpleNamespace

import upstreamwx.api.scheduler as scheduler

# `import upstreamwx.api.app as app_mod` would bind the FastAPI *instance* (the package
# re-exports it as the attribute `app`, shadowing the submodule); resolve the module itself.
app_mod = importlib.import_module("upstreamwx.api.app")


def test_scheduler_pass_runs_off_the_event_loop(monkeypatch):
    """warm/refresh run in a worker thread and the loop stays responsive meanwhile (C-3)."""
    monkeypatch.setattr(
        scheduler, "get_settings", lambda: SimpleNamespace(healthcheck_url=None)
    )
    monkeypatch.setattr(scheduler, "seconds_until_next_cycle", lambda: 0.01)
    calls: list[tuple[str, bool]] = []

    async def scenario() -> int:
        stop = asyncio.Event()
        loop_thread = threading.current_thread()

        class FakeService:
            def warm_and_prune(self) -> int:
                calls.append(("warm", threading.current_thread() is loop_thread))
                time.sleep(0.15)  # a blocking pass — must not stall the event loop
                return 0

            def refresh_active(self) -> int:
                calls.append(("refresh", threading.current_thread() is loop_thread))
                stop.set()
                return 0

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        await asyncio.wait_for(scheduler.run_scheduler(FakeService(), stop=stop), timeout=5)
        t.cancel()
        return ticks

    ticks = asyncio.run(scenario())
    assert [name for name, _ in calls] == ["warm", "refresh"]
    assert not any(on_loop for _, on_loop in calls), "pass ran on the event-loop thread"
    # The 0.15 s blocking warm ran concurrently with the 10 ms ticker: had it blocked the
    # loop, the ticker could not have fired more than once or twice.
    assert ticks >= 5, f"event loop starved during the pass (ticks={ticks})"


def test_scheduler_survives_failing_pass_and_pings_fail(monkeypatch):
    """Exceptions in warm/refresh are swallowed (one bad cycle never kills the loop) and
    the dead-man's-switch gets the /start then /fail pings (FR-12 monitoring)."""
    pings: list[str] = []

    async def fake_ping(url: str | None, suffix: str = "") -> None:
        pings.append(suffix)

    monkeypatch.setattr(scheduler, "_ping", fake_ping)
    monkeypatch.setattr(
        scheduler, "get_settings", lambda: SimpleNamespace(healthcheck_url="https://hc")
    )
    monkeypatch.setattr(scheduler, "seconds_until_next_cycle", lambda: 0.01)

    stop = asyncio.Event()

    class FailingService:
        def warm_and_prune(self) -> int:
            raise RuntimeError("warm boom")

        def refresh_active(self) -> int:
            stop.set()
            raise RuntimeError("refresh boom")

    async def scenario() -> None:
        await asyncio.wait_for(scheduler.run_scheduler(FailingService(), stop=stop), timeout=5)

    asyncio.run(scenario())  # returning at all proves the loop survived both exceptions
    assert pings == ["/start", "/fail"]


def test_lifespan_shutdown_does_not_hang_on_blocked_scheduler(monkeypatch, caplog):
    """Shutdown abandons a scheduler task that won't die within the timeout (C-3).

    Previously the lifespan awaited the cancelled task without a timeout, so a pass
    blocked in a way that delays cancellation could hang process exit indefinitely.
    """
    monkeypatch.setattr(app_mod, "_SCHEDULER_SHUTDOWN_TIMEOUT_S", 0.2)
    monkeypatch.setattr(
        app_mod,
        "get_settings",
        lambda: SimpleNamespace(
            api_enable_scheduler=True, api_enable_warm=False, api_enable_decode_pool=False
        ),
    )

    async def stubborn(service, *, stop):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Swallow the first cancel to simulate a pass that won't exit promptly; the
            # bounded sleep keeps the test finite regardless of wait_for internals.
            await asyncio.sleep(1)

    monkeypatch.setattr(app_mod, "run_scheduler", stubborn)

    async def scenario() -> None:
        async with app_mod.lifespan(app_mod.app):
            await asyncio.sleep(0.05)

    with caplog.at_level(logging.WARNING, logger="upstreamwx.api"):
        asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert any("did not exit" in rec.getMessage() for rec in caplog.records)
