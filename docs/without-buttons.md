<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Without the buttons

A box installed with `--without-zigbee` (or "no" to the buttons question) is
driven from a phone: `http://<hostname>.local:8000` opens a page of big
buttons, and `/docs` has the whole API.

What such a box does not have is the [installation screen](installation-screen.md):
every question on it is answered with the two buttons, and the first thing it
does is pair them. So it never opens by itself, and `POST /mode/installation`
answers 409.

## Most televisions need nothing

A fresh box uses the most standard frames in HDMI-CEC:

| | technique | what it sends |
|---|---|---|
| switching on | `image_view_on` | Image View On (0x04) |
| switching off | `standby` | Standby (0x36), touching nothing else |
| giving the programmes back | `power_cycle` | off, then on again with the box's output already asleep — works on every set |
| how the box leaves the screen | `hdmi_sleep: blank` | puts its own output to sleep, leaves the connector alone |

If the television switches on and off from the phone page, there is nothing
else to do. Set the startup channel and volume in its Hotel Mode, and stop
reading here.

## Somebody has already measured your television

[`profiles/`](../profiles/) holds one file per set somebody ran the
installation screen on. If yours is there, on the box:

```bash
tv-profile                       # what is in force, and what is available
tv-profile use SAMSUNG_0x7590
```

Nothing is restarted: the box reads its configuration every time it is asked
to do something.

## Trying techniques from Swagger

Open `http://<hostname>.local:8000/docs`, sign in with the API password,
and stand where you can see the television. Every call below has a **Try it
out** button.

**1. See what is in force.** `GET /tv/config` returns the technique for each
direction, where it came from (`default`, `detection` or `manual`), and under
`available` every name the box knows, best first.

**2. Try switching on and off.** With the set in standby, `POST /tv/on`. Then
`POST /tv/off`. Watch the television, not the response: a television in
standby often answers nothing at all, so a `200` only means the frame was
sent. `GET /tv/status` asks the set for its power state, when it is willing to
say.

**3. If one direction does nothing, change its technique and try again:**

- `PUT /tv/config/wake`, `technique` = `text_view_on`, then `POST /tv/on`
- `PUT /tv/config/sleep`, `technique` = `power_off_function`, then `POST /tv/off`

Go down the `available` lists in order. Each change is saved at once, marked
`manual`, and kept across reboots. Two warnings, because both were learned on
real sets:

- **`standby_after_active_source` and `power_off_function_after_active_source`
  claim the HDMI input before switching off**, so the television goes dark
  showing the box and, if it comes back on its last input, comes back on the
  box. Use them only when nothing above them works.
- **`power_toggle_key` has no direction.** A set that obeys it can be sent the
  wrong way just as easily. Last resort, in both lists.

**4. Check the way back to the programmes.** `POST /mode/music` puts the box on
screen; `POST /mode/television` with `switch_off` = `false` should then leave
the set showing its own programmes. The default, `power_cycle`, does that by
switching it off and on again, which works everywhere but is not subtle.

A set that obeys Inactive Source can do it without going dark:
`PUT /tv/config/release` with `technique` = `inactive_source`, then try again
from step 4. The other names it accepts are `set_stream_path`,
`routing_change` and `tuner_keys`. `power_cycle`, the default, is the way back
if none of them works.

**5. If the screen stays lit with the box's output asleep**, try
`PUT /tv/config/hdmi-sleep` with `method` = `connector`. Read the warning in
that call's description first: on a television that unplugs its HDMI input in
standby, it can leave the box unable to reach the set until somebody switches
it on by hand.

**Start again** with `DELETE /tv/config`: the techniques go back to the
defaults, and `hdmi_sleep` is kept.

## If nothing works at all

Some televisions cut power to their CEC circuit in standby. No frame can reach
them, and only a "quick start" or "fast standby" setting in the set's own menus
can change that. The [FAQ](faq.md) has the rest.

## Once it works, share it

`POST /tv/config/television` records which set this configuration was made
for, then `tv-profile save` writes it as a profile. A pull request with that
file saves the next person with the same television the whole of this page —
see [profiles/README.md](../profiles/README.md).

## Adding the buttons later

Run the installer again with `--with-zigbee`. On the next start, a box with no
button bound opens the installation screen and pairs them.
