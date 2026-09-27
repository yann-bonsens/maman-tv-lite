# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import cec_controller as cec


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """No test should actually wait: some functions sleep up to ~45 s in real
    conditions."""
    monkeypatch.setattr(cec.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def no_standby_carried_over(monkeypatch):
    """When the box last switched the set off is module-level state, as it must
    be — the two presses that care about it are minutes apart. Left over from
    the previous test, it makes a wake hold back for a reason belonging to
    another test entirely."""
    monkeypatch.setattr(cec, "_sent_to_standby_at", None)


def _completed(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


class TestGetPowerStatus:
    @pytest.mark.parametrize(
        "output,expected",
        [
            ("REPORT_POWER_STATUS (0x90):\n\tpwr-state: on (0x00)\n", "on"),
            ("\tpwr-state: standby (0x01)", "standby"),
            # A set that is on its way somewhere is not there yet, and is
            # deliberately not rounded to either end: only an explicit "on"
            # counts as on anywhere in this project.
            ("\tpwr-state: to-standby (0x03)", "to-standby"),
            ("\tPWR-STATE: ON (0x00)", "on"),
            ("no matching line here", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_parses_status(self, output, expected):
        with patch("cec_controller._run", return_value=output):
            assert cec.get_power_status() == expected
class TestPowerToggle:
    """The toggle lives in cec_controller so the /tv/toggle endpoint and the
    Zigbee buttons share one implementation."""

    def test_turns_off_when_on(self):
        with patch("cec_controller.get_power_status", return_value="on"), patch(
            "cec_controller.standby"
        ) as standby, patch("cec_controller.power_on") as power_on:
            result = cec.power_toggle()
        standby.assert_called_once()
        power_on.assert_not_called()
        assert result == {"action": "toggle-off", "status_before": "on"}

    @pytest.mark.parametrize(
        "status", ["standby", "in transition from on to standby", "unknown"]
    )
    def test_turns_on_for_any_other_state(self, status):
        """Turning on an already-on TV does nothing, which makes it the safest
        action when the state is ambiguous."""
        with patch("cec_controller.get_power_status", return_value=status), patch(
            "cec_controller.standby"
        ) as standby, patch("cec_controller.power_on") as power_on:
            result = cec.power_toggle()
        power_on.assert_called_once()
        standby.assert_not_called()
        assert result["action"] == "toggle-on"
class TestTheFramesActuallySent:
    def test_image_view_on_sends_the_frame_itself(self):
        """Regression: libCEC's `on 0` interrogates the set first and, against
        one that answers nothing, gave up before ever sending 0x04 — so the
        best technique in the table was never really tried."""
        with patch("cec_controller._run") as run:
            cec._wake_image_view_on()
        assert run.call_args[0][0] == "tx 10:04"

    def test_the_techniques_that_claim_the_input_come_last(self):
        """Claiming it makes libCEC re-announce itself, and that announcement
        carries Image View On: captured turning a set off and straight back on.
        A television that does not need the claim must never see it."""
        names = [name for name, _ in cec.SLEEP_TECHNIQUES]
        claiming = [n for n in names if n.endswith("after_active_source")]
        assert claiming, "the variants still exist for sets that need them"
        # Every technique that disturbs nothing is tried first. Only the
        # directionless toggle, itself a last resort, may come after them.
        clean = [n for n in names if n not in claiming and n != "power_toggle_key"]
        assert max(names.index(n) for n in clean) < min(names.index(n) for n in claiming)
class TestATogglePlacedLast:
    """Power Toggle (0x6B) is the one frame in the tables with no direction of
    its own. A set that obeys it can just as easily be sent the wrong way, so
    it must never be reached while a directional technique is still untried."""

    def test_it_is_the_last_resort_in_both_directions(self):
        for table in (cec.WAKE_TECHNIQUES, cec.SLEEP_TECHNIQUES):
            names = [name for name, _ in table]
            assert names[-1] == "power_toggle_key"

    def test_the_plain_frames_are_still_tried_first(self):
        """Ordering is a statement about quality: what disturbs nothing wins."""
        assert [n for n, _ in cec.SLEEP_TECHNIQUES][0] == "standby"
        assert [n for n, _ in cec.WAKE_TECHNIQUES][0] == "image_view_on"


class TestTheBoxesOwnPhysicalAddress:
    """It says which HDMI socket the box is plugged into, and two frames
    carry it: Inactive Source and Routing Change.

    It used to be hard-coded to the first socket, with a comment asking
    whoever moved the cable to edit the source. On any other socket those
    frames named an address that was not the box's — so a detection run could
    reject a release technique that would have worked, or credit one that had
    not. The value was already read a few lines away, for the report.
    """

    def setup_method(self):
        cec._own_address = None

    def teardown_method(self):
        cec._own_address = None

    def test_the_first_socket_reads_as_the_frame_bytes(self):
        assert cec._address_as_operands("1.0.0.0") == "10:00"

    def test_a_different_socket_gives_a_different_address(self):
        assert cec._address_as_operands("2.0.0.0") == "20:00"
        assert cec._address_as_operands("3.1.0.0") == "31:00"

    def test_an_unconfigured_adapter_is_refused(self):
        """f.f.f.f is what an adapter reports when it has no address at all;
        broadcasting it would name every device at once."""
        assert cec._address_as_operands("f.f.f.f") is None
        assert cec._address_as_operands("unknown") is None
        assert cec._address_as_operands("") is None

    def test_it_is_read_from_the_adapter(self, monkeypatch):
        monkeypatch.setattr(cec, "adapter_state",
                            lambda: {"physical_address": "2.0.0.0"})
        assert cec.own_physical_address() == "20:00"

    def test_an_adapter_that_says_nothing_falls_back_to_the_first_socket(self, monkeypatch):
        """A frame with a plausible address beats no frame at all."""
        monkeypatch.setattr(cec, "adapter_state", lambda: {"physical_address": "f.f.f.f"})
        assert cec.own_physical_address() == cec.FALLBACK_PHYSICAL_ADDRESS

    def test_a_failed_reading_is_not_remembered(self, monkeypatch):
        """A box that started before the television was reachable must pick
        the real address up later, not stay on the fallback for ever."""
        monkeypatch.setattr(cec, "adapter_state", lambda: {"physical_address": "unknown"})
        assert cec.own_physical_address() == cec.FALLBACK_PHYSICAL_ADDRESS
        monkeypatch.setattr(cec, "adapter_state", lambda: {"physical_address": "2.0.0.0"})
        assert cec.own_physical_address() == "20:00"

    def test_the_frames_that_name_the_box_use_it(self, monkeypatch):
        sent = []
        monkeypatch.setattr(cec, "_run", lambda command, *a, **kw: sent.append(command) or "")
        monkeypatch.setattr(cec, "adapter_state", lambda: {"physical_address": "2.0.0.0"})
        cec._release_inactive_source()
        cec._release_routing_change()
        assert sent == ["tx 1F:9d:20:00", "tx 1F:80:20:00:00:00"]


# The real output of `cec-ctl -d /dev/cec0` on the board, captured
# 2026-09-26. The indented bare words under "Capabilities" are the point:
# one of them is literally "Logical Addresses", with no colon at all.
CEC_CTL_OUTPUT = """Driver Info:
\tDriver Name                : vc4_hdmi
\tAdapter Name               : vc4-hdmi
\tCapabilities               : 0x0000031e
\t\tLogical Addresses
\t\tTransmit
\t\tPassthrough
\t\tRemote Control Support
\t\tConnector Info
\t\tReply Vendor ID
\tDriver version             : 6.18.34
\tAvailable Logical Addresses: 1
\tDRM Connector Info         : card 0, connector 35
\tPhysical Address           : 1.0.0.0
\tLogical Address Mask       : 0x0002
\tCEC Version                : 1.4
\tVendor ID                  : 0x001582 (Pulse-Eight)
\tOSD Name                   : ''
\tLogical Addresses          : 1 (Allow Fallback to Unregistered)
"""


class TestReadingTheAdapter:
    """`adapter_state()` parses what `cec-ctl` prints, and it used to do it
    by asking whether a substring appeared anywhere in the line.

    "Logical Addresses" appears as a bare capability with no colon, so the
    split had nothing to its right and this raised IndexError. It only ever
    ran from /report — until `own_physical_address()` started calling it,
    and then the exception ended a whole CEC detection from inside a search,
    mid-step, leaving somebody in front of a black screen.
    """

    def _run(self, monkeypatch, stdout):
        import subprocess
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: type("R", (), {"stdout": stdout})())
        return cec.adapter_state()

    def test_a_capability_line_with_no_colon_does_not_crash(self, monkeypatch):
        answer = self._run(monkeypatch, CEC_CTL_OUTPUT)
        assert answer["physical_address"] == "1.0.0.0"
        assert answer["ready"] is True

    def test_the_logical_addresses_field_wins_over_the_capability(self, monkeypatch):
        """"Available Logical Addresses: 1" matched the old substring test
        too, so which one survived depended on line order."""
        answer = self._run(monkeypatch, CEC_CTL_OUTPUT)
        assert answer["logical_addresses"] == "1 (Allow Fallback to Unregistered)"

    def test_reading_the_address_never_raises(self, monkeypatch):
        """It is called while composing an ordinary frame, on the everyday
        path. Anything escaping here does not fail one frame, it takes down
        whatever was running."""
        cec._own_address = None
        monkeypatch.setattr(cec, "adapter_state",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            assert cec.own_physical_address() == cec.FALLBACK_PHYSICAL_ADDRESS
        finally:
            cec._own_address = None


@pytest.fixture(autouse=True)
def a_working_hdmi_link(monkeypatch):
    """The adapter has an address, so nothing tries to repair the link.

    Its own repair is pinned in TestTheLinkComingBack; everywhere else it would
    only be a `cec-ctl` call nobody asked about.
    """
    monkeypatch.setattr(cec, "link_is_down", lambda reading=None: False)


class TestTheTransport:
    """cec-ctl, which sends the frame it is given and nothing else.

    It replaced libCEC on 2026-09-26 after two faults on one evening, both of
    them libCEC acting on its own and neither with a setting to turn it off.

    One: with bActivateSource it re-announces itself as the active source
    whenever the adapter's physical address comes back, and this television
    drops its HDMI hotplug at every power-on — so the set was dragged onto the
    box's input every single time, having been asked for nothing of the sort.

    Two: it can only address a television at logical address 0, and this one
    does not always live there. See TestWhereTheTelevisionIs.
    """

    @pytest.mark.parametrize("command,expected", [
        ("tx 10:04", ["--to", "0", "--image-view-on"]),
        ("tx 10:0d", ["--to", "0", "--text-view-on"]),
        ("tx 10:36", ["--to", "0", "--standby"]),
        ("tx 10:44:6d", ["--to", "0", "--user-control-pressed", "ui-cmd=0x6d"]),
        ("tx 10:44:6b", ["--to", "0", "--user-control-pressed", "ui-cmd=0x6b"]),
        ("tx 10:45", ["--to", "0", "--user-control-released"]),
        ("tx 1F:9d:10:00", ["--inactive-source", "phys-addr=1.0.0.0"]),
        ("tx 1F:86:00:00", ["--set-stream-path", "phys-addr=0.0.0.0"]),
        ("tx 1F:80:10:00:00:00",
         ["--routing-change",
          "orig-phys-addr=1.0.0.0,new-phys-addr=0.0.0.0"]),
        ("standby 0", ["--to", "0", "--standby"]),
        ("pow 0", ["--to", "0", "--give-device-power-status"]),
        ("ver 0", ["--to", "0", "--get-cec-version"]),
    ])
    def test_every_command_the_box_sends_has_an_equivalent(self, command, expected):
        """One table, checked frame by frame. A technique is a television's
        vocabulary and must not have to know which of the two tools carries
        it."""
        assert cec._arguments(command) == expected

    def test_the_active_source_claim_names_the_box_itself(self):
        with patch("cec_controller.own_physical_address", return_value="20:00"):
            assert cec._arguments("as") == ["--active-source",
                                                 "phys-addr=2.0.0.0"]

    @pytest.mark.parametrize("command", ["scan", "tx 10:ff", "", "on 0"])
    def test_anything_without_an_equivalent_raises_rather_than_vanishing(self, command):
        """A frame that never goes out while its caller is told it did is the
        failure this whole file exists to prevent."""
        with pytest.raises(cec.CECError):
            cec._arguments(command)

    def test_a_command_claims_the_bus_before_sending_on_the_warm_path(self):
        """With the television already found, `_run` still has to claim an
        address: the adapter loses it whenever the set drops its HDMI hotplug,
        and the address the box remembers says nothing about that.

        Pinned here and not in the simulation, which cannot see it — the model
        keeps an address once claimed, so a press that skipped the claim looks
        identical there.
        """
        claims = []
        with patch("cec_controller.take_the_bus",
                   side_effect=lambda reading=None: claims.append(reading)), \
             patch("cec_controller.adapter_state",
                   return_value={"physical_address": "1.0.0.0",
                                 "logical_address_mask": "0x0002"}), \
             patch("cec_controller._cec_ctl", return_value="Transmit from x"):
            cec._tv_address = 0
            cec._run("tx 10:36")
        assert claims, "the box sent a frame without claiming an address"

    def test_a_command_claims_the_bus_before_sending(self):
        """The adapter loses its address whenever the set drops its hotplug, so
        every frame has to be able to get it back."""
        with patch("cec_controller.take_the_bus") as claim, \
             patch("cec_controller._cec_ctl", return_value="") as ctl:
            cec._run("tx 10:36")
        claim.assert_called_once()
        assert ctl.call_args[0][0] == ["--to", "0", "--standby"]

    def test_a_frame_nobody_acknowledged_is_re_addressed_and_sent_again(self):
        """The 2026-09-26 outage: the set had moved to logical address 14 and
        every frame to 0 went unacknowledged, with the box reporting success on
        every press because it had sent what it meant to send."""
        sent = []

        def ctl(arguments):
            sent.append(arguments)
            # The first frame, addressed to 0, is refused by an empty bus.
            return "Tx, Not Acknowledged (4), Max Retries" if len(sent) == 1 else "ok"

        with patch("cec_controller._cec_ctl", side_effect=ctl), \
             patch("cec_controller.take_the_bus"), \
             patch("cec_controller.find_the_television",
                   side_effect=lambda: _move_the_television_to(14)):
            assert cec._run("tx 10:36") == "ok"
        assert sent[0] == ["--to", "0", "--standby"]
        assert sent[-1] == ["--to", "14", "--standby"], \
            "the retry must go to where the television was found"

    def test_a_silent_television_is_not_retried_in_a_loop(self):
        """A set that is off answers nothing at all, and a press must not turn
        into a minute of retries."""
        with patch("cec_controller._cec_ctl",
                   return_value="Tx, Not Acknowledged (4), Max Retries") as ctl, \
             patch("cec_controller.take_the_bus"), \
             patch("cec_controller.find_the_television", return_value=0):
            cec._run("tx 10:36")
        assert ctl.call_count == 1

    def test_a_logical_address_is_claimed_only_when_nothing_holds_one(self):
        """An unregistered sender can still transmit — measured on the real set,
        which obeyed a Text View On from Unregistered — but no reply can come
        back to an address that does not exist, and `pow` is a reply."""
        held = {"logical_address_mask": "0x0002"}
        with patch("cec_controller.adapter_state", return_value=held), \
             patch("cec_controller._cec_ctl", return_value="") as ctl:
            cec.take_the_bus()
        ctl.assert_not_called()

        none_held = {"logical_address_mask": "0x0000"}
        with patch("cec_controller.adapter_state", return_value=none_held), \
             patch("cec_controller._cec_ctl", return_value="") as ctl:
            cec.take_the_bus()
        assert ctl.call_args[0][0] == cec.REGISTRATION

    def test_a_failed_claim_does_not_stop_the_frame(self):
        """A box that cannot register still transmits as Unregistered. Sending
        nothing is strictly worse."""
        with patch("cec_controller._ensure_a_logical_address",
                   side_effect=cec.CECError("no")):
            cec.take_the_bus()

    def test_the_claim_keeps_the_identity_the_television_already_knows(self):
        """Recording Device 1, which is what libCEC registered as. Registering
        as something else makes the box turn up as a new device on the set."""
        assert cec.REGISTRATION[:1] == ["--record"]
        assert cec.OSD_NAME in cec.REGISTRATION

    def test_nothing_starts_libcec_any_more(self):
        source = Path(cec.__file__).read_text()
        for gone in ("_run_sequence", "_capture_bus_traffic",
                     "_send_and_capture_reply", "_Session", "_ensure_session",
                     "_drop_session", "_run_oneshot", "_base_args"):
            assert gone not in source, f"{gone} came back"
        assert 'subprocess.run(["cec-ctl"' in source or '"cec-ctl", "-d"' in source
        assert "Popen" not in source, "nothing here owns a process any more"


def _move_the_television_to(address: int) -> int:
    cec._tv_address = address
    return address


class TestWhereTheTelevisionIs:
    """A television is normally logical address 0, and the box assumed it for
    months. This one moves: measured on 2026-09-26 it answered on 0 all
    evening, then came back on 14 after a power cycle, and the box went
    completely inert — the set would not switch off, showed "no signal", and
    every press was reported as a success.

    14 is legal: the specification gives a television 0 and 14, and a set that
    finds 0 taken uses the other.
    """

    def test_the_address_is_polled_for_and_not_assumed(self):
        answered = {"14"}

        def ctl(arguments):
            if "--to" not in arguments:   # claiming an address of our own
                return ""
            address = arguments[arguments.index("--to") + 1]
            return ("Transmit from x to y: POLL" if address in answered
                    else "Transmit from x to y:\n\tTx, Not Acknowledged (2)")

        with patch("cec_controller._cec_ctl", side_effect=ctl):
            cec.forget_the_television()
            assert cec.tv_logical_address() == 14

    def test_zero_is_preferred_when_both_answer(self):
        """Not a beauty contest: a poll of 0 that answers is the ordinary case,
        and trying it first is what keeps the usual box at one poll."""
        with patch("cec_controller._cec_ctl", return_value="Transmit from x to y: POLL"):
            cec.forget_the_television()
            assert cec.tv_logical_address() == 0

    def test_a_set_that_answers_nothing_falls_back_rather_than_raising(self):
        """A box whose television is unplugged must still answer a press."""
        with patch("cec_controller._cec_ctl",
                   return_value="Transmit from x to y:\n\tTx, Not Acknowledged (2)"):
            cec.forget_the_television()
            assert cec.tv_logical_address() == cec.DEFAULT_TV_LOGICAL_ADDRESS

    def test_a_poll_that_never_went_out_is_not_an_answer(self):
        """With no logical address claimed, `cec-ctl --to 0 --poll` transmits
        nothing and reports nothing: no poll, no error. Read as "no failure",
        that looked exactly like a television answering, and the box announced
        it had found one at address 0 while the real set sat at 14 and answered
        nothing at all."""
        printed_when_nothing_was_sent = (
            "\tLogical Address Mask       : 0x0000\n"
            "\tLogical Addresses          : 0 (Allow RC Passthrough)\n")
        with patch("cec_controller._cec_ctl",
                   return_value=printed_when_nothing_was_sent):
            cec.forget_the_television()
            assert cec.tv_logical_address() == cec.DEFAULT_TV_LOGICAL_ADDRESS
            assert cec._tv_address is None, \
                "a poll nobody heard must not be remembered as a television"

    def test_an_address_of_our_own_is_claimed_before_polling(self):
        """Otherwise every poll is a question nobody was asked."""
        order = []
        with patch("cec_controller.take_the_bus",
                   side_effect=lambda reading=None: order.append("claim")), \
             patch("cec_controller._cec_ctl",
                   side_effect=lambda a: order.append("poll") or "Transmit from x: POLL"):
            cec.forget_the_television()
            cec.find_the_television()
        assert order[0] == "claim", order

    def test_a_poll_that_cannot_even_run_is_not_fatal(self):
        with patch("cec_controller._cec_ctl", side_effect=cec.CECError("no tool")):
            cec.forget_the_television()
            assert cec.tv_logical_address() == cec.DEFAULT_TV_LOGICAL_ADDRESS

    def test_it_is_remembered_rather_than_polled_before_every_frame(self):
        """A poll before each frame would double what a press costs."""
        with patch("cec_controller._cec_ctl", return_value="Transmit from x to y: POLL") as ctl:
            cec.forget_the_television()
            cec.tv_logical_address()
            calls = ctl.call_count
            cec.tv_logical_address()
            cec.tv_logical_address()
        assert ctl.call_count == calls

    def test_the_frames_follow_the_television(self):
        """Every frame, not just the ones built by hand: the header written in
        the source is the vocabulary, the destination is measured."""
        _move_the_television_to(14)
        assert cec._arguments("tx 10:36") == ["--to", "14", "--standby"]
        assert cec._arguments("pow") == ["--to", "14",
                                        "--give-device-power-status"]
        assert cec._tv_frame_prefix() == "1e"

    def test_a_broadcast_frame_does_not_follow_it(self):
        """Broadcast is broadcast wherever the set lives."""
        _move_the_television_to(14)
        assert "--to" not in cec._arguments("tx 1F:86:00:00")

    def test_the_api_leaving_forgets_it(self):
        _move_the_television_to(14)
        cec.shutdown()
        assert cec._tv_address is None


class TestWhatTheBoxReadsBack:
    """cec-ctl spells its answers its own way, and two of them are load
    bearing."""

    def test_the_adapter_reports_whether_an_address_is_actually_held(self):
        """"Logical Addresses: 1" is printed even when the mask is 0x0000 and
        nothing is allocated at all — measured with the set's hotplug down.
        The mask is the one that answers "can this box send anything"."""
        printed = ("\tPhysical Address           : 1.0.0.0\n"
                   "\tLogical Address Mask       : 0x0000\n"
                   "\tLogical Addresses          : 1 (Allow RC Passthrough)\n")
        with patch("cec_controller.subprocess.run", return_value=_completed(printed)):
            reading = cec.adapter_state()
        assert reading["logical_address_mask"] == "0x0000"
        assert reading["logical_addresses"] == "1 (Allow RC Passthrough)"

    def test_the_cec_version_reads_as_it_always_did(self):
        """cec-ctl says "cec-version: version-1-4 (0x05)"; libCEC said "CEC
        version 1.4". `profiles/SAMSUNG_0x7590.json` was measured through
        libCEC and carries the old spelling, so the new transport must render
        the old one or a box stops recognising the profile written for the
        television in front of it."""
        assert cec._parse_cec_version("\tcec-version: version-1-4 (0x05)") \
            == "CEC version 1.4"
        assert cec._parse_cec_version("nothing recognisable") is None


class TestTheLinkComingBack:
    """With no physical address the box is deaf and mute, and said nothing
    about it: every press was reported a success while no frame could leave at
    all. Measured at the installation site — forcing the connector to look
    again did nothing, and waking the box's own output brought the address back
    within seconds, because the set re-establishes the link when something is
    driving it.
    """

    def test_no_physical_address_means_the_link_is_down(self, monkeypatch):
        monkeypatch.undo()
        for reading in ("f.f.f.f", "unknown"):
            with patch("cec_controller.adapter_state",
                       return_value={"physical_address": reading}):
                assert cec.link_is_down()
        with patch("cec_controller.adapter_state",
                   return_value={"physical_address": "1.0.0.0"}):
            assert not cec.link_is_down()

    def test_a_frame_waits_for_the_link_to_be_repaired(self, monkeypatch):
        order = []
        monkeypatch.setattr(cec, "link_is_down", lambda reading=None: True)
        monkeypatch.setattr(cec, "bring_the_link_back",
                            lambda: order.append("repair") or True)
        monkeypatch.setattr(cec, "take_the_bus", lambda reading=None: None)
        with patch("cec_controller._cec_ctl",
                   side_effect=lambda a: order.append("send") or ""):
            cec._run("tx 10:36")
        assert order[0] == "repair", order
        assert "send" in order

    def test_the_output_is_handed_straight_back(self, monkeypatch):
        """It was asleep because the default state wants it asleep — that is
        what stops a set which remembers its last input from coming back on the
        box. The repair borrows it and nothing more."""
        import hdmi_output
        slept = []
        monkeypatch.setattr(cec, "link_is_down", lambda reading=None: True)
        monkeypatch.setattr(cec, "bring_the_link_back", lambda: True)
        monkeypatch.setattr(cec, "take_the_bus", lambda reading=None: None)
        monkeypatch.setattr(hdmi_output, "sleep", lambda why: slept.append(why))
        with patch("cec_controller._cec_ctl", return_value=""):
            cec._run("tx 10:36")
        assert slept, "the output was left awake after the repair"

    def test_every_branch_says_so_in_the_journal(self, monkeypatch, caplog):
        """One grep over a journal has to answer "did the television take its
        link away, and how often". All three lines begin "cec link"."""
        import hdmi_output
        monkeypatch.setattr(cec, "_link_down_seen", 0)
        monkeypatch.setattr(cec, "_link_recovered", 0)
        monkeypatch.setattr(hdmi_output, "state", lambda: hdmi_output.ASLEEP)
        monkeypatch.setattr(hdmi_output, "wake", lambda why: True)

        addresses = iter(["f.f.f.f", "1.0.0.0"])
        with caplog.at_level("WARNING"), \
             patch("cec_controller.adapter_state",
                   side_effect=lambda: {"physical_address": next(addresses)}):
            assert cec.bring_the_link_back() is True
        recovered = caplog.text
        assert "cec link down" in recovered
        assert "cec link back" in recovered
        assert "1.0.0.0" in recovered

        caplog.clear()
        with caplog.at_level("WARNING"), \
             patch("cec_controller.adapter_state",
                   return_value={"physical_address": "f.f.f.f"}):
            assert cec.bring_the_link_back() is False
        assert "cec link still down" in caplog.text
        assert "switched on by hand" in caplog.text

    def test_the_branch_that_can_do_nothing_is_the_loudest(self, monkeypatch, caplog):
        """It used to return in silence, which made the one situation the box
        cannot get itself out of the one situation it said nothing about."""
        import hdmi_output
        monkeypatch.setattr(hdmi_output, "state", lambda: hdmi_output.AWAKE)
        with caplog.at_level("WARNING"), \
             patch("cec_controller.adapter_state",
                   return_value={"physical_address": "f.f.f.f"}):
            assert cec.bring_the_link_back() is False
        assert "cec link down" in caplog.text
        assert "nothing left to try" in caplog.text

    def test_how_often_it_happened_is_carried_by_the_report(self, monkeypatch):
        """`/report` is where somebody looks days later, about an evening nobody
        was watching."""
        import hdmi_output
        monkeypatch.setattr(cec, "_link_down_seen", 0)
        monkeypatch.setattr(cec, "_link_recovered", 0)
        monkeypatch.setattr(hdmi_output, "state", lambda: hdmi_output.ASLEEP)
        monkeypatch.setattr(hdmi_output, "wake", lambda why: True)
        addresses = iter(["f.f.f.f", "1.0.0.0"])
        with patch("cec_controller.adapter_state",
                   side_effect=lambda: {"physical_address": next(addresses)}):
            cec.bring_the_link_back()
        printed = "\tPhysical Address           : 1.0.0.0\n"
        with patch("cec_controller.subprocess.run", return_value=_completed(printed)):
            reading = cec.adapter_state()
        assert reading["link_down_seen"] == 1
        assert reading["link_recovered"] == 1

    def test_a_television_that_moves_says_so(self, monkeypatch, caplog):
        """Said where the move is observed: on the frame that came back
        unacknowledged, which is the only moment the box knows both where it was
        looking and where the set actually is."""
        monkeypatch.setattr(cec, "take_the_bus", lambda reading=None: None)
        answered = {"14"}

        def ctl(arguments):
            if "--to" not in arguments:
                return ""
            address = arguments[arguments.index("--to") + 1]
            return ("Transmit from x to y: POLL" if address in answered
                    else "Transmit from x to y:\n\tTx, Not Acknowledged (2)")

        with caplog.at_level("WARNING"), patch("cec_controller._cec_ctl", side_effect=ctl):
            cec._tv_address = 0
            cec._run("tx 10:36")
        assert "moved from logical address 0 to 14" in caplog.text

    def test_an_output_already_awake_is_not_touched(self, monkeypatch):
        """Entering a mode wakes it first, and there is nothing left to try."""
        import hdmi_output
        monkeypatch.setattr(hdmi_output, "state", lambda: hdmi_output.AWAKE)
        monkeypatch.setattr(hdmi_output, "wake",
                            lambda why: pytest.fail("woke an awake output"))
        assert cec.bring_the_link_back() is False


class TestRunningCecCtl:
    """The one place a process is started. Its failures were covered for
    libCEC and had to be re-covered here: a missing tool or a hung adapter must
    read as a CECError, which `/tv/*` turns into a clear message rather than a
    traceback."""

    def test_it_concatenates_what_the_tool_printed(self):
        with patch("cec_controller.subprocess.run",
                   return_value=_completed("out", "err")):
            assert cec._cec_ctl(["--poll"]) == "outerr"

    def test_a_timeout_is_a_cec_error(self):
        expired = subprocess.TimeoutExpired(cmd="cec-ctl", timeout=15)
        with patch("cec_controller.subprocess.run", side_effect=expired):
            with pytest.raises(cec.CECError):
                cec._cec_ctl(["--poll"])

    def test_a_missing_tool_is_a_cec_error(self):
        """v4l-utils is what provides cec-ctl; a box without it must say so
        rather than raise a FileNotFoundError out of an HTTP handler."""
        with patch("cec_controller.subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(cec.CECError):
                cec._cec_ctl(["--poll"])

    def test_the_adapter_is_named_when_there_is_one(self, monkeypatch):
        monkeypatch.setattr(cec, "CEC_DEVICE", "/dev/cec0")
        with patch("cec_controller.subprocess.run",
                   return_value=_completed()) as run:
            cec._cec_ctl(["--poll"])
        assert run.call_args[0][0] == ["cec-ctl", "-d", "/dev/cec0", "--poll"]

    def test_no_adapter_named_means_no_empty_argument(self, monkeypatch):
        """`CEC_DEVICE=""` is documented as "let the tool choose". Passed
        through as `-d ""` it asks for a device called "" and fails every single
        call."""
        monkeypatch.setattr(cec, "CEC_DEVICE", "")
        with patch("cec_controller.subprocess.run",
                   return_value=_completed()) as run:
            cec._cec_ctl(["--poll"])
        assert run.call_args[0][0] == ["cec-ctl", "--poll"]


class TestEveryTechniqueCanActuallyBeSent:
    """The detection procedure's whole vocabulary, checked against the
    transport.

    This is the test the move to cec-ctl needed and did not have. A technique
    whose frame the transport cannot render raises, and
    `_InstallationHardware._send` catches everything on purpose — so the
    procedure would not crash: it would send nothing, the person would see
    nothing happen, answer "nothing", and that technique would be invisible on
    every television for ever. Silently doing nothing while reporting success is
    the one failure this project refuses.
    """

    def _every_command(self):
        sent = []
        with patch("cec_controller._run", side_effect=lambda c: sent.append(c) or ""), \
             patch("cec_controller.own_physical_address", return_value="10:00"):
            for table in (cec.WAKE_TECHNIQUES, cec.SLEEP_TECHNIQUES,
                          cec.RELEASE_TECHNIQUES):
                for name, send in table:
                    before = len(sent)
                    send()
                    assert len(sent) > before, f"{name} sent no frame at all"
                    for command in sent[before:]:
                        yield name, command

    def test_every_technique_in_every_table_has_an_equivalent(self):
        checked = 0
        for name, command in self._every_command():
            try:
                arguments = cec._arguments(command)
            except cec.CECError as exc:
                raise AssertionError(
                    f"{name} sends {command!r}, which the transport cannot "
                    f"render: {exc}") from None
            assert arguments, f"{name} rendered to nothing"
            checked += 1
        assert checked >= 20, "the tables stopped being readable"

    def test_a_directed_frame_goes_to_the_television_and_a_broadcast_does_not(self):
        for name, command in self._every_command():
            arguments = cec._arguments(command)
            broadcast = command.startswith("tx 1F") or command == "as"
            if broadcast:
                assert "--to" not in arguments, f"{name} broadcast was directed"
            else:
                assert arguments[:2] == ["--to", str(cec.tv_logical_address())], \
                    f"{name} was not addressed to the television"
