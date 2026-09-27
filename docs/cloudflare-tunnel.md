<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Exposing the API on the Internet — Cloudflare Tunnel

Goal: reach the API from anywhere **without touching the router
configuration**. The Pi opens an **outbound** connection to Cloudflare, and
public traffic comes back down that already-established tunnel. No open port,
and it works even behind CGNAT.

## What the installer already did

The Cloudflare tunnel is an optional component, and it is **off by default**
because it needs a domain name. Turn it on with:

```bash
./scripts/install.sh --with-cloudflare
```

That sets up everything which does not need an account:

- `cloudflared` in `/usr/local/bin/`
- `/etc/cloudflared/` created, owned by root, mode 700
- `systemd/cloudflared.service` installed but **deliberately not enabled**: it
  cannot start before the tunnel exists

What remains needs **your own Cloudflare account and your own domain**.

## Prerequisites

1. A domain name, from any registrar
2. A Cloudflare account (the free plan is enough)
3. **The Pi's network must have genuinely open Internet access.** A daemon
   cannot log into a captive portal. The diagnostic screen on the TV
   (`maman-tv screen`, or `POST /mode/diagnostic`) gives the verdict: it must show
   `OPEN ACCESS - no captive portal`.

## 1. Point the domain at Cloudflare

In the Cloudflare dashboard: *Add a site*, enter the domain, pick the **Free**
plan. Cloudflare shows two name servers.

At your registrar, replace the domain's name servers with the two Cloudflare
gave you. **Propagation takes 1 to 24 hours.** Do not go further until
Cloudflare shows the domain as active.

## 2. Authenticate cloudflared (on the Pi, over SSH)

```bash
cloudflared tunnel login
```

The Pi is headless, so the command prints a URL. Copy it into a browser on
another machine, sign in, then pick the domain. This writes
`~/.cloudflared/cert.pem` on the Pi. That file is an **account-level key**:
treat it like a password.

## 3. Create the tunnel

```bash
cloudflared tunnel create maman-tv-lite
```

Note the **tunnel identifier** (a UUID) it prints. The tunnel credentials are
written to `~/.cloudflared/<UUID>.json`.

## 4. Create the DNS record

```bash
cloudflared tunnel route dns maman-tv-lite k7m2x9.example.com
```

⚠️ **Pick a subdomain nobody can guess.** Only the API password protects
access, so the address itself should not be found by trying obvious names. Use
something like `k7m2x9.example.com` rather than `maman-tv-lite.example.com`.

## 5. Move the credentials somewhere safe

The service runs as root with `ProtectHome=read-only`, so it cannot read a
home directory.

```bash
sudo mv ~/.cloudflared/<UUID>.json /etc/cloudflared/
sudo chown root:root /etc/cloudflared/<UUID>.json
sudo chmod 600 /etc/cloudflared/<UUID>.json
```

## 6. Write the configuration

Copy `config/cloudflared-config.yml.example` to `/etc/cloudflared/config.yml`
and replace three values: the UUID (twice) and the hostname chosen in step 4.

```bash
sudo cp config/cloudflared-config.yml.example /etc/cloudflared/config.yml
sudo nano /etc/cloudflared/config.yml
```

## 7. Enable the service

```bash
sudo systemctl enable --now cloudflared
systemctl status cloudflared
```

## 8. Verify

From a **phone on mobile data** (not on the home WiFi, or the test proves
nothing):

```
https://k7m2x9.example.com/docs
```

The browser must ask for the API username and password
(`/etc/maman-tv-lite/api.env`), then show the Swagger documentation.

Then reboot the Pi and repeat the test: the tunnel must come back on its own.

## Troubleshooting

| Symptom | What to look at |
|---|---|
| The tunnel never establishes | Many networks block outbound UDP. Uncomment `protocol: http2` in `config.yml` to force TCP 443, then restart the service. |
| Nothing happens, no clear error | `journalctl -u cloudflared -f` |
| `502 Bad Gateway` | The tunnel works but the API does not answer: `systemctl status maman-api` |
| DNS error on the Cloudflare side | The domain is probably not active yet (name server propagation). |
| Everything breaks after moving to another network | Check the TV screen: captive portal? |

## Security — what this covers

- **HTTPS** is provided by Cloudflare, so the HTTP Basic password is no longer
  sent in the clear
- **The API password is the only barrier**, so choose a long one: the
  installer asks for it and never invents one
- The address is neither guessable nor indexed

Not covered: an identity layer in front of the tunnel (Cloudflare Access can
add one), and the system endpoints are reachable through it like the rest.
`/system/shutdown` requires a physical visit to power the machine back on.
