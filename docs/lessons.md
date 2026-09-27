<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Hard-won lessons

Things that cost hours to find out, kept here so nobody has to find them
again. `CLAUDE.md` holds the longer version, with the measurements.

Possibly more valuable than the code. Each of these cost hours.

**HDMI-CEC is not a reliable standard.** On some televisions almost every
command fails *silently* unless the box is the displayed source — which is why
two of the power-off techniques claim the input first. One TV tested here
refused everything
(`Feature Abort`) until someone had physically navigated its menus after
power-on. Verify against your own set; never assume.

**Returning to the TV tuner is impossible over CEC.** The protocol reserves
that command to the television itself. On one set, Set Stream Path came back
as "unrecognised opcode" and thirteen other frames were ignored without a
word. What works instead is having no signal on the box's input when the set
wakes: it then falls back to its own programmes. That is why the box's HDMI
output sleeps whenever it is not using the screen — and why the picture is put
to sleep rather than the connector forced off, which makes the kernel forget
the television's EDID and can leave the box unable to send a single frame.

**There is no absolute volume in CEC** — only step up and step down. Hotel
Mode can fix a startup volume, which is another reason to rely on it.

**ARMv6 is not ARM.** On a Pi 1, Debian `armhf` packages assume an ARMv7 CPU
and crash with `Illegal instruction`. Test that a binary *runs*, not merely
that it installed. This does not apply to 64-bit Pi 4 or 5.

**Zigbee2MQTT without its adapter will exhaust the machine.** It restarts
endlessly, burning around 50 s of CPU per attempt. On a Pi 1, 318 consecutive
restarts held the machine at load 2 until the hardware watchdog rebooted it.
Its unit therefore only starts when a serial adapter is plugged in.

**A television can refuse to give up being the active source, permanently.**
The set used for development accepts everything for a few seconds after
power-on, then settles into a state where it answers every claim by
re-claiming the source itself, and refuses to switch off. Nothing sent over
CEC gets out of it; only its own remote does. This is the deeper reason the
project leans on Hotel Mode instead of on CEC being clever, and the reason the
"choosing the television" section above is the most important one here.

**Claiming active source makes the TV refuse the next command.** Sending
Standby shortly after claiming came back as `Feature Abort, not in correct
mode to respond`: the set was still switching input, and only finished about
two seconds later. Turning the TV off therefore waits after claiming. Turning
it on claims nothing and stays immediate.

**The box talks to the television with `cec-ctl` (from v4l-utils), not with
libCEC.** libCEC does two things by itself that this product cannot have: it
re-announces itself as the active source whenever the adapter's physical address
is lost and re-acquired, which drags the set onto the box's input on any
television that drops its HDMI hotplug when it powers on, and it can only
address a television at logical address 0, while the specification allows 0 and
14. Neither can be switched off. `cec-ctl` sends the frame it is given and
nothing else, and the address the set answers on is polled for rather than
assumed.

`cec-utils` is deliberately not installed: the two tools cannot share the
adapter, so one started by hand to debug something stops the box talking to the
television. Use `cec-ctl --monitor`, which listens and transmits nothing.

**Recording a CEC session.** Two ways, and the first is usually enough:

```bash
# What the box sent, and what the adapter made of it. One line per frame.
sudo journalctl -u maman-api --no-pager | grep "^.*cec "
```

Everything `cec-ctl` printed, frame by frame, is one request away — no restart,
and it survives a reboot:

```bash
curl -u user:password -X PUT "http://<hostname>.local:8000/logging/cec?level=DEBUG"
curl -u user:password -X PUT "http://<hostname>.local:8000/logging/cec?level=INFO"
```

Or from the box's own Swagger page, `http://<hostname>.local:8000/docs`, under
**logging**. A restart is exactly what you cannot afford at that moment: it loses
the CEC session's state, re-reads the configuration and pokes the television on
the way through, which is to say it destroys the evidence it was asked to
collect. Only the CEC transport moves — raising the whole process to DEBUG would
turn on every library in it.

For what the **television** says as well, including frames it sends on its own,
there is a recorder, installed on every box and enabled on none:

```bash
sudo systemctl start maman-cec-monitor      # writes /var/log/maman-tv-lite/cec-bus.log
sudo systemctl stop maman-cec-monitor
```

It only listens, it gives way to everything else on the core, and it caps itself
at 50 MB plus one kept generation. Leave it off the rest of the time.

**Zigbee2MQTT rewrites its own configuration file.** Its serial port ended up
recorded on two lines, which YAML reads as a single value with both halves
joined by a space. The service then reported "No such file or directory" for
a path that plainly existed, and restarted forever. The setup script now
repairs that shape instead of editing only the first line.

**`.local` names never cross a router.** mDNS uses multicast with a TTL of 1.
From another network the name will not resolve — that is protocol design, not
a fault. This is why the diagnostic screen shows the raw IP address, and the
MAC address too, which you will need if the network filters by MAC.

**A captive portal blocks every remote access path.** Neither Tailscale nor a
Cloudflare tunnel can authenticate through one. If the network imposes one,
get it lifted — or fall back to a keyboard plugged into the box.

