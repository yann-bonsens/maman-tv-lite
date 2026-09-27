# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The installation screen: a menu of guided procedures, drawn on the
television. Two entries today: the CEC detection procedure
(`cec_detection.py`) and pairing the TV/Music buttons (`button_pairing.py`)
— a menu, not a single jump-in, because this screen is meant to grow more
procedures later (a camera step...) and a later one should cost a registry
entry, not a new mechanism.

This module owns the question/answer channel and the page drawing, and
builds each procedure's own `Environment` by adding `ask`/`abandoned`
(channel-backed) on top of a `Hardware` implementation `modes.py` supplies
— `modes.py` is the only place allowed to touch `cec_controller`/
`hdmi_output`/`zigbee_pairing`/`button_bindings` directly (see
`cec_detection.py`'s and `button_pairing.py`'s own docstrings), so this
module never imports any of them.

Driven only over HTTP for now (`GET /installation`, `POST
/installation/answer`, both in `main.py`) — no Zigbee buttons yet, except
that once a button-pairing procedure has bound one, `zigbee_bridge.py`
routes its presses to the same channel. The channel itself does not know or
care which one answers it.
"""

import logging
from typing import Callable, Optional, Protocol

import answer_channel
import button_bindings
import button_pairing
import cec_detection
import page_render
import procedure_support
import screen
import tv_config

logger = logging.getLogger(__name__)

channel = answer_channel.Channel()

PROCEDURES: "tuple[tuple[str, str], ...]" = (
    ("cec", "Set up the TV (CEC)"),
    ("buttons", "Pair the buttons"),
)


class Hardware(Protocol):
    """Everything `cec_detection.Environment` needs except the questions
    themselves — the part that actually touches CEC and the box's output,
    supplied by `modes.py`."""

    def step_seconds(self) -> float: ...
    def cycle_seconds(self) -> float: ...
    def wake_techniques(self) -> "tuple[str, ...]": ...
    def sleep_techniques(self) -> "tuple[str, ...]": ...
    def release_techniques(self) -> "tuple[str, ...]": ...
    def send_wake(self, technique: str) -> None: ...
    def send_sleep(self, technique: str) -> None: ...
    def send_release(self, technique: str) -> None: ...
    def claim_input(self) -> None: ...
    def begin_release_power_cycle(self) -> None: ...
    def end_release_power_cycle(self) -> None: ...
    def television_identity(self) -> dict: ...
    def save_wake(self, technique: str) -> None: ...
    def save_sleep(self, technique: str) -> None: ...
    def save_release(self, technique: str) -> None: ...
    def save_television(self, identity: dict) -> None: ...
    def finish(self) -> None: ...


class PairingHardware(Protocol):
    """Everything `button_pairing.Environment` needs except the questions
    themselves — the part that actually touches Zigbee2MQTT and the binding
    file, supplied by `modes.py`."""

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
    def list_adapters(self) -> "list[str]": ...
    def bring_bridge_up(self, port: str) -> bool: ...
    def stop_bridge(self) -> None: ...


# Shown when no button is bound to that role yet — an HTTP-only session, or
# a box `setup-zigbee.py` hasn't run on. A real button's name (whatever
# colour it was given at pairing) always wins over this.
_FALLBACK_LABEL = {"tv": "YES", "music": "NO"}


def _button_label(token: str) -> str:
    name = button_bindings.device_for_command(token)
    return name.upper() if name else _FALLBACK_LABEL.get(token, token.upper())


# Navigate/negative always on the left, validate/positive always on the
# right — one fixed order, not whatever a procedure happened to build its
# accepts dict in. Different pages disagreed before this existed: the menu
# already put "next line" first and "choose" second, but the interrupted-
# setup prompt was built the opposite way round. Enforced here, the one
# place every page's legend already passes
# through, rather than in page_render.py — which deliberately has no idea
# what "tv"/"music" mean any more (see its own docstring), and shouldn't
# need to.
_LEGEND_ORDER = (cec_detection.NO, cec_detection.YES)


def _legend(accepts: "dict[str, str]") -> "dict[str, str]":
    """Turn {token: meaning} into {token: "BUTTON NAME : meaning"} — the
    button's own configured name (see scripts/setup-zigbee.py), not a
    colour cec_detection.py has no way of knowing. Reordered to
    `_LEGEND_ORDER`; a token not in it (none exist today) keeps its
    original relative order, appended after the ones that are."""
    ordered = [t for t in _LEGEND_ORDER if t in accepts]
    ordered += [t for t in accepts if t not in _LEGEND_ORDER]
    return {token: f"{_button_label(token)} : {accepts[token]}" for token in ordered}


def _draw_question(question: "answer_channel.Question") -> None:
    """Draw and show a page. Never raises: a drawing hiccup must not abort
    the whole procedure — the person can still answer blind if they know
    what was asked, which is worse than a lost frame but not a lost
    session."""
    try:
        spec = page_render.PageSpec(body=question.body, legend=_legend(question.accepts),
                                    title=question.title, step=question.step)
        page_render.save(page_render.draw(spec))
        screen.page()
    except Exception:
        logger.exception("installation: could not draw the page for question %d",
                         question.id)


class _ProductionEnvironment:
    """Adds the question/answer channel to a `Hardware`, producing a full
    procedure `Environment`. Everything besides `ask`/`abandoned` is
    forwarded straight through to the hardware object.

    `hardware` is optional: the menu (`_run_menu`) only ever calls
    `ask`/`abandoned`, never a hardware-specific method, so it is built
    without one — which procedure's hardware to build is not known until
    the menu says which was chosen.
    """

    def __init__(self, hardware=None) -> None:
        self._hardware = hardware

    def ask(self, body: str, accepts: "dict[str, str]",
           seconds: "Optional[float]" = None,
           title: "Optional[str]" = None,
           step: "Optional[tuple[int, int]]" = None) -> "Optional[str]":
        return channel.ask(body, accepts, seconds, publish=_draw_question,
                           title=title, step=step)

    def abandoned(self) -> bool:
        return channel.is_abandoned()

    def __getattr__(self, name):
        if self._hardware is None:
            raise AttributeError(name)
        return getattr(self._hardware, name)


def _menu_body(cursor: int) -> str:
    # ">" rather than a Unicode arrow: guaranteed to be in any font, where a
    # glyph DejaVu happens not to carry would draw as a tofu box instead —
    # and misalign the line next to it, since a missing-glyph box is usually
    # much wider than the character it stands in for.
    lines = ["INSTALLATION"]
    for index, (_, label) in enumerate(PROCEDURES):
        marker = ">" if index == cursor else " "
        lines.append(f"{marker} {label}")
    marker = ">" if cursor == len(PROCEDURES) else " "
    lines.append(f"{marker} Quit")
    return "\n".join(lines)


def _run_menu(env: _ProductionEnvironment) -> "Optional[str]":
    """The one-entry-today menu. Returns the chosen procedure's key, or None
    for "Quit" or for the mode having been left."""
    cursor = 0
    entries = len(PROCEDURES) + 1  # +1 for "Quit"
    while True:
        answer = env.ask(_menu_body(cursor),
                         {cec_detection.NO: "next line", cec_detection.YES: "choose"},
                         seconds=None)
        if env.abandoned():
            return None
        if answer == cec_detection.NO:
            cursor = (cursor + 1) % entries
            continue
        return PROCEDURES[cursor][0] if cursor < len(PROCEDURES) else None


def start(hardware_for: "dict[str, Callable[[], object]]") -> None:
    """Run the installation screen until a procedure ends or the mode is
    left. `hardware_for` maps each `PROCEDURES` key to a zero-argument
    factory for that procedure's own `Hardware`/`PairingHardware` object.
    Building one costs nothing by itself (no connection opens until a
    method on it is actually called), but the menu (`_run_menu`) needs
    none at all, so it is only ever built once a procedure is actually
    about to run.

    Never raises: a crash here must not take the API process with it, it
    must just leave the screen wherever it was and let `modes.py` put the
    box back to television.
    """
    channel.reset()
    menu_env = _ProductionEnvironment()
    try:
        pairing_hardware = hardware_for["buttons"]()
        if not pairing_hardware.has_any_button():
            # The menu itself needs a working button to navigate — pointless
            # to show it, or the CEC procedure, to a box that has none. This
            # is the one thing that must happen before anything else,
            # including an interrupted CEC session: a box with no button at
            # all cannot be reached any other way (short of a phone), so
            # getting one working outranks resuming anything.
            #
            # run_bootstrap()'s own return value, not menu_env.abandoned()
            # alone: a genuine give-up (its ~20-minute deadline ran out, no
            # adapter ever answered) is not an abandonment — nobody left the
            # mode — but there is still no button to show a menu to, so this
            # must stop here exactly the same way.
            if not button_pairing.run_bootstrap(_ProductionEnvironment(pairing_hardware)):
                return

        # A loop, not a single pass: finishing either procedure returns here
        # to the menu rather than leaving the mode — the menu is the home
        # page of the whole screen, and only "Quit" (or the mode being left
        # from outside, which _run_menu already reports as None the same
        # way) actually closes it. Re-checked on every pass rather than
        # once: it is only ever true right after a session that really was
        # interrupted, and false again as soon as one procedure completes.
        while True:
            config = tv_config.load()
            interrupted = (not config["detection_complete"]
                          and (config["wake"]["source"] == "detection"
                               or config["sleep"]["source"] == "detection"))
            if interrupted:
                # "Start over" or "quit", not "resume" or "start over": the
                # procedure has no step-level resume — every step re-measures
                # its technique from a live human — so both halves of the old
                # question ran exactly the same code from step 1. Offering a
                # choice that changes nothing is worse than offering one
                # option, and the answer was not even read.
                answer = menu_env.ask(
                    "A TV setup was interrupted.\n\n"
                    "Answer below: start it over, or leave this screen.",
                    {cec_detection.YES: "start over", cec_detection.NO: "quit"},
                    seconds=None)
                if menu_env.abandoned() or answer != cec_detection.YES:
                    return
                cec_detection.run(_ProductionEnvironment(hardware_for["cec"]()))
                continue

            choice = _run_menu(menu_env)
            if choice is None:
                return
            if choice == "cec":
                cec_detection.run(_ProductionEnvironment(hardware_for["cec"]()))
            elif choice == "buttons":
                button_pairing.run(_ProductionEnvironment(hardware_for["buttons"]()))
    except procedure_support.Abandoned as exc:
        logger.info("installation: %s", exc)
    except Exception:
        logger.exception("installation: the procedure stopped unexpectedly")
