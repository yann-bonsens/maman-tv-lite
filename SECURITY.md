<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Security

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability
reporting (the *Security* tab → *Report a vulnerability*), which reaches the
maintainer directly.

Expect an acknowledgement within a week. This is a spare-time project with one
maintainer, so please be patient — and say if you intend to disclose publicly,
so the fix and the disclosure can be coordinated.

## What this box is, in security terms

A Raspberry Pi in somebody's living room that can switch a television on and off,
play their music and photographs, and reboot itself. It holds no personal data
beyond the media files its owner puts there. But two of its optional components
give it a route in from the internet, and one endpoint reboots the machine — so
it is worth being precise about what protects what.

| | |
|---|---|
| the REST API | HTTP Basic, one password, applied as global middleware to every route |
| that password | chosen by the installer's operator; **never generated** — a box whose password nobody chose is a box whose password nobody changes |
| transport on the LAN | **plain HTTP.** The API listens on `0.0.0.0:8000` with no TLS |
| transport from outside | a Cloudflare tunnel, which terminates TLS at Cloudflare's edge |
| SSH | password authentication is **deliberately left enabled** as a local fallback; `PermitRootLogin no` |
| reboot and shutdown | logind through a polkit rule granting exactly two actions to one account |
| the HDMI sleep units | the same rule grants exactly `start` and `stop` on two named units, nothing else |
| button bindings | resolved against a whitelist, so a binding file can never name arbitrary code |
| the media folder | optionally shared over SMB, password-protected, and by default only on the local network |

## Known limitations, accepted on purpose

- **No TLS on the local network.** Anyone who can see port 8000 can see the
  password. If that matters on your network, do not enable the tunnel, put the
  box on its own VLAN, or front it with a reverse proxy that terminates TLS.
- **SSH accepts passwords.** A deliberate safety net for a machine nobody can
  walk up to. It is only reachable from the local network: Tailscale intercepts
  port 22 before sshd, and the tunnel routes the API only. Disable it if you do
  not need it.
- **The failure counter can be evicted.** Failed attempts are counted per caller,
  deliberately — a global counter would let anyone lock the owner out of their
  own box. At most 1024 callers are tracked, and flooding distinct keys evicts
  entries, which resets a lockout. Spoofing the source address is the hard part;
  through the tunnel, Cloudflare's edge sets the header the box trusts, so it
  cannot be forged there.
- **`maman-screen` runs as root.** It owns a virtual console and the framebuffer,
  which are root's. It is sandboxed (`ProtectSystem=full`, `ProtectHome=read-only`,
  `NoNewPrivileges`) but it is root.
- **No memory limit.** On a board with 427 MB, a leak takes the product down
  rather than the service.
- **An image of a working card carries secrets.** docs/sd-card-image.md lists what a
  shareable recipe must avoid: Raspberry Pi Imager writes the account password
  hash, the WiFi key and a Connect account token in the clear onto a partition
  any computer can read. Never publish an image made from a box you have used.

## Hardening worth doing

- Set a real password. `--yes` without one refuses to install.
- Leave the Cloudflare tunnel off unless you need the API from outside.
- `sudo systemctl disable --now maman-cec-monitor` — it is off by default; keep it
  off except for a measurement session.
- Keep `/etc/maman-tv-lite/api.env` root-owned and mode 600. The installer does.
