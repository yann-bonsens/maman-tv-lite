# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""A model of one real television, standing in for `cec-ctl`.

Not a CEC simulator and not a conformant set: a model of **one measured
television**, built only from what was captured from it. Every behaviour below
says what it rests on, and the ones that were never isolated are marked and
modelled as doing nothing — a model that guesses in the product's favour is
worse than no model, because it turns a real fault into a passing test.

Why this exists rather than more unit tests. Everything between the detection
procedure and the bus was exercised by nothing: `test_cec_detection.py` models a
television at the level of *technique names*, and `test_modes.py` replaces the
technique tables with recorders. So the frames, the argv, the addressing, the
acknowledgements and the link were tested one function at a time and never
together — which is exactly where every fault of 2026-09-26 lived.

The shape is the one `test_screen_drawn.py` already uses for the screen: a fake
binary on PATH, and the real code run against it. Each `cec-ctl` call is its own
process, so the television's state lives in a JSON file both sides share.

**What the model deliberately does not encode.** Whether the set prefers "the
last input used" when it wakes. Every observation behind that belief was made
with libCEC announcing the box as active source at that exact moment, so it has
never been separated from libCEC's own doing. The model therefore has a set that
wakes onto its own programmes unless something explicitly claimed the input, and
a test that wants the opposite has to say so.
"""

import json
import os
import sys

STATE_ENV = "MAMAN_FAKE_TELEVISION"

# Message names by opcode, as cec-ctl prints them.
OPCODES = {
    "0x04": "IMAGE_VIEW_ON", "0x0d": "TEXT_VIEW_ON", "0x36": "STANDBY",
    "0x44": "USER_CONTROL_PRESSED", "0x45": "USER_CONTROL_RELEASED",
    "0x82": "ACTIVE_SOURCE", "0x86": "SET_STREAM_PATH", "0x80": "ROUTING_CHANGE",
    "0x9d": "INACTIVE_SOURCE", "0x8f": "GIVE_DEVICE_POWER_STATUS",
    "0x9f": "GET_CEC_VERSION", "poll": "POLL",
}


def high_one() -> dict:
    """The HIGH ONE HI3231HD-EL, as measured on 2026-09-26.

    `obeys` is per frame, and separate from whether the frame is delivered: a
    set acknowledges a frame it then ignores, which is the whole reason the
    detection procedure needs a person watching rather than a bus reading.
    """
    return {
        "name": "HIGH-ONE",
        "power": "on",
        # "tuner" = the set shows its own programmes, "box" = the box's input.
        "input": "tuner",
        # Where the set listens. It was seen on 0 and on 14 the same evening;
        # what moves it is unknown, so only a test moves it.
        "logical_address": 0,
        # Whether the set presents its HDMI link. Without it the adapter has no
        # physical address, nothing can be claimed and nothing is acknowledged.
        "hotplug": True,
        # Two hypotheses the captures could not separate, both off by default so
        # a test has to ask for them by name.
        "drops_link_when_the_output_sleeps": False,
        "recovers_link_when_the_output_wakes": True,
        "obeys": {
            # Measured: the set went off and stayed off (captures 23, 28).
            "0x36": True,
            # Measured: the set came back on (capture 23).
            "0x0d": True,
            # Never isolated. It was seen acting on 0x04 (capture 09, it
            # answered Set Stream Path) but only while already powering on for
            # another reason. Modelled as doing nothing, which is the
            # conservative choice: it makes the detection have to reach
            # text_view_on, and it cannot turn a broken wake into a pass.
            "0x04": False,
            # Measured at the installation site: sent alone, it did nothing.
            "0x44:6d": False,
            # Never tried on this set.
            "0x44:40": False, "0x44:6b": False, "0x44:6c": False,
            # Channel Up, among the frames the set ignored.
            "0x44:30": False,
            # Measured: the set obeys and switches its input to the box
            # (capture 09, it broadcast Set Stream Path naming the box).
            "0x82": True,
            # Never tried on this set. Recorded as refused or ignored at the
            # installation site, which is why they are modelled as inert.
            "0x9d": False, "0x86": False, "0x80": False,
        },
    }


def fresh(television: "dict | None" = None) -> dict:
    return {"television": television or high_one(),
            "claimed": False, "box_output": "awake",
            # "attempted" is every frame the box put on the bus; "received" only
            # the ones that reached the set. The difference is what a test needs
            # to see the box trying an address nothing is listening on.
            "attempted": [], "received": [], "sequence": 0,
            # How many times cec-ctl was run at all, frames and adapter reads
            # alike. On this board each one is a process, so this is what an
            # everyday press actually costs.
            "invocations": 0}


# ---------------------------------------------------------------------------
# Reading and writing the shared state
# ---------------------------------------------------------------------------

def load(path: "str | None" = None) -> dict:
    with open(path or os.environ[STATE_ENV], encoding="utf-8") as handle:
        return json.load(handle)


def save(state: dict, path: "str | None" = None) -> None:
    with open(path or os.environ[STATE_ENV], "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

def physical_address(state: dict) -> str:
    """The kernel derives it from the EDID, so it is gone with the set's
    link."""
    return "1.0.0.0" if state["television"]["hotplug"] else "f.f.f.f"


def _claimed(state: dict) -> bool:
    # A logical address cannot be held without a physical one: the kernel
    # refuses the ioctl. Measured — with the set's link gone, every claim
    # polled, timed out, and left the mask at 0x0000.
    return state["claimed"] and state["television"]["hotplug"]


def adapter_report(state: dict) -> str:
    return (
        "Driver Info:\n"
        "\tDriver Name                : vc4_hdmi\n"
        "\tAdapter Name               : vc4-hdmi\n"
        "\tCapabilities               : 0x0000031e\n"
        "\t\tLogical Addresses\n"
        "\t\tTransmit\n"
        "\tDriver version             : 6.18.34\n"
        "\tAvailable Logical Addresses: 1\n"
        f"\tPhysical Address           : {physical_address(state)}\n"
        f"\tLogical Address Mask       : {'0x0002' if _claimed(state) else '0x0000'}\n"
        "\tCEC Version                : 1.4\n"
        "\tOSD Name                   : 'Maman TV Lite'\n"
        "\tLogical Addresses          : 1 (Allow RC Passthrough)\n")


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _sender(state: dict) -> "tuple[str, int]":
    return ("Recording Device 1", 1) if _claimed(state) else ("Unregistered", 15)


def _destination(address: int) -> str:
    return {0: "TV", 14: "Specific"}.get(address, f"device {address}")


def _reaches_the_set(state: dict, address: int) -> bool:
    """Delivered only if the set presents its link and is listening there.

    A set in standby answers nothing at all on this model — measured, not even
    a Feature Abort — but its link survives the standby for a few seconds,
    which is why the power cycle works: both of its frames go out before the
    link goes away. The going-away is an event a test triggers, because the
    delay was never measured precisely.
    """
    television = state["television"]
    if not television["hotplug"]:
        return False
    if television["power"] == "standby":
        # Nothing comes back, but the frame is still carried: this is how the
        # set is woken at all.
        return address == television["logical_address"]
    return address == television["logical_address"]


def _acknowledges(state: dict, address: int) -> bool:
    """Whether the adapter reports the frame as taken.

    A set in standby acknowledges nothing, so a wake frame is transmitted and
    reported unacknowledged even when it works — the asymmetry the whole
    project rests on, and the reason the detection asks a person.
    """
    television = state["television"]
    return (television["hotplug"]
            and address == television["logical_address"]
            and television["power"] == "on")


def _act(state: dict, key: str) -> None:
    """What the set does about a frame it received."""
    television = state["television"]
    if not television["obeys"].get(key, False):
        return
    if key == "0x36":
        television["power"] = "standby"
    elif key in ("0x0d", "0x04"):
        if television["power"] == "standby":
            television["power"] = "on"
            # Wakes onto its own programmes: measured, nothing claimed the
            # input after the hotplug returned (capture 24). See the module
            # docstring for what is deliberately not modelled here.
            television["input"] = "tuner"
    elif key == "0x82":
        television["input"] = "box"
    elif key == "0x44:6b" and television["power"] in ("on", "standby"):
        television["power"] = "standby" if television["power"] == "on" else "on"


def transmit(state: dict, address: int, key: str, name: str) -> str:
    state["sequence"] += 1
    sender, sender_address = _sender(state)
    lines = [f"Transmit from {sender} to {_destination(address)} "
             f"({sender_address} to {address}):", name]
    state["attempted"].append({"to": address, "key": key, "name": name})
    carried = _reaches_the_set(state, address)
    if carried:
        state["received"].append({"to": address, "key": key, "name": name})
    # A poll sent from Unregistered to 14 was reported with no sequence number
    # and no status at all (capture 32). Reproduced, because requiring a
    # sequence number is what made the box conclude the set was not at 14.
    if sender_address == 15 and address == 14:
        return "\n".join(lines) + "\n"
    lines.append(f"\tSequence: {state['sequence']} Tx Timestamp: "
                 f"{1000 + state['sequence']}.000000s")
    if not _acknowledges(state, address):
        lines.append("\tTx, Not Acknowledged (4), Max Retries")
    if carried:
        _act(state, key)
    return "\n".join(lines) + "\n"


def _reply(state: dict, address: int, body: str) -> str:
    if not _acknowledges(state, address) or not _claimed(state):
        return ""
    return f"    Received from {_destination(address)} ({address}):\n{body}"


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def handle(state: dict, argv: "list[str]") -> str:
    """One `cec-ctl` invocation."""
    options = {}
    rest = list(argv)
    while rest:
        word = rest.pop(0)
        if not word.startswith("--") and word not in ("-d", "-o"):
            continue
        value = rest[0] if rest and not rest[0].startswith("-") else None
        if value is not None and word in ("-d", "-o", "--to", "--record",
                                          "--playback", "--active-source",
                                          "--inactive-source", "--set-stream-path",
                                          "--routing-change",
                                          "--user-control-pressed"):
            rest.pop(0)
        options[word] = value

    if "--record" in options or "--playback" in options:
        # Claiming an address polls it first, and that poll needs a working
        # bus. With the set's link gone it times out and nothing is allocated.
        state["claimed"] = state["television"]["hotplug"]
        return adapter_report(state)

    if "--to" not in options:
        return adapter_report(state)

    address = int(options["--to"])

    if "--poll" in options:
        return transmit(state, address, "poll", "POLL")
    if "--standby" in options:
        return transmit(state, address, "0x36", "STANDBY (0x36)")
    if "--image-view-on" in options:
        return transmit(state, address, "0x04", "IMAGE_VIEW_ON (0x04)")
    if "--text-view-on" in options:
        return transmit(state, address, "0x0d", "TEXT_VIEW_ON (0x0d)")
    if "--user-control-released" in options:
        return transmit(state, address, "0x45", "USER_CONTROL_RELEASED (0x45)")
    if "--user-control-pressed" in options:
        code = (options["--user-control-pressed"] or "").split("=")[-1]
        return transmit(state, address, f"0x44:{code[-2:]}",
                        f"USER_CONTROL_PRESSED (0x44):\n\tui-cmd: {code}")
    if "--give-device-power-status" in options:
        sent = transmit(state, address, "0x8f", "GIVE_DEVICE_POWER_STATUS (0x8f)")
        return sent + _reply(state, address,
                             "    REPORT_POWER_STATUS (0x90):\n"
                             f"\tpwr-state: {state['television']['power']} (0x00)\n")
    if "--get-cec-version" in options:
        sent = transmit(state, address, "0x9f", "GET_CEC_VERSION (0x9f)")
        return sent + _reply(state, address,
                             "    CEC_VERSION (0x9e):\n"
                             "\tcec-version: version-1-4 (0x05)\n")

    # The broadcast messages carry their address in an operand, and cec-ctl
    # sends them to 15 itself — the box never gives them a --to, so reaching
    # here with one means the transport built the wrong argv.
    raise SystemExit(f"fake cec-ctl: directed frame it does not know: {argv}")


def handle_broadcast(state: dict, argv: "list[str]") -> "str | None":
    for option, key, name in (("--active-source", "0x82", "ACTIVE_SOURCE (0x82)"),
                              ("--inactive-source", "0x9d", "INACTIVE_SOURCE (0x9d)"),
                              ("--set-stream-path", "0x86", "SET_STREAM_PATH (0x86)"),
                              ("--routing-change", "0x80", "ROUTING_CHANGE (0x80)")):
        if option in argv:
            # Broadcast: nobody acknowledges, and the set acts on it or not.
            state["sequence"] += 1
            sender, sender_address = _sender(state)
            state["attempted"].append({"to": 15, "key": key, "name": name})
            state["received"].append({"to": 15, "key": key, "name": name})
            _act(state, key)
            return (f"Transmit from {sender} to all ({sender_address} to 15):\n"
                    f"{name}\n\tSequence: {state['sequence']} Tx Timestamp: "
                    f"{1000 + state['sequence']}.000000s\n")
    return None


def main(argv: "list[str]") -> int:
    state = load()
    state["invocations"] = state.get("invocations", 0) + 1
    try:
        printed = handle_broadcast(state, argv)
        if printed is None:
            printed = handle(state, argv)
    finally:
        save(state)
    sys.stdout.write(printed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
