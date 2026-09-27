<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Maman TV Lite

**A television your parent can use with one press — and that you can look
after from anywhere.**

When someone can no longer manage a remote control, the television is often
the first thing they lose: too many buttons, a wrong input, a menu that will
not go away, and the set stays dark for days until somebody visits.

Maman TV Lite is a small box behind the television that takes all of that
away. There is nothing to learn on the sofa, and nothing to fix on a Sunday
visit.

| In the living room | On the family's phone |
|---|---|
| **One big button.** Press it: the TV comes on, on her channel, at her volume. Press it again: it goes off. | **The same television, from wherever you are.** Switch it on, start the music, skip a photo — from a page that fits in a pocket. |
| **A second button for music and photos.** Her own songs play while the family's photos go by on the big screen. | **Drop photos into a folder** from a Mac or a PC, and they are on her television that evening. |
| Nothing to aim, nothing to choose, no menu, no wrong input. | A diagnostic page on the TV itself, and remote access that needs no router setup. |

The guiding rule: **all the complexity lives in the box; the interface asks
nothing of anyone.**

**New here?** [What it does](#what-it-does) ·
[What you need](#what-you-need) ·
[Choosing a television](#choosing-the-television--the-decision-that-matters-most) ·
[Installation](#installation) · [FAQ](docs/faq.md) ·
[Going further](#going-further)

## What it does

**For the person watching**

- **The TV on and off, from one button**, and it comes back on its own
  programmes — not on a black HDMI input, which is what usually happens when a
  box sits on a television that "remembers its last input". The box's HDMI
  output sleeps whenever it is not using the screen; that is the whole trick.
- **Music and photos, from a second button**: the music plays and the photos go
  by together, in random order. Like a digital photo frame, except it is the
  screen she already has, and it is bigger. See [docs/media.md](docs/media.md).

**For the family**

- **A remote control in the browser**: `http://<hostname>.local:8000` on a
  phone opens big buttons — an on/off that shows whether the set is on, the
  television, music and photos (or one music folder), and the photo and track
  controls. It is the same password-protected API as everything else.
- **From anywhere**, through Tailscale or a Cloudflare tunnel, with no port to
  open on anybody's router.
- **A shared folder** for the photos and the music, reachable from a Mac or a
  PC. Photos are prepared first with `scripts/prepare-photos.sh`, because the
  board cannot read HEIC or shrink a picture.

**For whoever installs it**

- **Set up on the television itself, with no laptop**: on a box with buttons,
  an installation screen asks questions and somebody watching the set answers
  with the two buttons.
  It works out how to drive *that* television — how to wake it, how to switch
  it off, how to give the viewer their programmes back — and pairs the buttons.
  See [docs/installation-screen.md](docs/installation-screen.md).
- **Built to be forgotten**: services restart themselves, a hardware watchdog
  reboots a hung board, and the logs survive. A diagnostic page can be drawn
  on the TV on request, and never otherwise.
- **A complete REST API**, documenting itself at `/docs`, for anyone who wants
  to wire it into something else.

Not included, on purpose: streaming and video calls. This board is a television
and a photo album, and stays that.

## What you need

| | |
|---|---|
| **A Raspberry Pi** | Any model: every one of them speaks HDMI-CEC. It was built on a first-generation Pi 1, where music and photos take about 13% of the single core. A 4 GB card is enough. |
| **A television with HDMI-CEC** | Ideally with a Hotel Mode — [the next section](#choosing-the-television--the-decision-that-matters-most) says why, and it matters more than anything else here. |
| **Buttons, optional** | A USB Zigbee adapter and one or two Zigbee buttons, any of the many models [Zigbee2MQTT](https://www.zigbee2mqtt.io/guide/adapters/) supports. No brand is named anywhere in the code. Without them the box is driven from a phone. |
| **A network** | Wired or WiFi. Remote access needs genuinely open outbound internet. |

Which Pi, which image:

| Pi | Image to write | Notes |
|---|---|---|
| 1, Zero, Zero W | Raspberry Pi OS Lite **32-bit** | Measured every day on a Pi 1. Zigbee2MQTT compiles for about 20 minutes during the install. |
| 2 | Raspberry Pi OS Lite **32-bit** | Should work, not verified. |
| 3, 4, Zero 2 W | Raspberry Pi OS Lite **64-bit** (32-bit also works) | Should work, not verified: the same 64-bit install as the Pi 5. |
| 5 | Raspberry Pi OS Lite **64-bit** | Verified. Zigbee2MQTT installs in minutes, with the recipe of its own Linux guide. |

Tried it on a model marked "not verified"? An issue saying it worked is as
welcome as one saying it did not.

## Choosing the television — the decision that matters most

This is the single most important choice in the project, and it is
counter-intuitive: **prefer a TV that is not "smart", and that has a Hotel
Mode.**

Hotel Mode — meant for hospitality — usually lets you fix a **startup
channel**, a **startup volume** and a **maximum volume**. The television then
puts *itself* on the right channel when it powers on, and the box only has to
turn it on and off, which is the one thing CEC does reliably. Nothing over CEC
can make a television choose a channel: the protocol reserves that to the set.

A smart TV makes it worse: it boots into an app launcher instead of live
television, takes far longer, and updates itself in ways that can change its
behaviour overnight.

What to look for, in order of importance:

1. **HDMI-CEC** (often sold under a brand name: Anynet+, Bravia Sync, Viera
   Link, Simplink, EasyLink…)
2. **Hotel Mode with a startup channel setting** — check the manual before
   buying; the feature is often listed but its contents vary
3. **Not a smart TV**, or at least one that boots straight to live TV
4. A built-in tuner matching your signal source (terrestrial, satellite,
   cable)

## Installation

**1. Write the card** with Raspberry Pi Imager, choosing the image from the
table above. In Imager's settings choose a hostname, an account and the
network, and enable SSH. Any hostname and any account will do — nothing in the
project assumes either, and the box is then at `<hostname>.local` on the local
network.

**2. Fetch the project and run the installer**, over SSH, as that account:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/yann-bonsens/maman-tv-lite.git ~/maman-tv-lite-src
cd ~/maman-tv-lite-src
./scripts/install.sh
```

It asks a few questions — the buttons, remote access, music and photos, the
API password — then runs unattended and ends with the address of the box:
`http://<hostname>.local:8000` for the remote control, `/docs` for the whole
API. Every question, flag and option is in [docs/installer.md](docs/installer.md).

**3. Reboot.**

**To update**, later:

```bash
cd ~/maman-tv-lite-src && git pull && ./scripts/install.sh
```

**To uninstall**, `./scripts/uninstall.sh`. By default it removes the product
and keeps the settings and the compiled parts, so installing again takes
minutes and pairs nothing; it asks whether to remove everything instead. The
media folder is never touched.

Can't find the box on the network? `scripts/find-pi.sh <hostname>` on a Mac
tries the name, then the advertisements, then a scan — and the diagnostic page
on the television always prints the real address.

## After installing

- **The buttons.** Plug the adapter in and put batteries in the buttons. On
  its next start, a box with no button paired opens the installation screen
  on the television and pairs them, then offers to measure the television.
  [docs/pairing-guide.md](docs/pairing-guide.md) covers a third button and
  what to do when a step fails.
- **The television.** Set the startup channel and volume in its Hotel Mode. If
  it does not switch on and off from the box, [docs/television.md](docs/television.md)
  has four ways to fix that, from "use somebody else's measurements" to "try
  the techniques by hand".
- **Music and photos.** Prepare the photos on a computer with
  `scripts/prepare-photos.sh`, then copy them and the music to the box — see
  [docs/media.md](docs/media.md).
- **Remote access.** [docs/tailscale.md](docs/tailscale.md) for administration,
  [docs/cloudflare-tunnel.md](docs/cloudflare-tunnel.md) to reach the API
  from the internet.

### Checking it works

From a shell on the box:

```bash
maman-tv status     # what the box is doing, in a few lines
maman-tv report     # everything needed to diagnose it, written to a file
maman-tv screen     # draw the diagnostic page on the television
```

The diagnostic page shows the IP address, the network, the services and the
techniques in force, then ends by itself after five minutes and switches the
television off. It is never drawn unasked: an elderly person should never be
shown a page of technical text she did not ask for.

## Going further

| | |
|---|---|
| [FAQ](docs/faq.md) | "Will this work with my TV?", and every problem met so far |
| [How it works](docs/architecture.md) | The architecture, the repository, the tests, the measurements |
| [The installer](docs/installer.md) | Components, flags, password, updates, images for somebody else |
| [Your television](docs/television.md) | Profiles, the installation screen, the CEC techniques |
| [Without buttons](docs/without-buttons.md) | Configuring the television from Swagger |
| [Lessons](docs/lessons.md) and [CLAUDE.md](CLAUDE.md) | Everything that was tried and did not work — it will save you an evening |

## Contributing

**The most useful thing you can send is your television.** HDMI-CEC varies so
much between models that a quarter of an hour in front of a screen is the only
way to find out how a given set wants to be driven — and somebody else's
quarter of an hour is worth sharing. `tv-profile save` on the box writes the
file; [profiles/](profiles/README.md) is where it goes. A television that
misbehaves is nearly as valuable: half of what this project knows came from
bus captures of a set doing something unreasonable.

Read [CONTRIBUTING.md](CONTRIBUTING.md) for how. Code contributions need a
short [licence agreement](CLA.md); documentation, profiles and bug reports
need nothing.

- [Open an issue](../../issues/new/choose) — including "will this work with my
  TV?"
- [Security problems](SECURITY.md) go through GitHub's private reporting, not an
  issue
- [Code of conduct](CODE_OF_CONDUCT.md)

## Licence

Copyright (C) 2026 Yann Bonsens

This program is free software: you can redistribute it and/or modify it under
the terms of the **GNU General Public License version 3**, as published by the
Free Software Foundation, or (at your option) any later version.

It is distributed in the hope that it will be useful, but **without any
warranty**, without even the implied warranty of merchantability or fitness
for a particular purpose. See [LICENSE](LICENSE) for the full text.

In plain terms: use it, modify it and redistribute it freely, including
commercially — provided any version you distribute is also under GPL-3.0, with
its source.
