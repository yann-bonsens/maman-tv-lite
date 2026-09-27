# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The guided button-pairing procedure: how a box learns which button is
which, with a human watching the screen instead of an SSH session.

Pure logic, the same shape as `cec_detection.py`: this module never imports
`zigbee_pairing` or `button_bindings` directly, only the `Environment`
protocol it is handed, so it can be tested against a fake bridge instead of
real hardware. `modes.py` is the only place that builds the real
environment.

The screen has no keyboard, so this is a deliberately narrower thing than
`scripts/setup-zigbee.py`: exactly two fixed roles, TV and Music, named
outright rather than typed. `run_bootstrap()` also finds the Zigbee adapter
and brings the bridge up itself when it is not already running — nobody
should need `scripts/setup-zigbee.py` or an SSH session just to get a fresh
box's two buttons working; that script remains the full-power tool for
anything past those two (a third button bound to `diagnostic`, say).

For the very first button ever paired on a box with nothing bound yet,
there is by definition no working button to answer this procedure's own
questions with — the person answers from a phone instead (`POST
/installation/answer`), which the channel already supports identically to a
button press. Once the TV button is bound, it can answer the rest,
including pairing Music.
"""

import logging
import time
from typing import Optional, Protocol

from cec_detection import NO, YES
from procedure_support import _Asker, _check_abandoned

logger = logging.getLogger(__name__)

ROLES: "tuple[tuple[str, str], ...]" = (("tv", "TV"), ("music", "Music"))


class Environment(Protocol):
    """What the procedure needs, and nothing more. Production wraps
    `zigbee_pairing` and `button_bindings` (via `modes.py`); tests wrap a
    fake bridge."""

    def ask(self, body: str, accepts: "dict[str, str]",
           seconds: "float | None" = None) -> "Optional[str]": ...
    def abandoned(self) -> bool: ...
    def step_seconds(self) -> float: ...

    def bridge_ready(self) -> bool: ...
    def has_any_button(self) -> bool: ...
    def open_pairing(self) -> bool: ...
    def close_pairing(self) -> None: ...
    def capture_press(self) -> "tuple[str, str] | None": ...
    def rename_device(self, old: str, new: str) -> bool: ...
    def device_actions(self, device: str) -> "list[str]": ...
    def write_binding(self, role: str, device: str, actions: "list[str]") -> None: ...
    def reload_bindings(self) -> None: ...

    # The bootstrap alone: bringing the bridge up when nothing answers yet.
    def list_adapters(self) -> "list[str]": ...
    def bring_bridge_up(self, port: str) -> bool: ...
    def stop_bridge(self) -> None: ...


class Result:
    """What the procedure produced, for the summary page and the caller."""

    def __init__(self) -> None:
        self.paired: "dict[str, str]" = {}  # role -> the button's final name
        self.completed = False


def _rename_and_bind(env: Environment, role: str, label: str,
                     pressed: "tuple[str, str]") -> str:
    """Common to both flows below: turn one captured press into a named,
    bound button. Returns the button's final name (its own Zigbee2MQTT
    name on a failed rename, matching the standalone script's own
    fallback)."""
    device, action = pressed
    final_name = label if env.rename_device(device, label) else device
    actions = env.device_actions(final_name) or [action]
    env.write_binding(role, final_name, actions)
    return final_name


def _pair_one_role(env: Environment, asker: _Asker, step: int, role: str,
                   label: str, announce: str) -> "str | None":
    """Pair and name one role. Returns the button's final name, or None if
    it was skipped. `announce` is folded into the ready page rather than
    shown on a page of its own, the same "found: X" pattern step 2 of the
    CEC procedure already uses — one fewer press to read the same thing."""
    retry = False
    while True:
        bootstrap_note = (
            "\n\nNo button answers yet: answer this one from a phone "
            "instead (open the API's /docs page, POST to "
            "/installation/answer)." if not env.has_any_button() else "")
        asker.ask(
            f"{announce}STEP {step} OF {len(ROLES)} — "
            f"THE {label.upper()} BUTTON{' (again)' if retry else ''}\n\n"
            f"Hold the button you want for {label} for about 5 seconds, "
            "until its light blinks fast, to put it in pairing mode. "
            "Already paired? Just press it once.\n\n"
            f"When ready, answer below.{bootstrap_note}",
            {YES: "ready"}, seconds=None)
        env.open_pairing()
        pressed = env.capture_press()
        env.close_pairing()
        _check_abandoned(env)

        if pressed is not None:
            return _rename_and_bind(env, role, label, pressed)

        again = asker.ask(
            f"E-PAIR — NO PRESS SEEN FOR {label.upper()}\n\n"
            "Check the button's battery and that it was held long enough "
            "to blink, then try again — or go on without this button for "
            "now; it can be paired later by running this again.",
            {YES: "try again", NO: "go on without it"}, seconds=None)
        if again != YES:
            return None
        retry = True


# Real reading time between roles in the silent flow, not a fold-into-the-
# next-page trick: nothing there needs a press to advance anyway, so there
# is no press to save by folding.
BOOTSTRAP_PAUSE_SECONDS = 3

# How long the whole bootstrap — finding the adapter, bringing the bridge
# up, and waiting for both buttons — is given before giving up altogether
# and stopping the bridge. Generous on purpose: a person may need to go
# find the dongle, or come back with batteries, and nothing about this
# screen should feel rushed (see docs/development/installation-screen.md's own "give
# reading time" principle). But not infinite: a bridge left running
# against an adapter Zigbee2MQTT can never quite talk to is exactly the
# crash-loop CLAUDE.md measured at ~50s of CPU per attempt, hundreds of
# attempts, two hardware-watchdog reboots — silence for this long is
# nobody coming back, not somebody still reading.
GIVE_UP_SECONDS = 20 * 60

# How often to check again for an adapter once none has been found — a
# silent poll, the same env.ask(body, {}, seconds=N) trick used everywhere
# else in this module for "wait, then look again" with nothing to press.
ADAPTER_POLL_SECONDS = 20


def _give_up(env: Environment) -> None:
    env.stop_bridge()
    env.ask(
        "NO RESPONSE — ZIGBEE TURNED OFF\n\n"
        "Nothing happened for a while, so the Zigbee adapter has been "
        "turned off to avoid wearing the box out.\n\n"
        "Restart the box, or open the installation screen again, to try "
        "once more.",
        {}, seconds=0)


def _bring_up_the_bridge(env: Environment, deadline: float) -> bool:
    """Find the adapter and start Zigbee2MQTT — silently retrying, the
    same reasoning as everywhere else in this flow: nothing here asks
    whether to keep trying, because giving up early would leave the box
    exactly as unusable as it started. Shows which adapter was found, or
    that none was, since that is the one thing this step needs to tell the
    person without asking them anything.

    Returns True once the bridge is up, False if `deadline` ran out or the
    mode was left first — either way, `_give_up()` has already been called
    or the mode is already gone, so the caller just needs to stop.
    """
    while True:
        if env.abandoned():
            return False
        if time.monotonic() >= deadline:
            _give_up(env)
            return False
        adapters = env.list_adapters()
        if not adapters:
            env.ask(
                "NO ZIGBEE ADAPTER FOUND\n\n"
                "Plug a Zigbee USB adapter into the box. I'll keep "
                "checking.",
                {}, seconds=ADAPTER_POLL_SECONDS)
            continue
        adapter = adapters[0]
        # The page shows the readable end of it, not the whole
        # /dev/serial/by-id path — the box is being read from an armchair.
        # `adapter` itself stays the full path, which is what Zigbee2MQTT
        # has to be given.
        env.ask(
            f"ZIGBEE ADAPTER FOUND: {adapter.rsplit('/', 1)[-1]}\n\n"
            "Starting it up — this can take a few minutes.",
            {}, seconds=0)
        if env.bring_bridge_up(adapter):
            return True
        # Present but Zigbee2MQTT still could not talk to it, even after
        # trying every known chipset (see zigbee_pairing.bring_bridge_up()) —
        # loop back and try the whole thing again rather than giving up on
        # this one attempt; a re-seated cable or a moment for a flaky
        # adapter to settle is enough, sometimes.


def run_bootstrap(env: Environment) -> bool:
    """The one way in when nothing is bound yet — no button exists to
    answer a question with, so this asks none. Brings the Zigbee bridge up
    if it is not already, then pairs TV and Music, silently retrying a
    role that saw no press rather than ever asking whether to give up —
    except for the one, generous `GIVE_UP_SECONDS` deadline covering the
    whole thing, past which continuing to try would just be burning the
    box's one core for nobody.

    Each instruction page is drawn with `env.ask(body, {}, seconds=...)`:
    an empty `accepts` means nothing could ever answer it, so it publishes
    the page (through the same channel every other page uses) and returns
    at once, or after the given pause — not a new drawing mechanism, just
    the existing one used for a page nobody needs to press anything on.

    Returns True once both roles are actually paired, False on a genuine
    give-up (the deadline ran out) or the mode being left mid-wait — never
    raises. The return value matters to the caller: with nothing bound,
    there is no menu to fall through to, so `installation.start()` must
    stop rather than try to show one a person still has no button to
    navigate it with.
    """
    deadline = time.monotonic() + GIVE_UP_SECONDS
    if not env.bridge_ready():
        if not _bring_up_the_bridge(env, deadline):
            return False

    for step, (role, label) in enumerate(ROLES, start=1):
        env.ask(
            f"STEP {step} OF {len(ROLES)} — PAIRING THE {label.upper()} BUTTON\n\n"
            f"Hold the button you want for {label} for about 5 seconds, "
            "until its light blinks fast, to put it in pairing mode. "
            "Already paired? Just press it once.",
            {}, seconds=0)
        pressed = None
        while pressed is None:
            if env.abandoned():
                return False
            if time.monotonic() >= deadline:
                _give_up(env)
                return False
            env.open_pairing()
            pressed = env.capture_press()
            env.close_pairing()
        final_name = _rename_and_bind(env, role, label, pressed)
        env.ask(f"Paired as {final_name}.", {}, seconds=BOOTSTRAP_PAUSE_SECONDS)
    env.reload_bindings()
    return True


def run(env: Environment) -> Result:
    result = Result()
    asker = _Asker(env)

    if not env.bridge_ready():
        env.ask(
            "E-ADAPTER — NO ZIGBEE BRIDGE\n\n"
            "The Zigbee adapter has to be set up first, over SSH:\n"
            "./scripts/setup-zigbee.py",
            {YES: "ok"}, seconds=None)
        return result

    announce = ""
    for step, (role, label) in enumerate(ROLES, start=1):
        name = _pair_one_role(env, asker, step, role, label, announce)
        if name is not None:
            result.paired[role] = name
            announce = f"Paired as {name}.\n\n"
        else:
            announce = ""

    if result.paired:
        env.reload_bindings()

    summary = (
        "DONE\n\n"
        f"TV button: {result.paired.get('tv') or '(not paired)'}\n"
        f"Music button: {result.paired.get('music') or '(not paired)'}\n"
    )
    accept = asker.ask(summary, {YES: "save", NO: "start over"}, seconds=None)
    if accept == NO:
        return run(env)
    result.completed = True
    return result
