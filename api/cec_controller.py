# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

import glob
import logging
import os
import re
import subprocess
import threading
import time

import state
import tv_config

logger = logging.getLogger(__name__)

# Serialises every access to the CEC bus.
#
# Since the Zigbee buttons trigger actions (zigbee_bridge), two callers live in
# the same process: uvicorn's HTTP requests and the MQTT thread. Without this
# lock, a button pressed at the same moment as a command from a phone would run
# two `cec-ctl` processes against the same adapter, and their frames would
# interleave.
#
# RLock rather than Lock: some higher-level functions (power_toggle) hold the
# lock while calling _run, which takes it again. A plain Lock would deadlock.
_CEC_LOCK = threading.RLock()

# Which adapter. Unset, it is detected — see `_device()`. A path forces one.
# CEC_DEVICE="" names no device at all, which leaves cec-ctl to its own
# default — the empty value is passed as *no* `-d` argument rather than as an
# empty one, or every call would fail on a device called "".
CEC_DEVICE = os.environ.get("CEC_DEVICE")
CEC_TIMEOUT_SECONDS = 15

# The last adapter a television was seen on, kept across restarts.
ADAPTER_STATE_PATH = os.path.join(
    os.environ.get("STATE_DIRECTORY", "/var/lib/maman-tv-lite"), "cec-adapter")

# The box's own physical address, as the two operand bytes a CEC frame
# carries. Used by the commands that must name themselves — Inactive Source
# and Routing Change.
#
# **Read from the adapter, never assumed.** It is derived from the EDID and it
# says which HDMI socket the box is plugged into: 1.0.0.0 on the first, 2.0.0.0
# on the second, and so on. This was hard-coded to the first socket, with a
# comment asking whoever moved the cable to come and edit it — so on any other
# socket those two frames announced an address that was not the box's, and a
# detection run could reject a technique that would have worked, or credit one
# that had not. The value was already being read a few lines below, for the
# report, and simply not used.
FALLBACK_PHYSICAL_ADDRESS = "10:00"

OSD_NAME = "Maman TV Lite"

# Pause between claiming active source and the command that needed it.
#
# Claiming makes the television switch input, and a set busy switching can
# refuse what comes next. Captured on the bus, sending Standby 0.6 s after the
# claim: `>> 01:00:36:01`, Feature Abort, "not in correct mode to respond".
# The switch completed about 2.1 s after the claim on that television.
#
# Beware the middle: libCEC re-announces its source when the switch completes,
# and that announcement carries Image View On, which turns the set back on.
# Too short and the standby is refused; slightly longer and it is accepted but
# then undone, so the television visibly goes off and straight back on. Long
# enough and everything has settled first. On the set this was measured on, 0
# left it on, 0.5 made it cycle, and 1 worked repeatedly.
#
# It is only paid by the two sleep techniques that claim the input first, and
# by nothing else: a box whose television obeys plain Standby never waits.
ACTIVE_SOURCE_SETTLE_SECONDS = 2


# ============================================================================
# The techniques
# ----------------------------------------------------------------------------
# No two televisions agree on how to be switched on or off: Image View On, Text
# View On, three User Control power keys and a bare active-source announcement
# are each "the" way to wake a set, depending on the set.
#
# So the tables below are a vocabulary, not a search. The configuration names one
# entry per direction and a press sends exactly that, once; which entry belongs
# to a given television is decided by the installation mode, with somebody
# watching the screen.
#
# The order is best-first. Plain Standby comes before the variants that claim the
# HDMI input — the viewer should see the set go off, not see it switch to the box
# first — and Power Toggle is last in both tables, being the only frame with no
# direction of its own.
# ============================================================================
# A television that is off may be unable to answer at all — the set this box is
# for goes completely silent, not even a Feature Abort. While waking one, "no
# reply" therefore means "not up yet", not "the technique failed", and it needs
# its own budget: two readings, the failure budget, rejected the technique that
# had just started the wake and handed the credit to the next one in the table.

# ============================================================================
# A set that has just gone off cannot be woken yet
# ----------------------------------------------------------------------------
# A television takes a few seconds to settle into standby, and a wake frame that
# arrives before it has is ignored. Captured in the field, as the delay between
# the box's own standby and the frame:
#
#     power_on_function   14.7 s works    2.4 s fails
#     power_key           16.3 s works    9.9 s fails
#     active_source       24.0 s works
#
# Nothing under 10 s ever worked; nothing over 14 s ever failed. So a wake is not
# MEASURED until the set has been off this long, counted from the standby the box
# itself sent — a set off all night waits for nothing.
#
# Only while measuring: a confirmed technique is sent at once, or going back to
# the programmes (standby, then wake) would cost thirteen seconds. Read the table
# with care — the five readings compare different techniques at different delays,
# so "this frame does nothing" and "it is too soon" are confounded in them.

# When the box last told this television to go off, on the monotonic clock.
_sent_to_standby_at: "float | None" = None


REPLY_WAIT_SECONDS = 3  # how long to wait for an asynchronous reply on the bus

# The two answers the box reads back, as `cec-ctl` spells them.
POWER_STATUS_RE = re.compile(r"pwr-state:\s*([a-z-]+)", re.IGNORECASE)
CEC_VERSION_RE = re.compile(r"cec-version:\s*version-(\d+)-(\d+)",
                                  re.IGNORECASE)
AUDIO_STATUS_RE = re.compile(r">> \S+:7a:([0-9a-fA-F]{2})")
ACTIVE_SOURCE_RE = re.compile(r">> \S+:82:([0-9a-fA-F]{4})")
REPORT_FEATURES_RE = re.compile(r">> \S+:a6:([0-9a-fA-F:]+)")


class CECError(Exception):
    """Raised when cec-ctl fails to run or times out."""


# ============================================================================
# THE TRANSPORT
# ----------------------------------------------------------------------------
# `cec-ctl`, from v4l-utils, which sends the frame it is given and nothing else.
# It is a thin wrapper over the kernel's own CEC ioctls: no session to keep
# alive, no library deciding anything.
#
# **libCEC is not used, and `cec-utils` is not installed.** Two measured reasons,
# neither with a setting to turn it off:
#
# - it re-announces itself as the active source whenever the adapter's physical
#   address is lost and re-acquired, which on a set that drops its HDMI hotplug
#   is every power-on. Captured: the box sent Standby then Text View On; ten
#   seconds later libCEC sent Image View On and Active Source, and the set
#   answered Set Stream Path naming the box.
# - it can only address a television at logical address 0, and the specification
#   allows 0 and 14 — see `tv_logical_address()`. Every frame then goes
#   unacknowledged while the box reports success.
#
# The two tools cannot share the adapter either: whichever configures a logical
# address takes it from the other. Record the bus with `cec-ctl --monitor`, which
# listens and transmits nothing.
#
# The serialisation lock stays, for the reason it always existed: a button press
# and an HTTP request must not interleave frames on the bus.
# ============================================================================

# Claimed as a recording device so the box keeps the identity the television
# already knows it by — Recording Device 1, which is what libCEC registered —
# rather than turning up as a new device.
REGISTRATION = ["--record", "-o", OSD_NAME]

# Anything the adapter reports as not having reached its destination. The box
# used to have no idea: libCEC swallowed it, so a frame that was never
# acknowledged looked exactly like one the television had obeyed.
NOT_DELIVERED_RE = re.compile(r"Not Acknowledged|Tx, Timeout", re.IGNORECASE)

# And evidence that something was actually put on the bus: the line naming the
# transmission. cec-ctl prints no "OK" for a frame it accepted, so the absence
# of a complaint is all there is to go on — and that absence must mean "sent,
# and nothing objected", never "nothing happened at all". With the adapter's
# addresses cleared, `cec-ctl --to 0 --poll` transmits nothing and says nothing
# about it, which read as "no failure" is indistinguishable from a television
# answering: the box announced it had found a set at address 0 while the real
# one sat at 14.
#
# Matching the sequence number instead is wrong, and was tried: a poll sent
# from Unregistered — all the box can do once the set has taken its HDMI link
# away — carries no sequence number and no status at all. So an attempted poll
# with nothing said against it counts as an answer, which errs towards 14 on a
# dead bus, where it costs nothing, rather than towards 0 on a set that has
# moved, where it costs everything.
TRANSMITTED_RE = re.compile(r"Transmit from", re.IGNORECASE)


def _dotted_address(high: int, low: int) -> str:
    """The two operand bytes of a physical address as cec-ctl spells it:
    0x10, 0x00 -> "1.0.0.0"."""
    return f"{high >> 4:x}.{high & 0xF:x}.{low >> 4:x}.{low & 0xF:x}"


def _own_dotted_address() -> str:
    high, _, low = own_physical_address().partition(":")
    return _dotted_address(int(high, 16), int(low, 16))


# What each opcode becomes, on each side of the bus. Kept as data so a frame
# addressed to the television and one broadcast to everybody cannot be confused
# for one another.
BROADCAST_ADDRESS = 0xF

_DIRECTED_FRAMES = {
    "04": lambda operands: ["--image-view-on"],
    "0d": lambda operands: ["--text-view-on"],
    "36": lambda operands: ["--standby"],
    "44": lambda operands: ["--user-control-pressed",
                            f"ui-cmd=0x{operands[0]:02x}"],
    "45": lambda operands: ["--user-control-released"],
}

_BROADCAST_FRAMES = {
    "82": lambda operands: ["--active-source",
                            f"phys-addr={_dotted_address(*operands)}"],
    "9d": lambda operands: ["--inactive-source",
                            f"phys-addr={_dotted_address(*operands)}"],
    "86": lambda operands: ["--set-stream-path",
                            f"phys-addr={_dotted_address(*operands)}"],
    "80": lambda operands: ["--routing-change",
                            f"orig-phys-addr={_dotted_address(*operands[:2])},"
                            f"new-phys-addr={_dotted_address(*operands[2:])}"],
}


def _frame_arguments(frame: str) -> "list[str]":
    """The cec-ctl arguments that send one raw frame.

    The frames are still written the way every capture in CLAUDE.md and every
    CEC document spells them — `10:36`, `1F:9d:10:00` — because that is the
    vocabulary the measurements are recorded in. Only the header is not taken
    literally: a directed frame goes to wherever the television actually
    answers, not to the nibble in the string, so forgetting the address and
    rendering the same frame again is all a retry needs. There is one other
    device on this bus and it is the set.

    A broadcast frame is not given a `--to` at all: cec-ctl addresses those to
    15 itself, and saying it twice is how a caller ends up sending a directed
    frame to 15. Which of the two a frame is comes from the header, so an opcode
    used on the wrong side raises rather than quietly going somewhere else.
    """
    fields = frame.split(":")
    destination = int(fields[0], 16) & 0xF
    opcode = fields[1].lower()
    operands = [int(field, 16) for field in fields[2:]]
    table = _BROADCAST_FRAMES if destination == BROADCAST_ADDRESS else _DIRECTED_FRAMES
    render = table.get(opcode)
    if render is None:
        raise CECError(f"no cec-ctl equivalent for the frame {frame!r}")
    if destination == BROADCAST_ADDRESS:
        return render(operands)
    return ["--to", str(tv_logical_address())] + render(operands)


def _arguments(command: str) -> "list[str]":
    """The cec-ctl arguments for one command.

    Strict on purpose: a command with no equivalent raises instead of being
    dropped. A frame that never goes out while its caller is told it did is
    the failure this file spends most of its comments on.
    """
    words = command.split()
    verb = words[0] if words else ""
    television = ["--to", str(tv_logical_address())]
    if verb == "tx" and len(words) == 2:
        return _frame_arguments(words[1])
    if verb == "as":
        return ["--active-source", f"phys-addr={_own_dotted_address()}"]
    if verb == "standby":
        return television + ["--standby"]
    if verb == "pow":
        return television + ["--give-device-power-status"]
    if verb == "ver":
        return television + ["--get-cec-version"]
    raise CECError(f"no cec-ctl equivalent for the command {command!r}")


def _outcome(printed: str) -> str:
    """One word for what became of a frame, for the journal."""
    if not TRANSMITTED_RE.search(printed):
        return "never sent"
    return "not acknowledged" if NOT_DELIVERED_RE.search(printed) else "sent"


# ============================================================================
# Which adapter
# ----------------------------------------------------------------------------
# A Pi 4 or 5 has two micro-HDMI ports, each with its own CEC adapter, and
# nothing says which one the television is plugged into. This was hard-coded
# to /dev/cec0, so a set on the second port received nothing at all while the
# journal reported every frame as sent from the wrong adapter.
#
# The port with a display has a valid physical address, read from the set's
# EDID. A set in standby may drop its hotplug and look absent — exactly when it
# has to be woken — so the last adapter a television was seen on is remembered
# on disk and used then.
#
# Chosen once and kept for the life of the process: everything that needs the
# adapter costs a process on this board, and a press must not pay for asking
# both ports every time. A board with a single adapter never asks at all.
# ============================================================================
_chosen_device: "str | None" = None


def _adapters() -> "list[str]":
    return sorted(glob.glob("/dev/cec[0-9]*"))


def _physical_address_of(device: str) -> str:
    try:
        out = subprocess.run(["cec-ctl", "-d", device], capture_output=True,
                             text=True, timeout=CEC_TIMEOUT_SECONDS).stdout
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    for line in out.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip() == "Physical Address":
            return value.strip()
    return "unknown"


def _read_remembered_adapter() -> "str | None":
    try:
        with open(ADAPTER_STATE_PATH, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def _remember_adapter(device: str) -> None:
    try:
        os.makedirs(os.path.dirname(ADAPTER_STATE_PATH), exist_ok=True)
        with open(ADAPTER_STATE_PATH, "w", encoding="utf-8") as handle:
            handle.write(device + "\n")
    except OSError as exc:
        logger.warning("could not remember the CEC adapter: %s", exc)


def _device() -> str:
    """The adapter to talk through: forced, remembered from this process,
    or detected. "" means "let cec-ctl choose"."""
    global _chosen_device
    if CEC_DEVICE is not None:
        return CEC_DEVICE
    if _chosen_device is not None:
        return _chosen_device
    found = _adapters()
    if len(found) == 1:
        _chosen_device = found[0]
        return _chosen_device
    if not found:
        # No adapter visible (a machine without one, a test run): what this
        # always used, and cec-ctl will say what is wrong with it.
        return "/dev/cec0"
    remembered = _read_remembered_adapter()
    live = [d for d in found if _physical_address_of(d) not in ("unknown", "f.f.f.f")]
    if live:
        _chosen_device = remembered if remembered in live else live[0]
        if _chosen_device != remembered:
            _remember_adapter(_chosen_device)
        logger.info("cec adapter: %s, the port with a television on it", _chosen_device)
        return _chosen_device
    if remembered in found:
        _chosen_device = remembered
        logger.info("cec adapter: no port shows a television, using %s, "
                    "where one was last seen", remembered)
        return _chosen_device
    # Never seen a television on any port. Not kept: the next frame looks
    # again, so the set is found as soon as it is plugged in or wakes.
    logger.warning("cec adapter: no port shows a television and none was ever "
                   "seen; trying %s", found[0])
    return found[0]


def _cec_ctl(arguments: "list[str]") -> str:
    # No `-d` at all when no device is named: `-d ""` would ask for a device
    # called "" and fail every single call.
    chosen = _device()
    device = ["-d", chosen] if chosen else []
    try:
        done = subprocess.run(["cec-ctl"] + device + arguments,
                              capture_output=True, text=True,
                              timeout=CEC_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CECError(f"cec-ctl timed out on {' '.join(arguments)}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise CECError(f"cec-ctl could not run ({exc})") from exc
    printed = done.stdout + done.stderr
    # The whole of what the tool said, for when one line is not enough. At debug
    # level because it is several lines per frame: raise the service's level
    # with MAMAN_LOG_LEVEL rather than reaching for a monitor.
    logger.debug("cec-ctl %s\n%s", " ".join(arguments), printed.rstrip())
    return printed


def _ensure_a_logical_address(reading: "dict | None" = None) -> None:
    """Claim a logical address when nothing holds one.

    The adapter loses its address whenever the television drops its HDMI
    hotplug, which some sets do at every power-on. A transmit with no address at
    all was captured going out as Unregistered and never acknowledged, and no
    reply can come back to an address that does not exist — which `pow` is.

    **When** this is done matters as much as that it is done.
    `take_the_bus()` is called while the television is still displaying the box
    and the bus is certainly alive. Left until a frame was due — by which time
    the output had gone to sleep and the set had stopped showing that input —
    the claim polled, timed out, and the standby and wake that followed went
    nowhere. This call stays as the fallback for anything reaching the transport
    by another road.
    """
    reading = reading if reading is not None else adapter_state()
    if reading.get("logical_address_mask") not in ("unknown", "0x0000"):
        return
    _cec_ctl(REGISTRATION)


def take_the_bus(reading: "dict | None" = None) -> None:
    """Claim a logical address, best effort.

    Never raises: failing to register is not a reason to send nothing. The set
    this project was built against obeyed a Text View On sent from
    Unregistered, so a frame is still worth more than an exception.
    """
    try:
        _ensure_a_logical_address(reading)
    except CECError as exc:
        logger.warning("could not claim a logical address: %s", exc)


def shutdown() -> None:
    """Forget what was measured about the bus. Called when the API stops.

    There is no process to close any more — that was libCEC's session, and
    with it went the whole business of a live process that could die, be
    restarted, and poke the television twice each time it was.
    """
    with _CEC_LOCK:
        forget_the_television()


# ----------------------------------------------------------------------------
# Where the television is
# ----------------------------------------------------------------------------
# A television is normally logical address 0, and assuming it is what left the
# box inert for an evening: one set answered on 0 for hours, came back on **14**
# after a power cycle, and from then on every frame to 0 went unacknowledged.
# The set would not switch off, showed "no signal", and the box reported success
# on every press because it had sent what it meant to send.
#
# 14 is legal — the specification gives a television 0 and 14, and a set that
# finds 0 taken uses the other — so the address is measured like everything else
# hardware-specific here. A poll costs 16 ms when somebody answers and about
# 150 ms when nobody does.
TV_LOGICAL_ADDRESSES = (0, 14)
DEFAULT_TV_LOGICAL_ADDRESS = TV_LOGICAL_ADDRESSES[0]

_tv_address: "int | None" = None


def _answers_a_poll(address: int) -> bool:
    """Whether somebody acknowledges a poll at this address.

    Both halves are required: the poll has to have gone out, and nothing may
    have reported it undelivered. Silence from cec-ctl is not an answer — see
    TRANSMITTED_RE.
    """
    try:
        printed = _cec_ctl(["--to", str(address), "--poll"])
    except CECError as exc:
        logger.info("could not poll logical address %d: %s", address, exc)
        return False
    if not TRANSMITTED_RE.search(printed):
        logger.info("the poll of logical address %d never went out", address)
        return False
    return not NOT_DELIVERED_RE.search(printed)


def find_the_television(reading: "dict | None" = None) -> int:
    """Poll for the television and remember where it answered.

    Falls back to 0 rather than raising: a frame sent to the usual address is
    worth more than no frame at all, and a box whose set is unplugged must
    still answer a press.
    """
    global _tv_address
    # An address of our own first: with none claimed, cec-ctl puts nothing on
    # the bus, so every poll below would be a question nobody was asked.
    take_the_bus(reading)
    for address in TV_LOGICAL_ADDRESSES:
        if _answers_a_poll(address):
            if address != _tv_address:
                state.note("television found", logical_address=address,
                           was=_tv_address)
            _tv_address = address
            return address
    logger.info("no television answered a poll; assuming logical address %d",
                DEFAULT_TV_LOGICAL_ADDRESS)
    return DEFAULT_TV_LOGICAL_ADDRESS


def tv_logical_address() -> int:
    """Where to send a frame addressed to the television.

    Remembered once found: a set does not move while it is switched on, and a
    poll before every frame would double what a press costs. `_run()` forgets
    it whenever a frame goes unacknowledged, which is exactly the symptom of
    it having moved.
    """
    if _tv_address is not None:
        return _tv_address
    return find_the_television()


def forget_the_television() -> None:
    global _tv_address
    _tv_address = None


# How long to give the connector to come back once the output is driving it
# again. Measured on the board: the address returned within a few seconds of
# the output waking, and never at all while it slept.
LINK_RECOVERY_SECONDS = 6


def link_is_down(reading: "dict | None" = None) -> bool:
    """No physical address means no frame can go out at all.

    Takes an adapter reading when the caller already has one: every frame needs
    this answer and the address claim needs the same reading, and reading the
    adapter costs a process.

    The kernel derives the adapter's address from the EDID and refuses to
    allocate a logical address without one. Measured with the set off:
    `Physical Address: f.f.f.f`, no EDID, nothing claimable, every frame going
    out as Unregistered and unacknowledged — the box deaf and mute, and saying
    nothing about it, because every press was reported as a success.
    """
    reading = reading if reading is not None else adapter_state()
    return reading.get("physical_address") in ("unknown", "f.f.f.f")


# How often the link has gone away since boot, and how often waking the output
# brought it back. Counted because the question "did the television take its
# HDMI link away, and how often" is one somebody asks days later, from the
# journal or from /report, about an evening nobody was watching. Published by
# `adapter_state()`, so `/report` and `maman-tv report` carry them without
# anyone remembering to look somewhere else.
_link_down_seen = 0
_link_recovered = 0


def bring_the_link_back() -> bool:
    """Wake the box's own output, which is what brings the link back.

    Measured with a set unreachable: forcing the connector to look again
    (`echo detect`) did nothing, and waking the output brought the address back
    within seconds — the set re-establishes the link when something is driving
    it, and then answers CEC again.

    This is the one place outside `modes.py` that touches the output, against
    the architecture's own rule 4, and it is deliberate: the CEC bus
    physically depends on that output, and with the link down there is no
    other lever at all. It is repaired and handed straight back, so the mode
    machine still decides what the output is *for*; a caller that wanted the
    output asleep gets it asleep again below.

    Every branch says so out loud, at warning level and all beginning "cec
    link", so one `grep` over a journal answers what happened. The branch that
    can do nothing is the loudest: an earlier version returned from it in
    silence, which made the one situation the box cannot get itself out of the
    one situation it said nothing about.
    """
    import hdmi_output

    global _link_down_seen, _link_recovered
    _link_down_seen += 1
    before = adapter_state().get("physical_address", "unknown")
    if hdmi_output.state() == hdmi_output.AWAKE:
        logger.warning("cec link down: physical address %s, and the box's output is "
                       "already awake — nothing left to try. Seen %d time(s) since "
                       "boot, recovered %d.",
                       before, _link_down_seen, _link_recovered)
        state.note("cec link down", address=before, recovered=False,
                   why="the output was already awake", seen=_link_down_seen)
        return False
    logger.warning("cec link down: physical address %s. Waking the box's output to "
                   "bring it back. Seen %d time(s) since boot, recovered %d.",
                   before, _link_down_seen, _link_recovered)
    hdmi_output.wake("the CEC link was down")
    time.sleep(LINK_RECOVERY_SECONDS)
    after = adapter_state().get("physical_address", "unknown")
    recovered = after not in ("unknown", "f.f.f.f")
    if recovered:
        _link_recovered += 1
        logger.warning("cec link back: physical address %s, after %g s. "
                       "Seen %d time(s) since boot, recovered %d.",
                       after, LINK_RECOVERY_SECONDS, _link_down_seen, _link_recovered)
    else:
        logger.warning("cec link still down: physical address %s, %g s after waking "
                       "the output. The television has to be switched on by hand. "
                       "Seen %d time(s) since boot, recovered %d.",
                       after, LINK_RECOVERY_SECONDS, _link_down_seen, _link_recovered)
    state.note("cec link back" if recovered else "cec link still down",
               address=after, recovered=recovered, seen=_link_down_seen)
    return recovered


def _run(command: str) -> str:
    """Send one command and return everything cec-ctl printed."""
    with _CEC_LOCK:
        return _run_unlocked(command)


def _run_unlocked(command: str) -> str:
    # One reading of the adapter for the whole frame. "Is the link up" and "do I
    # hold an address" are both answered by it, and each reading costs a process
    # — two of them made an everyday press a third slower than it needed to be.
    reading = adapter_state()
    # Repaired before anything else is attempted: with no physical address,
    # claiming an address fails, every poll is a question nobody was asked and
    # every frame goes out unacknowledged.
    repaired = False
    if link_is_down(reading):
        repaired = bring_the_link_back()
        forget_the_television()
        # The repair changed the adapter, so the reading above is history.
        reading = None
    # Everything that needs the adapter happens here, once, so `_send` below can
    # be exactly "render it and put it on the bus". The discovery claims an
    # address on its own, so asking for one again afterwards would re-claim on a
    # reading that is already out of date — which it did, one wasted process per
    # first press.
    if _tv_address is None:
        find_the_television(reading)
    else:
        take_the_bus(reading)
    try:
        return _send(command)
    finally:
        if repaired:
            # Handed straight back to whatever the box was doing. The output
            # was asleep because the default state wants it asleep — that is
            # what stops a set which remembers its last input from coming back
            # on the box — and this borrowed it for a repair, nothing more.
            import hdmi_output
            hdmi_output.sleep("the CEC link is back")


def _send(command: str) -> str:
    """Render one command and put it on the bus.

    Takes no reading of the adapter: `_run_unlocked` above has done all of that,
    once, for the whole frame.
    """
    arguments = _arguments(command)
    output = _cec_ctl(arguments)
    # One line per frame, always. Without it the journal recorded that a
    # technique had been "sent" and nothing about whether it reached the
    # television — which is exactly how an evening of presses that did nothing
    # looked like an evening of presses that worked.
    logger.info("cec %s -> %s", " ".join(arguments), _outcome(output))
    if not NOT_DELIVERED_RE.search(output):
        return output
    # Nobody acknowledged it, which on a bus with one other device means the
    # box is talking to the wrong address. Look again and send it once more:
    # `_arguments` resolves the destination as it builds them, so the same
    # command comes out addressed to wherever the set has gone. Once, never in
    # a loop — a television that is off answers nothing at all, and a press
    # must not turn into a minute of retries.
    was = _tv_address
    forget_the_television()
    now = find_the_television()
    if now == was:
        logger.info("%r was not acknowledged and the television is still at %s",
                    command, was)
        return output
    # The one place a move is actually observed, so the one place that says so.
    # Saying it inside `find_the_television()` instead looked tidier and was
    # nearly dead code: the retry clears the remembered address before looking,
    # so by then there was nothing left to compare against.
    logger.warning("the television has moved from logical address %s to %s; "
                   "resending %r", was, now, command)
    return _cec_ctl(_arguments(command))


def _wake_image_view_on() -> str:
    """Image View On (0x04), as a raw frame.

    Deliberately not libCEC's `on 0`. That helper starts by interrogating the
    television, and against a set that answers nothing it gives up before ever
    sending the wake: captured over a whole session on such a set, `on 0` had
    emitted Give Vendor ID, a poll and Set OSD Name — and not once 0x04. The
    best technique in the table was therefore never actually tried. Sending the
    frame ourselves costs nothing and works whether or not the set talks back.
    """
    return _run(f"tx {_tv_frame_prefix()}:04")


def switch_to_pi() -> str:
    """Claim Active Source for this box.

    Not reliable on every TV — roughly a 50% success rate was observed on the
    set used for development. A television may simply ignore the request
    without reporting any CEC error.
    """
    return _run("as")


def _sleep_standby() -> str:
    """Standby (0x36), sent without touching the displayed input.

    The best technique when it works, because nothing else on the TV moves.
    Some televisions ignore it unless the box is the displayed source, which is
    what _sleep_standby_after_active_source() is for.
    """
    return _run("standby")


def _sleep_standby_after_active_source() -> str:
    """Standby, after claiming the input and letting it settle.

    This is what the box did unconditionally before the techniques were
    learned, and it remains correct — merely more intrusive, since the viewer
    sees the input change before the set goes off. See
    ACTIVE_SOURCE_SETTLE_SECONDS for why the pause is not optional.
    """
    traffic = switch_to_pi()
    time.sleep(ACTIVE_SOURCE_SETTLE_SECONDS)
    return traffic + _run("standby")


def get_power_status() -> str:
    """Query the TV's actual power state over CEC (on/standby/unknown).

    Every reading is recorded in the box's state, with the time it was taken.
    The diagnostic screen and the report then say "standby, read four minutes
    ago" rather than "standby", which is the difference between a fact and a
    guess when somebody is standing in front of a television that looks off.
    """
    output = _run("pow")
    match = POWER_STATUS_RE.search(output)
    reading = match.group(1).strip().lower() if match else "unknown"
    state.set_tv_power(reading)
    return reading


def power_toggle() -> dict[str, str]:
    """Read the TV's actual state, then do the opposite.

    Lives here rather than in the HTTP endpoint because two callers now use
    it: /tv/toggle and the Zigbee buttons (zigbee_bridge). One implementation
    instead of two that would drift apart.

    The whole block holds the lock: without it another command could slip
    between reading the state and acting on it, and we would switch off a TV
    that had just been switched on.

    Any state other than "on" (standby, in transition, unknown) is treated as
    "turn it on" — the safest default, since turning on an already-on TV does
    nothing.
    """
    with _CEC_LOCK:
        status = get_power_status()
        if status == "on":
            standby()
            action = "toggle-off"
        else:
            power_on()
            action = "toggle-on"
    return {"action": action, "status_before": status}


def _wake_text_view_on() -> str:
    """Text View On (0x0D), the sibling of Image View On."""
    return _run(f"tx {_tv_frame_prefix()}:0d")


def _wake_power_on_function() -> str:
    """User Control 'Power On Function' (0x6D), then release."""
    return _press_user_control("6d")


def _wake_power_key() -> str:
    """User Control 'Power' (0x40), the plain remote key, then release."""
    return _press_user_control("40")


def _wake_power_toggle_key() -> str:
    """User Control 'Power Toggle Function' (0x6B), then release."""
    return _press_user_control("6b")


def _wake_active_source() -> str:
    """Announce ourselves as the active source and nothing more.

    Some televisions wake on the announcement alone, having no separate
    power-on handling at all.
    """
    return switch_to_pi()


def _sleep_power_off_function() -> str:
    """User Control 'Power Off Function' (0x6C), then release."""
    return _press_user_control("6c")


def _sleep_power_off_function_after_active_source() -> str:
    traffic = switch_to_pi()
    time.sleep(ACTIVE_SOURCE_SETTLE_SECONDS)
    return traffic + _press_user_control("6c")


def _tv_frame_prefix() -> str:
    """Source and destination nibbles for a frame addressed to the television.

    Truthful about where the set is, so a frame quoted in a log reads like the
    frame that went out. The transport resolves the destination again as it
    builds the arguments, which is what lets a retry re-address the same
    frame — see `_frame_arguments()`.
    """
    return f"1{tv_logical_address():x}"


def _press_user_control(code: str) -> str:
    """Send User Control Pressed followed by Released.

    The release is not optional: a television that never receives it can treat
    the key as held down and ignore everything that follows.
    """
    prefix = _tv_frame_prefix()
    return _run(f"tx {prefix}:44:{code}") + _run(f"tx {prefix}:45")


# Which wake techniques are safe to send to a television that is ALREADY ON.
#
# This is not a detail: it decides whether an ordinary press has to read the
# set's power state first, and on a set that answers nothing in standby that
# read costs the full CEC_TIMEOUT_SECONDS — measured end to end, 15.8 s
# between pressing the music button and the wake frame going out, with no
# picture and no sound in between. The person presses again, and the box
# looks broken.
#
# A directional frame ("turn on", "show this source") does nothing harmful to
# a set that is already on, so entering a mode can simply send it and skip the
# read entirely. A toggle cannot: sent to a set that is on, it switches it
# OFF. Power Toggle (0x6B) is explicitly one, and User Control "Power" (0x40)
# behaves as one on many televisions, so both stay out of this set.
SAFE_TO_REPEAT_WAKE = frozenset({
    "image_view_on", "text_view_on", "power_on_function", "active_source",
})

# Ordered best-first. The first entry that works is the one kept, so the order
# is a statement about quality, not just a search sequence.
WAKE_TECHNIQUES: "tuple[tuple[str, object], ...]" = (
    ("image_view_on", _wake_image_view_on),
    ("text_view_on", _wake_text_view_on),
    ("power_on_function", _wake_power_on_function),
    ("power_key", _wake_power_key),
    ("active_source", _wake_active_source),
    # Last, like its twin in the sleep table: a toggle has no direction, so a
    # set that obeys it can just as well be sent the wrong way.
    ("power_toggle_key", _wake_power_toggle_key),
)

# "standby" first because it disturbs nothing: the viewer sees the set go off,
# not the input change first.
SLEEP_TECHNIQUES: "tuple[tuple[str, object], ...]" = (
    ("standby", _sleep_standby),
    ("power_off_function", _sleep_power_off_function),
    # Last on purpose, and the reason is historical but the ordering still
    # earned: claiming the input made libCEC believe it was the active source,
    # after which it re-announced itself on bus events — and that announcement
    # carried Image View On. Captured on a real set: the standby was obeyed and
    # two `<< 10:04` followed, so the television went off and straight back on,
    # on the box's input. libCEC is gone, so that particular echo cannot happen
    # any more; what remains true is that the viewer should not see the input
    # change before the set goes off.
    # A set that needs the claim still gets it; one that does not must never
    # see it.
    ("standby_after_active_source", _sleep_standby_after_active_source),
    ("power_off_function_after_active_source", _sleep_power_off_function_after_active_source),
    # Dead last, and on purpose. Power Toggle is the one frame in the table
    # with no direction of its own: a set that obeys it can just as easily be
    # sent the wrong way, and during a search for how to switch off it would
    # switch back on and poison everything measured afterwards. Kept only for a
    # television that answers nothing else.
    ("power_toggle_key", _wake_power_toggle_key),
)


def _release_inactive_source() -> str:
    """Inactive Source (0x9D), broadcast, naming the box's own address.

    Tells the television the box is no longer the active source. Unlike the
    variants below, this does not ask the set to go anywhere specific — some
    televisions fall back to whatever they were showing before on this alone.
    """
    return _run(f"tx 1F:9d:{own_physical_address()}")


def _release_set_stream_path() -> str:
    """Set Stream Path (0x86) to 0.0.0.0: "go to the top of the tree."

    Reserved to the television itself by the protocol, and refused outright
    (Feature Abort, unrecognised opcode) on the set this project was built
    against — kept here because a search must still measure it on every
    television, not assume the result from one.
    """
    return _run("tx 1F:86:00:00")


def _release_routing_change() -> str:
    """Routing Change (0x80): box's address to 0.0.0.0."""
    return _run(f"tx 1F:80:{own_physical_address()}:00:00")


def _release_tuner_keys() -> str:
    """User Control 'Channel Up' (0x30), press then release.

    Best-effort: the protocol has no dedicated "back to the tuner" key, and
    this is the one remote key CLAUDE.md's own history records as already
    having been tried at the installation site (among the frames a set
    ignored). Kept as a candidate for the general case rather than assumed to
    work anywhere in particular — like every entry in this table, a
    television it does nothing on simply will not select it.
    """
    return _press_user_control("30")


# Candidates for "give the person their programmes back" (spec step 5). The
# first four keep the box's output awake while they are tested: asleep, a set
# that switches away on its own for lack of a signal would be credited with a
# frame that did nothing. The fifth candidate, power_cycle, is not a table
# entry here at all — its mechanism IS the box's own output going to sleep,
# which only modes.py is allowed to touch, so it is driven from there instead
# of through this table.
RELEASE_TECHNIQUES: "tuple[tuple[str, object], ...]" = (
    ("inactive_source", _release_inactive_source),
    ("set_stream_path", _release_set_stream_path),
    ("routing_change", _release_routing_change),
    ("tuner_keys", _release_tuner_keys),
)


POWER_STATE_ALIASES = {
    "on": ("on",),
    "standby": ("standby", "off"),
}


def _safe_power_status() -> str:
    """The TV's power state, with a dead bus reported rather than raised.

    Verification must never turn a refused command into a 500: the caller's job
    is to decide whether a technique worked, and "the bus stopped answering" is
    an answer it knows how to use.
    """
    try:
        return get_power_status()
    except CECError as exc:
        logger.info("could not read the power state: %s", exc)
        return "unknown"


def _parse_cec_version(output: str) -> "str | None":
    """The `CEC version X.Y` line the TV replied with, or None when the bus
    said nothing recognisable — as opposed to get_cec_version()'s
    "unexpected reply: ..." fallback below, which is for logging only and
    must never be mistaken for genuine proof that something answered
    (see television_identity())."""
    # cec-ctl says "cec-version: version-1-4 (0x05)". Rendered as libCEC used
    # to print it, so a profile measured before this change and one measured
    # after read the same — `profiles/SAMSUNG_0x7590.json` carries the old
    # spelling and must not start disagreeing with the box in front of it.
    match = CEC_VERSION_RE.search(output)
    return f"CEC version {match.group(1)}.{match.group(2)}" if match else None


def get_cec_version() -> str:
    output = _run("ver")
    return _parse_cec_version(output) or (
        "unexpected reply: " + " | ".join(output.strip().splitlines()))


# ============================================================================
# The two commands the product actually sends
# ============================================================================
def _technique(kind: str, table) -> "tuple[str, object]":
    """The configured technique for one direction, and what to do if it is
    unknown — which can only happen if somebody hand-edited the file."""
    configured = tv_config.load()[kind]["technique"]
    known = dict(table)
    if configured in known:
        return configured, known[configured]
    fallback = tv_config.DEFAULT_WAKE if kind == "wake" else tv_config.DEFAULT_SLEEP
    logger.warning("unknown %s technique %r in %s; falling back to %r",
                   kind, configured, tv_config.PATH, fallback)
    return fallback, known[fallback]


def power_on() -> dict:
    """Wake the television with the configured technique. One frame, no more.

    No verification and no search: this is an everyday press, and the box has
    nothing to work out. Whether the set really came on is answered by the
    television itself — the viewer is looking at it — and by /tv/status for
    anybody who is not.
    """
    with _CEC_LOCK:
        name, technique = _technique("wake", WAKE_TECHNIQUES)
        technique()
    state.note("television: wake sent", technique=name)
    return {"action": "power-on", "technique": name}


def standby() -> dict:
    """Switch the television off with the configured technique."""
    with _CEC_LOCK:
        name, technique = _technique("sleep", SLEEP_TECHNIQUES)
        technique()
    state.note("television: standby sent", technique=name)
    return {"action": "standby", "technique": name}


# ============================================================================
# What the box can say about the television in front of it
# ============================================================================
EDID_PATHS = "/sys/class/drm/card*-HDMI-A-*/edid"

# Three letters packed five bits each, which is how EDID stores a manufacturer.
def _edid_manufacturer(block: bytes) -> str:
    packed = int.from_bytes(block[8:10], "big")
    return "".join(chr(64 + ((packed >> shift) & 31)) for shift in (10, 5, 0))


def _edid_name(block: bytes) -> str:
    for start in range(54, 126, 18):
        if block[start:start + 5] == b"\x00\x00\x00\xfc\x00":
            return block[start + 5:start + 18].split(b"\n")[0].decode("ascii", "replace").strip()
    return ""


def television_identity() -> dict:
    """Manufacturer, model and name as the set itself reports them.

    Nothing decides anything from this. It answers "was this configuration
    made for the television standing in front of me?", which during the
    2026-09-22 incident nobody could answer.

    An empty dict here is what cec_detection.py's E1 gate ("nothing
    answers") tests for, so a `cec_version` entry is only added when the
    bus actually sent back a recognisable version line — not
    get_cec_version()'s "unexpected reply: ..." fallback, which is truthy
    even when nothing at all replied and used to make E1 impossible to
    reach.
    """
    identity = {}
    for path in sorted(glob.glob(EDID_PATHS)):
        try:
            with open(path, "rb") as handle:
                block = handle.read(128)
        except OSError:
            continue
        if len(block) < 128:
            continue
        identity = {"manufacturer": _edid_manufacturer(block),
                    "product": f"0x{int.from_bytes(block[10:12], 'little'):04x}",
                    "name": _edid_name(block)}
        break
    try:
        version = _parse_cec_version(_run("ver"))
        if version:
            identity["cec_version"] = version
    except CECError:
        pass
    return identity


# Remembered once read: the address only changes when the cable moves to
# another socket, which needs hands on the box. Only a *successful* reading is
# kept, so a box that started before the television was reachable picks the
# real one up on a later call instead of being stuck with the fallback.
_own_address: "str | None" = None


def _address_as_operands(dotted: str) -> "str | None":
    """"1.0.0.0" -> "10:00", the two bytes a frame carries. None if the
    adapter has no usable address (unknown, or the unconfigured f.f.f.f)."""
    parts = dotted.strip().split(".")
    if len(parts) != 4:
        return None
    try:
        nibbles = [int(part, 16) for part in parts]
    except ValueError:
        return None
    if any(nibble > 0xF for nibble in nibbles) or nibbles == [0xF] * 4:
        return None
    return f"{nibbles[0]:x}{nibbles[1]:x}:{nibbles[2]:x}{nibbles[3]:x}"


def own_physical_address() -> str:
    """The box's own address, for the frames that must name it.

    Falls back to the first HDMI socket rather than raising: a frame with a
    plausible address is worth more than no frame at all, and the fallback is
    right on the wiring this project was built against.
    """
    global _own_address
    if _own_address is not None:
        return _own_address
    # Never raises. This is called while composing an ordinary frame, on the
    # everyday path — `television()` sends Inactive Source whenever the
    # detection found it — so anything that escapes here does not fail one
    # frame, it takes down whatever was running. That is exactly what
    # happened on 2026-09-26: an IndexError from the line below ended a CEC
    # detection in the middle of step 3.
    try:
        found = _address_as_operands(adapter_state().get("physical_address", ""))
    except Exception:
        logger.exception("could not read the adapter's own address")
        found = None
    if found is None:
        logger.info("no usable physical address from the adapter; using %s",
                    FALLBACK_PHYSICAL_ADDRESS)
        return FALLBACK_PHYSICAL_ADDRESS
    _own_address = found
    logger.info("the box's own physical address reads %s", found)
    return found


def adapter_state() -> dict:
    """Whether the box can talk to the bus at all.

    `cec-ctl` rather than the kernel's debug file: that one is root's, and this
    service is not root. Without this, "the television does not answer" and
    "the box cannot send anything" look identical — which cost an hour on
    2026-09-22.
    """
    device = _device()
    answer = {"device": device or "(cec-ctl's default)", "ready": False,
              "physical_address": "unknown", "logical_addresses": "unknown",
              "logical_address_mask": "unknown"}
    try:
        out = subprocess.run(["cec-ctl"] + (["-d", device] if device else []),
                             capture_output=True,
                             text=True, timeout=CEC_TIMEOUT_SECONDS).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        answer["error"] = str(exc)
        return answer
    for line in out.splitlines():
        # Matched on the exact label, after splitting on the separator —
        # never "is this substring somewhere in the line".
        #
        # `cec-ctl` lists the adapter's capabilities as bare indented words
        # under "Capabilities", and one of them is literally "Logical
        # Addresses", with no colon at all. A substring test matched it, the
        # split then had nothing to its right, and this raised IndexError. It
        # only ever ran from /report, where it made one route fail — until
        # `own_physical_address()` started calling it, and a crash here took
        # the whole CEC detection procedure down with it, mid-step, leaving a
        # black screen. Measured on the box on 2026-09-26.
        #
        # The exact match also settles "Available Logical Addresses: 1",
        # which the substring test matched too and which then overwrote the
        # real answer depending on line order.
        label, separator, value = line.partition(":")
        if not separator:
            continue
        label, value = label.strip(), value.strip()
        if label == "Physical Address":
            answer["physical_address"] = value
        elif label == "Logical Addresses":
            answer["logical_addresses"] = value
        elif label == "Logical Address Mask":
            # Not the same thing as the line above, and the difference is the
            # whole of "can this box send anything": the adapter goes on
            # listing "Logical Addresses: 1" when the mask is 0x0000 and
            # nothing is actually allocated. Measured with the set's hotplug
            # down.
            answer["logical_address_mask"] = value
    answer["ready"] = answer["physical_address"] not in ("unknown", "f.f.f.f")
    # Not a reading of the adapter but the history of this one: how often the
    # television has taken its HDMI link away, and how often waking the box's
    # output brought it back. `/report` is where somebody looks days later.
    answer["link_down_seen"] = _link_down_seen
    answer["link_recovered"] = _link_recovered
    return answer
