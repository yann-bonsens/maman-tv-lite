# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Which CEC adapter the box talks through, on a board with two HDMI ports.

It was hard-coded to /dev/cec0. On a Pi 4 or 5 with the television on the
second port, every frame went out of an adapter with nothing behind it.
"""

import subprocess
from unittest.mock import patch

import pytest

import cec_controller as cec

# What `cec-ctl -d /dev/cecN` prints, cut to the line that matters.
LIVE = "\tPhysical Address     : 2.0.0.0\n"
DEAD = "\tPhysical Address     : f.f.f.f\n"


class _Ports:
    """Two adapters; `printed` says what each one reports."""

    def __init__(self, printed: dict):
        self.printed = printed
        self.asked: list = []

    def run(self, argv, **_):
        self.asked.append(argv[2])
        return subprocess.CompletedProcess(argv, 0, self.printed[argv[2]], "")


@pytest.fixture
def two_ports(monkeypatch):
    monkeypatch.setattr(cec, "CEC_DEVICE", None)
    monkeypatch.setattr(cec, "_adapters", lambda: ["/dev/cec0", "/dev/cec1"])

    def install(printed):
        ports = _Ports(printed)
        monkeypatch.setattr(cec.subprocess, "run", ports.run)
        return ports
    return install


def _remember(device):
    with open(cec.ADAPTER_STATE_PATH, "w") as handle:
        handle.write(device + "\n")


def test_the_port_with_a_television_is_chosen(two_ports):
    two_ports({"/dev/cec0": DEAD, "/dev/cec1": LIVE})
    assert cec._device() == "/dev/cec1"


def test_it_is_remembered_for_when_the_set_sleeps(two_ports):
    two_ports({"/dev/cec0": DEAD, "/dev/cec1": LIVE})
    cec._device()
    assert cec._read_remembered_adapter() == "/dev/cec1"


def test_a_sleeping_set_is_found_where_it_was_last_seen(two_ports):
    """The moment it has to be woken is the moment it looks absent."""
    _remember("/dev/cec1")
    two_ports({"/dev/cec0": DEAD, "/dev/cec1": DEAD})
    assert cec._device() == "/dev/cec1"


def test_the_remembered_port_wins_when_both_answer(two_ports):
    _remember("/dev/cec1")
    two_ports({"/dev/cec0": LIVE, "/dev/cec1": LIVE})
    assert cec._device() == "/dev/cec1"


def test_a_set_moved_to_the_other_port_is_followed(two_ports):
    _remember("/dev/cec0")
    two_ports({"/dev/cec0": DEAD, "/dev/cec1": LIVE})
    assert cec._device() == "/dev/cec1"
    assert cec._read_remembered_adapter() == "/dev/cec1"


def test_the_choice_is_made_once_not_on_every_frame(two_ports):
    """Each reading is a process on this board; a press must not pay for
    asking both ports again."""
    ports = two_ports({"/dev/cec0": DEAD, "/dev/cec1": LIVE})
    cec._device()
    asked = len(ports.asked)
    cec._device()
    cec._device()
    assert len(ports.asked) == asked


def test_a_board_that_never_saw_a_set_keeps_looking(two_ports):
    """Not kept, so the television is found as soon as it appears."""
    ports = two_ports({"/dev/cec0": DEAD, "/dev/cec1": DEAD})
    assert cec._device() == "/dev/cec0"
    ports.printed["/dev/cec1"] = LIVE
    assert cec._device() == "/dev/cec1"


def test_a_single_adapter_is_used_without_asking_it_anything(monkeypatch):
    monkeypatch.setattr(cec, "CEC_DEVICE", None)
    monkeypatch.setattr(cec, "_adapters", lambda: ["/dev/cec0"])
    with patch.object(cec.subprocess, "run") as run:
        assert cec._device() == "/dev/cec0"
    run.assert_not_called()


def test_a_forced_device_is_never_second_guessed(monkeypatch):
    monkeypatch.setattr(cec, "CEC_DEVICE", "/dev/cec1")
    monkeypatch.setattr(cec, "_adapters", lambda: ["/dev/cec0", "/dev/cec1"])
    with patch.object(cec.subprocess, "run") as run:
        assert cec._device() == "/dev/cec1"
    run.assert_not_called()


def test_frames_go_out_of_the_chosen_adapter(two_ports):
    ports = two_ports({"/dev/cec0": DEAD, "/dev/cec1": LIVE})
    cec._cec_ctl(["--to", "0", "--standby"])
    assert ports.asked[-1] == "/dev/cec1"


def test_the_report_names_the_chosen_adapter(two_ports):
    two_ports({"/dev/cec0": DEAD, "/dev/cec1": LIVE})
    assert cec.adapter_state()["device"] == "/dev/cec1"
