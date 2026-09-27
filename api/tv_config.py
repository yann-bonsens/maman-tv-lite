# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""What this box knows about the television in front of it.

One file, read at start, **never written by an everyday press**. That last
point is the whole reason this module exists. Before it, the box searched for
a working CEC technique while somebody was pressing a button, recorded what it
believed, struck techniques off and forgot them again — and on 2026-09-22 an
accidental press wiped a configuration that had taken an evening to establish,
leaving an installation that switched the set off by claiming its HDMI input,
so the television came back on the box every time. A press now sends one
frame; only the installation mode, the API or a human edits this file.

The defaults are the two most standard frames in the protocol: Image View On
to wake, plain Standby to switch off. They are what a fresh installation uses
until somebody runs the detection, and they are deliberately the least
intrusive: neither of them touches which input the set displays.

`source` says where each answer came from — "default", "detection" or
"manual". A box that has never been configured therefore says so, out loud, in
the diagnostic screen and in the report, instead of looking configured.

The `television` block is informational: manufacturer and model as the set
reports them in its EDID, plus its CEC name. Nothing reads it to decide
anything. It answers the question that could not be answered during the
2026-09-22 incident — "was this configuration made for the set standing in
front of me?" — and it is what a library of per-model profiles would be built
from later.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

# In the service's own state directory rather than /etc: the API runs with
# ProtectSystem=strict, so /etc is read-only to it, and this file is written by
# the box itself when the installation mode or the API sets a technique.
PATH = os.environ.get(
    "MAMAN_TV_CONFIG",
    os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/maman-tv-lite"), "tv.json"))

# Written by the versions that learned techniques while the box was in use.
# Read once, for its techniques only: a box already installed must not lose a
# working answer to an upgrade.
LEGACY_PATH = os.path.join(os.path.dirname(PATH), "cec-learned.json")

DEFAULT_WAKE = "image_view_on"
DEFAULT_SLEEP = "standby"
# Not one of cec_controller.RELEASE_TECHNIQUES: it is the one release
# candidate that is not a CEC frame at all (the box's own output going to
# sleep is the mechanism — see modes.py's begin/end_release_power_cycle),
# which is exactly why it is a safe, always-applicable default: it needs
# nothing the television might refuse or ignore, only the box's own screen.
DEFAULT_RELEASE = "power_cycle"

# How long the CEC detection procedure gives one frame to be noticed (a wake
# or sleep candidate), and one full off-and-on cycle (the "power_cycle"
# release candidate, which switches the set off and on again). Properties of
# the television in front of the box, not of the product, so they live here
# and the summary page writes them back with everything else — a set that is
# slow to show a picture gets them raised by hand before a second run.
DEFAULT_DETECTION_STEP_SECONDS = 30
DEFAULT_DETECTION_CYCLE_SECONDS = 90

# How the box stops driving the television's screen.
#
# "blank" puts the output to sleep (FB_BLANK_POWERDOWN): the picture goes, the
# connector stays connected, and the kernel keeps the set's EDID and the box's
# CEC address. "connector" forces the connector off, which the set sees the
# same way but which makes the kernel forget the EDID — and it can only read
# it back from a television that answers, which one that unplugs its input
# while it sleeps never does. That is what left a box unable to send a single
# CEC frame at the installation site.
#
# So "blank" is the default, and "connector" is there for a set that ignores
# it. Measured on 2026-09-23: both made the television report an absent source.
#
# "none" is neither: the output is never put to sleep at all, on purpose. It
# exists for whatever setup genuinely does not need rule 2 of modes.py (a
# monitor rather than a television with its own tuner, say, or a set that
# demonstrably copes with coming back on the box's input) — CLAUDE.md is
# explicit that the sleeping output is *why* a set falls back to its own
# programmes rather than showing the box, so choosing "none" trades that
# guarantee away deliberately, not by accident.
HDMI_SLEEP_METHODS = ("blank", "connector", "none")
DEFAULT_HDMI_SLEEP = "blank"

SOURCES = ("default", "detection", "manual")

# A sleep technique whose name carries this suffix claims the input before
# switching the set off (see cec_controller.SLEEP_TECHNIQUES). Read off the
# name rather than duplicated as a second list here, which could drift from
# the one cec_controller actually uses.
_CLAIMS_INPUT_SUFFIX = "_after_active_source"


def _entry(technique: str, source: str) -> dict:
    return {"technique": technique, "source": source}


def defaults() -> dict:
    return {
        "wake": _entry(DEFAULT_WAKE, "default"),
        "sleep": _entry(DEFAULT_SLEEP, "default"),
        "release": _entry(DEFAULT_RELEASE, "default"),
        "claims_input": False,
        "hdmi_sleep": DEFAULT_HDMI_SLEEP,
        "television": {},
        "detection_complete": False,
        "detection_step_seconds": DEFAULT_DETECTION_STEP_SECONDS,
        "detection_cycle_seconds": DEFAULT_DETECTION_CYCLE_SECONDS,
        "tv_button_switches_off": False,
    }


def _from_legacy() -> "dict | None":
    """The techniques an older installation had settled on, if any."""
    try:
        with open(LEGACY_PATH, encoding="utf-8") as handle:
            old = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(old, dict):
        return None
    taken = defaults()
    for kind in ("wake", "sleep"):
        entry = old.get(kind)
        if isinstance(entry, dict) and entry.get("technique"):
            # "manual", not "detection": what those files hold was produced by
            # a mechanism that no longer exists, and half of it was wrong.
            taken[kind] = _entry(entry["technique"], "manual")
    logger.info("techniques taken over from %s: wake=%s sleep=%s", LEGACY_PATH,
                taken["wake"]["technique"], taken["sleep"]["technique"])
    return taken


def load() -> dict:
    """The configuration in force. Never raises: a box always has an answer."""
    try:
        with open(PATH, encoding="utf-8") as handle:
            stored = json.load(handle)
        if not isinstance(stored, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        stored = _from_legacy() or defaults()
    except (OSError, ValueError) as exc:
        logger.warning("%s is unreadable (%s); falling back to the defaults",
                       PATH, exc)
        stored = defaults()
    config = defaults()
    for kind in ("wake", "sleep", "release"):
        entry = stored.get(kind)
        if isinstance(entry, dict) and entry.get("technique"):
            config[kind] = _entry(entry["technique"],
                                  entry.get("source") if entry.get("source") in SOURCES
                                  else "manual")
    config["claims_input"] = _CLAIMS_INPUT_SUFFIX in config["sleep"]["technique"]
    if stored.get("hdmi_sleep") in HDMI_SLEEP_METHODS:
        config["hdmi_sleep"] = stored["hdmi_sleep"]
    elif stored.get("hdmi_sleep") is not None:
        logger.warning("unknown hdmi_sleep %r in %s; using %r",
                       stored["hdmi_sleep"], PATH, DEFAULT_HDMI_SLEEP)
    if isinstance(stored.get("television"), dict):
        config["television"] = stored["television"]
    config["detection_complete"] = bool(stored.get("detection_complete"))
    for setting, default in (("detection_step_seconds", DEFAULT_DETECTION_STEP_SECONDS),
                             ("detection_cycle_seconds", DEFAULT_DETECTION_CYCLE_SECONDS)):
        value = stored.get(setting)
        config[setting] = value if isinstance(value, (int, float)) and value > 0 else default
    config["tv_button_switches_off"] = bool(stored.get("tv_button_switches_off"))
    return config


def save(config: dict) -> bool:
    """Write the configuration. Only the installation mode and the API call this."""
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = f"{PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            # Fsync before the rename, and the directory after it: this box is
            # switched off by pulling its plug, and a configuration that only
            # exists in the page cache is one an unplug can take away.
            os.fsync(handle.fileno())
        os.replace(tmp, PATH)
        directory = os.open(os.path.dirname(PATH), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.chmod(PATH, 0o644)
        return True
    except OSError as exc:
        logger.warning("could not write %s: %s", PATH, exc)
        return False


def set_technique(kind: str, technique: str, source: str = "manual") -> dict:
    """Change one technique and keep everything else."""
    if kind not in ("wake", "sleep", "release"):
        raise ValueError(f"unknown kind {kind!r}")
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    config = load()
    config[kind] = _entry(technique, source)
    if kind == "sleep":
        config["claims_input"] = _CLAIMS_INPUT_SUFFIX in technique
    save(config)
    return config


def set_detection_complete(complete: bool) -> dict:
    """Flip the flag the summary page sets once it is accepted.

    False on every progressive write during the procedure, true only once:
    an interrupted session must never look like a finished one.
    """
    config = load()
    config["detection_complete"] = bool(complete)
    save(config)
    return config


def set_detection_seconds(step_seconds: "float | None" = None,
                          cycle_seconds: "float | None" = None) -> dict:
    """Change how long the detection procedure waits, for a slow television.

    A property of the set in front of the box, not of the product, so it
    lives here and the summary page writes it back with everything else.
    """
    config = load()
    if step_seconds is not None:
        config["detection_step_seconds"] = step_seconds
    if cycle_seconds is not None:
        config["detection_cycle_seconds"] = cycle_seconds
    save(config)
    return config


def set_hdmi_sleep(method: str) -> dict:
    """Choose how the box stops driving the screen."""
    if method not in HDMI_SLEEP_METHODS:
        raise ValueError(f"unknown hdmi_sleep {method!r}; one of {HDMI_SLEEP_METHODS}")
    config = load()
    config["hdmi_sleep"] = method
    save(config)
    return config


def set_tv_button_switches_off(enabled: bool) -> dict:
    """Choose what the tv button does while leaving music (or any other
    mode): give the person their programmes back (the default, `False` —
    see `modes.py::_give_back_the_programmes()`), or simply switch off,
    the same as the music button already does.

    A product decision, not a fallback: `False` is not "worse" than
    `True`, it is a different, deliberate trade for whoever will actually
    press the button — see `modes.py::tv_button()`'s own docstring for the
    reasoning either way.
    """
    config = load()
    config["tv_button_switches_off"] = bool(enabled)
    save(config)
    return config


def set_television(identity: dict) -> dict:
    """Record which set this configuration was made for."""
    config = load()
    config["television"] = identity
    save(config)
    return config


def clear() -> dict:
    """Forget what this box knows about the television: wake, sleep and
    release techniques back to their defaults, the recorded television
    gone, detection_complete cleared — the same state as a box nobody has
    ever run the detection on.

    Unlike `button_bindings.clear()`, this alone does **not** force the
    installation screen open at the next start: whether that screen opens
    is governed entirely by `button_bindings.has_any_button()`
    (`main.py`'s `_open_the_screen_at_startup()`) — a box with working
    buttons that has just forgotten its television goes on sitting quietly
    in television mode, using the defaults, until somebody deliberately
    opens "Set up the TV (CEC)" from the menu. A television that already
    answers to the standard frames should not be forced onto a
    configuration screen on every boot just because nobody has explicitly
    confirmed it.

    `hdmi_sleep`, `tv_button_switches_off` and the detection timings are
    left alone: independent preferences about this specific board,
    television and the person using it, not part of what "forget the TV"
    means, and a box that lost a deliberately-tuned choice along with its
    CEC techniques would be a worse surprise than keeping it.
    """
    config = load()
    fresh = defaults()
    for kind in ("wake", "sleep", "release", "claims_input",
                "television", "detection_complete"):
        config[kind] = fresh[kind]
    save(config)
    return config
