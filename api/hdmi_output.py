# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The box's own HDMI output: asleep unless the box needs the screen.

**Why this exists.** A television that comes back on "the last input used"
comes back on the box after music or photos, and the viewer gets a technical
screen instead of their programmes. No CEC frame fixes it: on the set at the
installation site, Set Stream Path was refused outright (Feature Abort,
unrecognised opcode) and thirteen other frames were ignored without a word.
What does fix it is having no signal on that input when the set wakes: the set
then falls back to its own programmes. Measured on two different televisions.

So the box's output is asleep whenever the box is not using the screen, and
the problem disappears by construction rather than by timing a sequence.

**Asleep, not disconnected.** Two ways to stop the picture were measured on
2026-09-22, both of which the television reports as an absent source ("check
the device's power supply" on the set used for the measurement):

| | connector forced off | output asleep (here) |
|---|---|---|
| picture | gone | gone |
| connector | reports disconnected | still connected |
| EDID kept by the kernel | **dropped** | kept |
| CEC address | kept, *on a set that stays connected in standby* | kept |

The difference is the EDID. Forcing the connector off makes the kernel forget
it, and it can only read it again from a set that answers — which a set that
electrically unplugs its input while it sleeps never does. That is what left
the box unable to send a single CEC frame at the installation site, with the
television having to be switched on by hand before it could be controlled
again. Putting the output to sleep never touches the connector, so that
failure cannot happen, on any set.

**Root, through one unit.** `/sys/class/graphics/fb0/blank` belongs to root
and this service runs with NoNewPrivileges, so it cannot write there even
though its account is in the `video` group. The polkit rule lets it start and
stop exactly one unit, the same way it grants reboot — and starting that unit
puts the output to sleep, stopping it wakes it.

**Never assumed.** Every call reads `dpms` back from every connected
connector. "I asked for it" is not "it happened": on 2026-09-22 a version
reported a cut that had not taken place, and the journal read as a working fix
for a quarter of an hour.
"""

import glob
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# Two ways to stop driving the screen, one unit each. Which one a box uses is
# a configuration point (tv_config.hdmi_sleep), because a television that
# ignores one may obey the other — and because they are not equally safe:
# forcing the connector off makes the kernel forget the set's EDID, so "blank"
# is the default and "connector" is the fallback.
UNITS = {"blank": "maman-hdmi-blank.service",
         "connector": "maman-hdmi-disconnect.service"}

CONNECTORS = "/sys/class/drm/card*-HDMI-A-*"

# systemctl answers in well under a second on this board; the ceiling is for a
# D-Bus that is not answering at all, which must not hold a button press.
SYSTEMCTL_TIMEOUT_SECONDS = 20

ASLEEP = "asleep"
AWAKE = "awake"
UNKNOWN = "unknown"


def method() -> str:
    import tv_config
    return tv_config.load()["hdmi_sleep"]


def _systemctl(verb: str, unit: str) -> bool:
    try:
        done = subprocess.run(["systemctl", verb, unit], capture_output=True,
                              text=True, timeout=SYSTEMCTL_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not %s %s: %s", verb, unit, exc)
        return False
    if done.returncode != 0:
        logger.warning("could not %s %s: %s", verb, unit,
                       (done.stderr or done.stdout).strip())
        return False
    return True


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return "?"


def outputs() -> "dict[str, dict]":
    """Every HDMI connector, with what each file says about it.

    All three are needed, and each lies about something on its own. `status`
    only echoes what was forced. `enabled` stays "enabled" while the output
    sleeps — a version that read it reported a cut that had not happened.
    `dpms` is the one that answers "is this output being driven?", and it is
    the only one that moves when the output is put to sleep.
    """
    found = {}
    for path in sorted(glob.glob(CONNECTORS)):
        found[os.path.basename(path)] = {
            "status": _read(os.path.join(path, "status")),
            "dpms": _read(os.path.join(path, "dpms")),
            "enabled": _read(os.path.join(path, "enabled")),
        }
    return found


def state() -> str:
    """asleep, awake, or unknown when nothing can be told.

    A connector is asleep when it is not driving a picture, which the two
    methods show differently: the output blanked leaves it connected with
    `dpms=Off`, the connector forced off leaves it `disconnected`.
    """
    seen = outputs()
    if not seen:
        return UNKNOWN
    # A board with two ports (Pi 4, Pi 5) has one with nothing plugged into
    # it, and that one is "disconnected" for good. Counted, it read as asleep
    # next to the port actually driving the television, the two disagreed,
    # and every transition was reported as "not as asked" (measured on a Pi 5,
    # 2026-10-02). Only the ports with a set behind them say anything — unless
    # none has, which is how the connector method leaves the one it forced off.
    connected = {name: files for name, files in seen.items()
                 if files["status"] == "connected"}
    seen = connected or seen
    def judge(files):
        if files["status"] != "connected":
            return ASLEEP
        if files["dpms"] == "Off":
            return ASLEEP
        if files["dpms"] == "On":
            return AWAKE
        return UNKNOWN
    verdicts = {judge(files) for files in seen.values()}
    return verdicts.pop() if len(verdicts) == 1 else UNKNOWN


def _describe(seen: "dict[str, dict]") -> str:
    return ", ".join(f"{name} status={files['status']} dpms={files['dpms']}"
                     for name, files in seen.items()) or "no HDMI connector found"


def _apply(verb: str, expected: str, why: str) -> bool:
    import state as box_state

    chosen = method()
    if verb == "start":
        # **`restart`, never `start`.** Both units are oneshots with
        # `RemainAfterExit=yes`, which makes them an on/off switch — and
        # systemd treats `start` on a unit it already considers active as a
        # no-op. It does not re-run ExecStart, and it reports success.
        #
        # That matters because the hardware can be changed behind the
        # switch's back. Anything that touches the framebuffer — fbi drawing
        # an installation page, a `chvt`, the photo slideshow — resets
        # /sys/class/graphics/fb0/blank, and from that moment the unit says
        # "active" while the output is wide awake. Every later attempt to put
        # it to sleep then did nothing at all, silently.
        #
        # Measured on the board on 2026-09-26, with the unit active since
        # 02:36 and the framebuffer reset behind it:
        #
        #     blank before      : 1  dpms=On
        #     after start (noop): 1  dpms=On
        #     after restart     : 1  dpms=Off
        #
        # This is the one mechanism the whole product rests on — a set that
        # comes back on its last input must find that input silent — so a
        # sleep that quietly does nothing is the worst failure this box has.
        #
        # Spelled as stop-then-start rather than `restart` on purpose. The
        # polkit rule grants exactly two verbs on exactly two units, and says
        # so: "the API cannot restart, enable or mask anything". Asking for
        # `restart` gets "Interactive authentication required" — measured —
        # and widening a security rule to buy one D-Bus round trip is the
        # wrong trade. `stop` runs ExecStop, `start` runs ExecStart, which is
        # what restart does anyway.
        _systemctl("stop", UNITS[chosen])
        refused = not _systemctl("start", UNITS[chosen])
    else:
        # Both are stopped on the way back: the method can have changed while
        # the output was asleep, and stopping a unit that is not running costs
        # nothing.
        results = [_systemctl("stop", unit) for unit in UNITS.values()]
        refused = not all(results)
    if refused:
        box_state.note("hdmi output: command refused", verb=verb, why=why,
                       method=chosen)
        box_state.set_hdmi(UNKNOWN)
        return False
    seen = outputs()
    got = state()
    box_state.set_hdmi(got)
    if got != expected:
        # Loudly: a sleep that did not happen is a television that will come
        # back on the box, and a wake that did not happen is a black screen.
        # "unknown" with nothing plugged in is not a failure worth shouting
        # about — there is no screen to drive.
        if got == UNKNOWN and not seen:
            logger.info("no HDMI connector to put %s (%s)", expected, why)
            return False
        logger.warning("HDMI output should be %s (%s, method %s) but reads %s: %s",
                       expected, why, chosen, got, _describe(seen))
        box_state.note("hdmi output: not as asked", asked=expected, got=got,
                       why=why, method=chosen)
        return False
    logger.info("HDMI output %s (%s, method %s): %s", expected, why, chosen,
                _describe(seen))
    box_state.note(f"hdmi output {expected}", why=why, method=chosen)
    return True


def sleep(why: str) -> bool:
    """Stop driving the output. True only when every screen really went
    dark — except when `hdmi_sleep` is "none", where nothing is asked to
    happen at all and the output simply stays whatever it already is (in
    practice always awake, since nothing else ever puts it to sleep either).
    A deliberate configuration choice, not a fallback: see tv_config.py's
    own note on what it trades away.
    """
    if method() == "none":
        import state as box_state
        box_state.note("hdmi output: sleep skipped", why=why, method="none")
        box_state.set_hdmi(AWAKE)
        return True
    return _apply("start", ASLEEP, why)


def wake(why: str) -> bool:
    """Drive the output again, because the box needs the screen."""
    return _apply("stop", AWAKE, why)
