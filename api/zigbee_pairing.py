# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Talking to Zigbee2MQTT for the on-screen button-pairing procedure.

The `paho.mqtt` equivalent of what `scripts/setup-zigbee.py` does with
`mosquitto_sub`/`mosquitto_pub` subprocesses — same retry-and-verify logic,
ported rather than reinvented, but a library call from inside the API
process instead of a subprocess. That also sidesteps the stdin/terminal
issue that bit the standalone script earlier: there is no subprocess to
share a controlling terminal with here at all.

Each function opens its own short-lived connection for exactly as long as it
needs one, then closes it — the same shape as the standalone script's own
`mqtt_listen()`, called fresh for each operation. Deliberately **not** the
persistent connection `zigbee_bridge.py` keeps for the life of the API: that
one subscribes only to `zigbee2mqtt/+` (device topics), explicitly excluding
`zigbee2mqtt/bridge/...` — this module needs exactly the `bridge/...` topics
that one excludes, plus a device topic while watching for a press. Two
independent connections coexist without conflict: Zigbee2MQTT is a normal
MQTT broker client, and a device's press already reaches every subscriber,
including `zigbee_bridge.py`'s own — which simply logs and ignores it, as it
already does for any press from a device with no binding yet.

Pure protocol access, no knowledge of the installation screen or `modes.py`:
this module is to Zigbee2MQTT what `cec_controller.py` is to the television.

Also owns bringing the bridge up in the first place — finding the adapter,
writing it into Zigbee2MQTT's own configuration, and starting the service —
ported from `scripts/setup-zigbee.py`'s own first few steps so the
installation screen's bootstrap (`button_pairing.run_bootstrap()`) never
needs that script to have been run first. Those functions use plain
`subprocess`/`systemctl`, not MQTT: see their own docstrings for the narrow
polkit rule that makes them possible from an otherwise unprivileged process.
"""

import json
import logging
import os
import queue
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

BASE_TOPIC = "zigbee2mqtt"

# Where a Zigbee adapter shows up, and where Zigbee2MQTT's own configuration
# lives — the two ends of "find it, tell Zigbee2MQTT about it". The by-id
# path is stable across a reboot or a replug in a different USB order,
# unlike /dev/ttyUSB0. MAMAN_Z2M_CONFIG is set by systemd/maman-api.service;
# the fallback matches scripts/setup-zigbee.py's own default for a plain
# checkout run by hand (this module is never run that way, but the tests
# are, and a sensible default costs nothing).
SERIAL_DIR = Path("/dev/serial/by-id")
Z2M_CONFIG = Path(os.environ.get(
    "MAMAN_Z2M_CONFIG",
    str(Path.home() / "maman-tv-lite" / "zigbee2mqtt" / "data" / "configuration.yaml")))

# Chipset values Zigbee2MQTT accepts, tried in turn when plain autodetection
# does not work — a widely sold EFR32 dongle on a CP210x bridge failed
# discovery outright with "No valid USB adapter found", and naming the
# chipset was the only cure. Kept identical to scripts/setup-zigbee.py's own
# list, in the same order: `ember` leads because EFR32 and Texas Instruments
# parts cover most of what people buy.
ADAPTER_CANDIDATES = ("ember", "zstack", "deconz", "zboss", "zigate")

# How long one attempt (autodetection, or one named chipset) waits for the
# bridge to answer online before moving on to the next.
#
# **Measured on the board, and 60 s was not enough by a wide margin.** From
# systemd's "Started zigbee2mqtt.service" to Zigbee2MQTT's own first log line
# is 45 seconds on this hardware — Node loading its libraries on one ARMv6
# core — and the serial port opens about fifty seconds after that. On
# 2026-09-25 the window expired eleven seconds after
# "zh:zstack:znp: Serialport opened", so this loop killed the attempt that
# had just succeeded, rewrote the chipset, and restarted it. The screen sat
# on "Starting it up — this can take a few minutes" while the box did that
# over and over at ~50 s of CPU each.
#
# Six attempts at this length no longer fit inside run_bootstrap()'s own
# ~20-minute budget, and that is the right trade: an attempt too short to
# succeed is not an attempt, it is a restart. In practice the answer comes
# in the first one or two — on the dongle used here, autodetection failed
# and the first named chipset worked.
BRIDGE_BRING_UP_SECONDS = 180

# Zigbee2MQTT 2.x takes {"time": N}; 1.x wanted {"value": true, "time": N}.
# Both are sent rather than pinning a version — see scripts/setup-zigbee.py.
PERMIT_JOIN_PAYLOADS = ['{"time": %d}', '{"value": true, "time": %d}']

# The Zigbee specification carries this duration in one byte; Zigbee2MQTT
# rejects anything longer. See scripts/setup-zigbee.py for how this was found.
PERMIT_JOIN_MAX_SECONDS = 254

# The bridge reports itself online before it will answer requests, so the
# first attempt after a restart is routinely lost — retried for the same
# reason as the standalone script.
PERMIT_JOIN_MAX_ATTEMPTS = 4
PERMIT_JOIN_RETRY_SECONDS = 5

RENAME_SECONDS = 15


class _Connection:
    """One MQTT connection, open for the life of a `with` block, collecting
    every message on its subscribed topics into a queue that `listen()`
    drains with a deadline."""

    def __init__(self) -> None:
        import paho.mqtt.client as mqtt
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_message = self._on_message
        self._client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self._client.loop_start()

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            return
        self._queue.put((message.topic, payload))

    def subscribe(self, *topics: str) -> None:
        for topic in topics:
            self._client.subscribe(topic)

    def publish(self, topic: str, payload: str) -> None:
        self._client.publish(topic, payload)

    def listen(self, seconds: float):
        """Yield (topic, payload) for `seconds`, as messages arrive."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                yield self._queue.get(timeout=remaining)
            except queue.Empty:
                return

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _connect() -> _Connection:
    """The one seam between this module's protocol logic and a real broker
    connection — a module-level function, not the class used directly, so
    tests can `monkeypatch.setattr(zigbee_pairing, "_connect", ...)` with a
    fake connection instead of needing a real broker, the same shape
    `scripts/setup-zigbee.py`'s tests already use for `mqtt_listen`/
    `mqtt_publish`."""
    return _Connection()


def bridge_ready(seconds: float) -> bool:
    """Whether Zigbee2MQTT is already online — retained, so this returns at
    once when it is. Read before any pairing step: pairing over a bridge
    that is not there yet has never worked and needs a shell either way."""
    with _connect() as conn:
        conn.subscribe(f"{BASE_TOPIC}/bridge/state")
        for _, payload in conn.listen(seconds):
            if "online" in payload:
                return True
    return False


def open_pairing(seconds: float) -> bool:
    """Open pairing for `seconds` (capped at the protocol's own limit), and
    confirm the bridge actually did it — retried, since a bridge that has
    just (re)started answers `bridge/state=online` before it services
    requests. See scripts/setup-zigbee.py's `permit_join()` for the same
    reasoning."""
    seconds = min(seconds, PERMIT_JOIN_MAX_SECONDS)
    for attempt in range(1, PERMIT_JOIN_MAX_ATTEMPTS + 1):
        with _connect() as conn:
            conn.subscribe(f"{BASE_TOPIC}/bridge/info")
            for template in PERMIT_JOIN_PAYLOADS:
                conn.publish(f"{BASE_TOPIC}/bridge/request/permit_join",
                            template % seconds)
            for _, payload in conn.listen(8):
                try:
                    info = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(info, dict) and info.get("permit_join"):
                    return True
        if attempt < PERMIT_JOIN_MAX_ATTEMPTS:
            time.sleep(PERMIT_JOIN_RETRY_SECONDS)
    return False


def close_pairing() -> None:
    """Close pairing again. Best-effort: leaving it open lets anything join,
    but a failed close is not worth blocking the procedure over."""
    with _connect() as conn:
        for template in PERMIT_JOIN_PAYLOADS:
            conn.publish(f"{BASE_TOPIC}/bridge/request/permit_join", template % 0)


def _classify(topic: str, payload: str):
    """Direct port of scripts/setup-zigbee.py's `classify_message()`."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    if not action:
        return None
    return topic.split("/", 1)[-1], action


def capture_press(seconds: float) -> "tuple[str, str] | None":
    """Wait for one real button press, returning (device, action) — or None
    on a timeout, for the caller to turn into a failure page. Unlike
    scripts/setup-zigbee.py's `capture_press()`, this never raises: the
    screen is the only place progress is shown, and a timeout here is an
    ordinary, expected outcome of somebody not having pressed anything yet,
    not a fatal error."""
    with _connect() as conn:
        conn.subscribe(f"{BASE_TOPIC}/+")
        for topic, payload in conn.listen(seconds):
            event = _classify(topic, payload)
            if event is not None:
                return event
    return None


def device_exists(name: str, seconds: float = 15) -> bool:
    """Whether Zigbee2MQTT currently lists a device under exactly this
    friendly name. Checks every message within `seconds`, not just the
    first: `bridge/devices` is retained, so the first message delivered the
    moment this subscribes is often the snapshot from *before* a rename —
    see scripts/setup-zigbee.py's own `device_exists()` for the same
    reasoning."""
    with _connect() as conn:
        conn.subscribe(f"{BASE_TOPIC}/bridge/devices")
        for _, payload in conn.listen(seconds):
            try:
                devices = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(devices, list):
                continue
            if any(entry.get("friendly_name") == name for entry in devices):
                return True
    return False


def rename_device(old_name: str, new_name: str, seconds: float = RENAME_SECONDS) -> bool:
    """Rename a device in Zigbee2MQTT. True once the bridge confirms it AND
    the device list actually shows the new name — never trusting the
    response message alone, the same "verify, don't assume" rule as
    scripts/setup-zigbee.py's own `rename_device()`."""
    if old_name == new_name:
        return True
    with _connect() as conn:
        conn.subscribe(f"{BASE_TOPIC}/bridge/response/device/rename")
        conn.publish(f"{BASE_TOPIC}/bridge/request/device/rename",
                    json.dumps({"from": old_name, "to": new_name}))
        for _, payload in conn.listen(seconds):
            try:
                response = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(response, dict):
                continue
            if response.get("status") == "ok":
                break
            if response.get("status") == "error":
                logger.info("zigbee_pairing: could not rename %r to %r: %s",
                           old_name, new_name, response.get("error", "unknown error"))
                return False
        else:
            return False
    return device_exists(new_name)


def device_actions(device: str, seconds: float = 15) -> "list[str]":
    """Every action value this device can publish, as Zigbee2MQTT describes
    it — direct port of scripts/setup-zigbee.py's `device_actions()`, so a
    paired button gets every press it can send bound to the same command,
    not just the one observed while pairing."""
    with _connect() as conn:
        conn.subscribe(f"{BASE_TOPIC}/bridge/devices")
        for _, payload in conn.listen(seconds):
            try:
                devices = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(devices, list):
                continue
            for entry in devices:
                if entry.get("friendly_name") != device:
                    continue
                definition = entry.get("definition") or {}
                for expose in definition.get("exposes") or []:
                    if expose.get("property") == "action" or expose.get("name") == "action":
                        return [str(v) for v in (expose.get("values") or [])]
                return []
    return []


# --- Bringing the bridge up in the first place -------------------------------
#
# Everything below is the paho/systemctl equivalent of what
# scripts/setup-zigbee.py's own steps 1-3 do by hand over SSH — ported so the
# installation screen's own bootstrap (button_pairing.run_bootstrap()) never
# needs that script to have been run first. `restart_bridge()`/
# `stop_bridge()` call `systemctl` directly, with no `sudo`: the API runs
# with NoNewPrivileges=true, which stops sudo from working (see
# polkit/50-maman-tv-lite-power.rules's own note), so this relies instead on
# the narrow polkit rule in polkit/51-maman-tv-lite-zigbee.rules, which grants
# exactly start/stop/restart of zigbee2mqtt.service and nothing else.


def list_adapters() -> "list[str]":
    """Serial devices that might be the Zigbee adapter, as **full paths**
    under /dev/serial/by-id. Empty, not an error, when none is plugged in —
    an ordinary and expected state this module leaves the caller to word for
    a person, the same way a timed-out `capture_press()` does.

    Full paths, not the bare `.name`, because what this returns is written
    straight into Zigbee2MQTT's `port:`. Returning the name alone is exactly
    what it did on 2026-09-25, and Zigbee2MQTT then died on every start with
    "No such file or directory, cannot open usb-Itead_Sonoff_...-if00-port0"
    — a name that plainly existed, in a directory nobody had told it about.
    With `Restart=always` it went round for ever at about 50 s of CPU per
    attempt, on one core, while the installation screen sat on "Starting it
    up — this can take a few minutes". `scripts/setup-zigbee.py`, which has
    always worked, returns paths and only shows `.name` to a person; this is
    the same, and `button_pairing.py` does the shortening for the page.
    """
    try:
        return sorted(str(path) for path in SERIAL_DIR.iterdir())
    except OSError:
        return []


def _set_serial_port(config_path: Path, port: str) -> bool:
    """Rewrite the `port:` line inside the `serial:` block of Zigbee2MQTT's
    configuration.yaml. True when changed, False when already correct or
    the file could not be read — direct port of
    scripts/setup-zigbee.py's own `set_serial_port()`, including the
    "repair a value folded across two lines" case measured on real
    hardware (see that script's own docstring for how YAML reads a value
    split over two lines, and what Zigbee2MQTT does with the result:
    "No such file or directory" for a path that plainly existed, restarting
    forever).
    """
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        logger.warning("zigbee_pairing: cannot read %s: %s", config_path, exc)
        return False

    in_serial = False
    for index, line in enumerate(lines):
        if re.match(r"^serial:\s*$", line):
            in_serial = True
            continue
        if in_serial and re.match(r"^\S", line):
            break  # a new top-level key: the serial block ended
        if in_serial:
            match = re.match(r"^(\s*)port:(.*)$", line)
            if match:
                indent = match.group(1)
                end = index + 1
                while end < len(lines):
                    nxt = lines[end]
                    if not nxt.strip() or re.match(r"^\s*[\w.-]+:", nxt):
                        break
                    if len(nxt) - len(nxt.lstrip()) <= len(indent):
                        break
                    end += 1
                folded = end > index + 1
                replacement = f"{indent}port: {port}\n"
                if not folded and line == replacement:
                    return False
                lines[index:end] = [replacement]
                try:
                    config_path.write_text("".join(lines), encoding="utf-8")
                except OSError as exc:
                    logger.warning("zigbee_pairing: cannot write %s: %s", config_path, exc)
                    return False
                return True
    logger.warning("zigbee_pairing: no `port:` line inside the `serial:` "
                   "block of %s", config_path)
    return False


def _set_adapter(config_path: Path, adapter: str) -> bool:
    """Write or replace the `adapter:` line under `serial:`, naming a
    chipset directly for a bridge that cannot work it out on its own.
    Direct port of scripts/setup-zigbee.py's own `set_adapter()`."""
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        logger.warning("zigbee_pairing: cannot read %s: %s", config_path, exc)
        return False
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)adapter:", line)
        if match:
            lines[index] = f"{match.group(1)}adapter: {adapter}\n"
            break
    else:
        for index, line in enumerate(lines):
            match = re.match(r"^(\s*)port:", line)
            if match:
                lines.insert(index + 1, f"{match.group(1)}adapter: {adapter}\n")
                break
        else:
            logger.warning("zigbee_pairing: no `port:` line in %s; cannot "
                           "place the adapter", config_path)
            return False
    try:
        config_path.write_text("".join(lines), encoding="utf-8")
    except OSError as exc:
        logger.warning("zigbee_pairing: cannot write %s: %s", config_path, exc)
        return False
    return True


def _systemctl(verb: str) -> bool:
    try:
        result = subprocess.run(["systemctl", verb, "zigbee2mqtt"],
                               capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("zigbee_pairing: could not %s zigbee2mqtt: %s", verb, exc)
        return False
    if result.returncode != 0:
        logger.warning("zigbee_pairing: systemctl %s zigbee2mqtt failed: %s",
                       verb, (result.stderr or result.stdout).strip())
        return False
    return True


def restart_bridge() -> bool:
    """Restart Zigbee2MQTT so a freshly-written adapter path takes effect.
    Safe to call even with no adapter plugged in at all:
    zigbee2mqtt.service's own `ConditionPathExistsGlob=` means systemd
    simply does not start it in that case — "inactive (condition failed)",
    not a failure, and no CPU spent finding that out."""
    return _systemctl("restart")


def bridge_running() -> bool:
    """Whether systemd currently has Zigbee2MQTT up — including while it is
    still starting, which on this board is a minute and a half of the two
    minutes it takes to be usable."""
    try:
        done = subprocess.run(["systemctl", "is-active", "zigbee2mqtt"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.strip() in ("active", "activating")


def stop_bridge() -> bool:
    """Stop Zigbee2MQTT. Used to give up cleanly once run_bootstrap()'s own
    deadline runs out, so a bad or unrecognised adapter — one that is
    present but that Zigbee2MQTT still cannot talk to after every known
    chipset has been tried — cannot be left crash-looping indefinitely."""
    return _systemctl("stop")


def bring_bridge_up(port: str, config_path: "Path | None" = None) -> bool:
    """Write the adapter's serial path into Zigbee2MQTT's configuration,
    restart it, and wait for the bridge to come online — trying each known
    chipset in turn if plain autodetection does not answer, the same
    fallback scripts/setup-zigbee.py already uses. True once the bridge is
    actually online; false if none of the attempts worked, for the caller
    to retry (a re-seated cable, say) or eventually give up on.
    """
    config_path = config_path or Z2M_CONFIG
    # Normalised here as well as produced correctly by `list_adapters()`:
    # this is the one door every caller comes through, and a bare name
    # reaching the configuration file is a crash loop that costs the board
    # its whole core.
    if "/" not in port:
        port = str(SERIAL_DIR / port)

    # **Restarted only when there is something new to apply.** Zigbee2MQTT
    # takes about two minutes to become usable on this board, so a restart
    # sent to one that is already on its way throws away everything it has
    # done and starts the two minutes again. `run_bootstrap()` calls this
    # in a loop, so without this guard a second pass would kill the first
    # one's progress, for ever. A configuration that is already right and a
    # bridge that is already running means: wait, do not restart.
    if _set_serial_port(config_path, port) or not bridge_running():
        restart_bridge()
    if bridge_ready(BRIDGE_BRING_UP_SECONDS):
        return True
    for candidate in ADAPTER_CANDIDATES:
        if _set_adapter(config_path, candidate) or not bridge_running():
            restart_bridge()
        if bridge_ready(BRIDGE_BRING_UP_SECONDS):
            return True
    return False
