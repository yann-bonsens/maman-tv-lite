# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

from unittest.mock import MagicMock, patch

import pytest

import system_controller
from system_controller import SystemCommandError


def _busctl(stdout: str, returncode: int = 0, stderr: str = ""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestQueryLogind:
    def test_parses_busctl_output(self):
        """busctl answers `s "yes"`: the type prefix and quotes must be
        stripped."""
        with patch("subprocess.run", return_value=_busctl('s "yes"\n')):
            assert system_controller._query_logind("CanReboot") == "yes"

    def test_parses_challenge(self):
        with patch("subprocess.run", return_value=_busctl('s "challenge"\n')):
            assert system_controller._query_logind("CanPowerOff") == "challenge"

    def test_raises_when_busctl_fails(self):
        with patch("subprocess.run", return_value=_busctl("", 1, "bus introuvable")):
            with pytest.raises(SystemCommandError, match="bus introuvable"):
                system_controller._query_logind("CanReboot")


class TestAuthorizationGuard:
    """Regression: without the polkit rule shipped here, logind answers
    "challenge" and `systemctl reboot` does nothing. The API would then return
    200 while the machine stayed up — the worst possible failure mode."""

    @pytest.mark.parametrize("verdict", ["challenge", "no", "na"])
    def test_refuses_when_not_authorized(self, verdict):
        with patch("system_controller._query_logind", return_value=verdict), patch(
            "system_controller._schedule"
        ) as schedule:
            with pytest.raises(SystemCommandError, match="polkit"):
                system_controller.reboot()
        schedule.assert_not_called()


class TestActions:
    def test_reboot_schedules_systemctl_reboot(self):
        with patch("system_controller._query_logind", return_value="yes"), patch(
            "system_controller._schedule"
        ) as schedule:
            system_controller.reboot()
        schedule.assert_called_once_with(["systemctl", "reboot"])

    def test_shutdown_schedules_systemctl_poweroff(self):
        with patch("system_controller._query_logind", return_value="yes"), patch(
            "system_controller._schedule"
        ) as schedule:
            system_controller.shutdown()
        schedule.assert_called_once_with(["systemctl", "poweroff"])

    def test_reboot_checks_the_right_logind_method(self):
        with patch("system_controller._query_logind", return_value="yes") as query, patch(
            "system_controller._schedule"
        ):
            system_controller.reboot()
        query.assert_called_once_with("CanReboot")

    def test_shutdown_checks_the_right_logind_method(self):
        with patch("system_controller._query_logind", return_value="yes") as query, patch(
            "system_controller._schedule"
        ):
            system_controller.shutdown()
        query.assert_called_once_with("CanPowerOff")


def test_schedule_runs_command_after_delay():
    """The command runs in a thread, after the delay that lets the HTTP
    response leave first."""
    with patch("system_controller.ACTION_DELAY_SECONDS", 0), patch(
        "subprocess.run"
    ) as run:
        system_controller._schedule(["systemctl", "reboot"])
        for _ in range(100):
            if run.called:
                break
            import time

            time.sleep(0.01)
    run.assert_called_once_with(["systemctl", "reboot"], check=False)
