<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Making your television obey

No two televisions agree on how to be switched on or off over HDMI-CEC. The box
ships with the two most standard frames, and never changes them by itself: what
it uses is decided once, by somebody watching the screen, and written to
`/var/lib/maman-tv-lite/tv.json`.

There are four ways to get there, from least to most effort.

## 1. Nothing at all

A fresh box wakes the set with Image View On and switches it off with plain
Standby. Neither touches which input the television displays. Most sets obey
both: if yours switches on and off, there is nothing to do.

## 2. Somebody else's measurements

[`profiles/`](../profiles/) holds one file per television somebody has already
measured. On the box:

```bash
tv-profile                      # what is in force, and what is available
tv-profile use SAMSUNG_0x7590
```

A profile is a copy of the box's own `tv.json` and nothing more. Nothing is
restarted: the box reads that file every time it is asked to do something.

## 3. The installation screen, with the buttons

The box asks questions on the television and somebody watching the set answers
with the two buttons. About a quarter of an hour, most of it spent watching and
doing nothing, and it measures three things on your actual set: how to wake it,
how to switch it off, and how to give the viewer their programmes back.

A box with buttons and none paired opens it by itself at startup, to pair them.
Otherwise ask for it:

```bash
curl -u user:password -X POST http://<hostname>.local:8000/mode/installation
```

[installation-screen.md](installation-screen.md) has every question it asks and
every way it can fail. A box installed without the buttons has no installation
screen, since it is answered with them.

## 4. By hand, from the browser

[without-buttons.md](without-buttons.md) walks through it in Swagger, at
`http://<hostname>.local:8000/docs`, and it works the same on a box that has
buttons. In short:

```bash
curl -u user:password http://<hostname>.local:8000/tv/config
curl -u user:password -X PUT \
     "http://<hostname>.local:8000/tv/config/wake?technique=text_view_on"
```

The box never changes a technique by itself. Test it; do not assume.

## The techniques

| Turning on | Turning off |
|---|---|
| Image View On (0x04) — the default | Standby, touching nothing else — the default |
| Text View On (0x0D) | User Control "Power Off Function" (0x6C) |
| User Control "Power On Function" (0x6D) | Standby, after claiming the input |
| User Control "Power" (0x40) | "Power Off Function", after claiming the input |
| the active-source announcement alone | User Control "Power Toggle" (0x6B) |
| User Control "Power Toggle" (0x6B) | |

Both lists are ordered best-first. For turning off, the techniques that claim
the HDMI input come last on purpose: a set switched off while showing the box
comes back on the box.

`GET /tv/config` says which one is in force and where it came from — `default`
for a box nobody has configured, `detection` for what the installation screen
measured, `manual` for a technique somebody chose by hand. Nothing else writes
it: no press, no search, no accident.

A third technique sits beside those two, **how to give the screen back**. A
television that obeys Inactive Source simply switches away when the music
stops; one that obeys none of the frames falls back to `power_cycle`,
switching off and back on with the box's output already asleep, which is the
one method that works everywhere. It is set the same way, with `PUT /tv/config/release`.

The same file holds `hdmi_sleep`, which is how the box stops driving the
screen: `blank` by default, which leaves the connector alone, or `connector` for
a television that ignores blanking — at the price of the kernel forgetting the
set's EDID, which on some televisions leaves the box unable to send it anything
until it is switched on by hand.

## If nothing works at all

Suspect that the television cuts power to its CEC circuit while in standby.
Some do. Then no frame can reach it, no software can fix it, and only a "quick
start" or "fast standby" setting in the set's own menus — if it has one — will
change that.

## Share what you found

`tv-profile save` on the box writes your television's file, and a pull request
puts it in [`profiles/`](../profiles/README.md) for the next person with the
same set.
