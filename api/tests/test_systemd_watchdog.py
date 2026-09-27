# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The watchdog ping's actual liveness check.

Not the socket plumbing (`_notify` needs a real NOTIFY_SOCKET and is not
worth faking here) — this is about the bug the audit found: a ping thread
running on its own proves nothing about whether the event loop is still
turning. `_loop_is_responsive()` is what closes that gap, and it must do it
with a clock that cannot be fooled by this board's own clock jumping across
a boot (see CLAUDE.md's "this board has no clock" lesson) — so every test
here drives it through `time.monotonic()`, never `time.time()`.
"""

import asyncio
import contextlib
import time

import systemd_watchdog as watchdog


class TestLoopIsResponsive:
    def setup_method(self):
        watchdog._loop_beat_monotonic = None

    def test_true_before_the_first_heartbeat_ever_lands(self):
        """A service still starting up must not be judged unresponsive —
        that is TimeoutStartSec's job, not this one's."""
        assert watchdog._loop_is_responsive()

    def test_true_right_after_a_heartbeat(self):
        watchdog.record_loop_beat()
        assert watchdog._loop_is_responsive()

    def test_false_once_the_heartbeat_is_stale(self):
        watchdog.record_loop_beat()
        stale_by = watchdog.LOOP_HEARTBEAT_SECONDS * watchdog.LOOP_STALL_MISSES + 1
        watchdog._loop_beat_monotonic = time.monotonic() - stale_by
        assert not watchdog._loop_is_responsive()

    def test_one_missed_beat_alone_is_not_a_stall(self):
        """Several misses in a row, not one: a single slow request or GC
        pause must not withhold a ping over nothing."""
        watchdog.record_loop_beat()
        watchdog._loop_beat_monotonic = time.monotonic() - watchdog.LOOP_HEARTBEAT_SECONDS
        assert watchdog._loop_is_responsive()


class TestKeepLoopBeat:
    def test_it_records_a_beat_on_every_pass(self, monkeypatch):
        """A tiny interval so the test does not actually wait — the
        coroutine itself, not the real timing, is what is under test. No
        pytest-asyncio in this project, so the loop is driven by hand with
        `asyncio.run()`, the same as everywhere else a coroutine needs
        exercising outside of uvicorn."""
        monkeypatch.setattr(watchdog, "LOOP_HEARTBEAT_SECONDS", 0.001)
        watchdog._loop_beat_monotonic = None

        async def run_briefly():
            task = asyncio.create_task(watchdog.keep_loop_beat())
            try:
                for _ in range(50):
                    if watchdog._loop_beat_monotonic is not None:
                        break
                    await asyncio.sleep(0.001)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(run_briefly())
        assert watchdog._loop_beat_monotonic is not None
