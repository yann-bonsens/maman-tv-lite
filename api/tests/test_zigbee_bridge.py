# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import button_bindings
import installation
import state
import zigbee_bridge
from conftest import settle_buttons


class FakeMessage:
    """Mimics a paho message: a topic and a payload in bytes."""

    def __init__(self, topic: str, payload):
        self.topic = topic
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        self.payload = payload.encode("utf-8") if isinstance(payload, str) else payload


def _message(action=None, device="living-room-button", raw=None):
    payload = raw if raw is not None else ({"action": action} if action else {})
    return FakeMessage(f"zigbee2mqtt/{device}", payload)


def _bound(command="tv", device="*", action="single"):
    """Bind one action and hand back the mock it resolves to."""
    handler = MagicMock()
    return (
        patch.dict(zigbee_bridge.BINDINGS, {device: {action: command}}, clear=True),
        patch.dict(zigbee_bridge.COMMANDS, {command: handler}, clear=False),
        handler,
    )


class TestActionDispatch:
    def test_bound_action_triggers_its_command(self):
        bindings, commands, handler = _bound()
        with bindings, commands:
            zigbee_bridge._on_message(None, None, _message("single"))
            settle_buttons()
        handler.assert_called_once()

    def test_unbound_action_is_ignored(self):
        bindings, commands, handler = _bound()
        with bindings, commands:
            zigbee_bridge._on_message(None, None, _message("hold"))
            settle_buttons()
        handler.assert_not_called()

    def test_device_name_is_extracted_from_topic(self):
        assert zigbee_bridge._device_from_topic("zigbee2mqtt/kitchen-button") == "kitchen-button"

    def test_device_specific_binding_wins_over_the_catch_all(self):
        """Adding a second button must not silently change what the first does."""
        specific, catch_all = MagicMock(), MagicMock()
        with patch.dict(
            zigbee_bridge.BINDINGS,
            {"kitchen-button": {"single": "diagnostic"}, "*": {"single": "tv"}},
            clear=True,
        ), patch.dict(
            zigbee_bridge.COMMANDS,
            {"diagnostic": specific, "tv": catch_all},
            clear=False,
        ):
            zigbee_bridge._on_message(None, None, _message("single", device="kitchen-button"))
            settle_buttons()
        specific.assert_called_once()
        catch_all.assert_not_called()

    def test_catch_all_applies_to_an_unknown_device(self):
        bindings, commands, handler = _bound(device="*")
        with bindings, commands:
            zigbee_bridge._on_message(None, None, _message("single", device="brand-new-button"))
            settle_buttons()
        handler.assert_called_once()


class TestBindingFile:
    """Which button does what is configuration, not code. A user with other
    hardware must never have to edit Python to make their button work.

    Loading the file itself is tested in test_button_bindings.py — this
    class covers what only zigbee_bridge.py knows: which command names are
    real."""

    def test_unknown_command_is_dropped_not_executed(self):
        """The binding file is data. It must never be able to name arbitrary
        Python: only the COMMANDS whitelist is reachable, checked at press
        time (_resolve + COMMANDS.get), not when the file is parsed."""
        assert "os.system" not in zigbee_bridge.COMMANDS
        with patch.dict(zigbee_bridge.BINDINGS, {"*": {"on": "os.system"}}, clear=True):
            zigbee_bridge._handle_action("any-device", "on")
            settle_buttons()
        assert zigbee_bridge._QUEUE.qsize() == 0, "an unknown command must never be queued"

    def test_every_advertised_command_is_callable(self):
        """The setup script offers this list to the user; a typo here would
        produce a binding that silently never fires."""
        for name, handler in zigbee_bridge.COMMANDS.items():
            assert callable(handler), name

    def test_setup_script_offers_exactly_these_commands(self):
        """scripts/setup-zigbee.py repeats the list so it can run without the
        API's virtualenv. Duplication is only acceptable while something
        notices when the two drift apart."""
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "scripts" / "setup-zigbee.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        advertised = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", None) == "COMMANDS"
        )
        assert sorted(advertised) == sorted(zigbee_bridge.COMMANDS)


class TestRobustness:
    """The MQTT thread must never die. If it does, the buttons stop working
    silently, and nobody finds out until someone presses one in vain."""

    def test_handler_exception_does_not_propagate(self):
        bindings, commands, handler = _bound()
        handler.side_effect = RuntimeError("cec unreachable")
        with bindings, commands:
            zigbee_bridge._on_message(None, None, _message("single"))  # must not raise
            settle_buttons()
        handler.assert_called_once()

    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            b"\xff\xfe binary",
            json.dumps({"battery": 87}),  # state update, no button press
            json.dumps({"action": ""}),
        ],
    )
    def test_payloads_without_usable_action_are_ignored(self, raw):
        bindings, commands, handler = _bound()
        with bindings, commands:
            zigbee_bridge._on_message(None, None, _message(raw=raw))
            settle_buttons()
        handler.assert_not_called()


class TestStartup:
    """The API must start even without MQTT: controlling the TV over HTTP is
    still useful without the buttons."""

    def test_returns_none_when_broker_unreachable(self):
        fake_client = MagicMock()
        fake_client.connect_async.side_effect = OSError("connection refused")
        with patch("paho.mqtt.client.Client", return_value=fake_client):
            assert zigbee_bridge.start() is None

    def test_subscribes_on_connect(self):
        client = MagicMock()
        zigbee_bridge._on_connect(client, None, None, 0)
        client.subscribe.assert_called_once_with(zigbee_bridge.MQTT_TOPIC)

    def test_stop_tolerates_none(self):
        zigbee_bridge.stop(None)  # must not raise


class TestDuplicatePresses:
    """Regression, from a real session: one press produced two MQTT publishes
    in the same second, with different link quality values — two distinct radio
    receptions. Acted on twice, tv turned the TV on and straight back
    off, so nothing appeared to happen and the person pressed again. The logs
    show eight presses in twenty seconds because of it."""

    def test_the_second_reception_of_one_press_is_ignored(self):
        command = MagicMock(return_value="ok")
        with patch.dict(zigbee_bridge.COMMANDS, {"tv": command}):
            zigbee_bridge._handle_action("0xabc", "single")
            settle_buttons()
            zigbee_bridge._handle_action("0xabc", "single")
        command.assert_called_once()

    def test_a_deliberate_press_later_still_works(self):
        """The window is short on purpose: nobody usefully presses an on/off
        button twice in a second, but they do press it again a few seconds on."""
        command = MagicMock(return_value="ok")
        with patch.dict(zigbee_bridge.COMMANDS, {"tv": command}), \
             patch("zigbee_bridge.BUTTON_DEBOUNCE_SECONDS", 0):
            zigbee_bridge._handle_action("0xabc", "single")
            settle_buttons()
            zigbee_bridge._handle_action("0xabc", "single")
            settle_buttons()
        assert command.call_count == 2

    def test_two_different_buttons_at_once_both_work(self):
        command = MagicMock(return_value="ok")
        with patch.dict(zigbee_bridge.COMMANDS, {"tv": command}):
            zigbee_bridge._handle_action("0xaaa", "single")
            settle_buttons()
            zigbee_bridge._handle_action("0xbbb", "single")
            settle_buttons()
        assert command.call_count == 2

    def test_a_different_action_on_the_same_button_still_works(self):
        """The window is per action, not per button: one button can carry the
        television on a short press and the diagnostic on a long one."""
        toggle, diagnostic = MagicMock(return_value="ok"), MagicMock(return_value="ok")
        with patch.dict(zigbee_bridge.COMMANDS,
                        {"tv": toggle, "diagnostic": diagnostic}), \
             patch.object(zigbee_bridge, "BINDINGS",
                          {"*": {"single": "tv", "hold": "diagnostic"}}):
            zigbee_bridge._handle_action("0xabc", "single")
            settle_buttons()
            zigbee_bridge._handle_action("0xabc", "hold")
            settle_buttons()
        toggle.assert_called_once()
        diagnostic.assert_called_once()

    def test_a_command_that_raised_still_blocks_its_duplicate(self):
        """A failure is no reason to let the duplicate through and run it
        twice: the window opens in a finally."""
        command = MagicMock(side_effect=RuntimeError("TV unreachable"))
        with patch.dict(zigbee_bridge.COMMANDS, {"tv": command}):
            zigbee_bridge._handle_action("0xabc", "single")
            settle_buttons()
            zigbee_bridge._handle_action("0xabc", "single")
        command.assert_called_once()
class TestAPressDuringALongCommand:
    """Dropping every press that landed while something ran made the button
    feel dead: measured on the Pi 5 box, a genuine press nine seconds into a
    thirteen-second wake was discarded and nothing happened at all."""

    def _slow(self, calls, started, release):
        def slow():
            calls.append(1)
            started.set()
            release.wait(5)
            return "done"
        return slow

    def test_a_second_reception_of_the_same_press_is_still_dropped(self):
        started, release, calls = threading.Event(), threading.Event(), []
        with patch.dict(zigbee_bridge.COMMANDS,
                        {"tv": self._slow(calls, started, release)}):
            zigbee_bridge._handle_action("0xabc", "single")
            assert started.wait(5)
            zigbee_bridge._handle_action("0xabc", "single")   # within the window
            release.set()
            settle_buttons()
        assert len(calls) == 1

    def test_but_a_real_press_later_waits_its_turn(self):
        started, release, calls = threading.Event(), threading.Event(), []
        with patch.dict(zigbee_bridge.COMMANDS,
                        {"tv": self._slow(calls, started, release)}), \
             patch("zigbee_bridge.BUTTON_DEBOUNCE_SECONDS", 0.05):
            zigbee_bridge._handle_action("0xabc", "single")
            assert started.wait(5)
            time.sleep(0.1)          # past the duplicate window
            zigbee_bridge._handle_action("0xabc", "single")
            release.set()
            settle_buttons()
        assert len(calls) == 2

    def test_and_impatient_presses_collapse_into_one(self):
        started, release, calls = threading.Event(), threading.Event(), []
        with patch.dict(zigbee_bridge.COMMANDS,
                        {"tv": self._slow(calls, started, release)}), \
             patch("zigbee_bridge.BUTTON_DEBOUNCE_SECONDS", 0.05):
            zigbee_bridge._handle_action("0xabc", "single")
            assert started.wait(5)
            time.sleep(0.1)
            for _ in range(5):
                zigbee_bridge._handle_action("0xabc", "single")
            release.set()
            settle_buttons()
        assert len(calls) == 2


class TestRoutingDuringInstallation:
    """The buttons have no other meaning until the mode ends: the two
    everyday ones answer the current question, as what they normally do —
    "tv" is yes, "music" is no — and anything else is inert."""

    def setup_method(self):
        state.set_mode(state.INSTALLATION)
        installation.channel.reset()

    def teardown_method(self):
        installation.channel.abandon()
        state.set_mode(state.TELEVISION)

    def _ask(self, accepts):
        published = []
        answers = []
        thread = threading.Thread(
            target=lambda: answers.append(
                installation.channel.ask("body", accepts, seconds=5,
                                         publish=published.append)))
        thread.start()
        for _ in range(200):
            if published:
                break
            time.sleep(0.01)
        return thread, answers, published[0] if published else None

    def test_the_tv_button_answers_yes(self):
        with patch.dict(zigbee_bridge.BINDINGS, {"*": {"single": "tv"}}, clear=True):
            thread, answers, question = self._ask({"tv": "oui", "music": "non"})
            assert question is not None
            zigbee_bridge._handle_action("any-device", "single")
            settle_buttons()
            thread.join(timeout=2)
        assert answers == ["tv"]

    def test_the_music_button_answers_no(self):
        with patch.dict(zigbee_bridge.BINDINGS, {"*": {"single": "music"}}, clear=True):
            thread, answers, question = self._ask({"tv": "oui", "music": "non"})
            assert question is not None
            zigbee_bridge._handle_action("any-device", "single")
            settle_buttons()
            thread.join(timeout=2)
        assert answers == ["music"]

    def test_a_press_bound_to_anything_else_is_ignored(self):
        command = MagicMock(return_value="ok")
        with patch.dict(zigbee_bridge.COMMANDS, {"diagnostic": command}), \
             patch.dict(zigbee_bridge.BINDINGS, {"*": {"single": "diagnostic"}}, clear=True):
            thread, answers, question = self._ask({"tv": "oui", "music": "non"})
            assert question is not None
            zigbee_bridge._handle_action("any-device", "single")
            settle_buttons()
            installation.channel.abandon()
            thread.join(timeout=2)
        command.assert_not_called()
        assert answers == [None]

    def test_normal_mode_dispatch_is_unaffected(self):
        """The same binding, outside installation, still calls the command —
        this feature must not change everyday behaviour."""
        state.set_mode(state.TELEVISION)
        command = MagicMock(return_value="ok")
        with patch.dict(zigbee_bridge.COMMANDS, {"tv": command}), \
             patch.dict(zigbee_bridge.BINDINGS, {"*": {"single": "tv"}}, clear=True):
            zigbee_bridge._handle_action("any-device", "single")
            settle_buttons()
        command.assert_called_once()


class TestPairingTakesEffectWithoutARestart:
    """Real-hardware bug: pairing a button through the installation screen
    wrote the right thing to buttons.json and even showed the right name on
    screen, but the button itself stayed unrecognised until the API was
    restarted by hand. Root cause: button_bindings.reload() used to rebind
    its own module-level BINDINGS name to a brand-new dict, but this module
    imports the name directly (`from button_bindings import BINDINGS`,
    above) — a copy of the dict *object* taken once, at import time, that a
    later rebind elsewhere never touches. These tests go through the real
    write_bindings()/reload() path deliberately, with no patch.dict: every
    other test in this file uses patch.dict, which mutates the dict in
    place and would never have caught this — the bug only shows up through
    the exact sequence a real pairing procedure uses.
    """

    def test_a_freshly_bound_device_is_recognised_without_restarting(self, tmp_path, monkeypatch):
        """Bound to "music", deliberately, not "tv": DEFAULT_BINDINGS's own
        catch-all ({"*": {"single": "tv"}}) would resolve "single" to "tv"
        anyway even off a completely stale BINDINGS object, which would
        make this test pass by coincidence for the wrong reason if it used
        "tv". "music" only ever resolves correctly if the reload was
        actually seen.
        """
        path = tmp_path / "buttons.json"
        monkeypatch.setattr(button_bindings, "BINDINGS_PATH", str(path))
        button_bindings.write_bindings({"bindings": {}}, str(path))
        button_bindings.reload()
        state.set_mode(state.TELEVISION)
        tv_command = MagicMock(return_value="ok")
        music_command = MagicMock(return_value="ok")

        button_bindings.set_binding("music", "Bouton Vert", ["single"], str(path))
        button_bindings.reload()

        with patch.dict(zigbee_bridge.COMMANDS, {"tv": tv_command, "music": music_command}):
            zigbee_bridge._handle_action("Bouton Vert", "single")
            settle_buttons()
        music_command.assert_called_once()
        tv_command.assert_not_called()


class TestAnsweringTheInstallationScreen:
    """A press answers the question that was on screen when it was pressed.

    The id used to be read inside the worker, which made it match by
    construction — so the channel's check could never drop anything, and a
    press waiting its turn behind a running action would answer whichever
    question had come up meanwhile. In a search that credits the wrong
    technique.
    """

    def test_the_question_id_is_captured_when_the_press_arrives(self, monkeypatch):
        import installation
        installation.channel.reset()
        asked: "list[int]" = []
        monkeypatch.setattr(installation.channel, "answer",
                            lambda id, token: asked.append(id) or True)

        published = []
        thread = threading.Thread(
            target=lambda: published.append(
                installation.channel.ask("first", {"tv": "yes"}, seconds=2)),
            daemon=True)
        thread.start()
        deadline = time.monotonic() + 2
        while installation.channel.current() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        first = installation.channel.current()
        assert first is not None

        # The press is resolved now; the worker runs it later, by which time
        # the channel has moved on to another question.
        handler = zigbee_bridge._installation_answer("tv", first.id)
        installation.channel.abandon()
        thread.join(timeout=2)
        installation.channel.reset()
        installation.channel.ask("second", {"tv": "yes"}, seconds=0)

        handler()
        assert asked == [first.id], "the answer carried the wrong question"

    def test_a_press_with_no_question_on_screen_answers_nothing(self):
        import installation
        installation.channel.reset()
        assert "no question" in zigbee_bridge._installation_answer("tv", None)()
