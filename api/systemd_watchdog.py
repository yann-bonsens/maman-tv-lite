# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

import asyncio
import logging
import os
import socket
import threading
import time

logger = logging.getLogger(__name__)

WATCHDOG_SAFETY_MARGIN = 0.5

# How often the event loop must prove it is still being scheduled, and how
# many misses in a row before that counts as a stall rather than an ordinary
# pause. Short and multiplied, not one long window: frequent enough to catch
# a stall well within WatchdogSec, and several misses in a row absorb a slow
# request or a GC pause without withholding a ping over nothing.
LOOP_HEARTBEAT_SECONDS = 2.0
LOOP_STALL_MISSES = 3

# time.monotonic(), never time.time(): this board has no RTC, and on a site
# with no network at all (no ethernet patched, no RTC battery — see
# CLAUDE.md's "this board has no clock" lesson) its wall clock stays wrong
# for the life of the boot, or jumps hours in either direction the moment
# something finally sets it. A staleness check built on time.time() would
# either never trip or trip on every single call depending on which way that
# jump went — monotonic time cannot jump and is the only clock this check may
# use.
_loop_beat_monotonic: "float | None" = None


def _notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(address)
        sock.sendall(message.encode())
    finally:
        sock.close()


def notify_ready() -> None:
    """Call once the application is ready to serve traffic."""
    _notify("READY=1")


def record_loop_beat() -> None:
    """Record that the event loop was just running. Called from
    `keep_loop_beat()`'s own task — never call this from anywhere else, or
    a beat could be recorded by something other than the loop actually
    turning, which is the one thing this exists to prove."""
    global _loop_beat_monotonic
    _loop_beat_monotonic = time.monotonic()


async def keep_loop_beat() -> None:
    """Run as a background task on the event loop for the life of the app.

    Its only job is to be resumed — `await asyncio.sleep()` hands control
    back to the loop, and only the loop scheduling this coroutine again
    proves it is still turning. A hung request handler (an await on CEC or
    MQTT that never completes — exactly the failure modes documented at
    length in CLAUDE.md) stops this coroutine from ever running again, which
    is what `start_watchdog_pings()` below checks for before each ping.
    """
    while True:
        record_loop_beat()
        await asyncio.sleep(LOOP_HEARTBEAT_SECONDS)


def _loop_is_responsive() -> bool:
    """Whether `keep_loop_beat()` has proven the event loop alive recently
    enough to trust. True before the first beat has ever landed — a service
    still starting up must not be killed by this, that is what
    TimeoutStartSec is for."""
    if _loop_beat_monotonic is None:
        return True
    return (time.monotonic() - _loop_beat_monotonic) < LOOP_HEARTBEAT_SECONDS * LOOP_STALL_MISSES


def start_watchdog_pings() -> None:
    """Periodically send WATCHDOG=1 to systemd when the unit sets WatchdogSec.

    A ping is withheld — not sent — whenever the event loop itself has gone
    quiet (see `_loop_is_responsive()`): this thread pings independently of
    request handling, so on its own it could only ever catch a total process
    freeze. Gating each ping on the loop's own heartbeat is what makes a
    stuck handler (a frozen event loop, not just a dead process) stop the
    pings and let systemd's own WatchdogSec restart the service.
    """
    watchdog_usec = os.environ.get("WATCHDOG_USEC")
    if not watchdog_usec:
        return
    interval = (int(watchdog_usec) / 1_000_000) * WATCHDOG_SAFETY_MARGIN

    def loop() -> None:
        while True:
            if _loop_is_responsive():
                _notify("WATCHDOG=1")
            else:
                logger.warning("event loop unresponsive: withholding the watchdog ping")
            threading.Event().wait(interval)

    threading.Thread(target=loop, daemon=True).start()
