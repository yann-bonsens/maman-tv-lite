# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""What the box is doing, in one place.

Three things render this: the API (`GET /state`), the shell command
(`maman-tv status`) and the diagnostic screen on the television. They must
never disagree, so they all read this object — the screen through the file it
publishes, because it runs as root in another process.

It also carries a short history. During the 2026-09-22 incident the journal
had already been vacuumed by the time anybody looked, and the box could not
say what it had done ten minutes earlier. Thirty events cost nothing and are
the difference between a diagnosis and a guess.

Times are recorded twice on purpose: as a clock reading and as seconds since
boot. This board has no clock of its own, so after a power cut it starts with
whatever time it last knew and jumps when the network answers — which made two
boots overlap in the journal and led to an hour of wrong conclusions. Seconds
since boot never lie.
"""

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# The default state, and the three modes. "television" is not a mode: it is
# what the box is when it is not doing anything of its own — the set shows its
# own programmes and the box's HDMI output is asleep.
TELEVISION = "television"
MUSIC = "music"
DIAGNOSTIC = "diagnostic"
INSTALLATION = "installation"
MODES = (MUSIC, DIAGNOSTIC, INSTALLATION)

RUNTIME_DIR = os.environ.get("RUNTIME_DIRECTORY", "/run/maman-tv-lite")
PATH = os.path.join(RUNTIME_DIR, "state.json")

EVENTS_KEPT = 30

_lock = threading.RLock()
_state = {
    "mode": TELEVISION,
    "mode_since": None,
    "hdmi": "unknown",          # awake | asleep | unknown
    "tv_power": "unknown",
    "tv_power_at": None,
    "events": [],
}


def _monotonic_boot_seconds() -> float:
    try:
        with open("/proc/uptime", encoding="ascii") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError):
        return time.monotonic()


def _stamp() -> dict:
    return {"clock": time.strftime("%Y-%m-%d %H:%M:%S"),
            "since_boot": round(_monotonic_boot_seconds(), 1)}


def note(event: str, **details) -> None:
    """Record something worth seeing in a report, and log it."""
    with _lock:
        entry = {"event": event, "at": _stamp()}
        if details:
            entry.update(details)
        _state["events"].append(entry)
        del _state["events"][:-EVENTS_KEPT]
    logger.info("%s%s", event,
                "".join(f" {k}={v!r}" for k, v in sorted(details.items())))
    publish()


def set_mode(mode: str) -> None:
    with _lock:
        if _state["mode"] == mode:
            return
        _state["mode"] = mode
        _state["mode_since"] = _stamp()
    note("mode", mode=mode)


def mode() -> str:
    with _lock:
        return _state["mode"]


def set_hdmi(status: str) -> None:
    with _lock:
        _state["hdmi"] = status
    publish()


def set_tv_power(status: str) -> None:
    with _lock:
        _state["tv_power"] = status
        _state["tv_power_at"] = _stamp()
    publish()


def snapshot() -> dict:
    """Everything the box knows about itself, right now.

    Imports are done here rather than at module scope: this module is the one
    everything else depends on, and a cycle through it would be a poor trade
    for a few microseconds.
    """
    import tv_config

    with _lock:
        state = json.loads(json.dumps(_state))
    # The whole configuration, not a hand-picked few of its keys.
    #
    # This used to name four of them, and every setting added afterwards was
    # simply invisible from then on. The diagnostic page on the television
    # reads `hdmi_sleep` from here and printed "output=asleep (?)" for ever,
    # because nothing ever published it; the release technique — the one that
    # decides what the tv button does while the music plays — appeared
    # nowhere at all; and neither the page nor `maman-tv status` could say
    # whether the detection had ever been completed. Three readers, all wrong
    # together, which is exactly what `/state`'s "they cannot disagree,
    # because there is only one" promises cannot happen.
    #
    # Publishing the lot means a setting added to tv_config.py is visible
    # everywhere without anyone remembering to come back here. A test pins
    # that every key the defaults carry arrives in this block.
    state["configuration"] = dict(tv_config.load())
    state["configuration"]["file"] = tv_config.PATH
    state["now"] = _stamp()
    return state


def publish() -> None:
    """Write the state where the screen — another process, running as root —
    can read it. Losing this file costs a screen that shows nothing, never a
    press that does nothing, so every failure is logged and swallowed."""
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        tmp = f"{PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(snapshot(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, PATH)
        os.chmod(PATH, 0o644)
    except OSError as exc:
        logger.debug("could not publish the state: %s", exc)
