# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""What `cec-ctl` really printed, and what the box concludes from it.

Every fragment below is verbatim output from a real adapter and a real
television — not a shape invented to match the parser. That distinction is the
whole point of this file: the parsers here have twice been correct about a
format nothing produces.

- `adapter_state()` matched "Logical Addresses" as a substring and hit the bare
  capability line, which has no colon at all; the IndexError took a whole CEC
  detection down mid-step.
- `_answers_a_poll()` read "no failure reported" as "somebody answered", and a
  poll that had never been transmitted looked exactly like a television
  acknowledging one. The box announced a set at logical address 0 while the real
  one sat at 14 and answered nothing.

Both were found on hardware, hours apart, and neither would have failed a test
written from the source.
"""

from unittest.mock import patch

import pytest

import cec_controller as cec


def _printed(text: str):
    """cec-ctl's output, as `_cec_ctl` hands it on."""
    return patch("cec_controller._cec_ctl", return_value=text)


# ---------------------------------------------------------------------------
# The adapter, as `cec-ctl -d /dev/cec0` describes it
# ---------------------------------------------------------------------------

HEALTHY_ADAPTER = """Driver Info:
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

# The same adapter with the television's HDMI link gone: no address at all, and
# nothing claimable. Note that "Logical Addresses" still says 1 while the mask
# says nothing is allocated — the two lines disagree on purpose, and only the
# mask answers "can this box send anything".
LINK_DOWN_ADAPTER = """Driver Info:
\tDriver Name                : vc4_hdmi
\tCapabilities               : 0x0000031e
\t\tLogical Addresses
\tAvailable Logical Addresses: 1
\tPhysical Address           : f.f.f.f
\tLogical Address Mask       : 0x0000
\tLogical Addresses          : 1 (Allow RC Passthrough)
\t  Logical Address          : Not Allocated
\t    Logical Address Type   : Record
"""


def _adapter(text: str):
    """`adapter_state()` runs cec-ctl itself rather than through `_cec_ctl`."""
    import subprocess
    done = subprocess.CompletedProcess(args=[], returncode=0, stdout=text, stderr="")
    return patch("cec_controller.subprocess.run", return_value=done)


class TestReadingTheRealAdapter:
    def test_a_healthy_adapter(self):
        """Two historical faults are covered by the exact values below, and had
        a test each until they were found to assert less than this one does:
        "Logical Addresses" appears under Capabilities with no colon at all,
        which an IndexError once turned into a dead CEC detection; and
        "Available Logical Addresses: 1" contains the same words and used to
        overwrite the real answer depending on line order."""
        with _adapter(HEALTHY_ADAPTER):
            reading = cec.adapter_state()
        assert reading["physical_address"] == "1.0.0.0"
        assert reading["logical_address_mask"] == "0x0002"
        assert reading["logical_addresses"] == "1 (Allow Fallback to Unregistered)"
        assert reading["ready"] is True

    def test_a_line_with_no_colon_contributes_nothing(self):
        """The guard that makes the reading independent of line order.

        `cec-ctl` lists "Logical Addresses" twice: once as a bare capability
        with no colon at all, and once as the real setting. It happens to print
        the bare one first, so the real one overwrites it and the reading is
        right either way — which is why the two tests that used to sit here
        passed with the guard removed and proved nothing. Appending the bare
        line last is a contrived order on purpose: it is the only way to show
        that a line with nothing to the right of a colon is skipped rather than
        read as an empty answer.
        """
        with _adapter(HEALTHY_ADAPTER + "\t\tLogical Addresses\n"):
            reading = cec.adapter_state()
        assert reading["logical_addresses"] == "1 (Allow Fallback to Unregistered)"
        assert reading["physical_address"] == "1.0.0.0"

    def test_a_television_that_took_its_link_away(self):
        with _adapter(LINK_DOWN_ADAPTER):
            reading = cec.adapter_state()
            assert reading["physical_address"] == "f.f.f.f"
            assert reading["logical_address_mask"] == "0x0000"
            assert reading["ready"] is False
            assert cec.link_is_down() is True


# ---------------------------------------------------------------------------
# What the television answered
# ---------------------------------------------------------------------------

POWER_ON_REPLY = """Transmit from Recording Device 1 to TV (1 to 0):
GIVE_DEVICE_POWER_STATUS (0x8f)
    Received from TV (0):
    REPORT_POWER_STATUS (0x90):
\tpwr-state: on (0x00)
\tSequence: 395 Tx Timestamp: 2704.092020s Rx Timestamp: 2704.379248s
\tApproximate response time: 215 ms
"""

POWER_QUERY_UNANSWERED = """Transmit from Recording Device 1 to TV (1 to 0):
GIVE_DEVICE_POWER_STATUS (0x8f)
\tSequence: 652 Tx Timestamp: 4604.391727s
\tTx, Not Acknowledged (4), Max Retries
"""

CEC_VERSION_REPLY = """    Received from TV (0):
    CEC_VERSION (0x9e):
\tcec-version: version-1-4 (0x05)
"""


class TestWhatTheTelevisionAnswered:
    def test_a_set_that_says_it_is_on(self):
        with _printed(POWER_ON_REPLY):
            assert cec.get_power_status() == "on"

    def test_a_query_nothing_answered_is_unknown_not_off(self):
        """The asymmetry this whole project rests on: silence is never "off"
        when the box is about to decide whether to wake a television."""
        with _printed(POWER_QUERY_UNANSWERED):
            assert cec.get_power_status() == "unknown"

    def test_the_version_the_set_reported(self):
        assert cec._parse_cec_version(CEC_VERSION_REPLY) == "CEC version 1.4"


# ---------------------------------------------------------------------------
# Whether a frame actually went anywhere
# ---------------------------------------------------------------------------

# A poll of an address nobody occupies. Transmitted, and refused.
POLL_UNANSWERED = """Transmit from Recording Device 1 to TV (1 to 0):
POLL
\tSequence: 709 Tx Timestamp: 4898.281145s
\tTx, Not Acknowledged (2), Max Retries
"""

# A poll the television acknowledged. There is no "OK" line: the absence of a
# complaint is the whole of the good news.
POLL_ANSWERED = """Transmit from Recording Device 1 to Specific (1 to 14):
POLL
\tSequence: 710 Tx Timestamp: 4898.597740s
"""

# The same poll sent from Unregistered, which is all the box can do once the
# television has taken its HDMI link away: no sequence number and no status at
# all. Requiring a sequence number made the box conclude the set was not at 14
# either, and fall back to an address nothing was listening on.
POLL_FROM_UNREGISTERED = """Transmit from Unregistered to Specific (15 to 14):
POLL
"""

# And a poll that was never put on the bus, because the adapter's addresses had
# been cleared. No transmission line, no error, nothing.
POLL_NEVER_SENT = """\tPhysical Address           : 1.0.0.0
\tLogical Address Mask       : 0x0000
\tCEC Version                : 2.0
\tOSD Name                   : ''
\tLogical Addresses          : 0 (Allow RC Passthrough)
"""

# A frame the bus refused, and one it took.
FRAME_REFUSED = """Transmit from Recording Device 1 to TV (1 to 0):
STANDBY (0x36)
\tSequence: 654 Tx Timestamp: 4646.745320s
\tTx, Not Acknowledged (4), Max Retries
"""

FRAME_DELIVERED = """Transmit from Recording Device 1 to TV (1 to 0):
TEXT_VIEW_ON (0x0d)
\tSequence: 797 Tx Timestamp: 7072.668086s
"""

# Waiting for a bus that never goes idle, which is what the adapter reports
# while the television is driving nothing at all.
FRAME_TIMED_OUT = """Transmit from Recording Device 1 to Recording Device 1 (1 to 1):
POLL
\tTx, Timeout, Max Retries
"""


class TestWhetherAFrameWentAnywhere:
    @pytest.mark.parametrize("printed,delivered", [
        (FRAME_DELIVERED, True),
        (POLL_ANSWERED, True),
        (POLL_FROM_UNREGISTERED, True),
        (FRAME_REFUSED, False),
        (POLL_UNANSWERED, False),
        (FRAME_TIMED_OUT, False),
    ])
    def test_the_two_ways_the_adapter_says_no(self, printed, delivered):
        refused = bool(cec.NOT_DELIVERED_RE.search(printed))
        assert refused is not delivered

    @pytest.mark.parametrize("printed,answered", [
        (POLL_ANSWERED, True),
        (POLL_FROM_UNREGISTERED, True),
        (POLL_UNANSWERED, False),
        (POLL_NEVER_SENT, False),
    ])
    def test_a_poll_answered_and_a_poll_that_never_left(self, printed, answered):
        with _printed(printed):
            assert cec._answers_a_poll(0) is answered

    def test_the_television_is_found_where_it_answers(self):
        """The two fragments together, as the box meets them: 0 refuses, 14
        acknowledges."""
        def ctl(arguments):
            if "--to" not in arguments:
                return HEALTHY_ADAPTER
            return POLL_ANSWERED if arguments[arguments.index("--to") + 1] == "14" \
                else POLL_UNANSWERED

        with patch("cec_controller._cec_ctl", side_effect=ctl):
            cec.forget_the_television()
            assert cec.find_the_television() == 14

    def test_a_poll_that_never_left_does_not_become_a_television(self):
        with patch("cec_controller._cec_ctl", return_value=POLL_NEVER_SENT):
            cec.forget_the_television()
            assert cec.find_the_television() == cec.DEFAULT_TV_LOGICAL_ADDRESS
            assert cec._tv_address is None
