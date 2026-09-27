# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""What the box is doing, and the rules for changing it.

One default state and three modes:

- **television** — the box is not using the screen. The set shows its own
  programmes, and the box's HDMI output is **asleep**.
- **music** — music and photos.
- **diagnostic** — the state of the box, drawn on the television, on request.
- **installation** — a menu of guided procedures, with a human watching. Today
  it holds one: the CEC detection procedure, which works out how to drive a
  new television.

Four rules, and the whole design rests on them:

1. **One at a time.** Entering a mode leaves whatever was running first.
2. **Entering a mode wakes the output; leaving a mode puts it back to sleep.**
3. **Leaving a mode never leaves the set on the box's input.** Either the
   television goes off, or it is sent back to its own programmes.
4. **Nothing else ever touches the output.** The sleep and the wake live here,
   so no press has to be sequenced by hand.

Rule 2 is what fixes the fault that this rewrite exists for. A set that comes
back on "the last input used" cannot come back on an input that is silent
when it wakes: it falls back to its programmes. Because the output sleeps
whenever the box is idle, every wake happens that way, without a single
timing to get right. An earlier attempt did the same thing by sequencing —
cut the signal, standby, wait two seconds, restore — and it failed in
production for two evenings, because the restore had to happen before the
next wake and there was no moment at which that was true.

**Going back to the programmes uses whichever release technique the CEC
detection found**, saved in `tv_config.load()["release"]`. Not every set has
one: the set at the installation site refused Set Stream Path outright and
ignored thirteen other frames, and for it (and for any box whose detection
never found a better answer) the default is a power cycle — switch the set
off and on again, with the output already asleep, which is the one candidate
that works on every television because it needs no CEC command the set might
refuse. A set that does obey one of the faster frames
(`cec_controller.RELEASE_TECHNIQUES`) uses that instead, sent while the
output is still awake — see `_give_back_the_programmes()`.

**The order inside an entry is not free**: the output must be awake before any
CEC command that assumes the box is visible, because claiming an input that
shows nothing is ignored by some sets. The music mode brings the television up
in a thread to save time, and that thread wakes the output before it claims
anything.
"""

import logging
import threading
import time

import button_bindings
import cec_controller as cec
import hdmi_output
import installation as installation_screen
import media
import screen
import state
import tv_config
import zigbee_pairing
from cec_controller import CECError

logger = logging.getLogger(__name__)

# One mode at a time, and every change serialised: two presses arriving
# together must not leave half of one mode and half of another.
_lock = threading.RLock()

# The diagnostic screen is for whoever is standing in front of the television.
# Left up it would keep the set on the box's input for ever, so it has a
# deadline: the mode ends by itself and the set goes off.
DIAGNOSTIC_SECONDS = 300

_timer: "threading.Timer | None" = None


def _cancel_timer() -> None:
    global _timer
    timer, _timer = _timer, None
    if timer is not None:
        timer.cancel()


def _arm_timer(seconds: float, reason: str) -> None:
    global _timer
    _cancel_timer()
    _timer = threading.Timer(seconds, lambda: television(reason))
    _timer.daemon = True
    _timer.start()
def _show_the_box(why: str) -> None:
    """Wake the output, wake the set, and put the set on the box's input.

    Failure is logged, not raised: a set already on the box's input shows the
    photos anyway, and the owner is better served by music out of a television
    nobody could talk to than by nothing at all.
    """
    hdmi_output.wake(why)
    try:
        # No power-state read when the configured wake technique is one that
        # does nothing to a set already on. That read is the single biggest
        # source of friction on a television that answers nothing in standby
        # — which is the one this box was built for: measured end to end,
        # 15.8 s between the music button and the wake frame, with no picture
        # and no sound in between, because `pow 0` waits out its whole
        # timeout for an answer that never comes. Entering a mode wants the
        # set on and showing the box either way, so on those techniques there
        # is nothing the reading could change.
        #
        # A toggle is the exception, and the reason this is conditional
        # rather than simply deleted: sent to a set that is already on, it
        # switches it off.
        wake = tv_config.load()["wake"]["technique"]
        if wake in cec.SAFE_TO_REPEAT_WAKE or cec._safe_power_status() != "on":
            cec.power_on()
            _wait_until_on()
        cec.switch_to_pi()
    except CECError as exc:
        logger.warning("could not bring the television to the box's input: %s", exc)


def _give_back_the_programmes(why: str) -> dict:
    """Give the person their programmes back, using whatever release
    technique the CEC detection actually found — or a power cycle, the
    default and the fallback for a box whose search never landed on
    anything better.

    The two mechanisms put the output to sleep at opposite ends, and that
    order is not free. A candidate from `cec.RELEASE_TECHNIQUES`
    (inactive_source, set_stream_path, routing_change, tuner_keys) is a CEC
    command that asks the set to switch away, and per
    docs/development/installation-screen.md it must be sent with the output still
    AWAKE: a set that merely reacted to losing the signal, not to the
    command, would credit a frame that did nothing — the exact false
    positive a search must not produce, now repeated on every press if it
    were allowed to happen here. The power cycle is the opposite: the
    output being asleep *is* its mechanism (the same reasoning
    `_back_to_the_programmes` used to carry alone), so it puts the output
    to sleep FIRST, before the set is touched at all.

    Either way the output ends up asleep, which is why both branches call
    `hdmi_output.sleep()` themselves rather than leaving it to the caller —
    `television()` used to do that unconditionally before dispatching here,
    which was fine for the power cycle but would have undone the whole
    point of trying a candidate with the output awake.
    """
    technique = tv_config.load()["release"]["technique"]
    table = dict(cec.RELEASE_TECHNIQUES)
    if technique in table:
        try:
            traffic = table[technique]()
        except CECError as exc:
            logger.warning("release technique %r did nothing: %s", technique, exc)
            traffic = None
        hdmi_output.sleep(f"back to television ({why})")
        return {"release": technique, "traffic": traffic}
    hdmi_output.sleep(f"back to television ({why})")
    # Each half caught on its own, like the technique branch above: a
    # refused standby must not stop the wake that puts the set back in a
    # normal state, and this is also what keeps this whole function from
    # ever raising CECError — see television()'s own except for why that
    # matters.
    try:
        off = cec.standby()
    except CECError as exc:
        logger.warning("power-cycle release: standby did nothing: %s", exc)
        off = None
    _wait_after_standby()
    try:
        on = cec.power_on()
    except CECError as exc:
        logger.warning("power-cycle release: wake did nothing: %s", exc)
        on = None
    return {"release": "power_cycle", "standby": off, "wake": on}


def _leave_installation_if_running() -> None:
    """Abandon a running installation procedure before any other mode takes
    over. Without this, switching straight from installation to music or
    diagnostic (rather than via `television()`) would leave the procedure
    thread blocked in the answer channel, oblivious to the mode having
    changed under it — and it would eventually call `television()` itself,
    undoing whichever mode had taken over by then."""
    if state.mode() == state.INSTALLATION:
        installation_screen.channel.abandon()


def _wait_after_standby() -> None:
    """A wake sent too soon after a standby is ignored: measured on the set at
    the installation site, 0.4 s did nothing and two seconds worked."""
    threading.Event().wait(cec.ACTIVE_SOURCE_SETTLE_SECONDS)


# How long a television just woken may take before it obeys a change of
# input, asked about once a second. Measured on the set at the installation
# site: Active Source sent 0.4 s after Text View On was ignored, and the music
# played on a screen still showing the programmes — while the same frame sent
# to a set already on switched it at once.
#
# Bounded in time as well as in attempts: a set still booting may be slow to
# answer, and a reading that waits is exactly what once kept the music button
# silent for 15.8 s (see test_a_directional_wake_is_sent_without_reading...).
WAKE_SETTLE_ATTEMPTS = 10
WAKE_SETTLE_PAUSE_SECONDS = 1.0
WAKE_SETTLE_MAX_SECONDS = 10.0


def _pause(seconds: float) -> None:
    threading.Event().wait(seconds)


def _wait_until_on() -> bool:
    """Ask the set until it says it is on, so a claim of its input is sent
    once it can obey one. A set that never says so gets the claim anyway,
    after the last attempt — as it always did."""
    deadline = time.monotonic() + WAKE_SETTLE_MAX_SECONDS
    for _ in range(WAKE_SETTLE_ATTEMPTS):
        if cec._safe_power_status() == "on":
            return True
        if time.monotonic() >= deadline:
            break
        _pause(WAKE_SETTLE_PAUSE_SECONDS)
    logger.warning("the television never said it was on; claiming its input anyway")
    return False


# --- Leaving ----------------------------------------------------------------

def television(why: str = "asked", switch_off: bool = True) -> dict:
    """Back to the default state: nothing playing, output asleep.

    `switch_off` says what to do with the set. It is switched off by default —
    that is what "stop" means — and sent back to its programmes instead when
    somebody asked for the television rather than for silence.
    """
    with _lock:
        _cancel_timer()
        was = state.mode()
        _leave_installation_if_running()
        media._stop_players()
        answer = {"mode": state.TELEVISION, "was": was}
        # Before the set is touched, not after: the box has stopped the
        # players and is about to put its output to sleep, so this is already
        # the default state, and a `finally` to guarantee the mode was set at
        # all is then unnecessary.
        state.set_mode(state.TELEVISION)
        # And claim the bus here, while the television is still displaying the
        # box. Claimed later, after the output has gone to sleep and the set
        # has stopped showing that input, the claim polls and times out and
        # every frame after it goes out unregistered and unacknowledged —
        # measured on the board, a release that did nothing at all, silently.
        cec.take_the_bus()
        try:
            if was == state.TELEVISION:
                hdmi_output.sleep(f"back to television ({why})")
            elif switch_off:
                # The output sleeps FIRST, before the set is touched at all.
                # What follows then happens with nothing on the box's input,
                # which is what makes a television that returns to its last
                # input choose its own programmes instead. Done the other
                # way round, as a first version of this function did, the
                # set is woken while the box is still driving the screen and
                # comes back on the box: the exact fault of September 2026.
                hdmi_output.sleep(f"back to television ({why})")
                answer["tv"] = cec.standby()
            else:
                # Not the same order: a release technique from the table
                # needs the output AWAKE while it is sent, so this puts the
                # output to sleep itself, at the right moment for whichever
                # mechanism it ends up using — see its own docstring.
                answer["tv"] = _give_back_the_programmes(why)
        except CECError as exc:
            # Reached only from the switch_off branch above: the output is
            # already asleep by the time `cec.standby()` there could raise.
            # `_give_back_the_programmes()` catches its own CECError
            # internally (both its branches) and never raises here at all —
            # see its own docstring for why the output being asleep before
            # the failure matters just as much there.
            logger.warning("could not settle the television while leaving %s: %s",
                           was, exc)
            answer["error"] = str(exc)
        state.note("television", why=why, was=was)
        return answer


# --- Entering ---------------------------------------------------------------

def music(folder: "str | None" = None) -> dict:
    """Music and photos. The press that starts the mode also ends the last one."""
    with _lock:
        _cancel_timer()
        _leave_installation_if_running()
        # Whatever was running stops first, including a previous music
        # session: a second press on the button means "start again".
        media._stop_players()
        state.set_mode(state.MUSIC)
        try:
            television_thread = threading.Thread(
                target=_show_the_box, args=("the music and photos need the screen",),
                name="tv-bring-up", daemon=True)
            television_thread.start()
            try:
                started = media._start_players(folder)
            finally:
                # Joined before returning, so the answer still describes a
                # television somebody has finished talking to.
                television_thread.join()
        except BaseException:
            # Nothing is playing: do not leave the box driving a screen for a
            # mode that never started.
            television(why="the music could not start")
            raise
        return {"mode": state.MUSIC, **started}


def diagnostic() -> dict:
    """Put the state of the box on the television, for a few minutes."""
    with _lock:
        _cancel_timer()
        _leave_installation_if_running()
        media._stop_players()
        state.set_mode(state.DIAGNOSTIC)
        _show_the_box("the diagnostic screen")
        screen.show("diagnostic")
        _arm_timer(DIAGNOSTIC_SECONDS, "the diagnostic screen timed out")
        return {"mode": state.DIAGNOSTIC, "seconds": DIAGNOSTIC_SECONDS}


class _InstallationHardware:
    """What the installation screen's `Hardware` protocol needs, wrapping
    `cec_controller`/`hdmi_output`/`tv_config` — the only place that does,
    per rule 4. `_lock` is taken for each individual action, never across a
    wait: a wait lives entirely in the answer channel, which this class never
    touches.
    """

    @staticmethod
    def step_seconds() -> float:
        return tv_config.load()["detection_step_seconds"]

    @staticmethod
    def cycle_seconds() -> float:
        return tv_config.load()["detection_cycle_seconds"]

    @staticmethod
    def wake_techniques() -> "tuple[str, ...]":
        return tuple(name for name, _ in cec.WAKE_TECHNIQUES)

    @staticmethod
    def sleep_techniques() -> "tuple[str, ...]":
        return tuple(name for name, _ in cec.SLEEP_TECHNIQUES)

    @staticmethod
    def release_techniques() -> "tuple[str, ...]":
        return tuple(name for name, _ in cec.RELEASE_TECHNIQUES)

    @staticmethod
    def _send(table: "tuple[tuple[str, object], ...]", technique: str) -> None:
        function = dict(table).get(technique)
        if function is None:
            logger.warning("installation: unknown technique %r", technique)
            return
        with _lock:
            try:
                function()
            except CECError as exc:
                logger.info("installation: %s did nothing (%s)", technique, exc)
            except Exception:
                # Anything else, too. A search sends techniques precisely
                # because nobody knows which one this television answers, so
                # one of them blowing up must mean "that one did nothing" and
                # nothing more. On 2026-09-26 an IndexError from the first
                # release candidate escaped this, ended the whole procedure
                # from inside `_search`, and left the person in front of a
                # black screen with no idea what had happened.
                logger.exception("installation: %s raised", technique)

    def send_wake(self, technique: str) -> None:
        self._send(cec.WAKE_TECHNIQUES, technique)

    def send_sleep(self, technique: str) -> None:
        self._send(cec.SLEEP_TECHNIQUES, technique)

    def send_release(self, technique: str) -> None:
        self._send(cec.RELEASE_TECHNIQUES, technique)

    @staticmethod
    def claim_input() -> None:
        """Step 4's active attempt to bring the screen back: the same
        Active Source claim every ordinary mode entry already sends, not a
        release technique from the table — this runs before step 5 even
        starts searching."""
        with _lock:
            try:
                cec.switch_to_pi()
            except CECError as exc:
                logger.info("installation: claim_input did nothing (%s)", exc)

    @staticmethod
    def begin_release_power_cycle() -> None:
        """The power_cycle release candidate: not a CEC frame, the box's own
        output going to sleep is the mechanism. Left asleep on return — the
        person is looking at their television, not at the box — until
        `end_release_power_cycle` reclaims it.

        The bus is claimed before the output goes to sleep, for the same
        reason `television()` does it: after the sleep the claim can time out,
        and then the standby and wake that make up this candidate never reach
        the set and it is measured as a failure it had nothing to do with.
        """
        cec.take_the_bus()
        with _lock:
            hdmi_output.sleep("installation: testing the power-cycle release candidate")
            try:
                cec.standby()
                _wait_after_standby()
                cec.power_on()
            except CECError as exc:
                logger.warning("installation: power-cycle candidate: %s", exc)

    @staticmethod
    def end_release_power_cycle() -> None:
        with _lock:
            hdmi_output.wake("installation: back from the power-cycle release candidate")

    @staticmethod
    def television_identity() -> dict:
        with _lock:
            return cec.television_identity()

    @staticmethod
    def save_wake(technique: str) -> None:
        tv_config.set_technique("wake", technique, source="detection")

    @staticmethod
    def save_sleep(technique: str) -> None:
        tv_config.set_technique("sleep", technique, source="detection")

    @staticmethod
    def save_release(technique: str) -> None:
        tv_config.set_technique("release", technique, source="detection")

    @staticmethod
    def save_television(identity: dict) -> None:
        tv_config.set_television(identity)

    @staticmethod
    def finish() -> None:
        tv_config.set_detection_complete(True)


# How long the button-pairing procedure waits for a single physical press
# (pairing mode, then a press) before offering to try again. Generous:
# putting a button in pairing mode is a hold of about 5 seconds, and the
# person may still be reading the page when it starts.
PAIRING_STEP_SECONDS = 120.0


class _PairingHardware:
    """What the button-pairing screen's `PairingHardware` protocol needs,
    wrapping `zigbee_pairing`/`button_bindings` — the only place that does.
    Unlike `_InstallationHardware`, nothing here touches CEC, the output, or
    `_lock`: pairing a button is independent of the television entirely, and
    serialising it against CEC access would only make it wait for no
    reason.
    """

    @staticmethod
    def step_seconds() -> float:
        return PAIRING_STEP_SECONDS

    @staticmethod
    def bridge_ready() -> bool:
        return zigbee_pairing.bridge_ready(5)

    @staticmethod
    def has_any_button() -> bool:
        return button_bindings.has_any_button()

    def open_pairing(self) -> bool:
        return zigbee_pairing.open_pairing(self.step_seconds())

    @staticmethod
    def close_pairing() -> None:
        zigbee_pairing.close_pairing()

    def capture_press(self) -> "tuple[str, str] | None":
        return zigbee_pairing.capture_press(self.step_seconds())

    @staticmethod
    def rename_device(old: str, new: str) -> bool:
        return zigbee_pairing.rename_device(old, new)

    @staticmethod
    def device_actions(device: str) -> "list[str]":
        return zigbee_pairing.device_actions(device)

    @staticmethod
    def write_binding(role: str, device: str, actions: "list[str]") -> None:
        button_bindings.set_binding(role, device, actions)

    @staticmethod
    def reload_bindings() -> None:
        button_bindings.reload()

    @staticmethod
    def list_adapters() -> "list[str]":
        return zigbee_pairing.list_adapters()

    @staticmethod
    def bring_bridge_up(port: str) -> bool:
        return zigbee_pairing.bring_bridge_up(port)

    @staticmethod
    def stop_bridge() -> None:
        zigbee_pairing.stop_bridge()


def _run_installation() -> None:
    """The procedure thread's body. Whatever happens — finished, abandoned,
    or crashed — the box has nothing further to do with the screen once this
    returns, so it goes back to its default state on its own rather than
    leaving the mode to be closed by hand.

    Only if the mode is STILL installation by then: somebody may already have
    switched to another mode (or pressed "installation" again) while this
    thread was unwinding, and calling `television()` unconditionally here
    would clobber whatever took over in the meantime.
    """
    try:
        installation_screen.start({"cec": _InstallationHardware,
                                   "buttons": _PairingHardware})
    finally:
        with _lock:
            if state.mode() == state.INSTALLATION:
                television(why="the installation procedure finished")


def installation() -> dict:
    """The installation screen: a menu of guided procedures (today, one: the
    CEC detection procedure), run with a human watching.

    The procedure runs in its own thread and never holds `_lock` across a
    wait: a step that did would put every button press behind it, which is
    the fault of 2026-09-22 in another shape — two accidental presses
    occupied the box for twenty-six minutes because one press started
    something long and the second queued behind it.
    """
    with _lock:
        if state.mode() == state.INSTALLATION:
            return {"mode": state.INSTALLATION, "already_running": True}
        _cancel_timer()
        media._stop_players()
        state.set_mode(state.INSTALLATION)
        _show_the_box("the installation screen")
        threading.Thread(target=_run_installation, name="installation",
                         daemon=True).start()
        return {"mode": state.INSTALLATION}


# --- The buttons ------------------------------------------------------------

def tv_button() -> dict:
    """The "tv" button: give the person their programmes, or switch off.

    In a mode, it leaves it and puts the set back on its programmes — or,
    if `tv_config.load()["tv_button_switches_off"]` is set, switches off
    outright, the same as the music button already does. A configuration
    point, not a fixed behaviour: giving the programmes back costs nothing
    extra to press, but it means the two buttons mean different things
    depending on which one is pressed while something is playing. Simply
    switching off both times is easier to predict for somebody who finds
    that distinction hard to hold onto — at the cost of an extra press
    (the tv button again, from the default state) to actually see the
    television again. In the default state it is always a plain on/off,
    because that is what somebody sitting in front of a television means
    by that button, whichever way this setting is configured.
    """
    with _lock:
        if state.mode() != state.TELEVISION:
            switch_off = tv_config.load()["tv_button_switches_off"]
            return television(why="the tv button", switch_off=switch_off)
        # First, before either frame goes out. In the default state the output
        # is asleep already and this costs nothing — but if anything had left
        # it awake, waking the set with the box's input live is precisely how
        # a television that remembers its last input comes back on the box.
        hdmi_output.sleep("the television mode needs no screen")
        status = cec._safe_power_status()
        # Nested rather than merged: the technique's own answer also carries an
        # "action", and merging quietly overwrote the one the caller asked
        # about — caught by a test that expected "off" and read "standby".
        if status == "on":
            answer = {"action": "off", "status_before": status,
                      "television": cec.standby()}
        else:
            answer = {"action": "on", "status_before": status,
                      "television": cec.power_on()}
        return answer


def watch_television() -> dict:
    """The phone page's "TV": the set on, showing its own programmes.

    The tv button without its off half. From a mode it gives the programmes
    back, the way the button does; in the default state it wakes a set that
    is not on and leaves one that is alone. Pressed twice, it stays where it
    is — switching off is the page's own on/off button, and somebody
    tapping "TV" on a phone means "I want the television", never "off".

    A set already on is left untouched rather than woken again: on some
    televisions Image View On also switches the input to whoever sent it,
    and the box's input is the one place the viewer must not end up.
    """
    with _lock:
        if state.mode() != state.TELEVISION:
            return television(why="the phone's tv", switch_off=False)
        hdmi_output.sleep("the television mode needs no screen")
        status = cec._safe_power_status()
        if status == "on":
            return {"action": "none", "status_before": status}
        return {"action": "on", "status_before": status,
                "television": cec.power_on()}


def music_button(folder: "str | None" = None) -> dict:
    """The "music" button: start the music, or stop everything and switch off."""
    with _lock:
        if state.mode() == state.MUSIC:
            return television(why="the music button", switch_off=True)
        return music(folder)


def diagnostic_button() -> dict:
    with _lock:
        if state.mode() == state.DIAGNOSTIC:
            return television(why="the diagnostic button", switch_off=True)
        return diagnostic()


def installation_button() -> dict:
    with _lock:
        if state.mode() == state.INSTALLATION:
            return television(why="the installation button", switch_off=True)
        return installation()
