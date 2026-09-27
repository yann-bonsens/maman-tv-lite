# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

import base64
import logging
import os
import time

import pytest

import auth
import cec_controller
import hdmi_output
import log_config
import media_config
import page_render
import screen
import state
import tv_config
import zigbee_bridge

# Credentials used by the whole test suite. Placed in the environment before
# the tests run: auth.py re-reads the environment on every request, so import
# order does not matter.
TEST_USER = "test-user"
TEST_PASSWORD = "test-password"

os.environ[auth.USER_ENV] = TEST_USER
os.environ[auth.PASSWORD_ENV] = TEST_PASSWORD

_token = base64.b64encode(f"{TEST_USER}:{TEST_PASSWORD}".encode()).decode()
AUTH_HEADERS = {"Authorization": f"Basic {_token}"}


@pytest.fixture(autouse=True)
def _no_installer_answers(tmp_path, monkeypatch):
    """Run as a box whose installer answers are unknown, which behaves as a
    box with buttons. Without this, the suite run on a real Pi would read
    that Pi's own install.conf and test whatever its owner chose."""
    import button_bindings
    monkeypatch.setattr(button_bindings, "INSTALL_CONF_PATH",
                        str(tmp_path / "install.conf"))


@pytest.fixture(autouse=True)
def _no_remembered_cec_adapter(tmp_path, monkeypatch):
    """Each test detects the CEC adapter afresh, and never reads or writes the
    real box's memory of which port the television was on."""
    import cec_controller
    monkeypatch.setattr(cec_controller, "_chosen_device", None)
    monkeypatch.setattr(cec_controller, "ADAPTER_STATE_PATH",
                        str(tmp_path / "cec-adapter"))


@pytest.fixture(autouse=True)
def _forget_per_caller_state():
    """Clear the duplicate-press window between tests.

    The bridge keeps it in module-level state, as it must: the MQTT callback
    has nowhere else to put it. Tests run milliseconds apart, so without this a
    test pressing the same button as the previous one has its press suppressed
    and fails for a reason that has nothing to do with what it checks.
    """
    zigbee_bridge._last_handled.clear()
    zigbee_bridge._in_flight = None
    zigbee_bridge._drop_pending()
    # Likewise the failed-authentication counters: tests share one process, and
    # a test that exhausts the allowance would lock out every test after it.
    auth.reset_throttle()
    # The half-second a rejection costs a real caller is the point of the
    # measure and worthless in a test suite that rejects on purpose dozens of
    # times: it made the whole run ten times slower. One test asserts the delay
    # is applied; everything else runs without it.
    auth.AUTH_FAILURE_DELAY_SECONDS = 0
    # And where the box last found the television, which is module-level
    # because a set does not move while it is switched on. Pinned rather than
    # merely cleared: cleared, the first frame of every test would go looking
    # for a real television with a real `cec-ctl`.
    yield
    zigbee_bridge._drop_pending()
    zigbee_bridge._last_handled.clear()
    zigbee_bridge._in_flight = None
    auth.reset_throttle()


@pytest.fixture(autouse=True)
def box(tmp_path, monkeypatch):
    """A box that touches nothing real: no systemctl, no /run, no /var/lib.

    Autouse, because forgetting it in one test would mean that test writing to
    the machine running the suite — and on a development Raspberry Pi, putting
    a real television's picture out in the middle of a test run.
    """
    monkeypatch.setattr(cec_controller, "_tv_address",
                        cec_controller.DEFAULT_TV_LOGICAL_ADDRESS)
    monkeypatch.setattr(tv_config, "PATH", str(tmp_path / "tv.json"))
    monkeypatch.setattr(tv_config, "LEGACY_PATH", str(tmp_path / "cec-learned.json"))
    monkeypatch.setattr(media_config, "PATH", str(tmp_path / "media.json"))
    # And the CEC log level, which is a file on the box and a live logger in the
    # process: left raised, one test would flood every test after it.
    monkeypatch.setattr(log_config, "PATH", str(tmp_path / "logging.json"))
    monkeypatch.setattr(logging.getLogger(log_config.CEC_LOGGER), "level",
                        logging.INFO)
    monkeypatch.setattr(state, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(state, "PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(page_render, "PAGE_PATH", str(tmp_path / "page.png"))
    monkeypatch.setattr(screen, "FIFO", str(tmp_path / "screen"))

    # The HDMI output: what it was asked to do, and what it pretends to be.
    asked = []
    pretend = {"state": hdmi_output.AWAKE}

    def fake_systemctl(verb, unit):
        asked.append(verb)
        # "restart", not "start": the sleep units are oneshots with
        # RemainAfterExit=yes, and systemd makes `start` a no-op on one it
        # already considers active — so the real code restarts them. See
        # TestPuttingTheOutputToSleepActuallyReapplies.
        pretend["state"] = hdmi_output.ASLEEP if verb == "start" else hdmi_output.AWAKE
        pretend["unit"] = unit
        return True

    monkeypatch.setattr(hdmi_output, "_systemctl", fake_systemctl)
    monkeypatch.setattr(hdmi_output, "state", lambda: pretend["state"])
    monkeypatch.setattr(hdmi_output, "outputs", lambda: {"card0-HDMI-A-1": {
        "status": "connected",
        "dpms": "Off" if pretend["state"] == hdmi_output.ASLEEP else "On",
        "enabled": "enabled"}})

    state._state.update({"mode": state.TELEVISION, "mode_since": None,
                         "hdmi": "unknown", "tv_power": "unknown",
                         "tv_power_at": None, "events": []})
    yield type("Box", (), {"hdmi_calls": asked, "hdmi": pretend, "path": tmp_path})


def settle_buttons(timeout: float = 5.0) -> None:
    """Wait for queued button actions to finish.

    Button actions no longer run in the caller's thread: a press is handed to a
    worker so the MQTT thread stays free. Tests therefore have to wait for the
    work rather than assume it already happened.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if zigbee_bridge._QUEUE.unfinished_tasks == 0 and zigbee_bridge._in_flight is None:
            return
        time.sleep(0.01)
    raise AssertionError("queued button actions did not finish in time")

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Say out loud what the default run did not do.

    The integration layer is excluded by default because it costs five times the
    rest of the suite for a twentieth of the tests. A test nobody runs is worse
    than no test, so this refuses to let the exclusion be quiet: CI runs both
    layers, and a person who has just changed the CEC transport or the screen
    needs reminding that the fast run proved nothing about either.
    """
    if config.getoption("markexpr") != "not integration":
        return
    deselected = len(terminalreporter.stats.get("deselected", []))
    if not deselected:
        return
    terminalreporter.write_line(
        f"{deselected} integration tests were NOT run (real binaries, ~40 s). "
        "Run them with: pytest -m integration", yellow=True)
