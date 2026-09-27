# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""How much the CEC transport says, changed without restarting anything.

Its own small file, the same shape as `tv_config.py` and `media_config.py`:
write/fsync/rename, and it never raises — a box always has an answer.

**Why this is a setting and not an environment variable.** Turning the CEC
transport up is what you do when a television is behaving oddly, and restarting
the API to do it destroys the evidence: the box loses the CEC session's state,
re-reads the configuration, and pokes the set on the way through. The one moment
somebody wants more detail is the one moment they cannot afford a restart. So it
is applied to the live logger and written down, and a box that reboots comes
back as verbose as it was left.

**Only the CEC logger, deliberately.** Raising the root logger to DEBUG turns on
every library in the process — paho's MQTT chatter above all — on a board whose
journal is capped at 200 MB and whose card is the only place logs live. The
whole-process level stays where `MAMAN_LOG_LEVEL` puts it.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

PATH = os.environ.get(
    "MAMAN_LOG_CONFIG",
    os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/maman-tv-lite"),
                 "logging.json"))

# The name of the logger this setting moves. `cec_controller` logs one line per
# frame at INFO and everything cec-ctl printed at DEBUG.
CEC_LOGGER = "cec_controller"

# What a box comes up with when nothing has ever been set. An environment
# variable rather than a constant so an installation can start verbose — a box
# being set up for the first time is exactly one somebody is watching.
DEFAULT_CEC_LEVEL = os.environ.get("MAMAN_CEC_LOG_LEVEL", "INFO").upper()

# Deliberately not the whole of logging's vocabulary: NOTSET would mean "ask the
# root logger", which reads as a level and behaves like a question, and CRITICAL
# on a diagnostic logger is indistinguishable from silence.
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def defaults() -> dict:
    level = DEFAULT_CEC_LEVEL if DEFAULT_CEC_LEVEL in LEVELS else "INFO"
    return {"cec": level}


def load() -> dict:
    """The configuration in force. Never raises."""
    try:
        with open(PATH, encoding="utf-8") as handle:
            stored = json.load(handle)
        if not isinstance(stored, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        return defaults()
    except (OSError, ValueError) as exc:
        logger.warning("%s is unreadable (%s); falling back to the defaults",
                       PATH, exc)
        return defaults()
    config = defaults()
    if str(stored.get("cec", "")).upper() in LEVELS:
        config["cec"] = str(stored["cec"]).upper()
    return config


def save(config: dict) -> bool:
    """Write, fsync, rename — this box is switched off by pulling its plug."""
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = f"{PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
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


def apply(config: "dict | None" = None) -> dict:
    """Put the configuration on the live loggers.

    Called at start and after every change, which is what makes the setting take
    effect without a restart. Only the logger's own level is set: the handler
    `logging.basicConfig` installed has no level of its own, so a DEBUG record
    from a logger that allows it is printed even with the root at INFO.
    """
    config = config or load()
    logging.getLogger(CEC_LOGGER).setLevel(config["cec"])
    return config


def set_cec_level(level: str) -> dict:
    """Change how much the CEC transport says, at once and for good."""
    wanted = str(level).upper()
    if wanted not in LEVELS:
        raise ValueError(f"unknown level {level!r}; one of {LEVELS}")
    config = load()
    config["cec"] = wanted
    save(config)
    applied = apply(config)
    logger.info("the CEC transport now logs at %s", wanted)
    return applied
