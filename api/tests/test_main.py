# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The HTTP surface: what each route promises, and what it refuses.

The API is one of three renderings of the same box — the others are the shell
command and the television screen — so the routes here are thin. What they
must guarantee is that they touch the same machinery the buttons do, and that
nothing changes the box's configuration by accident.
"""

from unittest.mock import patch

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import button_bindings
import cec_controller as cec
import main
import state
import tv_config
from conftest import AUTH_HEADERS
from main import app

# Anchored on this file, not on the working directory: CI runs pytest from
# api/, where a relative "api/..." path does not exist.
REPO_ROOT = Path(__file__).resolve().parents[2]

client = TestClient(app, headers=AUTH_HEADERS)


class TestTheTelevision:
    def test_on_sends_the_configured_wake(self):
        with patch("cec_controller.power_on", return_value={"action": "power-on"}) as on:
            assert client.post("/tv/on").status_code == 200
        on.assert_called_once()

    def test_off_sends_the_configured_standby(self):
        with patch("cec_controller.standby", return_value={"action": "standby"}) as off:
            assert client.post("/tv/off").status_code == 200
        off.assert_called_once()

    def test_toggle_is_the_tv_button(self):
        with patch("modes.tv_button", return_value={"action": "off"}) as button:
            assert client.post("/tv/toggle").json()["action"] == "off"
        button.assert_called_once()

    def test_status_reports_what_the_set_says(self):
        with patch("cec_controller.get_power_status", return_value="standby"):
            assert client.get("/tv/status").json() == {"power": "standby"}

    def test_a_television_that_cannot_be_reached_is_a_503_not_a_crash(self):
        """With the HDMI cable unplugged, the caller deserves a readable
        error rather than a Python traceback."""
        with patch("cec_controller.power_on", side_effect=cec.CECError("no adapter")):
            answer = client.post("/tv/on")
        assert answer.status_code == 503
        assert "no adapter" in answer.json()["detail"]


class TestTheModes:
    @pytest.mark.parametrize("route,target", [
        ("/mode/music", "modes.music"),
        ("/mode/diagnostic", "modes.diagnostic"),
        ("/mode/installation", "modes.installation"),
    ])
    def test_each_mode_has_a_route(self, route, target):
        with patch(target, return_value={"mode": "x"}) as entered:
            assert client.post(route).status_code == 200
        entered.assert_called_once()

    def test_television_switches_the_set_off_by_default(self):
        with patch("modes.television", return_value={"mode": "television"}) as left:
            client.post("/mode/television")
        assert left.call_args.kwargs["switch_off"] is True

    def test_television_can_be_asked_for_the_programmes_instead(self):
        with patch("modes.television", return_value={"mode": "television"}) as left:
            client.post("/mode/television?switch_off=false")
        assert left.call_args.kwargs["switch_off"] is False


class TestWhatTheScreenShowsAtStartup:
    """A box with no working button shows the installation screen instead
    of a "no signal" output; a box that has one just sleeps its output as
    before. Governed by `button_bindings.has_any_button()` alone — the CEC
    configuration state has no bearing on this any more: a television that
    already answers to the default frames is a working box, and forcing
    this screen open on every boot just because nobody explicitly
    confirmed a technique would make it a permanent nag rather than a
    one-time step. `button_bindings.clear()` (not `tv_config.clear()`) is
    what makes this screen reappear at the next start.

    Every test here patches `button_bindings.has_any_button` explicitly
    rather than relying on the module-level `BINDINGS` a fresh test process
    happens to have loaded, since that reflects whatever `buttons.json`
    exists (or not) on the machine running the suite, not the case each
    test means to describe.
    """

    def test_no_button_opens_installation_even_on_a_fresh_box(self):
        with patch("modes.installation", return_value={"mode": "installation"}) as opened, \
             patch("hdmi_output.sleep") as slept, \
             patch("button_bindings.has_any_button", return_value=False):
            main._open_the_screen_at_startup()
        opened.assert_called_once()
        slept.assert_not_called()

    def test_no_button_opens_installation_even_on_a_fully_configured_box(self):
        """The point of this test: the CEC configuration is irrelevant to
        this decision. A box that has completed detection, recorded its
        television and everything else still opens the installation screen
        if it has no working button."""
        tv_config.set_technique("wake", "text_view_on", source="detection")
        tv_config.set_technique("sleep", "standby", source="detection")
        tv_config.set_detection_complete(True)
        tv_config.set_television({"manufacturer": "SAM"})
        with patch("modes.installation", return_value={"mode": "installation"}) as opened, \
             patch("hdmi_output.sleep") as slept, \
             patch("button_bindings.has_any_button", return_value=False):
            main._open_the_screen_at_startup()
        opened.assert_called_once()
        slept.assert_not_called()

    def test_a_working_button_just_sleeps_the_output_even_on_a_fresh_box(self):
        """The other half of the same point: an unconfirmed CEC
        configuration (the defaults, never explicitly set or detected)
        does not by itself open the screen any more, as long as a button
        works."""
        with patch("modes.installation") as opened, \
             patch("hdmi_output.sleep") as slept, \
             patch("button_bindings.has_any_button", return_value=True):
            main._open_the_screen_at_startup()
        opened.assert_not_called()
        slept.assert_called_once()

    def test_a_box_installed_without_buttons_never_opens_it(self):
        """Regression: installed --without-zigbee, the box opened a pairing
        screen at every boot and waited twenty minutes for an adapter that
        was never going to be there."""
        with patch("modes.installation") as opened, \
             patch("hdmi_output.sleep") as slept, \
             patch("button_bindings.has_any_button", return_value=False), \
             patch("button_bindings.buttons_installed", return_value=False):
            main._open_the_screen_at_startup()
        opened.assert_not_called()
        slept.assert_called_once()


class TestInstalledWithoutButtons:
    def _write(self, tmp_path, *lines):
        conf = tmp_path / "answers.conf"
        conf.write_text("\n".join(("# Written by scripts/install.sh.",) + lines) + "\n")
        return str(conf)

    def test_an_explicit_no_means_no_buttons(self, tmp_path):
        path = self._write(tmp_path, "MAMAN_TAILSCALE=yes", "MAMAN_ZIGBEE=no")
        assert button_bindings.buttons_installed(path) is False

    def test_yes_means_buttons(self, tmp_path):
        path = self._write(tmp_path, "MAMAN_ZIGBEE=yes")
        assert button_bindings.buttons_installed(path) is True

    def test_no_file_behaves_like_a_box_with_buttons(self, tmp_path):
        """A development machine, or a box older than the file: what every
        box did before the question existed."""
        assert button_bindings.buttons_installed(str(tmp_path / "absent")) is True

    def test_a_file_that_does_not_say_behaves_like_a_box_with_buttons(self, tmp_path):
        path = self._write(tmp_path, "MAMAN_TAILSCALE=no")
        assert button_bindings.buttons_installed(path) is True

    def test_the_installation_screen_is_refused(self):
        with patch("modes.installation") as opened, \
             patch("button_bindings.buttons_installed", return_value=False):
            response = client.post("/mode/installation")
        assert response.status_code == 409
        assert "without-buttons" in response.json()["detail"]
        opened.assert_not_called()

    def test_the_installation_screen_still_opens_with_buttons(self):
        with patch("modes.installation", return_value={"mode": "installation"}) as opened, \
             patch("button_bindings.buttons_installed", return_value=True):
            response = client.post("/mode/installation")
        assert response.status_code == 200
        opened.assert_called_once()

    def test_the_document_the_refusal_names_exists(self):
        """The 409 sends somebody to a file; it must be there."""
        assert (REPO_ROOT / "docs" / "without-buttons.md").is_file()

    def test_the_saved_line_is_the_one_the_installer_writes(self):
        """The two sides of one contract: if the installer ever renamed the
        key, every box would silently go back to nagging."""
        script = (REPO_ROOT / "scripts" / "install.sh").read_text()
        assert '"MAMAN_ZIGBEE=$MAMAN_ZIGBEE"' in script
        assert 'printf -v "$name" \'%s\' "no"' in script


class TestForgettingTheButtons:
    def test_calls_button_bindings_clear(self):
        with patch("button_bindings.clear") as cleared:
            response = client.delete("/installation/buttons")
        cleared.assert_called_once()
        assert response.status_code == 200
        assert response.json() == {"cleared": True}


class TestWhatTheBoxSaysAboutItself:
    def test_state_carries_the_mode_the_output_and_the_configuration(self):
        answer = client.get("/state").json()
        assert answer["mode"] == state.TELEVISION
        assert "hdmi_output" in answer
        assert answer["configuration"]["wake"]["technique"] == tv_config.DEFAULT_WAKE
        assert answer["configuration"]["wake"]["source"] == "default"

    def test_state_asks_the_television_nothing(self):
        """It is read when the box seems stuck, and a CEC question takes
        seconds. Everything here is a snapshot already."""
        with patch("cec_controller._run", side_effect=AssertionError("asked the bus")):
            assert client.get("/state").status_code == 200

    def test_the_report_does_ask_and_says_what_it_found(self):
        with patch("cec_controller.adapter_state", return_value={"ready": True}), \
             patch("cec_controller.television_identity", return_value={"name": "SAMSUNG"}), \
             patch("cec_controller._safe_power_status", return_value="on"):
            answer = client.get("/report").json()
        assert answer["cec_adapter"] == {"ready": True}
        assert answer["television_identity"]["name"] == "SAMSUNG"
        assert answer["television_power"] == "on"


class TestTheConfiguration:
    def test_it_lists_what_is_in_force_and_what_can_be_chosen(self):
        answer = client.get("/tv/config").json()
        assert answer["wake"]["source"] == "default"
        assert "image_view_on" in answer["available"]["wake"]
        assert "standby" in answer["available"]["sleep"]

    def test_a_technique_can_be_set_and_is_recorded_as_manual(self):
        answer = client.put("/tv/config/wake?technique=text_view_on").json()
        assert answer["wake"] == {"technique": "text_view_on", "source": "manual"}
        assert tv_config.load()["wake"]["technique"] == "text_view_on"

    def test_a_name_that_is_not_a_real_technique_at_all_is_refused(self):
        """Caught by the enum's own Swagger-facing type, before this
        route's own code ever runs — FastAPI's validation error, not the
        route's hand-written one (see the next test for that case)."""
        assert client.put("/tv/config/sleep?technique=magic").status_code == 422

    def test_a_real_technique_for_the_wrong_direction_is_refused_with_the_list(self):
        """"active_source" is a real wake technique, just not a sleep one —
        a name the enum accepts (it exists), so this is the route's own
        check, not the enum's."""
        answer = client.put("/tv/config/sleep?technique=active_source")
        assert answer.status_code == 422
        assert "standby" in answer.json()["detail"]

    def test_the_release_can_be_set_by_hand(self):
        """A box installed without the buttons has no detection procedure
        to set it; the API is the only way."""
        answer = client.put("/tv/config/release?technique=inactive_source").json()
        assert answer["release"] == {"technique": "inactive_source", "source": "manual"}
        assert tv_config.load()["release"]["technique"] == "inactive_source"

    def test_power_cycle_is_offered_for_the_release_though_not_in_the_table(self):
        """The release that works on every set, and the way back to it."""
        assert client.get("/tv/config").json()["available"]["release"][-1] == "power_cycle"
        client.put("/tv/config/release?technique=inactive_source")
        answer = client.put("/tv/config/release?technique=power_cycle").json()
        assert answer["release"]["technique"] == "power_cycle"

    def test_a_wake_technique_is_no_release(self):
        answer = client.put("/tv/config/release?technique=image_view_on")
        assert answer.status_code == 422
        assert "power_cycle" in answer.json()["detail"]

    def test_only_the_three_kinds_exist(self):
        """kind is enum-typed too, the same as technique above: an unlisted
        value never reaches this route's own code."""
        assert client.put("/tv/config/colour?technique=standby").status_code == 422

    def test_the_television_identity_can_be_recorded(self):
        with patch("cec_controller.television_identity",
                   return_value={"manufacturer": "SAM", "name": "SAMSUNG"}):
            answer = client.post("/tv/config/television").json()
        assert answer["name"] == "SAMSUNG"
        assert tv_config.load()["television"]["manufacturer"] == "SAM"

    def test_hdmi_sleep_none_can_be_chosen(self):
        answer = client.put("/tv/config/hdmi-sleep?method=none").json()
        assert answer["hdmi_sleep"] == "none"
        assert tv_config.load()["hdmi_sleep"] == "none"

    def test_an_unknown_hdmi_sleep_method_is_refused(self):
        assert client.put("/tv/config/hdmi-sleep?method=unplug").status_code == 422

    def test_clearing_resets_the_learned_techniques_but_not_hdmi_sleep(self):
        tv_config.set_technique("wake", "text_view_on", source="detection")
        tv_config.set_television({"manufacturer": "SAM"})
        tv_config.set_hdmi_sleep("connector")

        answer = client.delete("/tv/config").json()

        assert answer["wake"] == {"technique": tv_config.DEFAULT_WAKE, "source": "default"}
        assert answer["television"] == {}
        assert answer["hdmi_sleep"] == "connector"

    def test_tv_button_switches_off_can_be_set(self):
        """Registered before /tv/config/{kind} in main.py, the same reason
        hdmi-sleep is (see that route's own docstring) — a route reached
        through the real TestClient, not tv_config.set_tv_button_switches_off()
        called directly, is what actually proves it is not shadowed."""
        answer = client.put("/tv/config/tv-button-switches-off?enabled=true").json()
        assert answer["tv_button_switches_off"] is True
        assert tv_config.load()["tv_button_switches_off"] is True

    def test_tv_button_switches_off_defaults_to_false(self):
        assert client.get("/tv/config").json()["tv_button_switches_off"] is False


class TestTheMachine:
    def test_reboot_and_shutdown_are_offered(self):
        with patch("system_controller.reboot") as reboot:
            assert client.post("/system/reboot").json()["ok"] is True
        reboot.assert_called_once()
        with patch("system_controller.shutdown") as off:
            assert client.post("/system/shutdown").json()["ok"] is True
        off.assert_called_once()

    def test_a_refused_system_action_is_a_503(self):
        from system_controller import SystemCommandError
        with patch("system_controller.reboot",
                   side_effect=SystemCommandError("polkit said no")):
            answer = client.post("/system/reboot")
        assert answer.status_code == 503
        assert "polkit said no" in answer.json()["detail"]


class TestEverythingIsBehindThePassword:
    def test_a_route_without_credentials_is_refused(self):
        bare = TestClient(app)
        assert bare.get("/state").status_code == 401

    def test_the_whole_surface_is_covered(self):
        """Applied as middleware rather than per route: with two dozen routes,
        one forgotten decorator would leave a hole, and any route added later
        is protected by default."""
        bare = TestClient(app)
        for route in app.routes:
            path = getattr(route, "path", "")
            if path in ("/", "/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"):
                continue
            if "{" in path:
                continue
            for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
                answer = bare.request(method, path)
                assert answer.status_code == 401, f"{method} {path} answered {answer.status_code}"


class TestTheStartupOrder:
    """Two things depend on READY=1 arriving before the work below it, and
    both broke a real boot on 2026-09-25 when it was moved to the end."""

    def _lifespan_source(self) -> str:
        """The lifespan's statements, with comments stripped — the comment
        explaining this ordering names both calls, in the opposite order."""
        source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
        body = source[source.index("async def lifespan("):]
        body = body[:body.index("\n    yield")]
        return "\n".join(line for line in body.splitlines()
                         if not line.lstrip().startswith("#"))

    def test_ready_is_signalled_before_the_screen_is_opened(self):
        """`maman-screen.service` is ordered `After=maman-api.service`, and
        this unit is Type=notify — so "after" means after READY=1. Sent last,
        the screen was opened with no screen service alive to hear it:
        "the screen service is not listening; 'page' not sent", and a box
        with no button showed its own boot messages instead of the
        installation screen."""
        body = self._lifespan_source()
        assert body.index("notify_ready()") < body.index("_open_the_screen_at_startup()")

    def test_the_screen_service_is_ordered_after_the_api(self):
        """The other half of the same fact: if this ever stops being true,
        the ordering above stops mattering and somebody will move it back."""
        unit = (REPO_ROOT / "systemd" / "maman-screen.service").read_text(encoding="utf-8")
        assert "After=maman-api.service" in unit

    def test_the_api_waits_for_polkit(self):
        """polkit is static and D-Bus-activated, so this service is what
        activates it — while holding CPUWeight=5000 against its default 100
        on a single core. Measured: the activation timed out after 25 s,
        twice, and the box never put its own HDMI output where it wanted."""
        unit = (REPO_ROOT / "systemd" / "maman-api.service").read_text(encoding="utf-8")
        assert "After=polkit.service" in unit
        assert "Wants=polkit.service" in unit, "After= alone never starts it"
