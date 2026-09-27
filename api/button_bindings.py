# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The button-binding file itself: which device sends which command name,
with no idea what a command name actually does.

Split out of `zigbee_bridge.py` so that other modules can read it without
depending on `zigbee_bridge.py`'s own `COMMANDS` dispatch table, which needs
`modes` — and `modes.py` needs `installation.py`, so `installation.py`
reading bindings through `zigbee_bridge.py` directly would be a cycle. This
module imports neither `modes` nor `installation` nor `zigbee_bridge`, so
both of those can import it safely.

`scripts/setup-zigbee.py` writes this file; nothing here executes what it
names — only `zigbee_bridge.COMMANDS` resolves a command name to a function,
and only for names it already knows.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

# Where the bindings live. Outside the repository, like the other machine
# specific configuration, so that updating the code never overwrites which
# button does what.
BINDINGS_PATH = os.environ.get("MAMAN_BUTTONS_CONFIG", "/etc/maman-tv-lite/buttons.json")

# The installer's saved answers. Read for one line only — whether the owner
# chose the buttons at all — and never written from here.
INSTALL_CONF_PATH = os.environ.get("MAMAN_INSTALL_CONF", "/etc/maman-tv-lite/install.conf")

# Used when no binding file exists yet. "*" matches any device, so a freshly
# paired button that publishes the most common action already works the
# television — which is what somebody pairing a button is trying to check.
ANY_DEVICE = "*"
DEFAULT_BINDINGS: "dict[str, dict[str, str]]" = {ANY_DEVICE: {"single": "tv"}}


def load_bindings(path: "str | None" = None) -> "dict[str, dict[str, str]]":
    """Read the binding file, falling back to the built-in default.

    Never raises. A missing file is the normal state before the setup script
    has run; a malformed one is worth an explicit log line, but it must not
    stop the API from starting, because the HTTP routes still work without
    buttons.

    Only checks the file's own shape — device -> {action: command name} —
    and not whether a command name is one `zigbee_bridge.COMMANDS` actually
    knows: that whitelist lives with the dispatch table, not with the data.
    """
    path = path or BINDINGS_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        logger.info("No binding file at %s, using the default mapping", path)
        return dict(DEFAULT_BINDINGS)
    except (OSError, ValueError):
        logger.exception("Unreadable binding file %s, using the default mapping", path)
        return dict(DEFAULT_BINDINGS)

    bindings: "dict[str, dict[str, str]]" = {}
    for device, actions in (raw.get("bindings") or {}).items():
        if not isinstance(actions, dict):
            logger.warning("Binding for %r ignored: expected an action mapping", device)
            continue
        for action, command in actions.items():
            if not isinstance(command, str) or not command:
                logger.warning("Binding %s/%s ignored: not a command name", device, action)
                continue
            bindings.setdefault(device, {})[action] = command

    if not bindings:
        logger.warning("Binding file %s has no usable entry, using the default", path)
        return dict(DEFAULT_BINDINGS)

    logger.info("Loaded %d binding(s) from %s", sum(len(a) for a in bindings.values()), path)
    return bindings


BINDINGS = load_bindings()


def reload(path: "str | None" = None) -> None:
    """Re-read the binding file and update `BINDINGS` in place.

    What lets a button-pairing procedure (`button_pairing.py`, driven from
    `modes.py`) take effect immediately, without restarting the API.

    Mutates the existing dict (`clear()` + `update()`) rather than rebinding
    the module-level name to a fresh one — confirmed on real hardware to
    matter: `zigbee_bridge.py` imports the name directly
    (`from button_bindings import BINDINGS`), which captures the dict
    *object* at import time. Reassigning `button_bindings.BINDINGS = ...`
    afterwards only changes what this module's own name points to; the copy
    `zigbee_bridge.py` already holds keeps referring to the original,
    now-stale object, so a real pairing "worked" — the file was right, the
    screen showed the right name — while the button itself stayed
    unrecognised until the process was restarted. Mutating the same object
    everyone already holds a reference to is what actually reaches every
    importer.
    """
    fresh = load_bindings(path)
    BINDINGS.clear()
    BINDINGS.update(fresh)


def read_raw(path: "str | None" = None) -> dict:
    """The binding file's own JSON, unfiltered — unlike `load_bindings()`,
    which only keeps what the command dispatch table understands. Used only
    by the write path below, so a key `load_bindings()` does not recognise
    (an installation may carry a "colors" block, for instance) survives a
    rewrite instead of being silently dropped."""
    path = path or BINDINGS_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def write_bindings(document: dict, path: "str | None" = None) -> None:
    """Write the whole document: write, fsync, rename, then fsync the
    directory — the same discipline `tv_config.save()` applies, and for the
    same reason: this box is switched off by pulling its plug, and a rename
    that only exists in the page cache is one an unplug takes away. A lost
    buttons.json is a box whose buttons do nothing at all, which is the whole
    interface.

    Explicitly chmod 664 rather than trusting the process umask: the
    directory is setgid (see scripts/install.sh) so a new file inherits the
    right *group*, but setgid says nothing about the *mode* bits, which
    still need the group-write bit set for `scripts/setup-zigbee.py` (a
    different account, run by hand) and this process to both be able to
    write it later.
    """
    path = path or BINDINGS_PATH
    body = json.dumps(document, indent=2, sort_keys=True) + "\n"
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o664)
    os.replace(tmp, path)
    directory = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def set_binding(command: str, device: str, actions: "list[str]",
                path: "str | None" = None) -> None:
    """Bind every action in `actions` on `device` to `command`, and remove
    that same command from any OTHER device first.

    A role like "tv" belongs to at most one button. Without this, re-pairing
    a role to a different physical button (choosing "start over" partway
    through, say) left the old one still bound too — and, since
    `device_for_command()` returns whichever bound device it finds first,
    still the one printed on screen. The same "verify, don't leave a stale
    entry behind" reasoning as `scripts/setup-zigbee.py`'s rename handling,
    generalised from a renamed identity to a replaced device.
    """
    path = path or BINDINGS_PATH
    document = read_raw(path)
    bindings = document.setdefault("bindings", {})
    for other_device, entry in list(bindings.items()):
        if other_device == device or not isinstance(entry, dict):
            continue
        for action in list(entry):
            if entry[action] == command:
                del entry[action]
        if not entry:
            del bindings[other_device]
    entry = bindings.setdefault(device, {})
    for action in actions:
        entry[action] = command
    write_bindings(document, path)


def has_any_button() -> bool:
    """Whether either everyday role — "tv" or "music" — is actually bound to
    a real device. False on a fresh box (the catch-all `"*"` default does
    not count) and right after `clear()`.

    A module-level function rather than something private to
    `modes._PairingHardware`, because two independent callers need the exact
    same answer: `main.py`'s boot logic (whether to force the installation
    screen open) and `installation.py`'s procedure dispatch (whether a box
    can be navigated with a button at all). One check, not two that could
    drift apart.
    """
    return bool(device_for_command("tv") or device_for_command("music"))


def buttons_installed(path: "str | None" = None) -> bool:
    """Whether the owner chose the Zigbee buttons when installing the box.

    A box installed `--without-zigbee` used to open the installation screen
    on every start, because it had no button — and that screen begins by
    pairing one, waiting twenty minutes for an adapter that was never going
    to be there. Without buttons the box is driven from a phone, and the
    television is configured through the API (docs/without-buttons.md).

    False only on an explicit `MAMAN_ZIGBEE=no`. A missing or unreadable file
    — a development machine, a box older than the file — keeps the behaviour
    a box with buttons has, which is what every box had before this question
    was asked.
    """
    path = path or INSTALL_CONF_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.strip().partition("=")
                if key == "MAMAN_ZIGBEE":
                    return value.strip() != "no"
    except OSError:
        pass
    return True


def clear(path: "str | None" = None) -> None:
    """Forget every button binding. Empties "bindings" rather than deleting
    the file or replacing it outright — `load_bindings()` already falls
    back to `DEFAULT_BINDINGS` on an empty mapping — while preserving
    whatever else the document carries (a "colors" block, say), the same
    read_raw()-first discipline `set_binding()` already uses. Reloads
    immediately, so a box in this state is reachable again by the
    no-button-bound bootstrap without anyone touching hardware."""
    document = read_raw(path)
    document["bindings"] = {}
    write_bindings(document, path)
    reload(path)


def device_for_command(command: str, bindings: "dict[str, dict[str, str]] | None" = None) -> "str | None":
    """The first real device name (never the "*" catch-all) bound to this
    command, on any action — what a page uses to print a button's name.

    Reads the module-level `BINDINGS` at call time when `bindings` is not
    given, the same reasoning `page_render.save`'s `path` argument follows:
    a default argument is bound once, at import time, and a test that
    monkeypatches `button_bindings.BINDINGS` would not be seen by it.
    """
    if bindings is None:
        bindings = BINDINGS
    for device, actions in bindings.items():
        if device == ANY_DEVICE:
            continue
        if command in actions.values():
            return device
    return None
