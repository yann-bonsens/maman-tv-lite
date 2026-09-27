#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Plug a Zigbee adapter in, press a button, done.

This is the step no documentation can replace, because every adapter exposes a
different serial path and every button publishes a different action name
("single", "on", "toggle", "1_single"...). Rather than asking the user to read
those out of a log and edit two files by hand, this script watches what the
hardware actually does and writes the result down.

What it does, in order:

  1. finds the Zigbee adapter under /dev/serial/by-id/
  2. writes that path into the Zigbee2MQTT configuration
  3. starts Zigbee2MQTT and waits for it to come online
  4. opens pairing mode
  5. waits for you to press the button, and records what it published
  6. writes the binding to /etc/maman-tv-lite/buttons.json
  7. restarts the API so the binding takes effect

Run it on the Pi, as the account that installed the project:

    ./scripts/setup-zigbee.py
    ./scripts/setup-zigbee.py --command channel_up   # bind something else
    ./scripts/setup-zigbee.py --list-commands

It is safe to re-run, and safe to interrupt: nothing is written before the
button has actually been seen.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERIAL_DIR = Path("/dev/serial/by-id")
BINDINGS_PATH = Path(os.environ.get("MAMAN_BUTTONS_CONFIG", "/etc/maman-tv-lite/buttons.json"))
Z2M_CONFIG = Path.home() / "maman-tv-lite" / "zigbee2mqtt" / "data" / "configuration.yaml"

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = os.environ.get("MQTT_PORT", "1883")

BASE_TOPIC = "zigbee2mqtt"
ANY_DEVICE = "*"

# Kept in step with zigbee_bridge.COMMANDS. Duplicated on purpose: this script
# must run from a plain checkout, without importing the API or its virtualenv.
COMMANDS = [
    "tv",
    "music",
    "diagnostic",
    "installation",
    "power_on",
    "standby",
]

# Chipset values Zigbee2MQTT accepts. Tried in this order when it cannot work
# out the adapter by itself, which happens: a widely sold EFR32 dongle on a
# CP210x bridge failed discovery outright with "No valid USB adapter found",
# and the only cure was naming the chipset. `ember` leads because EFR32 and
# Texas Instruments parts cover most of what people buy.
ADAPTER_CANDIDATES = ["ember", "zstack", "deconz", "zboss", "zigate"]

# Zigbee2MQTT 2.x takes {"time": N}; 1.x wanted {"value": true, "time": N}.
# Both are tried rather than pinning a version, so the script keeps working on
# whatever release the user ends up with.
PERMIT_JOIN_PAYLOADS = ['{"time": %d}', '{"value": true, "time": %d}']

# The Zigbee specification carries this duration in one byte, and Zigbee2MQTT
# rejects anything longer: "Cannot permit join for more than 254 seconds."
# Found the hard way on real hardware, asking for 300 — the request was
# refused, the error went to a topic nobody was reading, and the button was
# pressed for minutes against a network that was never open.
PERMIT_JOIN_MAX_SECONDS = 254

# The bridge reports itself online before it will answer requests, so the
# first attempt after a restart is routinely lost.
PERMIT_JOIN_MAX_ATTEMPTS = 4
PERMIT_JOIN_RETRY_SECONDS = 5


def say(message: str = "") -> None:
    print(message, flush=True)


def step(number: int, total: int, message: str) -> None:
    say(f"\n[{number}/{total}] {message}")


def fail(message: str) -> "None":
    say(f"\nERROR: {message}")
    sys.exit(1)


def require_tools() -> None:
    missing = [t for t in ("mosquitto_sub", "mosquitto_pub") if not shutil.which(t)]
    if missing:
        fail(
            f"missing command(s): {', '.join(missing)}. Install them with "
            "`sudo apt-get install mosquitto-clients`."
        )


# --- 1. Find the adapter ----------------------------------------------------


def list_adapters() -> list[Path]:
    if not SERIAL_DIR.is_dir():
        return []
    return sorted(p for p in SERIAL_DIR.iterdir())


def choose_adapter(wait_seconds: int) -> Path:
    deadline = time.monotonic() + wait_seconds
    adapters = list_adapters()
    while not adapters and time.monotonic() < deadline:
        say("  No serial adapter found. Plug the Zigbee adapter in now...")
        time.sleep(3)
        adapters = list_adapters()

    if not adapters:
        fail(
            "no serial adapter under /dev/serial/by-id/. Check that the "
            "adapter is plugged in, and prefer a USB 2.0 port with a short "
            "extension cable."
        )

    if len(adapters) == 1:
        say(f"  Found: {adapters[0].name}")
        return adapters[0]

    # More than one serial device is common: a USB modem, a UPS, a second
    # dongle. Guessing would silently bind the wrong one.
    say("  Several serial devices are present:")
    for index, path in enumerate(adapters, 1):
        say(f"    {index}. {path.name}")
    while True:
        answer = input("  Which one is the Zigbee adapter? [1] ").strip() or "1"
        if answer.isdigit() and 1 <= int(answer) <= len(adapters):
            return adapters[int(answer) - 1]
        say("  Enter one of the numbers above.")


# --- 2. Write it into the Zigbee2MQTT configuration -------------------------


def set_serial_port(config_path: Path, port: Path) -> bool:
    """Rewrite the `port:` line inside the `serial:` block. True when changed.

    Scoped to that block on purpose. A bare search for `port:` would also match
    an `mqtt: port:` entry if one were added above, and rewriting the broker
    port would break MQTT in a way that looks nothing like its cause.

    Also repairs a folded port value. Seen on real hardware: the file ended up
    holding the path on two lines, which YAML reads as one value with the two
    halves joined by a space. Zigbee2MQTT then reported "No such file or
    directory" for a path that plainly existed, and the service restarted
    forever. Rewriting only the first line would have left the orphan behind,
    so continuation lines are removed with it.
    """
    if not config_path.is_file():
        fail(
            f"{config_path} not found. Run scripts/install.sh first, or pass "
            "--z2m-config with the right path."
        )
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)

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
                # Any following line indented deeper than the key, and not a
                # key itself, is a continuation of this value.
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
                    say(f"  Already set to {port}")
                    return False
                if folded:
                    say(f"  Repairing a port value split over {end - index} lines")
                lines[index:end] = [replacement]
                config_path.write_text("".join(lines), encoding="utf-8")
                say(f"  Set to {port}")
                return True

    fail(f"no `port:` line inside the `serial:` block of {config_path}; edit it by hand.")


def set_adapter(config_path: Path, adapter: str) -> None:
    """Write or replace the `adapter:` line under `serial:`.

    Python rather than sed: inserting a line after a match is where sed
    dialects differ, and the BSD one silently did nothing while reporting
    success. Whatever edits this file then has to prove it worked.
    """
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
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
            fail(f"no `port:` line in {config_path}; cannot place the adapter.")
    config_path.write_text("".join(lines), encoding="utf-8")
    if not re.search(rf"^\s*adapter:\s*{re.escape(adapter)}\s*$",
                     config_path.read_text(encoding="utf-8"), re.M):
        fail(f"failed to write the adapter into {config_path}; set it by hand.")


# --- 3. Start Zigbee2MQTT and wait for the bridge to come online ------------


def systemctl(*args: str) -> int:
    return subprocess.run(["sudo", "systemctl", *args], check=False).returncode


def mqtt_publish(topic: str, payload: str) -> None:
    subprocess.run(
        ["mosquitto_pub", "-h", MQTT_HOST, "-p", MQTT_PORT, "-t", topic, "-m", payload],
        check=False,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def split_message(line: str) -> "tuple[str, str]":
    """Split one mosquitto_sub line into its topic and its payload.

    Separated by a tab (see mqtt_listen), so a device name with a space in it
    survives. A line with no tab at all is not one of ours.
    """
    topic, tab, payload = line.rstrip("\n").partition("\t")
    return (topic, payload) if tab else ("", "")


def mqtt_listen(topics: list[str], seconds: int):
    """Yield (topic, payload) for `seconds`, as messages arrive.

    mosquitto_sub's own -W timeout terminates the subprocess, so the generator
    always ends even if nothing is ever published.

    stdin is explicitly closed (DEVNULL): without it, mosquitto_sub inherits
    this script's own controlling terminal, and this function is called
    repeatedly, forcibly killing that subprocess (`terminate()`, below) each
    time it has what it needs. A process sharing the terminal's stdin that
    gets killed mid-read can leave the terminal unable to deliver the next
    keystroke to whoever reads it next — the likely cause of a report from
    real hardware where the `input()` prompt right after a press capture
    (immediately after this function's last use before it) accepted nothing
    and fell straight through to its default, as though the question had
    been skipped. Not confirmed by a controlled repro, only by removing the
    one thing in this script that shares stdin with a subprocess it kills.
    """
    # "%t\t%p" rather than -v, whose "topic payload" is ambiguous the moment a
    # topic holds a space — and a Zigbee2MQTT topic ends in the device's
    # friendly name, which the owner chooses ("Yellow Button"). Measured on
    # the Pi 5 box, 2026-09-20: with -v, a press on a named button was read as
    # topic "zigbee2mqtt/Yellow" and payload "Button {...}", which is not
    # JSON, so the press was silently dropped and pairing reported "no button
    # press detected". A tab cannot appear in a name the script accepts.
    command = ["mosquitto_sub", "-h", MQTT_HOST, "-p", MQTT_PORT,
               "-F", "%t\t%p", "-W", str(seconds)]
    for topic in topics:
        command += ["-t", topic]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in process.stdout:
            yield split_message(line)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def wait_for_bridge(seconds: int) -> bool:
    """Wait for zigbee2mqtt/bridge/state to report that the bridge is online.

    Retained messages mean the current state arrives immediately when it is
    already up, so this usually returns at once on a re-run.
    """
    for _, payload in mqtt_listen([f"{BASE_TOPIC}/bridge/state"], seconds):
        # Older releases published the bare string "online"; 2.x publishes
        # {"state": "online"}.
        if "online" in payload:
            return True
    return False


# --- 4 and 5. Pair, then watch a real press ---------------------------------


def permit_join(seconds: int) -> bool:
    """Open (or close) pairing, and confirm the bridge really did it.

    Retried, because the bridge announces itself online before it will service
    requests: measured, it published state=online three seconds before it
    logged "started", and a request sent in that gap is dropped in silence.
    That gap only appears when something restarted the bridge just before, so
    it bites on a second run and not the first, which is the worst way for a
    bug to behave.

    The bridge answers on a response topic and otherwise fails quietly, so
    bridge/info is read back as proof rather than trusting the send.
    """
    seconds = min(seconds, PERMIT_JOIN_MAX_SECONDS)

    if seconds == 0:
        for template in PERMIT_JOIN_PAYLOADS:
            mqtt_publish(f"{BASE_TOPIC}/bridge/request/permit_join", template % seconds)
        return True  # closing again: nothing worth blocking the run for

    for attempt in range(1, PERMIT_JOIN_MAX_ATTEMPTS + 1):
        for template in PERMIT_JOIN_PAYLOADS:
            mqtt_publish(f"{BASE_TOPIC}/bridge/request/permit_join", template % seconds)
        # bridge/info is retained, so the current state arrives immediately.
        for _, payload in mqtt_listen([f"{BASE_TOPIC}/bridge/info"], 8):
            try:
                info = json.loads(payload)
            except ValueError:
                continue
            if isinstance(info, dict) and info.get("permit_join"):
                return True
        if attempt < PERMIT_JOIN_MAX_ATTEMPTS:
            say(f"  The bridge is not accepting requests yet, retrying "
                f"({attempt}/{PERMIT_JOIN_MAX_ATTEMPTS})")
            time.sleep(PERMIT_JOIN_RETRY_SECONDS)
    return False


def classify_message(topic: str, payload: str):
    """Say what one MQTT message means for pairing, or None if nothing.

    Pulled out of the listening loop so it can be tested: this is the part that
    decides whether the user pressed the button, and it is the only part of
    this script that can be exercised without an adapter and a live broker.

    Returns one of:
      ("press", device, action)   the button was pressed
      ("joined", name)            a device joined the network
      ("paired", name, model)     a device finished its interview
      None                        battery reports, link quality, noise
    """
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    # Zigbee2MQTT also publishes bare strings and numbers on some topics.
    # Calling .get() on those would raise and kill the loop mid-pairing.
    if not isinstance(data, dict):
        return None

    if topic.endswith("/bridge/event"):
        event = data.get("type", "")
        info = data.get("data") or {}
        name = info.get("friendly_name") or "?"
        if event == "device_joined":
            return ("joined", name)
        if event == "device_interview" and info.get("status") == "successful":
            definition = info.get("definition") or {}
            return ("paired", name, definition.get("model") or "unknown model")
        return None

    action = data.get("action")
    if not action:
        return None  # battery level, link quality, and other state updates
    return ("press", topic.split("/", 1)[-1], action)


def capture_press(seconds: int) -> "tuple[str, str] | None":
    """Return (device, action) for the first real button press observed, or
    None when the window passed without one."""
    topics = [f"{BASE_TOPIC}/+", f"{BASE_TOPIC}/bridge/event"]
    announced: set[str] = set()

    for topic, payload in mqtt_listen(topics, seconds):
        event = classify_message(topic, payload)
        if event is None:
            continue
        if event[0] == "press":
            return event[1], event[2]
        if event[0] == "joined":
            # Zigbee2MQTT repeats this while the device settles; say it once.
            if event[1] not in announced:
                announced.add(event[1])
                say(f"  Device joined: {event[1]}")
        elif event[0] == "paired":
            say(f"  Paired: {event[1]} ({event[2]})")
            say("  Now press the button once, normally.")

    fail(
        "no button press detected. Check that Zigbee2MQTT is running "
        "(`systemctl status zigbee2mqtt`), and that the button is in pairing "
        "mode — usually a long press of about five seconds until its LED "
        "blinks fast."
    )


# --- 6. Name the button ------------------------------------------------------

# An appearance, never a role: "the music button" during the installation
# screen means something different from what it means day to day (see
# docs/development/installation-screen.md), so a name like that would be a lie half the
# time. Colours are suggested for exactly this reason.
SUGGESTED_COLOURS = ("Red", "Green", "Blue", "Yellow", "White", "Black", "Orange")

RENAME_SECONDS = 15


def device_exists(name: str, seconds: int = 15) -> bool:
    """Whether Zigbee2MQTT currently lists a device under exactly this
    friendly name. Used to confirm a rename actually landed rather than
    trusting the response message alone — the same `bridge/devices` read
    `device_actions()` already does, for the same reason: verify, don't
    assume.

    Checks every message that arrives within `seconds`, not just the first:
    `bridge/devices` is retained, so the first message delivered the moment
    this subscribes is often the snapshot from *before* the rename, with the
    republished, up-to-date list following a moment later. Returning on the
    first message risked calling a genuinely successful rename a failure.
    """
    found = False
    for _, payload in mqtt_listen([f"{BASE_TOPIC}/bridge/devices"], seconds):
        try:
            devices = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(devices, list):
            continue
        if any(entry.get("friendly_name") == name for entry in devices):
            found = True
            break
    return found


def rename_device(old_name: str, new_name: str, seconds: int = RENAME_SECONDS) -> bool:
    """Rename a device in Zigbee2MQTT. True once the bridge confirms it AND
    the device list actually shows the new name.

    The binding itself already says what a button does; a rename only
    changes how it is described to a person — which is why installation.py
    (the API side) can print a button's real name just by looking up which
    device is bound to which command, with nothing extra to keep in sync.

    The response message alone is not trusted: a stray or stale message on
    the response topic could report "ok" without the rename having actually
    taken effect, and the script would write the binding under a name the
    device never adopts, breaking it silently. Reading the device list back
    is the same "never assume a command worked; verify state" rule this
    project already applies to CEC.
    """
    if old_name == new_name:
        return True
    mqtt_publish(f"{BASE_TOPIC}/bridge/request/device/rename",
                json.dumps({"from": old_name, "to": new_name}))
    for _, payload in mqtt_listen([f"{BASE_TOPIC}/bridge/response/device/rename"], seconds):
        try:
            response = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(response, dict):
            continue
        if response.get("status") == "ok":
            if device_exists(new_name):
                return True
            say("  Zigbee2MQTT reported the rename as done, but the device "
                "list still doesn't show the new name.")
            return False
        if response.get("status") == "error":
            say(f"  Could not rename: {response.get('error', 'unknown error')}")
            return False
    return False


def ask_for_name(current: str) -> str:
    """How this button is recognised at a glance. Defaults to keeping the
    current name when it is not a fresh Zigbee2MQTT default (does not start
    with "0x") — a re-run on an already-named button just confirms it."""
    say("\n  How is this button recognised at a glance? A colour works best,")
    say(f"  never a role like \"music\" ({', '.join(SUGGESTED_COLOURS)}...).")
    default = current if not current.startswith("0x") else SUGGESTED_COLOURS[0]
    answer = input(f"  Name [{default}]: ").strip()
    return answer or default


# --- 7. Write the binding ---------------------------------------------------


def device_actions(device: str) -> list[str]:
    """Every action value this device can publish, as Zigbee2MQTT describes it.

    Asked rather than assumed: one button offers single/double/hold, another
    on/off/toggle, a third numbered variants. Reading the definition is how
    "any press turns the TV on" works without knowing the model.

    Returns [] when the device or its definition cannot be read.
    """
    for _, payload in mqtt_listen([f"{BASE_TOPIC}/bridge/devices"], 15):
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


def read_bindings(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_bindings(path: Path, document: dict) -> None:
    """Write as root, since the file lives in /etc alongside the other secrets.

    Mode 664, not 644: the installation screen's own button-pairing
    procedure (api/button_pairing.py) now writes this same file from inside
    the running API, as the service account — group-write is what lets both
    writers use it. The directory itself is setgid (see scripts/install.sh),
    which sets a new file's *group* automatically but says nothing about its
    *mode*, so this still needs to be explicit.
    """
    body = json.dumps(document, indent=2, sort_keys=True) + "\n"
    subprocess.run(["sudo", "mkdir", "-p", str(path.parent)], check=True)
    subprocess.run(["sudo", "tee", str(path)], input=body, text=True,
                   stdout=subprocess.DEVNULL, check=True)
    subprocess.run(["sudo", "chmod", "664", str(path)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect a Zigbee adapter, pair a button, and bind it to a TV command.",
    )
    parser.add_argument("--command", default="power_toggle",
                        help="command the button triggers (default: power_toggle)")
    parser.add_argument("--device", default=None,
                        help="bind to this device name only, instead of the one detected")
    parser.add_argument("--any-device", action="store_true",
                        help="bind the action for every device rather than just this button")
    parser.add_argument("--all-actions", action="store_true",
                        help="bind every press this button can send (single, double, hold...) "
                             "to the same command, so any press works")
    parser.add_argument("--timeout", type=int, default=180,
                        help="seconds to wait for a button press (default: 180)")
    parser.add_argument("--wait-adapter", type=int, default=30,
                        help="seconds to wait for the adapter to appear (default: 30)")
    parser.add_argument("--adapter", default=None, choices=ADAPTER_CANDIDATES,
                        help="name the chipset instead of letting Zigbee2MQTT "
                             "detect it, and instead of trying each in turn")
    parser.add_argument("--wait-bridge", type=int, default=300,
                        help="seconds to wait for Zigbee2MQTT to come online (default: 300)")
    parser.add_argument("--z2m-config", type=Path, default=Z2M_CONFIG,
                        help="path to the Zigbee2MQTT configuration.yaml")
    parser.add_argument("--bindings", type=Path, default=BINDINGS_PATH,
                        help="path to the button binding file")
    parser.add_argument("--list-commands", action="store_true",
                        help="print the commands a button can be bound to, then exit")
    args = parser.parse_args()

    if args.list_commands:
        for name in COMMANDS:
            say(name)
        return

    if args.command not in COMMANDS:
        fail(f"unknown command {args.command!r}. Known: {', '.join(COMMANDS)}")

    require_tools()
    total = 8

    step(1, total, "Looking for the Zigbee adapter")
    adapter = choose_adapter(args.wait_adapter)

    step(2, total, "Writing the adapter path into the Zigbee2MQTT configuration")
    changed = set_serial_port(args.z2m_config, adapter)

    step(3, total, "Starting Zigbee2MQTT")
    if args.adapter:
        set_adapter(args.z2m_config, args.adapter)
        say(f"  Adapter set to {args.adapter} as asked.")
        changed = True
    systemctl("enable", "zigbee2mqtt")
    systemctl("restart" if changed else "start", "zigbee2mqtt")
    # Generous by default: a first-generation Pi needs several minutes to load
    # Zigbee2MQTT, and a setup script that gives up too early looks like a
    # hardware fault when it is only impatience.
    say(f"  Waiting up to {args.wait_bridge}s for the bridge (slow on an old Pi)...")
    if not wait_for_bridge(args.wait_bridge):
        # Zigbee2MQTT cannot always work out the chipset by itself: a widely
        # sold EFR32 dongle failed discovery outright. Rather than leaving the
        # user to edit YAML, name each chipset in turn until one answers. This
        # is the last step in the whole project that used to need a text
        # editor, so it is worth the few minutes it can cost.
        say("  The bridge did not start. Trying each adapter type in turn.")
        for candidate in ADAPTER_CANDIDATES:
            say(f"  Trying adapter: {candidate}")
            set_adapter(args.z2m_config, candidate)
            systemctl("restart", "zigbee2mqtt")
            if wait_for_bridge(args.wait_bridge):
                say(f"  Bridge online with adapter: {candidate}")
                say(f"  Written to {args.z2m_config}; nothing more to do.")
                break
        else:
            fail(
                "Zigbee2MQTT did not come online with any adapter type. Look "
                "at `journalctl -u zigbee2mqtt -n 50`. Check the serial path "
                "first: the adapter must appear under /dev/serial/by-id/, and "
                "a USB 2.0 port with a short extension cable helps."
            )
    else:
        say("  Bridge online.")

    step(4, total, "Opening pairing mode")
    pairing_seconds = min(args.timeout, PERMIT_JOIN_MAX_SECONDS)
    if not permit_join(pairing_seconds):
        fail(
            "the bridge did not open pairing mode. Look at "
            "`mosquitto_sub -t 'zigbee2mqtt/bridge/response/#' -v` while "
            "retrying: the bridge reports the reason there. Without this, a "
            "new button can never join, however long you hold it."
        )
    say(f"  Pairing is open for {pairing_seconds}s.")
    # The press window may legitimately be longer than a pairing window: an
    # already-paired button does not need one.
    if args.timeout > pairing_seconds:
        say(f"  (capped from {args.timeout}s; the Zigbee limit is "
            f"{PERMIT_JOIN_MAX_SECONDS}s)")

    step(5, total, "Waiting for the button")
    say("  If the button has never been paired, long-press it for about five")
    say("  seconds until its LED blinks fast. If it is already paired, just")
    say("  press it once.")
    device, action = capture_press(args.timeout)
    say(f"\n  Seen: device {device!r}, action {action!r}")

    permit_join(0)  # close pairing again; leaving it open lets anything join

    previous_key = device  # the identity this device was known by coming in
    if args.any_device or args.device:
        # There is no single device to name ("*"), or the caller already
        # named it on the command line.
        key = ANY_DEVICE if args.any_device else args.device
    else:
        step(6, total, "Naming the button")
        name = ask_for_name(device)
        if rename_device(device, name):
            if name != device:
                say(f"  Renamed to {name!r}.")
            device = name
        else:
            say(f"  Could not rename in Zigbee2MQTT; keeping {device!r}.")
        key = device

    step(7, total, "Writing the binding")

    actions = [action]
    if args.all_actions:
        # The press we just saw is always included, even when the device
        # definition is unreadable: the thing we observed beats the thing we
        # looked up.
        advertised = device_actions(device)
        if advertised:
            actions = sorted(set(advertised) | {action})
            say(f"  This button can send: {', '.join(actions)}")
        else:
            say("  Could not read the device definition; binding the observed "
                "press only.")

    document = read_bindings(args.bindings)
    bindings = document.setdefault("bindings", {})
    if previous_key != key:
        # Zigbee2MQTT renamed the device; its old identity no longer
        # publishes anything. Leaving that entry behind left a device bound
        # under both its technical id and its new name — and since
        # device_for_command() (api/button_bindings.py) returns the first
        # match it finds, a page could keep printing the raw id for ever
        # even though the rename had genuinely worked. Measured on a real
        # box: "0xa4c1..." and "Bouton Vert" both bound to "tv" at once.
        removed = bindings.pop(previous_key, None)
        if removed:
            say(f"  Removed the old binding under {previous_key!r}.")
    entry = bindings.setdefault(key, {})
    for name in actions:
        entry[name] = args.command
    write_bindings(args.bindings, document)
    for name in actions:
        say(f"  {key} / {name} -> {args.command}")
    say(f"  ({args.bindings})")

    step(8, total, "Restarting the API")
    # The binding (and a button's new name) only take effect once the API
    # re-reads buttons.json at start — it is loaded once, into memory, not
    # watched. A silently failed restart is exactly the fault this project
    # refuses: the screen would go on showing the old name (or the old
    # binding) with nothing anywhere saying why. `check=False` in systemctl()
    # is what makes that silence possible, so the result is checked here.
    if systemctl("restart", "maman-api") != 0:
        say("  Could not restart maman-api — see `systemctl status maman-api` "
            "and `journalctl -u maman-api -n 50`.")
        say("  The binding was written, but nothing will use it until the "
            "API actually restarts: run `sudo systemctl restart maman-api` "
            "by hand.")

    say("\nDone. Press the button: the TV should react.")
    say("Bind another button or another action by running this script again.")
    say(f"Available commands: {', '.join(COMMANDS)}")


if __name__ == "__main__":
    main()
