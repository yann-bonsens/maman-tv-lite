# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Tests for scripts/setup-zigbee.py.

That script is the five-minute path for anyone whose hardware differs from
ours, and most of it cannot be tested here: it needs a real adapter, a running
broker and a thumb on a button. What CAN be tested is every decision it makes
about the data it sees, which is exactly where a wrong guess would be silent
and expensive.

It lives under api/tests/ so that one `pytest` run covers the whole project.
The script is loaded by path because its name is not an importable module.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "setup-zigbee.py"


def _load():
    spec = importlib.util.spec_from_file_location("setup_zigbee", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup_zigbee = _load()


class TestClassifyMessage:
    """Deciding what a message means is the only risky logic that can be
    exercised without hardware. Every button model publishes something
    slightly different, so this has to survive surprises rather than assume a
    shape."""

    def test_a_press_yields_the_device_and_the_action(self):
        assert setup_zigbee.classify_message(
            "zigbee2mqtt/kitchen-button", json.dumps({"action": "single"})
        ) == ("press", "kitchen-button", "single")

    @pytest.mark.parametrize("action", ["single", "on", "toggle", "1_single", "hold"])
    def test_any_action_name_is_accepted(self, action):
        """The whole point is that the name is learned, not guessed."""
        result = setup_zigbee.classify_message(
            "zigbee2mqtt/b", json.dumps({"action": action})
        )
        assert result == ("press", "b", action)

    @pytest.mark.parametrize(
        "payload",
        [
            "not json at all",
            "",
            json.dumps({"battery": 87}),          # periodic state report
            json.dumps({"linkquality": 120}),
            json.dumps({"action": ""}),           # present but empty
            json.dumps({"action": None}),
        ],
    )
    def test_noise_is_ignored(self, payload):
        assert setup_zigbee.classify_message("zigbee2mqtt/b", payload) is None

    @pytest.mark.parametrize("payload", ['"online"', "123", "[1, 2]", "null"])
    def test_non_object_json_does_not_raise(self, payload):
        """Zigbee2MQTT publishes bare strings on some topics. Calling .get()
        on one would raise and kill the loop in the middle of pairing."""
        assert setup_zigbee.classify_message("zigbee2mqtt/b", payload) is None

    def test_device_joined(self):
        payload = json.dumps({"type": "device_joined", "data": {"friendly_name": "0x00124b"}})
        assert setup_zigbee.classify_message(
            "zigbee2mqtt/bridge/event", payload
        ) == ("joined", "0x00124b")

    def test_successful_interview_reports_the_model(self):
        payload = json.dumps({
            "type": "device_interview",
            "data": {
                "friendly_name": "button1",
                "status": "successful",
                "definition": {"model": "TS0041"},
            },
        })
        assert setup_zigbee.classify_message(
            "zigbee2mqtt/bridge/event", payload
        ) == ("paired", "button1", "TS0041")

    def test_interview_without_a_definition_still_reports(self):
        """A device Zigbee2MQTT does not recognise still joins, and saying
        'unknown model' beats crashing on a missing key."""
        payload = json.dumps({
            "type": "device_interview",
            "data": {"friendly_name": "mystery", "status": "successful"},
        })
        assert setup_zigbee.classify_message(
            "zigbee2mqtt/bridge/event", payload
        ) == ("paired", "mystery", "unknown model")

    @pytest.mark.parametrize("status", ["started", "failed"])
    def test_unfinished_interview_is_not_a_pairing(self, status):
        payload = json.dumps({
            "type": "device_interview",
            "data": {"friendly_name": "b", "status": status},
        })
        assert setup_zigbee.classify_message("zigbee2mqtt/bridge/event", payload) is None

    def test_malformed_bridge_event_does_not_raise(self):
        for payload in ['{"type": "device_joined"}', '{"type": "device_joined", "data": null}', "{}"]:
            setup_zigbee.classify_message("zigbee2mqtt/bridge/event", payload)


class TestSetSerialPort:
    """Rewriting the wrong line here breaks the install in a way that looks
    nothing like its cause."""

    BASE = (
        "version: 5\n"
        "mqtt:\n"
        '    server: "mqtt://localhost"\n'
        "    port: 1883\n"
        "serial:\n"
        "    # TODO: replace with the real path\n"
        "    port: /dev/ttyUSB0\n"
        "    #adapter: ember\n"
        "advanced:\n"
        "    network_key: GENERATE\n"
    )
    PORT = Path("/dev/serial/by-id/usb-Vendor_Adapter-if00-port0")

    def _config(self, tmp_path, text=None):
        path = tmp_path / "configuration.yaml"
        path.write_text(text if text is not None else self.BASE)
        return path

    def test_rewrites_the_serial_port(self, tmp_path):
        path = self._config(tmp_path)
        assert setup_zigbee.set_serial_port(path, self.PORT) is True
        assert f"    port: {self.PORT}\n" in path.read_text()

    def test_leaves_the_mqtt_broker_port_alone(self, tmp_path):
        """An unscoped search for `port:` would rewrite the broker port and
        break MQTT instead of the adapter."""
        path = self._config(tmp_path)
        setup_zigbee.set_serial_port(path, self.PORT)
        assert "    port: 1883\n" in path.read_text()

    def test_running_twice_changes_nothing(self, tmp_path):
        path = self._config(tmp_path)
        setup_zigbee.set_serial_port(path, self.PORT)
        before = path.read_text()
        assert setup_zigbee.set_serial_port(path, self.PORT) is False
        assert path.read_text() == before

    def test_keeps_the_rest_of_the_file_intact(self, tmp_path):
        """Zigbee2MQTT writes the generated network key back into this file.
        Losing it would invalidate the network and unpair every device."""
        path = self._config(tmp_path)
        setup_zigbee.set_serial_port(path, self.PORT)
        text = path.read_text()
        assert "network_key: GENERATE" in text
        assert "#adapter: ember" in text
        assert '    server: "mqtt://localhost"' in text

    def test_repairs_a_port_value_split_over_two_lines(self, tmp_path):
        """The exact corruption seen on real hardware: the path ended up on
        two lines, YAML joined them with a space, and Zigbee2MQTT reported
        "No such file" for a path that existed. Rewriting only the first line
        would leave the orphan behind and keep the file broken."""
        broken = (
            "serial:\n"
            "  port: /dev/serial/by-id/usb-A-if00\n"
            "    /dev/serial/by-id/usb-A-if00\n"
            "  adapter: ember\n"
            "advanced:\n"
            "  network_key: GENERATE\n"
        )
        path = self._config(tmp_path, broken)
        assert setup_zigbee.set_serial_port(path, self.PORT) is True
        text = path.read_text()
        assert text.count("/dev/serial/by-id/") == 1
        assert f"  port: {self.PORT}\n" in text
        assert "  adapter: ember\n" in text
        assert "network_key: GENERATE" in text

    def test_no_serial_port_line_fails_loudly(self, tmp_path):
        """Rather than rewriting some other `port:` further down the file."""
        path = self._config(tmp_path, "serial:\n    adapter: ember\nadvanced:\n    port: 9\n")
        with pytest.raises(SystemExit):
            setup_zigbee.set_serial_port(path, self.PORT)
        assert "port: 9" in path.read_text()

    def test_missing_file_fails_loudly(self, tmp_path):
        with pytest.raises(SystemExit):
            setup_zigbee.set_serial_port(tmp_path / "absent.yaml", self.PORT)


class TestSetAdapter:
    """Zigbee2MQTT cannot always work out the chipset by itself: a widely sold
    EFR32 dongle failed discovery outright with "No valid USB adapter found",
    and naming the chipset was the only cure. This used to mean editing YAML
    by hand, the last step in the project that needed a text editor."""

    BASE = "serial:\n  port: /dev/serial/by-id/usb-X\nadvanced:\n  pan_id: GENERATE\n"

    def _config(self, tmp_path, text=None):
        path = tmp_path / "configuration.yaml"
        path.write_text(text if text is not None else self.BASE)
        return path

    def test_inserts_the_adapter_after_the_port(self, tmp_path):
        path = self._config(tmp_path)
        setup_zigbee.set_adapter(path, "ember")
        assert "  port: /dev/serial/by-id/usb-X\n  adapter: ember\n" in path.read_text()

    def test_replaces_an_existing_adapter(self, tmp_path):
        path = self._config(tmp_path, "serial:\n  port: /dev/x\n  adapter: zstack\n")
        setup_zigbee.set_adapter(path, "ember")
        text = path.read_text()
        assert "adapter: ember" in text
        assert "zstack" not in text

    def test_keeps_the_rest_of_the_file(self, tmp_path):
        path = self._config(tmp_path)
        setup_zigbee.set_adapter(path, "deconz")
        assert "pan_id: GENERATE" in path.read_text()

    def test_fails_loudly_when_there_is_nowhere_to_put_it(self, tmp_path):
        """Rather than writing a line that Zigbee2MQTT will not read."""
        path = self._config(tmp_path, "advanced:\n  pan_id: GENERATE\n")
        with pytest.raises(SystemExit):
            setup_zigbee.set_adapter(path, "ember")

    def test_every_candidate_is_a_value_the_flag_accepts(self):
        """The search and the flag must offer the same set, or a value found by
        trying could not be passed back on a second machine."""
        assert setup_zigbee.ADAPTER_CANDIDATES == [
            "ember", "zstack", "deconz", "zboss", "zigate",
        ]


class TestBindings:
    def test_missing_file_reads_as_empty(self, tmp_path):
        assert setup_zigbee.read_bindings(tmp_path / "absent.json") == {}

    def test_malformed_file_reads_as_empty(self, tmp_path):
        path = tmp_path / "buttons.json"
        path.write_text("{ not json")
        assert setup_zigbee.read_bindings(path) == {}

    def test_adding_a_button_keeps_the_existing_ones(self, tmp_path):
        """Pairing a second button must not silently drop the first."""
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {"red": {"single": "power_toggle"}}}))
        document = setup_zigbee.read_bindings(path)
        document.setdefault("bindings", {}).setdefault("blue", {})["on"] = "channel_up"
        assert document["bindings"] == {
            "red": {"single": "power_toggle"},
            "blue": {"on": "channel_up"},
        }


class TestSubprocessesNeverShareStdin:
    """Reported from real hardware: the naming prompt (input()) accepted
    nothing and fell straight through to its default, right after a
    mqtt_listen() call — the likely cause is mosquitto_sub inheriting this
    script's controlling terminal and being killed mid-read while holding
    it, which can leave the terminal unable to deliver the next keystroke.
    Every subprocess that does not itself need interactive input must have
    its stdin explicitly closed, not inherited by default."""

    def test_mqtt_listen_closes_stdin(self):
        import inspect
        source = inspect.getsource(setup_zigbee.mqtt_listen)
        assert "stdin=subprocess.DEVNULL" in source

    def test_mqtt_publish_closes_stdin(self):
        import inspect
        source = inspect.getsource(setup_zigbee.mqtt_publish)
        assert "stdin=subprocess.DEVNULL" in source


class TestTheApiRestartIsNotTrusted:
    """A binding (and a button's new name) only take effect once the API
    re-reads buttons.json at start — it is loaded once, into memory, not
    watched. systemctl()'s own check=False means a failed restart returns
    quietly; reported from real hardware as "I named my button, but the
    installation screen still shows its technical id" — the API process
    was very likely still the pre-rename one. The script must at least say
    so, rather than print "Done" regardless."""

    def test_main_checks_the_restart_result(self):
        import inspect
        source = inspect.getsource(setup_zigbee.main)
        restart_call = source.index('systemctl("restart", "maman-api")')
        line_start = source.rindex("\n", 0, restart_call) + 1
        line = source[line_start:source.index("\n", restart_call)]
        assert line.strip() != 'systemctl("restart", "maman-api")', \
            "the restart's exit code must be checked, not discarded"


class TestStaleBindingRemovedOnRename:
    """Reported from real hardware: renaming a button left BOTH its old
    technical id and its new name bound to the same command in
    buttons.json — "0xa4c1..." and "Bouton Vert" both bound to "tv" at
    once. device_for_command() (api/button_bindings.py) returns the first
    match it finds by key order, so the installation screen kept printing
    the raw id even though the rename had genuinely worked. main() must
    drop the old key once the device is renamed, not just add the new
    one."""

    def test_main_pops_the_previous_key_on_rename(self):
        import inspect
        source = inspect.getsource(setup_zigbee.main)
        assert "bindings.pop(previous_key" in source


class TestPermitJoin:
    def test_both_payload_shapes_are_sent(self):
        """Zigbee2MQTT 2.x takes {"time": N}; 1.x wanted a "value" key too.
        Sending both keeps the script working across releases."""
        payloads = [template % 120 for template in setup_zigbee.PERMIT_JOIN_PAYLOADS]
        assert json.loads(payloads[0]) == {"time": 120}
        assert json.loads(payloads[1]) == {"value": True, "time": 120}


class TestNamingTheButton:
    """The screen prints a button's own configured name ("Red Button"),
    never a role — see docs/development/installation-screen.md. ask_for_name suggests a
    colour and defaults to keeping whatever the button is already called,
    unless that is still a fresh Zigbee2MQTT default (an "0x..." address)."""

    def test_suggests_keeping_an_already_human_name(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "")
        assert setup_zigbee.ask_for_name("Red Button") == "Red Button"

    def test_suggests_a_colour_for_a_fresh_default_name(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "")
        assert setup_zigbee.ask_for_name("0xa4c1385a2f7b9c3d") in \
            setup_zigbee.SUGGESTED_COLOURS

    def test_a_typed_name_wins_over_the_suggestion(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "Vert")
        assert setup_zigbee.ask_for_name("0xa4c1385a2f7b9c3d") == "Vert"


def _mock_mqtt_listen(by_topic: dict):
    """A `mqtt_listen` stand-in that answers by topic rather than always the
    first one — rename_device() now reads two different topics (the rename
    response, then bridge/devices to confirm it stuck), so a mock that
    ignores which topic was asked for would hand the device-list read a
    rename-response payload and vice versa.

    A topic's value may be one payload, or a list of them delivered in
    order — the latter is what stands in for a retained message (the stale
    snapshot) followed by a republish (the up-to-date one).
    """
    def listen(topics, seconds):
        for topic in topics:
            if topic not in by_topic:
                continue
            value = by_topic[topic]
            payloads = value if isinstance(value, list) else [value]
            for payload in payloads:
                yield topic, payload
    return listen


RENAME_RESPONSE_TOPIC = "zigbee2mqtt/bridge/response/device/rename"
DEVICES_TOPIC = "zigbee2mqtt/bridge/devices"


class TestRenameDevice:
    def test_the_same_name_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(setup_zigbee, "mqtt_publish",
                            lambda *a: pytest.fail("must not publish a no-op rename"))
        assert setup_zigbee.rename_device("Red Button", "Red Button") is True

    def test_a_confirmed_rename_returns_true(self, monkeypatch):
        monkeypatch.setattr(setup_zigbee, "mqtt_publish", lambda *a: None)
        monkeypatch.setattr(setup_zigbee, "mqtt_listen", _mock_mqtt_listen({
            RENAME_RESPONSE_TOPIC: json.dumps({"status": "ok"}),
            DEVICES_TOPIC: json.dumps([{"friendly_name": "Red Button"}]),
        }))
        assert setup_zigbee.rename_device("0xa4c1", "Red Button") is True

    def test_ok_without_the_device_list_actually_changing_returns_false(self, monkeypatch):
        """The regression this hardening exists for: Zigbee2MQTT (or a
        stale/unrelated retained message) reports "ok", but the device is
        still listed under its old name."""
        monkeypatch.setattr(setup_zigbee, "mqtt_publish", lambda *a: None)
        monkeypatch.setattr(setup_zigbee, "mqtt_listen", _mock_mqtt_listen({
            RENAME_RESPONSE_TOPIC: json.dumps({"status": "ok"}),
            DEVICES_TOPIC: json.dumps([{"friendly_name": "0xa4c1"}]),
        }))
        assert setup_zigbee.rename_device("0xa4c1", "Red Button") is False

    def test_an_error_response_returns_false(self, monkeypatch):
        monkeypatch.setattr(setup_zigbee, "mqtt_publish", lambda *a: None)
        monkeypatch.setattr(setup_zigbee, "mqtt_listen", _mock_mqtt_listen({
            RENAME_RESPONSE_TOPIC: json.dumps({"status": "error",
                                              "error": "no such device"}),
        }))
        assert setup_zigbee.rename_device("0xa4c1", "Red Button") is False

    def test_no_response_at_all_returns_false(self, monkeypatch):
        monkeypatch.setattr(setup_zigbee, "mqtt_publish", lambda *a: None)
        monkeypatch.setattr(setup_zigbee, "mqtt_listen", lambda topics, seconds: iter([]))
        assert setup_zigbee.rename_device("0xa4c1", "Red Button") is False

    def test_sends_the_from_and_to_names(self, monkeypatch):
        published = []
        monkeypatch.setattr(setup_zigbee, "mqtt_publish",
                            lambda topic, payload: published.append((topic, payload)))
        monkeypatch.setattr(setup_zigbee, "mqtt_listen", _mock_mqtt_listen({
            RENAME_RESPONSE_TOPIC: json.dumps({"status": "ok"}),
            DEVICES_TOPIC: json.dumps([{"friendly_name": "Red Button"}]),
        }))
        setup_zigbee.rename_device("0xa4c1", "Red Button")
        topic, payload = published[0]
        assert topic == "zigbee2mqtt/bridge/request/device/rename"
        assert json.loads(payload) == {"from": "0xa4c1", "to": "Red Button"}


class TestDeviceExists:
    def test_true_when_the_name_is_listed(self, monkeypatch):
        monkeypatch.setattr(setup_zigbee, "mqtt_listen", _mock_mqtt_listen({
            DEVICES_TOPIC: json.dumps([{"friendly_name": "Red Button"},
                                       {"friendly_name": "Green Button"}]),
        }))
        assert setup_zigbee.device_exists("Red Button") is True

    def test_false_when_it_is_not(self, monkeypatch):
        monkeypatch.setattr(setup_zigbee, "mqtt_listen", _mock_mqtt_listen({
            DEVICES_TOPIC: json.dumps([{"friendly_name": "Green Button"}]),
        }))
        assert setup_zigbee.device_exists("Red Button") is False

    def test_false_when_nothing_answers(self, monkeypatch):
        monkeypatch.setattr(setup_zigbee, "mqtt_listen", lambda topics, seconds: iter([]))
        assert setup_zigbee.device_exists("Red Button") is False

    def test_true_when_only_a_later_message_shows_the_rename(self, monkeypatch):
        """bridge/devices is retained: the first message on subscribe is
        often the pre-rename snapshot, with the republished, up-to-date
        list following a moment later. The first message alone must not
        decide the answer."""
        monkeypatch.setattr(setup_zigbee, "mqtt_listen", _mock_mqtt_listen({
            DEVICES_TOPIC: [
                json.dumps([{"friendly_name": "0xa4c1"}]),       # stale
                json.dumps([{"friendly_name": "Red Button"}]),   # republished
            ],
        }))
        assert setup_zigbee.device_exists("Red Button") is True


class TestReadingWhatTheBrokerPrints:
    """A button's friendly name is the owner's ("Yellow Button"), and it ends
    up in the topic. Split on the first space, as mosquitto_sub's -v output
    invites, the topic was cut in half and the payload was no longer JSON: the
    press was dropped in silence and pairing failed with "no button press
    detected" (measured 2026-09-20). It only ever worked because a button is
    renamed after its first pairing."""

    def test_a_name_with_a_space_survives(self):
        line = 'zigbee2mqtt/Yellow Button\t{"action":"single"}\n'
        topic, payload = setup_zigbee.split_message(line)
        assert topic == "zigbee2mqtt/Yellow Button"
        assert json.loads(payload)["action"] == "single"
        assert setup_zigbee.classify_message(topic, payload) == \
            ("press", "Yellow Button", "single")

    def test_a_plain_name_still_works(self):
        topic, payload = setup_zigbee.split_message(
            'zigbee2mqtt/0xa4c138\t{"action":"hold"}\n')
        assert setup_zigbee.classify_message(topic, payload) == \
            ("press", "0xa4c138", "hold")

    def test_a_line_without_the_separator_is_not_ours(self):
        assert setup_zigbee.split_message("something else\n") == ("", "")

    def test_the_listener_asks_for_that_format(self):
        """The parsing above only holds if the subscription prints it."""
        source = SCRIPT.read_text(encoding="utf-8")
        assert '"-F", "%t\\t%p"' in source
        assert '"-v"' not in source
