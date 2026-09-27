<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Pairing guide — Zigbee adapter and buttons

Nothing here is tied to a brand. Any adapter supported by Zigbee2MQTT works,
and any battery button that publishes an `action` field works too.

This assumes you answered yes to the Zigbee component during installation. If
you did not, add it with `./scripts/install.sh --with-zigbee` first.

## The shortest way: nothing

On a box with no button paired yet, the installation screen does all of this by
itself at startup: plug the adapter in, put the batteries in the two buttons,
and follow the television. The rest of this page is for pairing from a shell: a
third button, a different command, or a step that failed.

## From a shell

Plug the adapter into a **USB 2.0** port, put the batteries in the button, and
run:

```bash
~/maman-tv-lite/scripts/setup-zigbee.py
```

It does the whole sequence: finds the adapter, writes its path into the
Zigbee2MQTT configuration, starts the service, waits for the bridge to come
online, opens pairing mode, waits for you to press the button, records what
that button published, writes the binding, and restarts the API.

The one thing it asks of you is to press the button. If the button has never
been paired, hold it for about five seconds first, until its LED blinks fast.

Useful flags:

```bash
~/maman-tv-lite/scripts/setup-zigbee.py --all-actions      # any press works
~/maman-tv-lite/scripts/setup-zigbee.py --list-commands   # what a button can do
~/maman-tv-lite/scripts/setup-zigbee.py --command channel_up
~/maman-tv-lite/scripts/setup-zigbee.py --all-actions --command music  # music and photos
~/maman-tv-lite/scripts/setup-zigbee.py --all-actions --command tv     # back to the TV
~/maman-tv-lite/scripts/setup-zigbee.py --any-device      # bind for every button
~/maman-tv-lite/scripts/setup-zigbee.py --timeout 300     # more time to pair
```

Run it once per button. It is safe to re-run and safe to interrupt: nothing is
written before a press has actually been seen.

### Where the result goes

`/etc/maman-tv-lite/buttons.json`, outside the repository:

```json
{
  "bindings": {
    "living-room-button": { "single": "tv" }
  }
}
```

A device's own entry wins over the `"*"` catch-all, so adding a second button
cannot change what the first one does. Command names are checked against a
whitelist in `api/zigbee_bridge.py`; an unknown name is logged and ignored,
never executed.

Edit the file by hand if you prefer, then `sudo systemctl restart maman-api`.

## The long way, and what to do when a step fails

Use this when the script stops with an error, to find out which step actually
failed.

### 1. Is the adapter visible?

```bash
ls -l /dev/serial/by-id/
```

Nothing listed means the operating system does not see the adapter at all: try
another port, and prefer a short shielded extension cable. USB 3 ports and
internal hubs emit 2.4 GHz noise that interferes with Zigbee, so a few
centimetres of distance is the cheapest fix there is.

Always use the `/dev/serial/by-id/…` path rather than `/dev/ttyUSB0`, whose
number changes when another USB serial device is attached.

### 2. Does Zigbee2MQTT start?

```bash
sudo systemctl status zigbee2mqtt
sudo journalctl -u zigbee2mqtt -n 50
```

You want a line saying the Zigbee network is ready. The network key is
generated on first start.

`No such file or directory` means the port path is wrong.

`No valid USB adapter found` means Zigbee2MQTT could not work out the chipset.
That happens with common adapters, so the setup script names each one in turn
until the bridge answers, then writes the winner into the configuration. If
you already know yours, skip the search:

```bash
~/maman-tv-lite/scripts/setup-zigbee.py --adapter ember
```

Values: `ember` for EFR32 and Silicon Labs, `zstack` for Texas Instruments
CC2652 and CC1352, then `deconz`, `zboss`, `zigate`.

### 3. Does pairing mode open?

Through the web frontend, which is the more visual option: open
`http://<hostname>.local:8080` from the same network and click "Permit join".

Or over MQTT:

```bash
mosquitto_pub -t zigbee2mqtt/bridge/request/permit_join -m '{"time": 120}'
```

### 4. Did the device join?

```bash
sudo journalctl -u zigbee2mqtt -f | grep -i "interview\|joined"
```

Write down the `friendly_name` that was generated. Rename it to something
meaningful from the frontend, or in `configuration.yaml` under `devices:`.

### Pairing mode closes itself after a few minutes

Zigbee allows a pairing window of 254 seconds at most; ask for more and pairing
does not open at all. Open it again, or use `--timeout`.

### 5. What does the button publish?

```bash
mosquitto_sub -t 'zigbee2mqtt/+' -v
```

Press it and watch for a JSON payload carrying an `action` field. The value
depends on the model: `single`, `double` and `hold` are the most common, but
some buttons use `on`, `toggle` or `1_single`.

Put the pair you observed into `/etc/maman-tv-lite/buttons.json` as shown above,
then restart the API.
