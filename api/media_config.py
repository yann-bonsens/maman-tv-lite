# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""How loud the music mode plays, independent of the television's own
volume — deliberately so. CEC has no absolute volume at all, only step up
and step down (see docs/development/installation-screen.md's own "out of scope" note),
so this setting was never going to be that anyway: it is mpg123's own gain,
applied in software before the signal ever reaches the television, over
its remote-control protocol's `VOLUME <percent>` command (confirmed against
the real binary: `VOLUME 50` is echoed back as `@V 50.000000%`).

Same shape as `tv_config.py` — its own small file, write/fsync/rename,
never raises — kept separate from it because this has nothing to do with
the television.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

PATH = os.environ.get(
    "MAMAN_MEDIA_CONFIG",
    os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/maman-tv-lite"), "media.json"))

# mpg123's own default when nothing is said: the track's own level,
# unscaled. Kept as the default here too, rather than inventing a number,
# so a box that has never touched this setting sounds exactly as it
# always did.
DEFAULT_VOLUME = 100

MIN_VOLUME = 0
MAX_VOLUME = 100


def defaults() -> dict:
    return {"volume": DEFAULT_VOLUME}


def load() -> dict:
    """The configuration in force. Never raises: a box always has an answer."""
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
    volume = stored.get("volume")
    if isinstance(volume, (int, float)) and MIN_VOLUME <= volume <= MAX_VOLUME:
        config["volume"] = volume
    return config


def save(config: dict) -> bool:
    """Write the configuration. Write, fsync, rename — the same discipline
    `tv_config.py` uses, for the same reason: this box is switched off by
    pulling its plug."""
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


def set_volume(percent: float) -> dict:
    """Change the music mode's own volume and persist it. Applying it to a
    player that may already be running is `media.py`'s job (it is the only
    place that talks to mpg123), not this module's — this one only knows
    the number, not the player."""
    if not (MIN_VOLUME <= percent <= MAX_VOLUME):
        raise ValueError(f"volume must be between {MIN_VOLUME} and {MAX_VOLUME}")
    config = load()
    config["volume"] = percent
    save(config)
    return config
