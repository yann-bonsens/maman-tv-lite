<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# FAQ

If your problem is not here, [open an issue](../../issues/new/choose) — a
television that behaves oddly is useful information, not a nuisance.

## Choosing

### Will this work with my television?

Very likely. Out of the box the box uses the two most standard frames in
HDMI-CEC, which most sets obey, so for most people there is nothing to do. If
yours does not switch on or off, [television.md](television.md) has the ways to
fix it: somebody else's measurements, the installation screen, or a few calls
from the browser.

What decides whether the result is *good* is whether your television comes
back on the right channel when it is switched on. A set with a Hotel Mode or a
startup channel does that by itself; nothing over CEC can choose a channel for
it. See the README's section on choosing a television.

### Which Raspberry Pi?

Any. It runs comfortably on a first-generation Pi 1 — music and photos take
about 13% of its single core — and on everything newer. The README's table
says which image to write for each model.

## Installing and updating

### The installer asks me a lot of questions. Which do I need?

The API, the CEC control and the screen are always installed. Zigbee (the
buttons), Tailscale, Cloudflare, the music and photos and the file share are
optional, and can be added later by running the installer again.
[installer.md](installer.md) describes each one.

### How do I update?

Pull, then run the installer again. It keeps your answers, your password, your
Zigbee network and your button bindings, and replaces the code.

### How do I remove it?

`./scripts/uninstall.sh`. By default it keeps the settings and the compiled
parts, so installing again is quick; it asks whether to remove everything
instead. The media folder is never touched.

## The buttons

### Zigbee2MQTT does not start

It only starts when a USB adapter is plugged in. Plug it in; the installation
screen, or `scripts/setup-zigbee.py`, does the rest.

### Zigbee2MQTT says "No valid USB adapter found"

Some adapters are not recognised automatically. `scripts/setup-zigbee.py` tries
each chipset in turn (`ember`, `zstack`, `deconz`, `zboss`, `zigate`), and
`--adapter ember` names one straight away.

### Pairing fails at "opening pairing mode"

Usually the board is busy, not misconfigured: on a single-core Pi the adapter
gives up when it is not answered in time. **Pair when the machine is idle** — no
restarts, file copies or extra SSH sessions meanwhile — and simply try again.

### A button press does nothing

First the obvious: the batteries, and whether the button's LED blinks. Then
watch what arrives on the box:

```bash
mosquitto_sub -v -t 'zigbee2mqtt/#'
```

Nothing there: the button is not reaching the adapter (range, batteries,
pairing). The press appears but nothing happens: its action is not bound in
`/etc/maman-tv-lite/buttons.json` — run `scripts/setup-zigbee.py` again.

## The television

### The television comes back on the box's input instead of its programmes

Change how the box gives the screen back: the installation screen measures it,
or set it by hand with `PUT /tv/config/release` — see
[television.md](television.md). The default, a power cycle, works on every set.

### Nothing I send has any effect

Check whether the box can talk to the set at all:

```bash
maman-tv report
```

Under "CEC adapter", a physical address of `f.f.f.f` means the television has
taken its HDMI link away, which some sets do in standby. The box tries to bring
it back by itself; if the journal says `cec link still down`, switch the set on
by hand once. Some televisions cut power to CEC entirely in standby: a "quick
start" or "fast standby" setting in their menus is then the only fix.

### A press takes a second or two

Expected: a frame takes about half a second on the bus, and reading the set's
state about a second.

### How do I record what happens on the CEC bus?

For more detail in the journal, with no restart:

```bash
curl -u user:password -X PUT "http://<hostname>.local:8000/logging/cec?level=DEBUG"
```

For what the television says as well, start the recorder installed on every
box: `sudo systemctl start maman-cec-monitor`. It only listens, and caps its
own size.

## Access and networking

### `<hostname>.local` does not resolve

From another network it never will: a `.local` name does not cross a router.
Use the IP address, or Tailscale. On the same network, check that
`avahi-daemon` is running; `scripts/find-pi.sh` looks for the box several ways,
and the diagnostic page on the television always prints its address.

### Remote access does not work at all

If the site has a captive portal, neither Tailscale nor a Cloudflare tunnel can
get through it. The diagnostic page on the television says so.

### Is the API safe to expose?

It is HTTP Basic over plain HTTP, so on your local network anyone who can see
the traffic can see the password. Through the Cloudflare tunnel it is HTTPS.
Repeated wrong passwords slow a caller down and then lock it out. Read
[SECURITY.md](../SECURITY.md) before opening it up.

## Music and photos

### The music plays but there is no sound

The board may be sending it to the headphone jack. Find the HDMI device's name
with `aplay -L` and set `MAMAN_AUDIO_DEVICE` in `/etc/maman-tv-lite/api.env`.

### The first seconds of a track are missing

A television takes a moment to lock onto a new HDMI audio stream. Known, and
only the first track of a session is affected.

### My photos do not appear, or the slideshow is slow

Prepare them on a computer first with `scripts/prepare-photos.sh`: the board
cannot read HEIC or turn a picture upright. See [media.md](media.md).

## Contributing

### What makes a good bug report?

The television's make and model, and what it did versus what you expected. For
anything CEC, the output of `maman-tv report` and a few lines of
`journalctl -u maman-api | grep "cec "`.

### I measured my television. Where do I put the answers?

`tv-profile save` on the box, then a pull request adding the file to
`profiles/` — see [profiles/README.md](../profiles/README.md). It saves the
next person with the same set a quarter of an hour.

### Do I have to sign anything?

For code, a short [contributor licence agreement](../CLA.md). Documentation,
profiles and bug reports need nothing. See [CONTRIBUTING.md](../CONTRIBUTING.md).
