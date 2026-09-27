<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Television profiles

A profile is one television's answers, measured on that television.

HDMI-CEC is not a reliable standard. Which frame wakes a set, which one
switches it off, and whether anything at all persuades it to give the viewer
their programmes back — all three vary by manufacturer, by model, and
sometimes by firmware. There is no way to know but to try, with somebody
standing in front of the screen saying what happened. The box does that in its
[installation screen](../docs/installation-screen.md), and it takes about a
quarter of an hour.

A profile is the result of that quarter of an hour, so that the next person
with the same television does not have to spend it.

## Using one

On the box:

```bash
tv-profile              # what is in force, and what is available
tv-profile use SAMSUNG_0x7590
```

That copies the file to `/var/lib/maman-tv-lite/tv.json`, keeping the box's
own copy timestamped first. **Nothing is restarted**: the box opens that file
on every button press, so a profile applies to the next one.

## Adding one

Run the installation screen's CEC procedure on the box, then:

```bash
tv-profile save
```

The name comes from the television itself — `NAME_PRODUCT`, as the set reports
itself in its EDID — so `SAMSUNG_0x7590.json`. That is the point of the
convention: a profile has to say which set it was measured on, and the set is
the only thing that knows.

It lands in `profiles/` on the box; copy it into a clone to send it on.
Pull requests with new profiles are welcome. What makes one worth having is
the `source` field on each technique: `detection` means the box measured it
with a human confirming each step, `manual` means somebody set it by hand.
Both are honest; they are not the same thing.

## What is in one

| | |
|---|---|
| `wake` | the frame that turns this set on |
| `sleep` | the frame that turns it off |
| `release` | how to give the viewer their programmes back. `power_cycle` — off and on again with the box's output already asleep — is the fallback that works on every television, and what a set that answers none of the CEC release frames ends up with |
| `hdmi_sleep` | `blank` puts the box's output to sleep and leaves the connector alone; `connector` forces it off, which makes the kernel forget the set's EDID. `blank` unless a set ignores it |
| `claims_input` | true when the chosen sleep technique claims the HDMI input before switching off. Such a set goes dark showing the box |
| `television` | which set this was measured on |
| `detection_step_seconds` | how long to wait for one frame to take effect, during a detection. Raise it for a television that is slow to react |
| `detection_cycle_seconds` | the same, for a candidate that switches the set off and on |

## A note on the two here

`SAMSUNG_0x7590` was measured by the installation screen, on the set the box
was developed against.

`HIGH-ONE_HI3231HD-EL` was established by hand on site, and its identity is
the model off the back of the television rather than what it reports over
HDMI. Running `tv-profile save` on a box plugged into one would
produce the properly named version.
