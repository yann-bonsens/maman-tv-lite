# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The real code, a fake `cec-ctl`, and a model of a real television behind it.

The layer nothing else tests. `test_cec_detection.py` models a television at the
level of technique *names*; `test_modes.py` replaces the technique tables with
recorders; `test_cec_controller.py` mocks `subprocess`. So the frames, the argv,
the addressing, the acknowledgements and the link were each tested alone and
never together — which is where every fault of 2026-09-26 lived.

Here the real `cec_controller`, the real `modes`, and the real detection
procedure run against `television_model.py`, whose behaviour comes only from
what was captured from the set. See that module for what it deliberately does
not model.
"""

import os
import sys
from pathlib import Path

import pytest

import cec_controller as cec
import cec_detection
import hdmi_output
import log_config
import media
import modes
import state
import tv_config
from tests import television_model as tm

# Slow, and deliberately so — see pyproject.toml.
# Executes a fake `cec-ctl` once per frame, in its own process, which is
# what makes it faithful and what makes it slow.
pytestmark = pytest.mark.integration


TESTS_DIR = Path(__file__).resolve().parent


class Television:
    """The model set, as a test talks to it."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        return tm.load(str(self.path))

    def _write(self, whole: dict) -> None:
        tm.save(whole, str(self.path))

    @property
    def power(self) -> str:
        return self._read()["television"]["power"]

    @property
    def input(self) -> str:
        return self._read()["television"]["input"]

    @property
    def frames(self) -> "list[str]":
        """Every frame the set received, in order, by opcode."""
        return [frame["key"] for frame in self._read()["received"]]

    @property
    def invocations(self) -> int:
        """How many times the box ran cec-ctl. Each one is a process."""
        return self._read()["invocations"]

    @property
    def attempts(self) -> "list[dict]":
        """Every frame the box put on the bus, delivered or not."""
        return self._read()["attempted"]

    def addressed_to(self) -> "set[int]":
        return {frame["to"] for frame in self.attempts}

    def forget_frames(self) -> None:
        whole = self._read()
        whole["received"] = []
        whole["attempted"] = []
        self._write(whole)

    def set(self, **changes) -> None:
        whole = self._read()
        whole["television"].update(changes)
        self._write(whole)

    def moves_to(self, address: int) -> None:
        """The set takes another logical address. What makes it do that is
        unknown, so only a test does it."""
        self.set(logical_address=address)

    def takes_its_link_away(self) -> None:
        self.set(hotplug=False)

    # The person's own remote, which is the only thing that always works.
    def switched_off_by_hand(self) -> None:
        self.set(power="standby")

    def switched_on_by_hand(self) -> None:
        self.set(power="on", input="tuner")


@pytest.fixture
def television(box, tmp_path, monkeypatch):
    """A fake `cec-ctl` on PATH, and the box's HDMI output coupled to the set.

    The coupling is the point: the faults worth catching are in what happens to
    the CEC bus when the box stops driving its own output.
    """
    path = tmp_path / "television.json"
    tm.save(tm.fresh(), str(path))

    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake = binaries / "cec-ctl"
    fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" '
                    f'"{TESTS_DIR / "television_model.py"}" "$@"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(tm.STATE_ENV, str(path))

    monkeypatch.setattr(cec, "CEC_DEVICE", "/dev/cec0")
    monkeypatch.setattr(cec, "_own_address", None)
    monkeypatch.setattr(cec, "_tv_address", None)
    # `raising=False` so this fixture can also be pointed at an older
    # cec_controller, which is how the everyday paths were compared against what
    # is deployed: a counter the old module never had must not stop the setup.
    monkeypatch.setattr(cec, "_link_down_seen", 0, raising=False)
    monkeypatch.setattr(cec, "_link_recovered", 0, raising=False)
    # Nothing in a test may wait out a real television's delays.
    monkeypatch.setattr(cec.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(modes, "_wait_after_standby", lambda: None)
    monkeypatch.setattr(media, "_stop_players", lambda: None)
    monkeypatch.setattr(media, "_start_players",
                        lambda folder=None: {"pictures": 0, "music": 0})
    monkeypatch.setattr(modes, "screen", type("S", (), {
        "show": staticmethod(lambda *a, **k: None),
        "stop": staticmethod(lambda *a, **k: None)}))

    # The box's output, and what the set makes of it.
    #
    # Wrapped at `sleep()`/`wake()` rather than at `_systemctl()`: putting the
    # output to sleep is spelled stop-then-start, so at the systemctl level the
    # first half of a sleep is indistinguishable from a wake — and the model
    # would see the link repaired by the very call that was taking it away.
    # What reaches the television is the resulting state, not the calls.
    def couple(resting: str):
        whole = tm.load(str(path))
        whole["box_output"] = resting
        set_ = whole["television"]
        if resting == "asleep" and set_["drops_link_when_the_output_sleeps"]:
            set_["hotplug"] = False
        elif resting == "awake" and not set_["hotplug"] \
                and set_["recovers_link_when_the_output_wakes"]:
            set_["hotplug"] = True
        tm.save(whole, str(path))

    already_asleep, already_awake = hdmi_output.sleep, hdmi_output.wake

    def sleep(why):
        answered = already_asleep(why)
        couple("asleep")
        return answered

    def wake(why):
        answered = already_awake(why)
        couple("awake")
        return answered

    monkeypatch.setattr(hdmi_output, "sleep", sleep)
    monkeypatch.setattr(hdmi_output, "wake", wake)

    tv_config.set_technique("wake", "text_view_on", source="detection")
    tv_config.set_technique("sleep", "standby", source="detection")
    tv_config.set_technique("release", "power_cycle", source="detection")
    return Television(path)


# ---------------------------------------------------------------------------
# The everyday presses
# ---------------------------------------------------------------------------

class TestTheEverydayPresses:
    def test_the_tv_button_switches_a_set_that_is_on_off(self, television):
        answer = modes.tv_button()
        assert answer["action"] == "off"
        assert "0x36" in television.frames
        assert television.power == "standby"

    def test_the_tv_button_wakes_a_set_that_is_off(self, television):
        television.switched_off_by_hand()
        answer = modes.tv_button()
        assert answer["action"] == "on"
        assert television.power == "on"
        assert television.input == "tuner", \
            "waking it must not bring it to the box's input"

    def test_entering_the_music_mode_brings_the_set_to_the_box(self, television):
        modes.music()
        assert television.power == "on"
        assert television.input == "box"

    def test_the_release_gives_the_programmes_back_without_claiming_the_input(
            self, television):
        """The 2026-09-26 fault, at the level it was visible from the sofa.

        libCEC used to announce the box as active source the moment the set's
        hotplug came back, and the television obeyed. Nothing may do that now:
        the release sends a standby and a wake, and not one frame that claims
        the input.
        """
        modes.music()
        television.forget_frames()
        modes.television(why="the tv button", switch_off=False)
        assert television.frames[:2] == ["0x36", "0x0d"], television.frames
        assert "0x82" not in television.frames, \
            "something claimed the television's input during the release"
        assert television.input == "tuner"
        assert television.power == "on"

    def test_a_press_runs_cec_ctl_no_more_than_it_has_to(self, television):
        """Each run is a process, and on this board that is about 0.2 s.

        A press that switches the set off is: one adapter reading, and the frame.
        It was two readings until the reading was shared — "is the link up" and
        "do I hold an address" are the same reading, and asking twice made an
        everyday press a third slower than it needed to be.

        Kept as a ceiling rather than an equality: what matters is that nobody
        adds a third reading without noticing.
        """
        television.set(logical_address=0)

        cold = television.invocations
        modes.tv_button()
        cold = television.invocations - cold
        assert "0x36" in television.frames
        # The first press after a start also finds the television: one reading,
        # the claim, one poll, then a frame each for the power read and the
        # standby.
        assert cold <= 6, f"the first press ran cec-ctl {cold} times"

        television.switched_on_by_hand()
        warm = television.invocations
        modes.tv_button()
        warm = television.invocations - warm
        # Afterwards: one adapter reading and one frame, twice over. It was
        # three readings and two frames until the reading was shared — "is the
        # link up" and "do I hold an address" are the same reading, asked twice.
        assert warm <= 4, f"an ordinary press ran cec-ctl {warm} times"

    def test_the_music_button_switches_the_set_off(self, television):
        modes.music()
        television.forget_frames()
        modes.television(why="the music button", switch_off=True)
        assert television.frames == ["0x36"], television.frames
        assert television.power == "standby"


# ---------------------------------------------------------------------------
# What the journal will say afterwards
# ---------------------------------------------------------------------------

class TestWhatTheJournalSays:
    """Somebody reads this days later, about an evening nobody was watching.

    The journal used to record that a technique had been sent and nothing about
    whether it reached the television — which is exactly how an evening of
    presses that did nothing looked like an evening of presses that worked.
    """

    def test_every_frame_is_logged_with_what_became_of_it(self, television, caplog):
        with caplog.at_level("INFO"):
            modes.tv_button()
        assert "cec --to 0 --standby -> sent" in caplog.text, caplog.text

    def test_a_frame_nobody_took_says_so(self, television, caplog):
        """The set has to move *after* the box has found it, or there is no
        wrong address to send to: a press that starts by looking never gets it
        wrong."""
        modes.tv_button()
        television.switched_on_by_hand()
        television.moves_to(14)
        with caplog.at_level("INFO"):
            modes.tv_button()
        assert "-> not acknowledged" in caplog.text, caplog.text
        assert "the television has moved from logical address 0 to 14" in caplog.text

    def test_turning_it_up_records_everything_the_tool_said(self, television, caplog):
        """The cheapest way to record a session: no second process on the bus.

        Driven through the setting rather than through the logging module,
        because that is what somebody with Postman actually does — and because
        the point of the setting is that it needs no restart.
        """
        with caplog.at_level("DEBUG"):
            modes.tv_button()
            assert "Transmit from" not in caplog.text, \
                "INFO already prints everything, so the setting proves nothing"
            caplog.clear()
            log_config.set_cec_level("DEBUG")
            modes.tv_button()
        assert "Transmit from" in caplog.text, \
            "turning it up recorded nothing more"

    def test_turning_it_back_down_takes_effect_at_once_too(self, television, caplog):
        log_config.set_cec_level("DEBUG")
        log_config.set_cec_level("INFO")
        with caplog.at_level("DEBUG"):
            modes.tv_button()
        assert "Transmit from" not in caplog.text
        assert "cec --to 0" in caplog.text, "the one line per frame must stay"


# ---------------------------------------------------------------------------
# A television that moves
# ---------------------------------------------------------------------------

class TestASetThatMoves:
    def test_the_box_finds_a_set_that_listens_on_14(self, television):
        television.moves_to(14)
        modes.tv_button()
        assert television.power == "standby"
        assert television.addressed_to() == {0, 14}, \
            "it should have tried 0, been refused, and found 14"

    def test_a_set_that_moves_between_presses_is_followed(self, television):
        modes.tv_button()
        assert television.power == "standby"
        television.switched_on_by_hand()
        television.moves_to(14)
        television.forget_frames()
        modes.tv_button()
        assert television.power == "standby", \
            "the box kept talking to an address nothing was listening on"

    def test_the_frames_reach_the_set_at_its_new_address(self, television):
        modes.music()
        television.moves_to(14)
        television.forget_frames()
        cec.forget_the_television()
        modes.television(why="the music button", switch_off=True)
        received = tm.load(str(television.path))["received"]
        standby = [frame for frame in received if frame["key"] == "0x36"]
        assert standby and standby[-1]["to"] == 14, received


# ---------------------------------------------------------------------------
# A television that takes its HDMI link away
# ---------------------------------------------------------------------------

class TestASetThatTakesItsLinkAway:
    def test_the_box_notices_and_brings_it_back(self, television, caplog):
        television.takes_its_link_away()
        with caplog.at_level("WARNING"):
            modes.tv_button()
        assert "cec link down" in caplog.text
        assert "cec link back" in caplog.text
        assert television.power == "standby", \
            "the frame should have landed once the link was back"

    def test_a_link_that_cannot_be_brought_back_says_so(self, television, caplog):
        television.set(hotplug=False, recovers_link_when_the_output_wakes=False)
        with caplog.at_level("WARNING"):
            modes.tv_button()
        assert "cec link still down" in caplog.text
        assert "switched on by hand" in caplog.text

    def test_the_release_still_delivers_when_the_output_sleeping_kills_the_link(
            self, television):
        """The fault that made a release do nothing in silence.

        The first cec-ctl version claimed the adapter's logical address when a
        frame was due — by then the output had gone to sleep, the claim timed
        out, and the standby and wake went out as Unregistered and were never
        acknowledged. Claimed before the output sleeps, they land.

        `drops_link_when_the_output_sleeps` is a hypothesis about this set, not
        a measured fact — see television_model.py. It is asked for by name here
        because it is the condition under which the ordering matters.
        """
        television.set(drops_link_when_the_output_sleeps=True)
        modes.music()
        television.forget_frames()
        modes.television(why="the tv button", switch_off=False)
        assert "0x36" in television.frames, \
            "the standby never reached the set"
        assert "0x0d" in television.frames, \
            "the wake never reached the set"


# ---------------------------------------------------------------------------
# The whole detection procedure, against the model set
# ---------------------------------------------------------------------------

def _an_edid(manufacturer: str = "HIO", product: int = 0x3231,
             name: str = "HIGH ONE") -> bytes:
    """A 128-byte EDID of the shape the kernel exposes.

    Built rather than recorded because this set's EDID was never readable — it
    read 0 bytes all evening. What is exercised here is the box's parsing, which
    is the part a test can own.
    """
    block = bytearray(128)
    assert len(manufacturer) == 3, "EDID packs exactly three letters"
    packed = sum((ord(letter) - 64) << shift
                 for letter, shift in zip(manufacturer, (10, 5, 0), strict=True))
    block[8:10] = packed.to_bytes(2, "big")
    block[10:12] = product.to_bytes(2, "little")
    block[54:59] = b"\x00\x00\x00\xfc\x00"
    block[59:72] = name.encode("ascii").ljust(13, b"\n")
    return bytes(block)


class Viewer:
    """Somebody in front of the television, answering what they see.

    No answer is scripted. Each one is read off the model set's own state and
    compared with what it was before the frame went out — so if the box sends
    the wrong frame, this person sees nothing happen and says so, exactly as in
    the room. That is what makes the procedure's outcome mean something rather
    than restate the test's own expectations.
    """

    def __init__(self, television: Television):
        self.television = television
        self.phase = None
        self.before = None
        self.pages = []

    def watch(self) -> None:
        self.before = (self.television.power, self.television.input)

    def _something_changed(self) -> "tuple[str, str]":
        return (self.television.power, self.television.input)

    def ask(self, body, accepts, seconds=None, title=None, step=None):
        self.pages.append(body)
        offered = set(accepts.values())

        if offered == {"done"}:
            return cec_detection.YES

        if offered == {"ready"}:
            self._do_what_the_page_asks(body)
            return cec_detection.YES

        if offered == {"it happened", "nothing"}:
            return self._did_it(body)

        if offered == {"got them back", "still nothing"}:
            return (cec_detection.YES if self.television.input == "tuner"
                    else cec_detection.NO)

        # A failure page: go on rather than loop for ever.
        if offered == {"try again", "go on without it"}:
            return cec_detection.NO
        if offered == {"try again", "give up"}:
            return cec_detection.NO
        if offered == {"save", "start over"}:
            return cec_detection.YES
        raise AssertionError(f"the viewer does not know this question: {accepts}")

    def _do_what_the_page_asks(self, body: str) -> None:
        if "Turn the TV off with its own remote" in body:
            self.phase = "wake"
            self.television.switched_off_by_hand()
        elif "Switch back to your programmes with your remote" in body:
            self.phase = "sleep"
            self.television.switched_on_by_hand()
        elif "GIVING YOU BACK YOUR PROGRAMMES" in body:
            self.phase = "release"
            # The page says to switch it back on and select the box's input, so
            # this is the viewer doing as it is told — not the fixture quietly
            # arranging the state the procedure needs. It reads the page to
            # prove the page says it.
            assert "Switch the TV back on" in body, \
                "the page no longer asks for the set the search needs"
            assert "HDMI input" in body
            self.television.set(power="on", input="box")
        elif "SECOND TRY" in body:
            self.phase = "release"
            # Already on the box's input: the failed search above left it there.
        else:
            raise AssertionError(f"the viewer does not know this page: {body[:60]}")

    def _did_it(self, body: str) -> str:
        power, screen_input = self._something_changed()
        was_power, _ = self.before or (power, screen_input)
        if self.phase == "wake":
            happened = was_power == "standby" and power == "on"
        elif self.phase == "sleep":
            happened = was_power == "on" and power == "standby"
        else:
            happened = screen_input == "tuner"
        return cec_detection.YES if happened else cec_detection.NO


class WatchedHardware:
    """The real hardware bridge, with the viewer told when to look.

    `modes._InstallationHardware` is used unchanged — the frames really go
    through the transport and the fake `cec-ctl` — and only `ask` and the
    look-before-each-frame hook belong to the test.
    """

    def __init__(self, viewer: Viewer):
        self.viewer = viewer
        self.hardware = modes._InstallationHardware()

    def ask(self, *args, **kwargs):
        return self.viewer.ask(*args, **kwargs)

    def abandoned(self) -> bool:
        return False

    def __getattr__(self, name):
        attribute = getattr(self.hardware, name)
        if name in ("send_wake", "send_sleep", "send_release"):
            def send(technique):
                self.viewer.watch()
                return attribute(technique)
            return send
        if name == "begin_release_power_cycle":
            def begin():
                self.viewer.watch()
                return attribute()
            return begin
        return attribute


class TestTheWholeDetectionAgainstTheModelSet:
    """What the procedure would have concluded on this television.

    It was never run on it. This is the nearest thing to running it: the real
    procedure, the real mode machine, the real transport, a fake `cec-ctl`, and
    a set that reacts only as it was measured to react.
    """

    @pytest.fixture
    def procedure(self, television, tmp_path, monkeypatch):
        edid = tmp_path / "drm" / "card0-HDMI-A-1"
        edid.mkdir(parents=True)
        (edid / "edid").write_bytes(_an_edid())
        monkeypatch.setattr(cec, "EDID_PATHS", str(edid / "edid"))
        state.set_mode(state.INSTALLATION)
        viewer = Viewer(television)
        yield viewer, WatchedHardware(viewer)
        state.set_mode(state.TELEVISION)

    def test_it_finds_the_three_techniques_the_set_actually_obeys(self, procedure,
                                                                 television):
        viewer, hardware = procedure
        result = cec_detection.run(hardware)
        assert result.completed, "the procedure did not reach the end"
        assert result.wake == "text_view_on", \
            f"wake came out as {result.wake!r}"
        assert result.sleep == "standby", f"sleep came out as {result.sleep!r}"
        assert result.release == "power_cycle", \
            f"release came out as {result.release!r}"

    def test_it_writes_what_it_found(self, procedure):
        viewer, hardware = procedure
        cec_detection.run(hardware)
        written = tv_config.load()
        assert written["wake"]["technique"] == "text_view_on"
        assert written["sleep"]["technique"] == "standby"
        assert written["release"]["technique"] == "power_cycle"
        assert written["detection_complete"] is True

    def test_it_reads_the_set_identity_from_the_edid(self, procedure):
        viewer, hardware = procedure
        result = cec_detection.run(hardware)
        assert result.television["name"] == "HIGH ONE"
        assert result.television["manufacturer"] == "HIO"
        assert result.television["cec_version"] == "CEC version 1.4"

    def test_the_wake_search_reaches_the_second_candidate(self, procedure,
                                                          television):
        """`image_view_on` is first in the table and this set is not modelled as
        obeying it, so the search has to carry on to `text_view_on`. If the
        first candidate ever silently "worked" — because a library answered
        behind it — this is the test that would notice."""
        viewer, hardware = procedure
        cec_detection.run(hardware)
        sent = [frame["key"] for frame in television.attempts]
        assert sent.index("0x04") < sent.index("0x0d")

    def test_the_release_only_lands_on_the_power_cycle_after_the_frames_fail(
            self, procedure, television):
        viewer, hardware = procedure
        cec_detection.run(hardware)
        sent = [frame["key"] for frame in television.attempts]
        for refused in ("0x9d", "0x86", "0x80", "0x44:30"):
            assert refused in sent, f"{refused} was never tried"

    def test_nothing_claimed_the_input_before_the_summary(self, procedure,
                                                          television):
        """The procedure claims the input once, deliberately, at the very end —
        so the summary is drawn where somebody can read it. Anything claiming it
        earlier would corrupt the release measurement."""
        viewer, hardware = procedure
        cec_detection.run(hardware)
        sent = [frame["key"] for frame in television.attempts]
        assert sent.count("0x82") == 1, sent
        assert sent[-1] == "0x82", sent
