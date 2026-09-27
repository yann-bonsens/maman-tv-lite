<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Making an SD card image

Two different things, and mixing them up hands your passwords to a stranger.

**A backup of your own card.** Shut the Pi down, take the card out, and copy it
from another machine. Find the device first, and read the size carefully: the
next command writes nothing to the card but a wrong device here is how people
destroy the wrong disk later, when restoring.

```bash
diskutil list
```

A card in a laptop's built-in slot is often reported as *internal*, so do not
filter on external devices. Then, replacing `N`, and compressing as it goes
because `dd` copies every block including the empty ones:

```bash
diskutil unmountDisk /dev/diskN && sudo dd if=/dev/rdiskN bs=4m | gzip -c > maman-tv-lite-backup.img.gz
```

`rdiskN` is the raw device, several times faster than `diskN`, and `bs=4m`
reads four megabytes at a time instead of the default 512 bytes. On Linux the
equivalents are `lsblk`, `/dev/sdX` and `bs=4M`.

Check the archive before trusting it. A truncated backup announces itself only
on the day you need it:

```bash
gzip -t maman-tv-lite-backup.img.gz
```

Raspberry Pi Imager restores a `.img.gz` directly, so there is nothing to
decompress.

Keep it private: it contains your API password, your SSH keys, your WiFi
credentials, your Zigbee network key, and whatever Raspberry Pi Imager wrote
on the boot partition when you first flashed the card.

**An image for someone else.** Not by stripping this one. Build it from a
recipe instead, with a tool that starts from the official Raspberry Pi OS
image and adds a layer: `CustomPiOS`, a custom stage in `pi-gen`, or
`rpi-image-gen`. Nothing personal ever enters such an image, so nothing has to
be removed from it.

Stripping a used card is the wrong way round: a list of things to remove is only
as good as the last person who thought about it, and Raspberry Pi Imager leaves
secrets in places that are easy to miss.

What a recipe must therefore never create, and what to check for in the image
it produces:

| Never in the image | Why |
|---|---|
| `/boot/firmware/user-data`, `network-config`, `meta-data` | Imager's answers: password hash, WiFi key, Connect token, in the clear on a partition any computer reads |
| `/etc/maman-tv-lite/api.env` | the API password |
| `/etc/maman-tv-lite/buttons.json` | which of your buttons does what |
| `/etc/ssh/ssh_host_*` | the machine's identity. The image must regenerate them on first boot |
| `~/.ssh/authorized_keys` | keys that let you in |
| `/etc/cloudflared/*`, `/var/lib/tailscale/tailscaled.state` | account-level credentials |
| `zigbee2mqtt/data/` | your Zigbee network key and paired devices |
| `/var/lib/systemd/random-seed` | every clone would start from the same seed |
| `/etc/machine-id` | must be empty so each machine generates its own |
| `/var/log/journal/*`, shell history | your addresses and what you typed |

Building that recipe is not done yet. Until it is, do not hand anyone an image
made from a card that has been used.

