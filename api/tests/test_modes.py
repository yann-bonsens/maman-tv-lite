# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The mode machine: every transition, and what each one must guarantee.

Written after two evenings in which the box was driven by four different
sequences in a row and nobody could say, at any moment, what state it was
supposed to be in. Each transition is pinned here with the three things that
matter and that a person in front of the television actually sees:

- **the frames**: exactly which CEC technique went out, and in which order;
- **the players**: nothing plays outside the music mode;
- **the output**: the box's HDMI is asleep in the default state and awake in
  every mode — which is what stops a set that remembers its last input from
  coming back on the box.
"""

import threading
from unittest.mock import MagicMock

import pytest

import cec_controller as cec
import hdmi_output
import media
import modes
import state
import tv_config


# The real technique tables, taken at import time: the `box` fixture below
# replaces them with recording stubs for every other test in this file, which is
# what makes a transport fault invisible here.
REAL_TABLES = (("wake", cec.WAKE_TECHNIQUES), ("sleep", cec.SLEEP_TECHNIQUES),
               ("release", cec.RELEASE_TECHNIQUES))
# Two techniques reach the bus through this rather than through a frame string,
# and the fixture replaces it too.
REAL_SWITCH_TO_PI = cec.switch_to_pi


def _settle_installation(timeout: float = 5.0) -> None:
    """Wait for the installation procedure thread to actually finish.

    Without this, a thread left blocked in the answer channel by one test
    survives into the next with its monkeypatches already reverted by
    pytest — calling the real cec_controller/hdmi_output underneath it.
    """
    for thread in threading.enumerate():
        if thread.name == "installation" and thread.is_alive():
            thread.join(timeout=timeout)


@pytest.fixture
def box(box, monkeypatch):
    """A box whose television and players are recorded instead of driven.

    Everything the mode machine can touch is captured in one ordered list, so
    a transition can be read as the story of what the viewer saw: the output
    going dark, a frame going out, the music stopping.
    """
    told = []

    def frame(name):
        def send():
            told.append(f"frame {name}")
            return ""
        return send

    monkeypatch.setattr(cec, "WAKE_TECHNIQUES",
                        tuple((name, frame(name)) for name, _ in cec.WAKE_TECHNIQUES))
    monkeypatch.setattr(cec, "SLEEP_TECHNIQUES",
                        tuple((name, frame(name)) for name, _ in cec.SLEEP_TECHNIQUES))
    monkeypatch.setattr(cec, "RELEASE_TECHNIQUES",
                        tuple((name, frame(name)) for name, _ in cec.RELEASE_TECHNIQUES))
    monkeypatch.setattr(cec, "switch_to_pi", lambda: told.append("claim the input"))
    def _power_status():
        told.append("read the power state")
        return box.tv_power
    monkeypatch.setattr(cec, "_safe_power_status", _power_status)
    monkeypatch.setattr(media, "_start_players",
                        lambda folder=None: told.append("players started")
                        or {"pictures": 3, "music": 2, "folder": folder})
    monkeypatch.setattr(media, "_stop_players", lambda: told.append("players stopped"))
    monkeypatch.setattr(modes, "screen", MagicMock())
    # The two seconds a real power cycle waits between the standby and the
    # wake are measured on a television, not on a test runner.
    monkeypatch.setattr(modes, "_wait_after_standby", lambda: told.append("waited"))
    # And the pause between two readings of a set that is waking up.
    monkeypatch.setattr(modes, "_pause", lambda seconds: None)

    def note_output(verb, unit=None):
        # "restart" is the sleep verb, not "start": the units are oneshots
        # with RemainAfterExit=yes, and systemd makes `start` a no-op on one
        # it already considers active — so a sleep sent that way succeeds
        # and does nothing at all. Measured on the board 2026-09-26.
        # Putting the output to sleep is spelled stop-then-start (see
        # hdmi_output._apply: `start` alone is a no-op on a oneshot systemd
        # already considers active). The stop of that pair is an
        # implementation detail and must not read as a moment when the box
        # drove the screen, so only the start of a stop+start pair is
        # recorded — a stop followed by another stop is a real wake.
        asleep = verb == "start"
        if asleep and told and told[-1] == "output awake":
            told.pop()
        told.append("output asleep" if asleep else "output awake")
        box.hdmi["state"] = hdmi_output.ASLEEP if asleep else hdmi_output.AWAKE
        return True

    monkeypatch.setattr(hdmi_output, "_systemctl", note_output)
    # cec.television_identity() is left real (it fails fast and harmlessly
    # without a real adapter) except where a test needs a specific answer.
    box.told = told
    box.tv_power = "on"
    return box


@pytest.fixture(autouse=True)
def _leave_whatever_mode_after_each_test():
    """However a test ends, leave no mode running and no installation thread
    dangling into the next test — see `_settle_installation`."""
    yield
    modes.television(why="test teardown")
    _settle_installation()


def last_output(told):
    """asleep or awake, as the transition left it."""
    return next((e for e in reversed(told) if e.startswith("output ")), "untouched")


# --- The default state -------------------------------------------------------

class TestTheDefaultState:
    def test_a_box_that_has_just_started_is_in_television(self):
        assert state.mode() == state.TELEVISION

    def test_the_tv_button_switches_a_set_that_is_on_off(self, box):
        box.tv_power = "on"
        answer = modes.tv_button()
        assert answer["action"] == "off"
        assert "frame standby" in box.told
        assert last_output(box.told) == "output asleep", \
            "the output must not be driven under a set that is going off"

    def test_the_tv_button_switches_a_set_that_is_off_on(self, box):
        box.tv_power = "standby"
        answer = modes.tv_button()
        assert answer["action"] == "on"
        assert "frame image_view_on" in box.told

    def test_waking_leaves_the_output_asleep(self, box):
        """The whole fix in one line: the set wakes with nothing on the box's
        input, so a television that returns to the last input used falls back
        to its own programmes instead."""
        box.tv_power = "standby"
        modes.tv_button()
        assert last_output(box.told) == "output asleep"
        assert "output awake" not in box.told


class TestThePhonesTv:
    """The phone page's "TV": the tv button without its off half."""

    def test_it_wakes_a_set_that_is_off(self, box):
        box.tv_power = "standby"
        assert modes.watch_television()["action"] == "on"
        assert "frame image_view_on" in box.told
        assert last_output(box.told) == "output asleep"

    def test_it_never_switches_a_set_that_is_on_off(self, box):
        box.tv_power = "on"
        assert modes.watch_television()["action"] == "none"
        assert "frame standby" not in box.told

    def test_it_does_not_wake_a_set_that_is_already_on(self, box):
        """Image View On can also take the input; a set that is on must not
        be handed to the box's input for nothing."""
        box.tv_power = "on"
        modes.watch_television()
        assert "frame image_view_on" not in box.told

    def test_from_the_music_it_gives_the_programmes_back(self, box):
        modes.music()
        box.told.clear()
        modes.watch_television()
        assert state.mode() == state.TELEVISION
        assert "players stopped" in box.told
        assert last_output(box.told) == "output asleep"


# --- Entering a mode ---------------------------------------------------------

class TestGoingToMusic:
    def test_from_television(self, box):
        box.tv_power = "standby"
        answer = modes.music()
        assert answer["mode"] == state.MUSIC
        assert state.mode() == state.MUSIC
        assert box.hdmi["state"] == hdmi_output.AWAKE
        assert "players started" in box.told

    def test_the_output_wakes_before_the_input_is_claimed(self, box):
        """Claiming an input that shows nothing is ignored by some sets, and
        the television is brought up in a thread to save time — which is
        exactly where that order could be lost."""
        modes.music()
        assert box.told.index("output awake") < box.told.index("claim the input")

    def test_a_set_that_is_off_is_woken_first(self, box):
        box.tv_power = "standby"
        modes.music()
        assert box.told.index("frame image_view_on") < box.told.index("claim the input")

    def test_a_directional_wake_is_sent_without_reading_the_power_state(self, box):
        """The read is what made a press feel broken.

        On a television that answers nothing in standby — the one this box
        was built for — `pow 0` waits out its whole 15-second timeout.
        Measured end to end before this: 15.8 s between the music button and
        the wake frame going out, with no picture and no sound in between,
        which is exactly long enough for somebody to press again and conclude
        the box is dead.

        Entering a mode wants the set on and showing the box whatever the
        answer would have been, and "image view on" does nothing to a set
        already on, so there is nothing the reading could change.
        """
        box.tv_power = "on"
        modes.music()
        assert "frame image_view_on" in box.told
        assert "read the power state" not in \
            box.told[:box.told.index("frame image_view_on")]

    def test_a_toggle_wake_is_never_sent_without_checking_first(self, box):
        """The exception, and the reason the skip is conditional rather than
        the read simply deleted: a toggle sent to a set that is already on
        switches it OFF. Power Toggle (0x6B) is one outright, and User
        Control "Power" (0x40) behaves as one on many televisions."""
        tv_config.set_technique("wake", "power_toggle_key", source="detection")
        box.tv_power = "on"
        modes.music()
        assert "read the power state" in box.told
        assert not [e for e in box.told if e.startswith("frame power_toggle_key")], \
            "a toggle must not be sent to a set that is already on"

    def test_every_technique_that_skips_the_check_is_directional(self):
        """A toggle in this set would switch off the television it was meant
        to wake, on every single press."""
        for name in cec.SAFE_TO_REPEAT_WAKE:
            assert "toggle" not in name, name
            assert name != "power_key", "User Control 'Power' is a toggle on many sets"
            assert name in dict(cec.WAKE_TECHNIQUES), f"{name} is not a wake technique"

    def test_pressing_music_again_restarts_the_session(self, box):
        modes.music()
        box.told.clear()
        modes.music()
        assert box.told.index("players stopped") < box.told.index("players started")

    def test_a_music_that_cannot_start_does_not_leave_the_box_holding_the_screen(
            self, box, monkeypatch):
        monkeypatch.setattr(media, "_start_players",
                            MagicMock(side_effect=media.MediaError("nothing to play")))
        with pytest.raises(media.MediaError):
            modes.music()
        assert state.mode() == state.TELEVISION
        assert box.hdmi["state"] == hdmi_output.ASLEEP


class TestGoingToDiagnostic:
    def test_it_takes_the_screen_and_arms_its_own_end(self, box):
        answer = modes.diagnostic()
        assert answer["mode"] == state.DIAGNOSTIC
        assert box.hdmi["state"] == hdmi_output.AWAKE
        modes.screen.show.assert_called_once_with("diagnostic")
        assert modes._timer is not None, "a page nobody ends would hold the set for ever"
        modes._cancel_timer()

    def test_it_ends_by_itself(self, box, monkeypatch):
        """Five minutes is plenty to read it, and a television left on the
        box's input is exactly what this rewrite exists to prevent."""
        monkeypatch.setattr(modes, "DIAGNOSTIC_SECONDS", 0.05)
        modes.diagnostic()
        threading.Event().wait(0.4)
        assert state.mode() == state.TELEVISION
        assert box.hdmi["state"] == hdmi_output.ASLEEP


class TestGoingToInstallation:
    def test_it_takes_the_screen_and_starts_the_procedure(self, box):
        answer = modes.installation()
        assert answer["mode"] == state.INSTALLATION
        assert state.mode() == state.INSTALLATION
        assert box.hdmi["state"] == hdmi_output.AWAKE
        assert any(t.name == "installation" and t.is_alive()
                  for t in threading.enumerate()), \
            "the procedure must run in its own thread, never inline"

    def test_pressing_it_again_is_a_no_op_not_a_second_procedure(self, box):
        modes.installation()
        running = [t for t in threading.enumerate() if t.name == "installation"]
        answer = modes.installation()
        assert answer.get("already_running") is True
        assert [t for t in threading.enumerate() if t.name == "installation"] == running, \
            "a second call must not start a second procedure thread"

    def test_leaving_abandons_the_procedure_thread(self, box):
        modes.installation()
        modes.television(why="test")
        _settle_installation()
        assert not any(t.name == "installation" and t.is_alive()
                       for t in threading.enumerate())

    def test_switching_straight_to_another_mode_also_abandons_it(self, box):
        """Rule 1, "one mode at a time": going installation -> music directly
        (not via television()) must still tear the procedure down, or the
        forgotten thread would eventually call television() itself and undo
        whichever mode had taken over by then."""
        modes.installation()
        modes.music()
        _settle_installation()
        assert not any(t.name == "installation" and t.is_alive()
                       for t in threading.enumerate())
        assert state.mode() == state.MUSIC


# --- Leaving a mode ----------------------------------------------------------

class TestLeavingMusic:
    def test_the_music_button_stops_everything_and_switches_the_set_off(self, box):
        modes.music()
        box.told.clear()
        answer = modes.music_button()
        assert answer["was"] == state.MUSIC
        assert "players stopped" in box.told
        assert "frame standby" in box.told
        assert last_output(box.told) == "output asleep"
        assert state.mode() == state.TELEVISION

    def test_the_tv_button_sends_the_set_back_to_its_programmes(self, box):
        """A box whose CEC detection never found (or never ran) a faster
        release technique — DEFAULT_RELEASE is "power_cycle" — falls back
        to switching the set off and on again, with the output already
        asleep, which is what makes the set choose its programmes."""
        modes.music()
        box.told.clear()
        modes.tv_button()
        assert box.told.index("players stopped") < box.told.index("frame standby")
        assert box.told.index("frame standby") < box.told.index("waited") \
            < box.told.index("frame image_view_on")
        assert last_output(box.told) == "output asleep"

    def test_the_output_sleeps_before_the_set_is_woken_again(self, box):
        modes.music()
        box.told.clear()
        modes.tv_button()
        assert box.told.index("output asleep") < box.told.index("frame image_view_on"), \
            "woken with the box's input live, the set comes back on the box"

    def test_a_faster_release_technique_is_used_when_one_was_found(self, box):
        """A set whose CEC detection found a real release candidate
        (inactive_source, say) must not pay for a full power cycle on every
        press — that is precisely what the search exists to avoid."""
        tv_config.set_technique("release", "inactive_source", source="detection")
        modes.music()
        box.told.clear()
        modes.tv_button()
        assert "frame inactive_source" in box.told
        assert "frame standby" not in box.told, \
            "the power-cycle fallback must not run once a real technique is known"
        assert "waited" not in box.told

    def test_the_output_stays_awake_while_a_release_frame_is_sent(self, box):
        """The opposite order from the power cycle, and deliberately so: a
        set reacting to losing the signal rather than to the command would
        credit a frame that did nothing — see _give_back_the_programmes()'s
        own docstring."""
        tv_config.set_technique("release", "inactive_source", source="detection")
        modes.music()
        box.told.clear()
        modes.tv_button()
        assert box.told.index("frame inactive_source") < box.told.index("output asleep"), \
            "the release frame must be sent before the output goes to sleep"

    def test_a_release_frame_that_fails_still_puts_the_output_to_sleep(self, box, monkeypatch):
        tv_config.set_technique("release", "inactive_source", source="detection")
        monkeypatch.setattr(cec, "RELEASE_TECHNIQUES",
                            (("inactive_source",
                              MagicMock(side_effect=cec.CECError("no bus"))),))
        modes.music()
        answer = modes.tv_button()
        assert answer["tv"]["traffic"] is None
        assert box.hdmi["state"] == hdmi_output.ASLEEP

    def test_the_tv_button_switches_off_when_configured_to(self, box):
        """The accessibility setting: both buttons mean the same thing
        while something is playing, at the cost of an extra press to see
        the television again."""
        tv_config.set_tv_button_switches_off(True)
        modes.music()
        box.told.clear()
        answer = modes.tv_button()
        assert "frame standby" in box.told
        assert "waited" not in box.told, \
            "a plain standby, not the power-cycle _give_back_the_programmes() uses"
        # cec.standby()'s own shape, not _give_back_the_programmes()'s —
        # the latter always carries a "release" key, this never does.
        assert "release" not in answer["tv"]

    def test_the_default_still_gives_the_programmes_back(self, box):
        """The setting defaults to False: unchanged behaviour for anyone
        who has not touched it."""
        modes.music()
        box.told.clear()
        answer = modes.tv_button()
        assert isinstance(answer["tv"], dict) and "release" in answer["tv"]

    def test_a_television_that_refuses_still_leaves_the_mode(self, box, monkeypatch):
        """A set that cannot be reached is no reason to keep playing music at
        it, or to keep driving a screen nobody is watching."""
        modes.music()
        monkeypatch.setattr(cec, "standby", MagicMock(side_effect=cec.CECError("no bus")))
        answer = modes.television(why="test")
        assert "error" in answer
        assert state.mode() == state.TELEVISION
        assert box.hdmi["state"] == hdmi_output.ASLEEP


class TestLeavingTheOtherModes:
    @pytest.mark.parametrize("enter,button", [
        (lambda: modes.diagnostic(), lambda: modes.diagnostic_button()),
        (lambda: modes.installation(), lambda: modes.installation_button()),
    ])
    def test_pressing_the_same_button_again_leaves(self, box, enter, button):
        enter()
        button()
        assert state.mode() == state.TELEVISION
        assert box.hdmi["state"] == hdmi_output.ASLEEP
        assert "frame standby" in box.told

    def test_the_tv_button_leaves_any_mode_by_the_programmes(self, box):
        modes.diagnostic()
        box.told.clear()
        modes.tv_button()
        assert state.mode() == state.TELEVISION
        assert box.told.index("frame standby") < box.told.index("frame image_view_on")


# --- Every pair ---------------------------------------------------------------

ENTER = {
    state.MUSIC: lambda: modes.music(),
    state.DIAGNOSTIC: lambda: modes.diagnostic(),
    state.INSTALLATION: lambda: modes.installation(),
    state.TELEVISION: lambda: modes.television(why="test"),
}


class TestEveryTransition:
    @pytest.mark.parametrize("first", list(ENTER))
    @pytest.mark.parametrize("second", list(ENTER))
    def test_one_mode_at_a_time_and_the_output_follows_it(self, box, first, second):
        ENTER[first]()
        box.told.clear()
        ENTER[second]()
        assert state.mode() == second
        expected = hdmi_output.ASLEEP if second == state.TELEVISION else hdmi_output.AWAKE
        assert box.hdmi["state"] == expected, \
            f"{first} -> {second} left the output {box.hdmi['state']}"
        modes._cancel_timer()

    @pytest.mark.parametrize("first", list(ENTER))
    @pytest.mark.parametrize("second", list(ENTER))
    def test_nothing_plays_outside_the_music_mode(self, box, first, second):
        ENTER[first]()
        box.told.clear()
        ENTER[second]()
        if first == state.INSTALLATION and second == state.INSTALLATION:
            # A second "installation" while one is already running is a
            # deliberate no-op (a 15-minute guided search must not be thrown
            # away by a double press) rather than the "second press restarts"
            # shape every other mode has, so nothing runs a second time here.
            return
        if second != state.MUSIC:
            assert "players started" not in box.told
            assert "players stopped" in box.told
        modes._cancel_timer()

    @pytest.mark.parametrize("mode", [state.MUSIC, state.DIAGNOSTIC, state.INSTALLATION])
    def test_leaving_never_leaves_the_set_on_an_input_with_nothing_on_it(self, box, mode):
        """The one thing a viewer must never be given: a live television
        showing the box's input with no picture on it."""
        ENTER[mode]()
        box.told.clear()
        modes.television(why="test")
        assert last_output(box.told) == "output asleep"
        assert "frame standby" in box.told, "the set is switched off, not abandoned"


# --- What the configuration decides ------------------------------------------

class TestTheConfiguredTechniques:
    def test_the_defaults_are_the_two_standard_frames(self, box):
        modes.tv_button()
        assert "frame standby" in box.told
        box.tv_power = "standby"
        box.told.clear()
        modes.tv_button()
        assert "frame image_view_on" in box.told

    def test_a_configured_technique_is_the_one_sent(self, box):
        tv_config.set_technique("wake", "text_view_on", source="detection")
        tv_config.set_technique("sleep", "standby_after_active_source", source="manual")
        box.tv_power = "on"
        modes.tv_button()
        assert "frame standby_after_active_source" in box.told
        box.tv_power = "standby"
        box.told.clear()
        modes.tv_button()
        assert "frame text_view_on" in box.told

    def test_a_press_never_writes_the_configuration(self, box):
        """The fault this whole rewrite exists for: a press used to search,
        record, strike off and forget techniques, and an accidental one wiped
        an evening's work at the installation site."""
        before = tv_config.load()
        modes.music()
        modes.tv_button()
        modes.music_button()
        assert tv_config.load() == before


class TestWhatTheAuditFound:
    def test_entering_installation_disarms_the_diagnostic_deadline(self, box):
        """The diagnostic page ends itself after five minutes. Entered while
        that deadline was armed, installation inherited it and was thrown out
        mid-way by a timer belonging to the mode before it."""
        modes.diagnostic()
        modes.installation()
        assert modes._timer is None
        modes._cancel_timer()

    @pytest.mark.parametrize("enter", [lambda: modes.music(),
                                       lambda: modes.diagnostic(),
                                       lambda: modes.installation()])
    def test_no_transition_leaves_a_timer_from_the_mode_before(self, box, enter):
        modes.diagnostic()
        enter()
        armed = modes._timer
        modes._cancel_timer()
        # Only the diagnostic arms one, and only its own.
        assert armed is None or state.mode() == state.DIAGNOSTIC

    def test_the_output_is_asleep_before_the_tv_button_sends_anything(self, box):
        """It already is in the default state, so this costs nothing — but an
        output left awake by anything else would make the set come back on the
        box, which is the fault this design exists to remove."""
        box.hdmi["state"] = hdmi_output.AWAKE
        box.tv_power = "standby"
        modes.tv_button()
        assert box.told.index("output asleep") < box.told.index("frame image_view_on")


def test_a_technique_that_raises_does_not_end_the_procedure(box, monkeypatch):
    """A search sends techniques precisely because nobody knows which one
    this television answers, so one of them blowing up must mean "that one
    did nothing" and nothing more.

    On 2026-09-26 an IndexError from the first release candidate escaped
    this, ended the whole CEC detection from inside `_search`, and left the
    person in front of a black screen with no idea what had happened.
    """
    def explode():
        raise IndexError("list index out of range")
    monkeypatch.setattr(cec, "RELEASE_TECHNIQUES", (("boom", explode),))
    modes._InstallationHardware().send_release("boom")  # must not raise

# --- The bus, and where the television is --------------------------------------

class TestTheBusIsClaimedWhileTheSetCanStillHearIt:
    """Two faults of 2026-09-26, pinned at the level they happened.

    The box put its output to sleep, sent a standby and a wake, and the set
    came back on the box's input every time. Captured on the bus: libCEC had
    sent Image View On and Active Source of its own accord, because the set
    drops its HDMI hotplug at every power-on and libCEC re-announces itself
    whenever the adapter's address comes back. libCEC is gone; cec-ctl sends
    the frame it is given and nothing else.

    What is left to get right is the *order*: the adapter's logical address has
    to be claimed while the television is still displaying the box. Claimed
    after the output has gone to sleep, the claim polled and timed out, and the
    standby and wake that followed went out unregistered and unacknowledged —
    a release that did nothing at all, silently.
    """

    def _watch_the_order(self, monkeypatch, box):
        """Record the bus claim among the frames and the output changes."""
        monkeypatch.setattr(cec, "take_the_bus",
                            lambda: box.told.append("bus claimed"))
        return box.told

    def test_the_bus_is_claimed_before_the_output_sleeps(self, box, monkeypatch):
        tv_config.set_technique("release", "power_cycle", source="detection")
        told = self._watch_the_order(monkeypatch, box)
        modes.music()
        told.clear()
        modes.television(why="the tv button", switch_off=False)
        assert "bus claimed" in told, "the box never claimed the bus"
        assert told.index("bus claimed") < told.index("output asleep"), told

    def test_switching_the_set_off_claims_it_first_too(self, box, monkeypatch):
        told = self._watch_the_order(monkeypatch, box)
        modes.music()
        told.clear()
        modes.television(why="the music button", switch_off=True)
        assert told.index("bus claimed") < told.index("frame standby"), told

    def test_a_transition_that_sends_nothing_still_claims_it(self, box, monkeypatch):
        """A mode can end without a single frame — the diagnostic screen timing
        out on a box whose set is already where it should be. The claim must not
        be left to whichever frame happens to go out."""
        told = self._watch_the_order(monkeypatch, box)
        told.clear()
        modes.television(why="asked")
        assert "bus claimed" in told

    def test_the_detection_claims_it_before_the_power_cycle(self, box, monkeypatch):
        """Otherwise the candidate is measured as a failure it had nothing to do
        with: the standby and wake never reach the set at all."""
        told = self._watch_the_order(monkeypatch, box)
        told.clear()
        modes._InstallationHardware().begin_release_power_cycle()
        assert told.index("bus claimed") < told.index("output asleep"), told

    def test_the_mode_is_announced_before_the_set_is_touched(self, box, monkeypatch):
        """It was announced in a `finally`, after the frames. Stating it first
        is both truthful — the players are stopped, the box has left the mode —
        and what removes the need for that `finally` at all."""
        seen = []
        monkeypatch.setattr(cec, "SLEEP_TECHNIQUES",
                            (("standby", lambda: seen.append(state.mode()) or ""),))
        modes.music()
        modes.television(why="the music button", switch_off=True)
        assert seen == [state.TELEVISION], seen


class TestTheDetectionReachesTheBusForReal:
    """Every other test of the procedure replaces the technique tables, so the
    bridge between the procedure and the transport is exercised by nothing.
    That is where the move to cec-ctl could have broken a whole television's
    worth of detection without a single test failing: `_send` catches
    everything, so a technique the transport cannot render is not an error, it
    is a technique that silently does nothing for ever.
    """

    def test_each_named_technique_puts_a_frame_on_the_bus(self, box, monkeypatch):
        hardware = modes._InstallationHardware()
        senders = {"wake": hardware.send_wake, "sleep": hardware.send_sleep,
                   "release": hardware.send_release}
        calls = []
        # The two `_after_active_source` techniques wait ACTIVE_SOURCE_SETTLE_
        # SECONDS between their two frames, which is measured on a television
        # and has no business being waited out here: it was four seconds of the
        # suite's whole runtime, in one test.
        monkeypatch.setattr(cec.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(cec, "_cec_ctl",
                            lambda arguments: calls.append(arguments) or "")
        monkeypatch.setattr(cec, "take_the_bus", lambda reading=None: None)
        monkeypatch.setattr(cec, "link_is_down", lambda reading=None: False)
        monkeypatch.setattr(cec, "own_physical_address", lambda: "10:00")
        monkeypatch.setattr(cec, "switch_to_pi", REAL_SWITCH_TO_PI)
        for kind, table in REAL_TABLES:
            monkeypatch.setattr(cec, f"{kind.upper()}_TECHNIQUES", table)
            for name, _ in table:
                del calls[:]
                senders[kind](name)
                assert calls, f"{name} reached no cec-ctl call at all"

    def test_a_technique_the_transport_cannot_send_is_not_silent(self, box, monkeypatch, caplog):
        """It stays caught — a search must not be ended by one bad frame — but it
        has to leave a trace, or a technique that can never work looks exactly
        like a television that ignores it."""
        monkeypatch.setattr(cec, "WAKE_TECHNIQUES",
                            (("nonsense", lambda: cec._run("scan")),))
        with caplog.at_level("INFO"):
            modes._InstallationHardware().send_wake("nonsense")
        assert "nonsense" in caplog.text


class TestThePhonePageUsesTheConfiguredTechniques:
    """What the phone page calls, through the real routes, with a television
    configured by hand. Switching off on the programmes went through
    /mode/television, which sends nothing there, and read as a box ignoring
    its configuration."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from conftest import AUTH_HEADERS
        from main import app
        return TestClient(app, headers=AUTH_HEADERS)

    def test_off_on_the_programmes_sends_the_configured_sleep(self, box, client):
        tv_config.set_technique("sleep", "standby_after_active_source")
        assert client.post("/tv/off").json()["technique"] == "standby_after_active_source"
        assert "frame standby_after_active_source" in box.told

    def test_off_from_the_music_sends_the_configured_sleep(self, box, client):
        tv_config.set_technique("sleep", "standby_after_active_source")
        modes.music()
        box.told.clear()
        client.post("/mode/television")
        assert "frame standby_after_active_source" in box.told

    def test_tv_sends_the_configured_wake(self, box, client):
        tv_config.set_technique("wake", "text_view_on")
        box.tv_power = "standby"
        client.post("/tv/watch")
        assert "frame text_view_on" in box.told
        assert "frame image_view_on" not in box.told

    def test_tv_on_sends_the_same_configured_wake(self, box, client):
        tv_config.set_technique("wake", "text_view_on")
        client.post("/tv/on")
        assert "frame text_view_on" in box.told

    def test_the_page_switches_off_with_tv_off_on_the_programmes(self, client):
        page = client.get("/").text
        assert 'mode === "television" ? "/tv/off" : "/mode/television"' in page


class TestTheTwoButtonsWithAConfiguredTelevision:
    """Every case of the two everyday buttons, pressed through the bridge's
    own dispatch table, on a television configured the way the installation
    site's is: Image View On to wake, Standby after claiming the input to
    switch off, and a power cycle to give the programmes back. Each test
    reads the frames that actually went out, in order."""

    @pytest.fixture(autouse=True)
    def configured(self, box):
        tv_config.set_technique("wake", "image_view_on")
        tv_config.set_technique("sleep", "standby_after_active_source")
        tv_config.set_technique("release", "power_cycle")

    @staticmethod
    def press(name):
        import zigbee_bridge
        return zigbee_bridge.COMMANDS[name]()

    @staticmethod
    def frames(box):
        return [e for e in box.told if e.startswith(("frame ", "claim the input"))]

    def test_tv_on_a_set_that_is_off_wakes_it_with_the_configured_frame(self, box):
        box.tv_power = "standby"
        self.press("tv")
        assert self.frames(box) == ["frame image_view_on"]
        assert last_output(box.told) == "output asleep"

    def test_tv_on_a_set_showing_its_programmes_switches_it_off_as_configured(self, box):
        box.tv_power = "on"
        self.press("tv")
        assert self.frames(box) == ["frame standby_after_active_source"]

    def test_tv_during_the_music_power_cycles_back_to_the_programmes(self, box):
        self.press("music")
        box.told.clear()
        self.press("tv")
        assert state.mode() == state.TELEVISION
        assert self.frames(box) == ["frame standby_after_active_source",
                                    "frame image_view_on"]
        assert box.told.index("players stopped") < box.told.index("output asleep") \
            < box.told.index("frame standby_after_active_source")
        assert last_output(box.told) == "output asleep"

    def test_music_on_a_set_that_is_off_wakes_it_then_claims_the_input(self, box):
        box.tv_power = "standby"
        self.press("music")
        assert state.mode() == state.MUSIC
        assert self.frames(box) == ["frame image_view_on", "claim the input"]
        assert box.told.index("output awake") < box.told.index("frame image_view_on")

    def test_music_on_a_set_showing_its_programmes_takes_the_input(self, box):
        """Image View On goes out too: it is safe to repeat, and skipping the
        power-state read it would need saves 15 s on a set that answers
        nothing in standby. The input is claimed with the standard frame."""
        box.tv_power = "on"
        self.press("music")
        assert self.frames(box) == ["frame image_view_on", "claim the input"]

    def test_music_during_the_music_switches_off_as_configured(self, box):
        self.press("music")
        box.told.clear()
        self.press("music")
        assert state.mode() == state.TELEVISION
        assert self.frames(box) == ["frame standby_after_active_source"]
        assert box.told.index("players stopped") < box.told.index("frame standby_after_active_source")
        assert last_output(box.told) == "output asleep"


class TestTheInputIsClaimedOnceTheSetIsUp:
    """Measured at the installation site: Active Source sent 0.4 s after the
    wake was ignored by a set still starting, and the music played on a
    screen showing the programmes."""

    def answers(self, box, monkeypatch, *replies):
        replies = list(replies)
        def read():
            box.told.append("read the power state")
            return replies.pop(0) if len(replies) > 1 else replies[0]
        monkeypatch.setattr(cec, "_safe_power_status", read)

    def test_the_claim_waits_until_the_set_says_it_is_on(self, box, monkeypatch):
        self.answers(box, monkeypatch, "standby", "standby", "on")
        modes.music()
        wake = box.told.index("frame image_view_on")
        claim = box.told.index("claim the input")
        assert box.told[wake:claim].count("read the power state") == 3

    def test_a_set_that_never_says_so_is_claimed_anyway(self, box, monkeypatch):
        self.answers(box, monkeypatch, "unknown")
        modes.music()
        assert "claim the input" in box.told
        assert box.told.count("read the power state") <= modes.WAKE_SETTLE_ATTEMPTS

    def test_a_set_already_on_costs_one_reading(self, box, monkeypatch):
        self.answers(box, monkeypatch, "on")
        modes.music()
        assert box.told.count("read the power state") == 1
