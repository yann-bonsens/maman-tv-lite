<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# The installer

`scripts/install.sh` is the only way a box is installed, and the only way it is
updated: pull, then run it again. This page is everything the README leaves
out.

## What it does

Run it as a normal user, not as root. No username is hard-coded: the service
units and the polkit rule ship with placeholders that the installer fills in
with whatever account ran it. It asks for your password once at the beginning
and then runs unattended.

If it is interrupted, by a dropped connection or a power cut, just run it
again: it clears an incomplete build and starts that step over, keeping any
Zigbee network already paired.

Everything it prints is also appended to `~/maman-tv-lite-install.log`, with a
header and the arguments for each run. The interesting part is rarely the last
screenful — a transient network error, a build warning, a service that refused
to start — and over SSH the session can end before anyone has read it. Set
`MAMAN_INSTALL_LOG` to put it elsewhere.

It ends with the steps no script can perform (those needing an account, a
domain or hardware) and with the box's address.

## Every Raspberry Pi

Every model is meant to work. What differs is how the buttons get installed,
and the installer picks by itself from the image you wrote:

| Image | Zigbee2MQTT | Time on top of the rest |
|---|---|---|
| **32-bit** (`armhf`), any Pi | Node.js 20 from the unofficial ARMv6 builds, Zigbee2MQTT 2.12.0 compiled on the board | about 20 minutes on a Pi 1, much less on a newer board |
| **64-bit** (`arm64`), Pi 3, 4, 5, Zero 2 | Node.js 24 from NodeSource and a newer Zigbee2MQTT, the recipe Zigbee2MQTT's own Linux guide uses — nothing compiled natively | a few minutes |

A Pi 1 or a first Zero only runs the 32-bit image. On anything newer, the 64-bit
image is the quicker install. The 32-bit road is the one measured every day on
the box this project was built for, a Pi 1; the 64-bit one follows
Zigbee2MQTT's own Linux guide and has been installed and run on a Pi 5.

## Choosing what to install

It asks a few questions, then installs only what you asked for.

| Component | Default | What it gives you |
|---|---|---|
| `zigbee` | yes | The buttons: Zigbee2MQTT, an MQTT broker, Node.js. The slow part on a 32-bit Pi. |
| `tailscale` | yes | Administration over SSH from anywhere, across a network you do not control |
| `cloudflare` | no | The API reachable on the internet through an outbound tunnel. Needs a Cloudflare account and a domain. |
| `media` | yes | Music and photos on the television. See [media.md](media.md). |
| `share` | no | The media folder as a network share, to drop files in from a computer |

The API, the CEC control and the screen service are always installed — the
screen draws the installation screen, which is how a box with buttons is set up
without a laptop. A purely local box with buttons and no remote access is a
supported configuration, and so is a box with no buttons, driven from a phone:
without the buttons there is no installation screen, the box keeps the standard
CEC frames, and [without-buttons.md](without-buttons.md) shows how to change
them from the browser.

Answers are saved to `/etc/maman-tv-lite/install.conf`. Re-running the script
reuses them without asking again, and without undoing anything you configured
afterwards. Change them with `--reconfigure`. Turning a component off actually
stops and removes its service, so the saved answers always describe the real
state of the machine.

Every question also has a flag, for an unattended install:

```bash
./scripts/install.sh --yes --without-cloudflare
./scripts/install.sh --without-zigbee --without-tailscale
./scripts/install.sh --help
```

## The password

The API password is **asked for, never generated**: a password nobody chose is
a password nobody changes, and a generated one has to be written down before
it scrolls past.

For an unattended install, set `MAMAN_API_PASSWORD` in the environment. It is
deliberately not a command-line flag, because command lines are visible to
every user on the machine. Without it, `--yes` stops and says so rather than
inventing one.

To change it later, edit `/etc/maman-tv-lite/api.env` and restart `maman-api`.
The installer never touches an existing password: overwriting it would cut off
a phone or a script already using it.

## Updating

Pull, then run the installer again. It replaces the code, restarts the services
so they actually pick it up, and rebuilds Zigbee2MQTT if the pinned version
changed. Your password, your Zigbee network and your button bindings are left
alone: everything specific to one box lives in `/etc/maman-tv-lite/` and
`/var/lib/maman-tv-lite/`, never in the repository.

Developing on a Mac, `scripts/push-to-pi.sh` sends your committed HEAD to
`~/maman-tv-lite-src` on the box and offers to run the installer there.

## Uninstalling

```bash
./scripts/uninstall.sh            # asks whether to remove everything
./scripts/uninstall.sh --yes      # the product only, without asking
./scripts/uninstall.sh --purge --yes
```

By default it removes the services, the commands, what the installer added to
systemd, polkit and sshd, and the code — and keeps the settings
(`/etc/maman-tv-lite`, `/var/lib/maman-tv-lite`) and the compiled parts (the
Python environment, Zigbee2MQTT with its Zigbee network, Node.js). Installing
again then takes minutes and pairs nothing. `--purge` removes those too.

It only removes files that carry its own marker line, because the unit names
are common ones another project could use too. It never touches the media
folder, apt packages, Tailscale, `/etc/cloudflared` or the edits to
`config.txt` and to the journal and watchdog settings.

## Optional: what a fresh card runs and this box does not need

Raspberry Pi Imager signs the machine into Raspberry Pi Connect, and the
desktop image also starts a Bluetooth media-key proxy in the account's user
session. On a single-core Pi they cost about half a point of the core between
them, for ever, and they keep a whole `systemd --user` session alive to do it:

```bash
sudo systemctl --global disable mpris-proxy.service    # this board has no Bluetooth
rpi-connect off                                        # if you reach the box another way
sudo loginctl disable-linger "$(id -un)"               # the session existed only for those
```

Keep Raspberry Pi Connect if it is how you reach the machine — it is one of
three possible routes, alongside Tailscale and a Cloudflare tunnel, and one of
them has to work from wherever the box ends up.

## An SD card image for somebody else

Never publish an image of a card you have used: it carries your credentials,
your Zigbee network key and whatever Raspberry Pi Imager wrote to the boot
partition. [sd-card-image.md](sd-card-image.md) lists what a shareable recipe
has to avoid and how to build one.
