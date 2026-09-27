# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Asking the screen service to draw something.

Consoles belong to root. Every one of them on this board is `root:root 0600`
and held by a login prompt, and this service runs as an ordinary account with
NoNewPrivileges — so it cannot draw a photo or a diagnostic page itself. A
small root service does that, and this module is how the two talk.

A **named pipe**, not a file somebody polls. The screen used to come round
once a second for the whole life of the machine, which on this board cost 2%
of the single core for ever and was the reason the diagnostic display existed
at all. It now blocks on this pipe and costs nothing until it is asked for
something.

The write never blocks. A pipe with no reader refuses to open, which is
exactly the answer wanted: the screen service is not running, the box says so,
and nothing waits. A press must never hang because a screen is missing.
"""

import errno
import logging
import os

logger = logging.getLogger(__name__)

RUNTIME_DIR = os.environ.get("RUNTIME_DIRECTORY", "/run/maman-tv-lite")
FIFO = os.path.join(RUNTIME_DIR, "screen")


def ask(command: str) -> bool:
    """Ask the screen for something. True when it was handed over."""
    try:
        handle = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ENOENT):
            logger.info("the screen service is not listening; %r not sent", command)
        else:
            logger.warning("could not reach the screen: %s", exc)
        return False
    try:
        os.write(handle, (command + "\n").encode("ascii", "replace"))
        return True
    except OSError as exc:
        logger.warning("could not send %r to the screen: %s", command, exc)
        return False
    finally:
        os.close(handle)


def show(what: str) -> bool:
    """Draw a page: "diagnostic"."""
    return ask(what)


def photos() -> bool:
    """Start the slideshow from the list the media module has just written."""
    return ask("photos")


def page() -> bool:
    """Show the image the installation screen has just written to page.png."""
    return ask("page")


def press(key: str) -> bool:
    """Press one key in the slideshow — "next", "previous"."""
    return ask(f"key {key}")


def off() -> bool:
    """Stop drawing: no photos, no page, nothing on the console."""
    return ask("off")
