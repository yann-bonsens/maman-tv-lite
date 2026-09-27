# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""zigbee_pairing.py's protocol logic, against a fake MQTT connection — the
paho equivalent of test_setup_zigbee.py's `_mock_mqtt_listen`, since this
module's own tests must not need a real broker.
"""

import json
from pathlib import Path

import zigbee_pairing as zp

# Anchored on this file, not on the working directory: CI runs pytest
# from api/, where a relative "api/..." path does not exist.
REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeConnection:
    """Stands in for `zp._Connection`: `by_topic` maps a topic
    to one payload or a list of them (delivered in order, the same "stale
    snapshot then republish" shape `test_setup_zigbee.py` already models
    for a retained message)."""

    def __init__(self, by_topic: dict):
        self.by_topic = by_topic
        self.subscribed: "list[str]" = []
        self.published: "list[tuple[str, str]]" = []

    def subscribe(self, *topics: str) -> None:
        self.subscribed.extend(topics)

    def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))

    @staticmethod
    def _matches(pattern: str, topic: str) -> bool:
        pattern_parts, topic_parts = pattern.split("/"), topic.split("/")
        return len(pattern_parts) == len(topic_parts) and all(
            p == "+" or p == t
            for p, t in zip(pattern_parts, topic_parts, strict=True))

    def listen(self, seconds: float):
        for topic, value in self.by_topic.items():
            if not any(self._matches(pattern, topic) for pattern in self.subscribed):
                continue
            payloads = value if isinstance(value, list) else [value]
            for payload in payloads:
                yield topic, payload

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *exc) -> None:
        pass


def _fake_connect(by_topic: dict):
    return lambda: FakeConnection(by_topic)


BRIDGE_STATE = "zigbee2mqtt/bridge/state"
BRIDGE_INFO = "zigbee2mqtt/bridge/info"
RENAME_RESPONSE = "zigbee2mqtt/bridge/response/device/rename"
DEVICES = "zigbee2mqtt/bridge/devices"


class TestBridgeReady:
    def test_true_when_online(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({
            BRIDGE_STATE: json.dumps({"state": "online"}),
        }))
        assert zp.bridge_ready(5) is True

    def test_false_when_nothing_answers(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({}))
        assert zp.bridge_ready(5) is False


class TestOpenPairing:
    def test_true_once_the_bridge_confirms(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({
            BRIDGE_INFO: json.dumps({"permit_join": True}),
        }))
        assert zp.open_pairing(60) is True

    def test_capped_at_the_protocol_limit(self, monkeypatch):
        seen = []

        class Capturing(FakeConnection):
            def publish(self, topic, payload):
                seen.append(payload)
                super().publish(topic, payload)

        monkeypatch.setattr(zp, "_connect",
                            lambda: Capturing({BRIDGE_INFO: json.dumps({"permit_join": True})}))
        zp.open_pairing(9999)
        assert all(json.loads(p).get("time", 0) <= zp.PERMIT_JOIN_MAX_SECONDS
                  for p in seen)

    def test_false_when_the_bridge_never_confirms(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({}))
        monkeypatch.setattr(zp, "PERMIT_JOIN_RETRY_SECONDS", 0)
        assert zp.open_pairing(5) is False


class TestCapturePress:
    def test_returns_the_device_and_action(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({
            "zigbee2mqtt/Red Button": json.dumps({"action": "single"}),
        }))
        assert zp.capture_press(5) == ("Red Button", "single")

    def test_none_on_a_timeout(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({}))
        assert zp.capture_press(5) is None

    def test_state_updates_with_no_action_are_ignored(self, monkeypatch):
        """Battery level, link quality: not a press."""
        monkeypatch.setattr(zp, "_connect", _fake_connect({
            "zigbee2mqtt/Red Button": json.dumps({"battery": 80}),
        }))
        assert zp.capture_press(5) is None


def _fail_if_connected():
    raise AssertionError("must not open a connection for a no-op rename")


class TestRenameDevice:
    def test_the_same_name_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fail_if_connected)
        assert zp.rename_device("Red Button", "Red Button") is True

    def test_a_confirmed_rename_returns_true(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({
            RENAME_RESPONSE: json.dumps({"status": "ok"}),
            DEVICES: json.dumps([{"friendly_name": "TV"}]),
        }))
        assert zp.rename_device("0xa4c1", "TV") is True

    def test_ok_without_the_device_list_actually_changing_returns_false(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({
            RENAME_RESPONSE: json.dumps({"status": "ok"}),
            DEVICES: json.dumps([{"friendly_name": "0xa4c1"}]),
        }))
        assert zp.rename_device("0xa4c1", "TV") is False

    def test_an_error_response_returns_false(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({
            RENAME_RESPONSE: json.dumps({"status": "error", "error": "no such device"}),
        }))
        assert zp.rename_device("0xa4c1", "TV") is False

    def test_no_response_at_all_returns_false(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({}))
        assert zp.rename_device("0xa4c1", "TV") is False


class TestDeviceActions:
    def test_reads_the_exposed_action_values(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({
            DEVICES: json.dumps([{
                "friendly_name": "TV",
                "definition": {"exposes": [
                    {"property": "action", "values": ["single", "double", "hold"]},
                ]},
            }]),
        }))
        assert zp.device_actions("TV") == ["single", "double", "hold"]

    def test_empty_when_the_device_is_not_listed(self, monkeypatch):
        monkeypatch.setattr(zp, "_connect", _fake_connect({
            DEVICES: json.dumps([{"friendly_name": "Other"}]),
        }))
        assert zp.device_actions("TV") == []


class TestListAdapters:
    def test_lists_serial_devices_by_their_stable_full_path(self, tmp_path, monkeypatch):
        """Paths, not names: what this returns goes straight into
        Zigbee2MQTT's `port:`, and a bare name there is a crash loop."""
        by_id = tmp_path / "by-id"
        by_id.mkdir()
        (by_id / "usb-Silicon_Labs-if00").touch()
        (by_id / "usb-Another-if00").touch()
        monkeypatch.setattr(zp, "SERIAL_DIR", by_id)
        assert zp.list_adapters() == [str(by_id / "usb-Another-if00"),
                                      str(by_id / "usb-Silicon_Labs-if00")]

    def test_empty_when_nothing_is_plugged_in(self, tmp_path, monkeypatch):
        monkeypatch.setattr(zp, "SERIAL_DIR", tmp_path / "does-not-exist")
        assert zp.list_adapters() == []


class TestSetSerialPort:
    """The write path is a direct port of scripts/setup-zigbee.py's own
    set_serial_port() (see its docstring for the full reasoning, including
    the folded-value repair); these tests confirm the port kept that
    behaviour, not re-prove logic already pinned there."""

    BASE = (
        "serial:\n"
        "    port: /dev/ttyUSB0\n"
        "mqtt:\n"
        '    server: "mqtt://localhost"\n'
    )

    def test_rewrites_the_port(self, tmp_path):
        path = tmp_path / "configuration.yaml"
        path.write_text(self.BASE)
        assert zp._set_serial_port(path, "/dev/serial/by-id/usb-X") is True
        assert "    port: /dev/serial/by-id/usb-X\n" in path.read_text()

    def test_leaves_the_mqtt_broker_port_alone(self, tmp_path):
        path = tmp_path / "configuration.yaml"
        path.write_text(self.BASE + "    port: 1883\n")
        zp._set_serial_port(path, "/dev/serial/by-id/usb-X")
        assert "    port: 1883\n" in path.read_text()

    def test_a_missing_file_is_reported_not_raised(self, tmp_path):
        assert zp._set_serial_port(tmp_path / "absent.yaml", "/dev/x") is False


class TestSetAdapter:
    def test_inserts_the_adapter_after_the_port(self, tmp_path):
        path = tmp_path / "configuration.yaml"
        path.write_text("serial:\n  port: /dev/serial/by-id/usb-X\n")
        assert zp._set_adapter(path, "ember") is True
        assert "  port: /dev/serial/by-id/usb-X\n  adapter: ember\n" in path.read_text()

    def test_replaces_an_existing_adapter(self, tmp_path):
        path = tmp_path / "configuration.yaml"
        path.write_text("serial:\n  port: /dev/x\n  adapter: zstack\n")
        zp._set_adapter(path, "ember")
        text = path.read_text()
        assert "adapter: ember" in text
        assert "zstack" not in text


class MockCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestSystemctlCalls:
    def test_restart_bridge_calls_systemctl_restart(self, monkeypatch):
        calls = []
        monkeypatch.setattr(zp.subprocess, "run", lambda cmd, **kw:
                            calls.append(cmd) or MockCompleted(0))
        assert zp.restart_bridge() is True
        assert calls == [["systemctl", "restart", "zigbee2mqtt"]]

    def test_stop_bridge_calls_systemctl_stop(self, monkeypatch):
        calls = []
        monkeypatch.setattr(zp.subprocess, "run", lambda cmd, **kw:
                            calls.append(cmd) or MockCompleted(0))
        assert zp.stop_bridge() is True
        assert calls == [["systemctl", "stop", "zigbee2mqtt"]]

    def test_a_denied_call_is_reported_not_raised(self, monkeypatch):
        """A polkit rule that is missing or scoped wrong must not crash the
        installation screen — it must just fail to bring the bridge up,
        exactly like a missing adapter does."""
        monkeypatch.setattr(zp.subprocess, "run", lambda cmd, **kw:
                            MockCompleted(1, stderr="Interactive authentication required."))
        assert zp.restart_bridge() is False


class TestBringBridgeUp:
    """The orchestration new to this module: write the port, restart, and
    if the bridge does not answer, try each known chipset in turn before
    giving up — scripts/setup-zigbee.py's own fallback, ported."""

    def test_succeeds_on_plain_autodetection(self, monkeypatch, tmp_path):
        config = tmp_path / "configuration.yaml"
        config.write_text("serial:\n  port: /dev/x\n")
        monkeypatch.setattr(zp, "restart_bridge", lambda: True)
        monkeypatch.setattr(zp, "bridge_ready", lambda seconds: True)
        adapters_tried = []
        monkeypatch.setattr(zp, "_set_adapter",
                            lambda path, name: adapters_tried.append(name))
        assert zp.bring_bridge_up("/dev/serial/by-id/usb-X", config) is True
        assert adapters_tried == []  # never needed the chipset fallback

    def test_falls_back_through_every_chipset_in_order(self, monkeypatch, tmp_path):
        config = tmp_path / "configuration.yaml"
        config.write_text("serial:\n  port: /dev/x\n")
        monkeypatch.setattr(zp, "restart_bridge", lambda: True)
        tried = []

        def fake_ready(seconds):
            # Succeeds only once every chipset up to "deconz" (the third)
            # has been tried, so the order actually matters to this test.
            return tried[-1:] == ["deconz"] if tried else False

        monkeypatch.setattr(zp, "bridge_ready", fake_ready)
        monkeypatch.setattr(zp, "_set_adapter",
                            lambda path, name: tried.append(name))
        assert zp.bring_bridge_up("/dev/serial/by-id/usb-X", config) is True
        assert tried == ["ember", "zstack", "deconz"]

    def test_false_when_nothing_works(self, monkeypatch, tmp_path):
        config = tmp_path / "configuration.yaml"
        config.write_text("serial:\n  port: /dev/x\n")
        monkeypatch.setattr(zp, "restart_bridge", lambda: True)
        monkeypatch.setattr(zp, "bridge_ready", lambda seconds: False)
        monkeypatch.setattr(zp, "_set_adapter", lambda path, name: None)
        assert zp.bring_bridge_up("/dev/serial/by-id/usb-X", config) is False


class TestTheAdapterPath:
    """What `list_adapters()` returns is written straight into Zigbee2MQTT's
    `port:`, so it has to be something Zigbee2MQTT can open.

    It returned the bare `.name` on 2026-09-25. Zigbee2MQTT then died on
    every start with "No such file or directory, cannot open
    usb-Itead_Sonoff_...-if00-port0" — a name that plainly existed, in a
    directory nobody had told it about — and with Restart=always it went
    round for ever at about 50 s of CPU per attempt, on one core, while the
    installation screen sat on "Starting it up".
    """

    def test_adapters_are_listed_as_full_paths(self, tmp_path, monkeypatch):
        serial = tmp_path / "by-id"
        serial.mkdir()
        (serial / "usb-Some_Dongle-if00-port0").write_text("")
        monkeypatch.setattr(zp, "SERIAL_DIR", serial)
        found = zp.list_adapters()
        assert found == [str(serial / "usb-Some_Dongle-if00-port0")]
        assert found[0].startswith("/"), "Zigbee2MQTT cannot open a bare name"

    def test_the_port_written_to_the_configuration_is_openable(self, tmp_path, monkeypatch):
        serial = tmp_path / "by-id"
        serial.mkdir()
        device = serial / "usb-Some_Dongle-if00-port0"
        device.write_text("")
        config = tmp_path / "configuration.yaml"
        config.write_text("serial:\n  port: /dev/ttyUSB9\n")
        monkeypatch.setattr(zp, "SERIAL_DIR", serial)
        monkeypatch.setattr(zp, "restart_bridge", lambda: True)
        monkeypatch.setattr(zp, "bridge_ready", lambda seconds: True)

        assert zp.bring_bridge_up(str(device), config) is True
        written = config.read_text()
        assert str(device) in written, written

    def test_a_bare_name_is_still_resolved(self, tmp_path, monkeypatch):
        """The one door every caller comes through, so it repairs the fault
        rather than trusting every caller not to reintroduce it."""
        serial = tmp_path / "by-id"
        serial.mkdir()
        (serial / "usb-Some_Dongle-if00-port0").write_text("")
        config = tmp_path / "configuration.yaml"
        config.write_text("serial:\n  port: /dev/ttyUSB9\n")
        monkeypatch.setattr(zp, "SERIAL_DIR", serial)
        monkeypatch.setattr(zp, "restart_bridge", lambda: True)
        monkeypatch.setattr(zp, "bridge_ready", lambda seconds: True)

        zp.bring_bridge_up("usb-Some_Dongle-if00-port0", config)
        assert str(serial / "usb-Some_Dongle-if00-port0") in config.read_text()


def test_the_two_adapter_listings_agree():
    """`scripts/setup-zigbee.py` has always returned paths and only shown
    `.name` to a person. The API's own copy returned the name itself, and
    that divergence is the whole of the 2026-09-25 crash loop."""
    script = (REPO_ROOT / "scripts" / "setup-zigbee.py").read_text(encoding="utf-8")
    listing = script[script.index("def list_adapters("):]
    listing = listing[:listing.index("\ndef ", 1)]
    assert ".name" not in listing, "the script returns paths; so must the API"


class TestBringingTheBridgeUpWithoutKillingIt:
    """Zigbee2MQTT takes about two minutes to become usable on this board:
    45 s from systemd's "Started" to its own first log line, and the serial
    port about fifty seconds after that. Anything that restarts it inside
    that window throws the whole two minutes away and starts again.

    Measured on 2026-09-25: the wait expired eleven seconds after
    "Serialport opened", so the loop killed the attempt that had just
    succeeded, rewrote the chipset and restarted. The installation screen
    sat on "Starting it up" while the box did that over and over.
    """

    def _config(self, tmp_path):
        config = tmp_path / "configuration.yaml"
        config.write_text("serial:\n  port: /dev/serial/by-id/right\n  adapter: zstack\n")
        return config

    def test_the_window_is_longer_than_the_board_takes_to_start(self):
        assert zp.BRIDGE_BRING_UP_SECONDS >= 150, (
            "measured: 45 s to the first log line, the serial port about "
            "fifty seconds later")

    def test_a_running_bridge_with_the_right_port_is_not_restarted(self, tmp_path, monkeypatch):
        restarts = []
        monkeypatch.setattr(zp, "restart_bridge", lambda: restarts.append(1) or True)
        monkeypatch.setattr(zp, "bridge_running", lambda: True)
        monkeypatch.setattr(zp, "bridge_ready", lambda seconds: True)
        assert zp.bring_bridge_up("/dev/serial/by-id/right", self._config(tmp_path))
        assert restarts == [], "it was already starting; restarting throws that away"

    def test_a_new_port_is_applied_with_a_restart(self, tmp_path, monkeypatch):
        restarts = []
        monkeypatch.setattr(zp, "restart_bridge", lambda: restarts.append(1) or True)
        monkeypatch.setattr(zp, "bridge_running", lambda: True)
        monkeypatch.setattr(zp, "bridge_ready", lambda seconds: True)
        zp.bring_bridge_up("/dev/serial/by-id/different", self._config(tmp_path))
        assert restarts == [1], "a configuration change has to be applied"

    def test_a_stopped_bridge_is_started_even_when_nothing_changed(self, tmp_path, monkeypatch):
        restarts = []
        monkeypatch.setattr(zp, "restart_bridge", lambda: restarts.append(1) or True)
        monkeypatch.setattr(zp, "bridge_running", lambda: False)
        monkeypatch.setattr(zp, "bridge_ready", lambda seconds: True)
        zp.bring_bridge_up("/dev/serial/by-id/right", self._config(tmp_path))
        assert restarts == [1]
