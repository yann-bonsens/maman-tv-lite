# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Bridge between the Zigbee buttons and the TV commands.

Subscribes to what Zigbee2MQTT publishes and triggers the matching action.
Deliberately hosted INSIDE the API process rather than in a separate service:

- a single process drives the CEC bus, so a button press and a concurrent HTTP
  request cannot interleave their frames (the lock in cec_controller
  serialises both callers);
- no HTTP round-trip over the loopback interface just to call Python code that
  is already loaded;
- no API credentials to store somewhere in order to call ourselves.

Which button does what is NOT written in this file. It comes from a JSON
binding file that `scripts/setup-zigbee.py` generates by watching a real
button being pressed, because action names differ from one model to the next
("single", "on", "toggle", "1_single"...). Guessing them is how this breaks
for anyone whose hardware differs.
"""

import json
import logging
import os
import queue
import threading
import time

import cec_controller as cec
import installation
import modes
import state
from button_bindings import ANY_DEVICE, BINDINGS

logger = logging.getLogger(__name__)

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# Zigbee2MQTT publishes each device state on zigbee2mqtt/<name>. The "+"
# wildcard matches a single level, so internal topics (zigbee2mqtt/bridge/...)
# are naturally excluded.
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "zigbee2mqtt/+")

# Commands a button may be bound to. This is an explicit whitelist: a name in
# the JSON file is looked up here, never resolved dynamically against the
# module. A binding file is ordinary configuration, and configuration must not
# be able to call arbitrary code.
#
# Only zero-argument functions belong here. Which command name is valid lives
# HERE, not in button_bindings.load_bindings(): that one only knows the
# file's shape (device -> {action: command name}), not what a name means —
# an unrecognised one is simply never found in this dict, at press time
# (_resolve below), which is what keeps a binding file unable to name
# arbitrary Python regardless of where the file itself was parsed.
COMMANDS = {
    # The two everyday buttons. What a press means depends on what the box is
    # doing, which is the mode machine's business, not the bridge's — except
    # during installation, where these two are answers to a question instead
    # (see _handle_action).
    "tv": modes.tv_button,
    "music": modes.music_button,
    # The two administrative modes. Bound only where somebody asks for them:
    # on a box with a network, they are an API call away, and a button that
    # nobody meant to press is a button that changes the box by accident.
    "diagnostic": modes.diagnostic_button,
    "installation": modes.installation_button,
    # Raw, for a box whose owner wants a plain on/off button.
    "power_on": cec.power_on,
    "standby": cec.standby,
}


def _resolve(device: str, action: str):
    """Find the command NAME for this device and action, or None.

    The device's own bindings win over the "*" catch-all, so a single button
    can be given its own behaviour without disturbing the others.
    """
    for key in (device, ANY_DEVICE):
        command = BINDINGS.get(key, {}).get(action)
        if command:
            return command
    return None


def _device_from_topic(topic: str) -> str:
    return topic.split("/", 1)[-1]


# ============================================================================
# SETTING: ZIGBEE_DEBOUNCE_SECONDS
# ----------------------------------------------------------------------------
# A single press can reach us twice. Captured from a real session: two MQTT
# publishes for one press, in the same second, with DIFFERENT link quality
# values — so two distinct radio receptions, not a duplicated log line. Acted
# on twice, `power_toggle` turns the TV on and straight back off, and the
# person in front of it sees nothing happen and presses again. That is exactly
# the burst of eight presses in twenty seconds found in the logs.
#
# The window is measured from the END of the previous identical action, not its
# start. The MQTT callback runs the command synchronously, so a duplicate that
# arrived 200 ms in sits in the socket buffer until the first one finishes —
# which for a learning search is several seconds. Measuring from the start
# would let it through precisely when the command was slowest.
#
# Only an identical action from the same device is suppressed, so two different
# buttons pressed together both work.
# ============================================================================
BUTTON_DEBOUNCE_SECONDS = float(os.environ.get("ZIGBEE_DEBOUNCE_SECONDS", "1.5"))

_last_handled: dict = {}
_debounce_lock = threading.Lock()


def _repeat_reason(device: str, action: str) -> "str | None":
    """Why this action is not worth running, or None to run it.

    While an action runs, the debounce window applies from the moment it
    started. A second reception of one press arrives within a second, so it is
    caught; a press that comes later is a person pressing again and is queued,
    not thrown away. Dropping every press that landed during a running action
    was what made the button feel dead: measured on the Pi 5 box, a genuine
    press nine seconds into a thirteen-second wake was discarded and the viewer
    saw nothing happen at all. One press waits its turn, and further ones
    collapse into it — the television is never sent the same order twice over.
    """
    if BUTTON_DEBOUNCE_SECONDS <= 0:
        return None
    now = time.monotonic()
    with _debounce_lock:
        if (_in_flight == (device, action)
                and (now - _in_flight_since) < BUTTON_DEBOUNCE_SECONDS):
            return "a second reception of the press being handled"
        if (device, action) in _pending:
            return "one of these is already waiting its turn"
        finished_at = _last_handled.get((device, action))
    if finished_at is not None and (now - finished_at) < BUTTON_DEBOUNCE_SECONDS:
        return f"repeat within {BUTTON_DEBOUNCE_SECONDS:.1f}s"
    return None


def _mark_handled(device: str, action: str) -> None:
    with _debounce_lock:
        _last_handled[(device, action)] = time.monotonic()


# Commands that must not wait their turn.

# Button actions are executed here, one at a time, and never in the MQTT
# callback.
#
# Running them inline looked simpler and had two faults. A search can take a
# minute and a half, and for all that time paho's network thread is blocked:
# no further press is even read from the socket — the stop button was queued
# behind the very search it was meant to stop, and arrived after it, wiping
# what had just been learned. That same block also starves MQTT's keepalive,
# so a long search risks being disconnected from the broker.
#
# One worker, so two presses still cannot interleave on the CEC bus.
_QUEUE: "queue.Queue" = queue.Queue()
_worker: "threading.Thread | None" = None
_in_flight: "tuple[str, str] | None" = None
_in_flight_since = 0.0
# Presses queued and not started yet: at most one per device and action.
_pending: set = set()


def _run_queued_actions() -> None:
    global _in_flight, _in_flight_since
    while True:
        device, action, handler, name = _QUEUE.get()
        with _debounce_lock:
            _pending.discard((device, action))
            _in_flight = (device, action)
            _in_flight_since = time.monotonic()
        try:
            logger.info("Button %s: action %r -> %s", device, action, name)
            logger.info("Button %s: %s", device, handler())
        except Exception:
            logger.exception("Button %s: action %r failed", device, action)
        finally:
            with _debounce_lock:
                _in_flight = None
            # In `finally` so a command that raised still starts the window: a
            # failure is no reason to let the duplicate through and run it twice.
            _mark_handled(device, action)
            _QUEUE.task_done()


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run_queued_actions,
                                   name="button-actions", daemon=True)
        _worker.start()


def _drop_pending() -> None:
    """Forget presses waiting their turn. Stopping means stopping."""
    dropped = 0
    with _debounce_lock:
        _pending.clear()
    while True:
        try:
            _QUEUE.get_nowait()
        except queue.Empty:
            break
        _QUEUE.task_done()
        dropped += 1
    if dropped:
        logger.info("dropped %d queued button action(s)", dropped)


def _installation_answer(command: str, question_id: "int | None"):
    """A zero-argument callable answering the installation screen's question
    `question_id` with `command` ("tv" or "music") — the same shape as an
    entry in COMMANDS, so it can go through the same queue.

    **The id is captured when the press arrives, not when the worker gets to
    it.** That is the whole point of the channel's id check, and an earlier
    version defeated it: it read `channel.current()` inside the worker, so
    the id always matched by construction and the check could never drop
    anything. A press that waits its turn behind a running action would then
    answer whatever question had come up in the meantime — in a search, that
    credits the wrong technique, which is the one failure this design exists
    to prevent.
    """
    def answer() -> str:
        if question_id is None:
            return "no question to answer"
        accepted = installation.channel.answer(question_id, command)
        return f"answered {command!r} ({'accepted' if accepted else 'dropped'})"
    return answer


def _handle_action(device: str, action: str) -> None:
    reason = _repeat_reason(device, action)
    if reason is not None:
        logger.info("Button %s: action %r ignored (%s)", device, action, reason)
        return

    command = _resolve(device, action)
    if command is None:
        logger.info("Button %s: action %r ignored (not bound)", device, action)
        return

    if state.mode() == state.INSTALLATION:
        # The buttons have no other meaning until the mode ends: only the two
        # everyday ones answer a question, and as what they normally do —
        # "tv" is yes, "music" is no — matching the spec's own vocabulary.
        if command not in ("tv", "music"):
            logger.info("Button %s: action %r ignored (%r has no meaning "
                       "during installation)", device, action, command)
            return
        # Read here, in the thread the press arrived on, so the answer stays
        # attached to the question that was actually on screen at the time.
        question = installation.channel.current()
        handler = _installation_answer(command, question.id if question else None)
        name = f"installation:{command}"
    else:
        handler = COMMANDS.get(command)
        if handler is None:
            logger.info("Button %s: action %r ignored (unknown command %r)",
                       device, action, command)
            return
        name = command

    _ensure_worker()
    with _debounce_lock:
        _pending.add((device, action))
    _QUEUE.put((device, action, handler, name))


def _on_message(client, userdata, message) -> None:
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return  # non-JSON message (assorted state topics): not for us

    action = payload.get("action")
    if not action:
        return  # state update with no button press (battery, link quality...)

    _handle_action(_device_from_topic(message.topic), action)


def _on_connect(client, userdata, flags, reason_code, properties=None) -> None:
    logger.info("MQTT connected, subscribing to %s", MQTT_TOPIC)
    client.subscribe(MQTT_TOPIC)


def start() -> "object | None":
    """Start listening to MQTT in the background.

    Never blocks API startup and never raises: if the broker is unreachable (or
    paho is not installed) the API must still come up — controlling the TV over
    HTTP remains useful without the buttons. paho reconnects on its own
    afterwards.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.warning("paho-mqtt missing: Zigbee buttons disabled")
        return None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = _on_connect
    client.on_message = _on_message
    try:
        client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        client.loop_start()
    except Exception:
        logger.exception("Cannot start MQTT: Zigbee buttons inactive")
        return None

    logger.info("Zigbee bridge started (%s:%s)", MQTT_HOST, MQTT_PORT)
    return client


def stop(client) -> None:
    if client is None:
        return
    client.loop_stop()
    client.disconnect()
