<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Tailscale — the second way in

Cloudflare exposes the API on a public URL. Tailscale answers a different and
complementary need: **SSH access from anywhere**, over a private network, that
stays available even when the Cloudflare tunnel is down, the domain has
expired, or the API itself no longer starts.

Both are **outbound** connections: no router configuration, and they work
behind CGNAT.

## What the installer already did

Tailscale is one of the optional components. If you answered yes to it,
`scripts/install.sh` set up everything that does not need an account:

- The `tailscale` package from the official repository
- The `tailscaled` service, enabled at boot
- Its state appears on the TV diagnostic screen

If you answered no, add it now:

```bash
./scripts/install.sh --with-tailscale
```

It runs on ARMv6, so a first-generation Pi is fine. What remains is
authentication, which needs **your account**.

## Authenticate the machine

```bash
sudo tailscale up --ssh --accept-dns=false
```

The command prints a URL. Open it in a browser on another machine and sign in.
The Pi then joins your private network.

- The machine joins under its own hostname, the one chosen in Raspberry Pi
  Imager. Add `--hostname=<name>` only if Tailscale should show another name.
- `--ssh` enables **Tailscale SSH**: you connect without managing keys, using
  your Tailscale account for authentication. That is valuable for a fallback
  path, because a crisis is the worst moment to discover a key is missing.
  Drop this flag if you prefer plain SSH keys.
- `--accept-dns=false` matters, see just below.

## ⚠️ Do not let Tailscale take over DNS

By default Tailscale replaces the system DNS resolvers with its own (MagicDNS,
`100.100.100.100`).

That is backwards here: **the fallback mechanism becomes able to break the
main path**. If `tailscaled` dies badly (a crash, an expired key),
`/etc/resolv.conf` can stay pointed at a resolver that no longer answers, and
the Cloudflare tunnel, which needs DNS, goes down with it.

Hence `--accept-dns=false`: Tailscale stays purely additive, one more network
path, touching nothing else. You lose MagicDNS short names, which does not
matter here (the TV screen shows the address, and `<hostname>.local` covers the
local network).

On an already authenticated machine the setting can be changed without
dropping the current connection, unlike `tailscale up`:

```bash
sudo tailscale set --accept-dns=false
```

Then install the Tailscale app on the machines you will connect from, signed
into the same account.

## ⚠️ Disable key expiry — do not skip this

By default **a Tailscale node key expires after a few months**. On the day it
expires the machine drops off the private network and this fallback entry
point vanishes, with no warning, and probably on the day you need it.

In the Tailscale admin console: *Machines → the box's hostname → ⋯ menu →
**Disable key expiry***.

This is essential for a device that runs unattended at a remote site.

## Verify

```bash
tailscale status
tailscale ip -4
```

On the TV screen, the `Remote` line must go from `tailscale logged out` to
`tailscale OK`, and a connection line appears:

```
  ANYWHERE  ssh <user>@100.x.y.z
```

That is the most useful address on the whole screen: it works from anywhere,
depending on neither the local network nor the tunnel.

## The three access paths, and when each one helps

| Path | Works when | Fails if |
|---|---|---|
| `ssh <user>@192.168.1.x` | Your machine is on the same network | Partitioned networks, guest VLAN |
| `ssh <user>@100.x.y.z` (Tailscale) | **From anywhere** | Expired key, no outbound Internet |
| `https://...` (Cloudflare) | From any browser, with nothing to install | Tunnel stopped, domain expired |

The last two both depend on outbound Internet access: if the wall socket sits
behind a captive portal, neither will work. The diagnostic screen on the TV is
then the only recourse, which is why it exists.
