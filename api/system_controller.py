# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

import logging
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

# Delay before the action actually runs, so that uvicorn can send the HTTP
# response before the machine goes down. Without it the client just sees a
# dropped connection and cannot tell whether the request was accepted.
ACTION_DELAY_SECONDS = 2.0

LOGIND_BUS = [
    "org.freedesktop.login1",
    "/org/freedesktop/login1",
    "org.freedesktop.login1.Manager",
]
DBUS_TIMEOUT_SECONDS = 10


class SystemCommandError(Exception):
    """The system action is not permitted, or could not be started."""


def _query_logind(method: str) -> str:
    """Ask logind whether an action is allowed (CanReboot / CanPowerOff).

    Returns "yes", "no", "challenge" or "na". "challenge" means interactive
    authentication would be required — impossible here, since the machine runs
    with no screen and no keyboard.
    """
    try:
        result = subprocess.run(
            ["busctl", "call", *LOGIND_BUS, method],
            capture_output=True,
            text=True,
            timeout=DBUS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemCommandError(f"cannot query logind: {exc}") from exc

    if result.returncode != 0:
        raise SystemCommandError(f"logind refused {method}: {result.stderr.strip()}")

    # Expected output: s "yes"
    return result.stdout.strip().removeprefix("s ").strip('"')


def _ensure_allowed(method: str) -> None:
    """Fail early and clearly rather than returning a success that never happens.

    Without the polkit rule shipped in this repository
    (polkit/50-maman-tv-lite-power.rules), logind answers "challenge" and
    `systemctl reboot` does nothing at all: the API would report success while
    the machine stayed up.
    """
    verdict = _query_logind(method)
    if verdict != "yes":
        raise SystemCommandError(
            f"action not permitted (logind {method} = {verdict!r}) — check that "
            "polkit/50-maman-tv-lite-power.rules is installed in "
            "/etc/polkit-1/rules.d/"
        )


def _schedule(command: list[str]) -> None:
    def run() -> None:
        time.sleep(ACTION_DELAY_SECONDS)
        logger.info("Running %s", " ".join(command))
        subprocess.run(command, check=False)

    threading.Thread(target=run, daemon=True).start()


def reboot() -> None:
    """Reboot the machine after a short delay."""
    _ensure_allowed("CanReboot")
    logger.warning("Reboot requested through the API")
    _schedule(["systemctl", "reboot"])


def shutdown() -> None:
    """Power the machine off after a short delay.

    Beware: once off, a Raspberry Pi can only be started again physically —
    there is no power button and no Wake-on-LAN by default.
    """
    _ensure_allowed("CanPowerOff")
    logger.warning("Shutdown requested through the API")
    _schedule(["systemctl", "poweroff"])
