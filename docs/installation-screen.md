<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# The installation screen

On a box with buttons, the box is set up from the television itself: it asks
questions on the screen, and somebody watching answers with the two buttons.
No keyboard, no laptop. It does two things: **pair the buttons**, and **work
out how to drive this particular television**.

A box installed without the buttons has no installation screen; see
[without-buttons.md](without-buttons.md) instead.

## Opening it

- **By itself**, at startup, on a box with no button paired yet — a fresh box.
  It finds the Zigbee adapter and pairs the two buttons first.
- **From the API**: `POST /mode/installation`, or `maman-tv installation` on the
  box.

It shows a menu: **Set up the TV (CEC)**, **Pair the buttons**, **Quit**.

## Answering

The two buttons mean the same thing on every page:

| TV button | Music button |
|---|---|
| yes · it happened · go on · choose this line | no · nothing happened · next line |

The bottom of each page shows which button does what. A question left
unanswered three times in a row ends the procedure and gives the television its
programmes back, keeping what was already found.

Before any button is paired, the questions can be answered from a phone:
`GET /installation` shows the current one, and
`POST /installation/answer?id=<id>&token=<tv|music>` answers it.

## Pair the buttons

For each of the two roles, "TV" then "Music": hold the button for about five
seconds until its light blinks fast — or press it once if it is already
paired — and the box records it. Any Zigbee button that sends an action works.
For a third button or another command, use `scripts/setup-zigbee.py`
([pairing-guide.md](pairing-guide.md)).

## Set up the TV (CEC)

About a quarter of an hour, most of it watching the set. The box tries its
techniques one at a time and asks after each one whether something happened.

1. **Preparation.** The box reads the television's name. Turn HDMI-CEC on in
   the television's menus (it has a brand name: Anynet+, Bravia Sync,
   Simplink…) and turn "quick start" or "eco" standby off.
2. **How to switch it on.** Switch the set off and leave it off; say when it
   comes on.
3. **How to switch it off.** Put the set back on its own programmes; say when
   it goes off.
4. **How to give the screen back.** Switch the set on, on the box's input; say
   when it shows its own programmes again. If no command does it, the box tries
   switching the set off and on again, which works on every set.
5. **Summary.** What was found for each, with **save** or **start over**.

Everything is saved as it is found, so an interrupted session is not lost: the
next time the screen opens, it offers to resume.

### When a step finds nothing

| Code | Meaning | What to try |
|---|---|---|
| E1 — nothing answers | the television does not answer on HDMI-CEC at all | turn CEC on in its menus, try another HDMI socket, press a key on its own remote |
| E2 — nothing turned it on | no technique woke it | turn off "quick start" or "eco" standby, try again |
| E3 — nothing turned it off | no technique switched it off | the same as E2 |
| E5 — nothing gives the screen back | the set stays on the box's input | redo the step; otherwise use the television's own startup channel or Hotel Mode |

Each failure offers to try again or to go on without it; the box keeps the
standard techniques for anything not found.

## Sharing what you found

`tv-profile save` on the box writes the result as a profile, so the next person
with the same television can skip the quarter of an hour — see
[profiles/README.md](../profiles/README.md).
