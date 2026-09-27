<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# The installation screen — design notes

For somebody changing the code. How to *use* the screen is in
[../installation-screen.md](../installation-screen.md).

The box has one screen — the television — and two buttons. The installation
screen is what turns those two things into a way of configuring the box with
no keyboard, no SSH session and no laptop: the box asks, a person watching the
television answers with a button.

It is a **mode**, like music and the diagnostic page. The four rules of
`modes.py` apply to it unchanged: one mode at a time, entering wakes the
output and leaving puts it back to sleep, leaving never leaves the set on the
box's input, and nothing outside `modes.py` touches the output.

It holds a menu of guided procedures. Today there are two: **CEC detection**
(how the box learns to drive the television in front of it) and **pairing the
TV/Music buttons**. A procedure added later costs a menu entry and a list of
steps, not a new mechanism — see "Adding a procedure later" below.

## The vocabulary

Two buttons, and they mean the same thing on every page of every procedure:

| token | the TV button | the Music button |
|---|---|---|
| meaning | *yes · it happened · go on · choose this line* | *no · nothing happened · next line* |

Internally these are literally the tokens `"tv"` and `"music"` — the same
names a binding in `buttons.json` resolves a press to, so a button's role
*is* the answer it gives, with no separate colour-to-meaning table to keep in
sync. **A button is named by its role, not by an arbitrary appearance.**
Pairing (below) fixes the two roles outright — "TV" and "Music" — because a
screen with no keyboard cannot ask an open "what do you call this button?"
question the way `scripts/setup-zigbee.py` can over SSH.

Every page that expects an answer shows each bound button's real configured
name next to what it means (`installation._legend()`), falling back to a
generic "YES"/"NO" label when nothing is bound to that role yet — an
HTTP-only session, or a box that has not paired anything. The legend's
left/right order is fixed by position (navigate/negative always on the left,
validate/positive always on the right) regardless of which button happens to
answer which token, so every page agrees with every other one.

There is no long-press abandon. Telling a deliberate hold from an ordinary
single press turned out to be genuinely model-dependent across Zigbee button
hardware, so it was dropped. Leaving the mode still works two other ways: the
menu's "Quit" entry, and three unanswered questions in a row (see "Abandoning"
below).

## The mode

`state.INSTALLATION`. `modes.installation()` enters it; `modes.television()`
leaves it, like any other mode. It is entered:

- from the API, `POST /mode/installation`;
- from the shell, `maman-tv installation`;
- from a button bound to `installation`, on a box whose owner asked for one —
  not the norm, and never bound by default;
- **automatically at start**, whenever the box has no working button bound at
  all (`button_bindings.has_any_button()` is false) — a fresh box, one whose
  bindings were just cleared (`DELETE /installation/buttons`), or one that
  lost its `buttons.json`. This is not a first-boot-only check: it runs on
  every start, because a box with no button is only reachable over SSH or a
  phone regardless of how many times it has booted before.

**Never on a box installed without the buttons** (`MAMAN_ZIGBEE=no` in
`/etc/maman-tv-lite/install.conf`, read by `button_bindings.buttons_installed()`).
Every question here is answered with them and the first procedure pairs them,
so on such a box the screen would wait twenty minutes for an adapter that is
not there, at every boot. It does not open at start, and `POST
/mode/installation` answers 409 and points to
[without-buttons.md](../without-buttons.md), which explains how to set the
techniques by hand. A missing `install.conf` counts as a box with buttons.

`_open_the_screen_at_startup()` in `main.py` is what makes this decision.
Deliberately **not** keyed to whether the CEC configuration has ever been
confirmed: a television that already answers the two default frames
(`image_view_on`/`standby`) is a working box, and forcing this screen open on
every boot just because nobody ran the CEC detection by hand would turn a
one-time step into a permanent nag. A missing button, by contrast, genuinely
cannot be worked around from the television at all.

### The procedure never holds the mode lock

A procedure can run for a quarter of an hour. It runs in its own thread and
takes `modes._lock` for each individual action — one CEC frame, one output
transition — never across a wait. A step that held the lock while counting to
thirty would put every button press behind it, which is the fault of
2026-09-22 in another shape: two accidental presses occupied the box for
twenty-six minutes because one press started something long and the second
queued behind it.

Leaving the mode interrupts whichever procedure is running: `modes.py` calls
`installation.channel.abandon()`, the procedure's next `ask()` (or the next
check between steps) raises `Abandoned`, and the thread unwinds without
writing anything more.

## The question/answer channel

`api/answer_channel.py`. A procedure is a list of steps; a step asks a
question and waits for an answer.

A **question** carries:

| | |
|---|---|
| `id` | a number that only ever increases within the channel's lifetime |
| `body` | the text to draw, already laid out |
| `accepts` | which tokens are answers, and what each one means |
| `seconds` | how long to wait, or `None` for no deadline |

Rules, each one there because of something already measured on this box:

- **An answer belongs to the question that was showing when it arrived.** A
  press carries the `id` the box last displayed; a press whose `id` is not
  the current one is dropped. One physical press can reach the bridge twice,
  well under a second apart, with two different link-quality readings — two
  genuine radio receptions — and the duplicate must never answer the
  question that has moved on in the meantime. A time window is the weaker
  version of this rule; the id is exact.
- **A press that arrives after a question has timed out or been abandoned is
  dropped**, for the same reason and by the same mechanism.
- **While the box is in the installation mode, the Zigbee bridge routes a
  `tv`/`music` press to the channel** instead of through its normal command
  table (`zigbee_bridge._handle_action`). Any other bound command — a third
  button somebody wired to `diagnostic`, say — is logged and ignored while
  installation is running: the two everyday buttons have no other meaning
  until the mode ends.
- **Three questions in a row with no answer abandon the procedure**
  (`MAX_MISSES` in `procedure_support.py`), give the television back its
  programmes, and keep whatever was already measured. A button with a flat
  battery must not leave the box stuck on a page forever. This rule does not
  apply inside a technique search (see below) — going through a whole table
  with no answer is that search's normal, expected shape, not a sign that
  nobody is in the room.

The channel is also what the API exposes, so a procedure can be driven from a
phone whenever a button cannot answer it yet — most notably the very first
button a box ever pairs, since by definition nothing can press an answer to
that question:

- `GET /installation` — the current question (id, body, accepts, seconds
  remaining), or `{"question": null}` when nothing is running.
- `POST /installation/answer?id=<id>&token=<tv|music>` — answers it. An id
  that is not the current question's is dropped, not an error — the same
  mechanism that drops a late or duplicated button press.

The buttons and the HTTP route are two ways into the same channel, never two
separate implementations.

## The pages

Real graphics, drawn by `api/page_render.py` (Pillow) as a 1920×1080 PNG and
shown by `fbi` — the same tool already used for the photo slideshow, and cheap
for the same reason: a page changes every 30 seconds to several minutes, so a
few hundred milliseconds of rendering is negligible. `scripts/screen.sh`'s
`page` request kills any running `fbi` and starts a fresh one on the new
image; `screen.page()` (`api/screen.py`) is how the API asks for that.

Layout, paid for by a real television:

- **A 5% margin on every side.** Old sets crop the edges (overscan), and the
  line that gets lost first is the one that says what to do.
- **The largest font that still fits.** `_fit_body()` tries several sizes,
  largest first, and keeps the first one whose wrapped text fits above the
  legend — a page's body is prose built by the procedure, not a fixed
  layout, so a longer page (the preparation step, a failure page with three
  bullet points) shrinks to fit instead of silently losing its last lines.
- **A permanent header**, "SETUP IN PROGRESS", on every page.
- **A step counter**, "STEP n OF total", on every page that carries one.
- **The bound buttons' names, with what each one does**, at the bottom of
  any page that expects an answer — see "The vocabulary" above.

Fonts come from the system's DejaVu package
(`/usr/share/fonts/truetype/dejavu/`), never from Pillow's own scalable
default (whose `size=` argument needs Pillow ≥10.1), with
`ImageFont.load_default()` as a last resort so rendering never crashes even
off-hardware.

`page_render.save()` writes the PNG with the same write/fsync/rename
discipline every other persisted file in this project uses, so the screen
service can never open a half-drawn image.

## The CEC detection procedure

`api/cec_detection.py`. Four numbered steps plus a preparation page and a
summary, driven entirely by an `Environment` protocol — the module never
imports `cec_controller`, `hdmi_output` or `tv_config` directly, which is what
lets it be tested against a small model television (a power state, a
displayed input, a policy consuming frames) instead of real hardware.
`modes.py`'s `_InstallationHardware` is the only place that builds the real
one.

One principle carries the whole procedure: **during a search, the box sends
only what it is measuring.** Everything the product layers on top afterwards
— claiming the input, sleeping the output — can only improve on what was
measured here, which is why there is no confirmation round after each answer.

### Preparation

Reads the television's identity (EDID manufacturer/model, plus its CEC
version if the bus answers) and shows it, then asks the person to turn on
HDMI-CEC and turn off quick-start/eco mode in the TV's own menu — the single
most common cause of failure on hardware nobody tested against, ahead of CEC
being off at all: a set with quick-start on stops answering the bus entirely
once it is off, so no wake technique can ever work.

The inventory runs again after the press — this is the **E1** gate. Nothing
answering at all offers "try again" (enable CEC under whatever name the
manufacturer gives it, try another HDMI socket, press a key on the TV's own
remote) or "give up", which abandons the procedure with nothing recorded.

### Step 1 of 4 — how to wake the set

A ready page ("turn the TV off, then leave it off"), then a search: the box
sends each technique from `cec_controller.WAKE_TECHNIQUES`, in order, one
every `detection_step_seconds` (30s by default), asking "did it turn on?"
after each one. Each question shows a running checklist of what has been
tried so far and how it went, so a multi-technique search is never a single
static page for however long it takes.

The output stays awake throughout, and nothing claims the input during this
step: the only frame on the bus is the one being measured.

No technique confirmed through the whole table → **E2**, offering "try again"
(check quick-start/eco, the remote's battery, the distance to the box) or "go
on without it".

### Step 2 of 4 — how to switch the set off

Same shape, against `cec_controller.SLEEP_TECHNIQUES`. The ready page asks the
person to switch back to their own programmes first: the set must be measured
from its own tuner, on its own input, exactly the state an everyday press
sends this frame in. Measuring from the box's own input instead is how a
technique that works from there and does nothing from the tuner would pass —
on the television used for development, nearly every CEC command failed
silently unless the box was the displayed source.

No technique confirmed → **E3**, same offer as E2.

### Step 3 of 4 — giving the screen back

**The ready page asks the person to switch the set back on and put it on the
box's HDMI input.** Step 2 has just finished measuring how to switch it off, so
the set is off — and everything in this step needs it on, on the box's input,
with the person able to read these pages. The page said none of that while the
search page that follows assumed all of it: a person works it out, but a
procedure that depends on being guessed fails differently for each installer.

Up to two attempts. The first searches `cec_controller.RELEASE_TECHNIQUES`
(`inactive_source`, `set_stream_path`, `routing_change`, `tuner_keys`) with
the output kept **awake** the whole time — a candidate here is a CEC command
that asks the set to switch away, and a set that merely reacted to losing the
signal (not to the command) would credit a frame that did nothing, the exact
false positive a search must not produce.

If nothing in that table is confirmed, a second attempt (**step 3 bis**) puts
the box's own output fully asleep instead — `power_cycle`, the one candidate
whose mechanism is the output going to sleep, not a CEC frame at all. The
output stays asleep for the whole question that follows (the person is
watching their television, not the box) and is only reclaimed once an answer
or a timeout ends the wait. This attempt gets a longer window
(`detection_cycle_seconds`, 90s by default): a set can take ten seconds to go
off and twenty to come back.

Neither attempt confirmed anything → **E5**. If both wake and sleep already
succeeded earlier, the message offers to redo this step rather than blaming
the television outright: candidates 5 and 5 bis both re-use the wake and the
sleep techniques already found, so a failure here can mean one of those two
did not actually hold rather than "this set always keeps the box's input".
Otherwise the page can only point at the television's own menu — no setting
on the box can substitute for a hotel mode or a startup channel.

### The summary

Shows what was found for each direction (or "(not found)"), with "save" or
"start over" — choosing "start over" re-runs the whole procedure from the
preparation page. Accepting writes `detection_complete: true` in `tv.json`;
nothing before this point does, so an interrupted session cannot be mistaken
for a finished one (see "What is written" below).

## The button-pairing procedure

`api/button_pairing.py`. Pairs exactly two fixed roles, `"tv"` and `"music"`,
named "TV" and "Music" outright — the deliberately narrower thing a
keyboard-less screen can do, next to `scripts/setup-zigbee.py`'s full
generality (arbitrary roles, typed names, a third button bound to
`diagnostic`) which remains the tool for anything past those two.

Reached from the menu ("Pair the buttons") whenever a button already exists to
navigate with. Per role: a ready page ("hold the button for about 5 seconds
until its light blinks fast — already paired? press it once"), then Zigbee2MQTT's
pairing mode is opened and the box waits for one press. On success the device
is renamed to the role's label in Zigbee2MQTT and the binding is written; on
timeout, "try again" or "go on without it" (skipping that role without
blocking the other one). `write_binding()` (`button_bindings.set_binding()`)
also removes that role from any other device first, so re-pairing a role
never leaves two buttons claiming it. A summary page ends the run with "save"
or "start over".

### Pairing with no button at all

`run_bootstrap()` — a second entry point, used automatically (never from the
menu, since a box in this state cannot navigate one) whenever
`button_bindings.has_any_button()` is false. No ready gate, no retry
question, no summary question: there is nothing that could answer one, so
every page is shown with `env.ask(body, {}, seconds=0)` — an empty `accepts`
publishes the page through the same channel every other page uses and returns
at once. It loops silently on a role with no press, checking only whether the
mode was left, since giving up would leave the box with no working button at
all — the one outcome this path exists to avoid.

`run_bootstrap()` also finds the Zigbee adapter and brings Zigbee2MQTT up by
itself if it is not already running (`zigbee_pairing.list_adapters()` /
`bring_bridge_up()`), trying each known chipset in turn the same way
`scripts/setup-zigbee.py` does — so a fresh box needs no SSH session at all,
only a Zigbee adapter and two buttons plugged in. It is bounded by one
generous overall deadline, `GIVE_UP_SECONDS` (20 minutes): past it, the
bridge is stopped (`_give_up()`) and a page explains that Zigbee was turned
off and the box needs a restart, or the installation screen reopened, to try
again — the same crash-loop CLAUDE.md measured at ~50s of CPU per attempt,
hundreds of attempts, two hardware-watchdog reboots is what this deadline
exists to avoid on an adapter that is missing or unrecognisable.

`installation.start()` calls `run_bootstrap()` before anything else — before
the interrupted-CEC-session check, before the menu — precisely because a box
with no button cannot be shown either of those. Falling through afterward
(bootstrap finished, or a button was already present) reaches the ordinary
flow unchanged.

## Abandoning

Three ways a procedure stops without finishing:

- the menu's "Quit" entry (once a button exists to reach it);
- three unanswered questions in a row, at any of a procedure's real decision
  points (not inside a technique search — see "The question/answer channel");
- the mode being left from outside (another mode entered, or the API/a button
  asked for `television` directly) — `modes._leave_installation_if_running()`
  calls `channel.abandon()` before the new mode takes over.

All three raise `procedure_support.Abandoned` inside the procedure thread,
caught by `installation.start()`'s own outer handler. Nothing further is
written in any of the three cases; whatever a procedure had already saved
progressively stays saved (see below).

## What is written, and when

`tv.json` (`api/tv_config.py`), progressively as each step of CEC detection
completes — an interrupted session is not wasted:

| field | |
|---|---|
| `wake` | technique + `source: "detection"` |
| `sleep` | technique + `source` |
| `release` | technique + `source` — `power_cycle` is the default and the one that always works |
| `claims_input` | not written directly — derived by `tv_config.load()` from whether the chosen `sleep` technique's name claims the input first |
| `television` | manufacturer, product, name, CEC version |
| `detection_complete` | **false until the summary is accepted** |
| `detection_step_seconds` / `detection_cycle_seconds` | defaults 30 / 90, read back and reused by the next run |

`buttons.json` (`api/button_bindings.py`), written once per successfully
paired role, immediately — `reload_bindings()` at the end of a run picks it
up in the running process with no restart needed, since the API process now
writes this file directly (see `scripts/install.sh`'s `2775` setgid
directory and `ReadWritePaths=` for how that is made possible without
running the API as root).

### Interruption and resuming — CEC detection

The mode itself lives in memory and does not survive a reboot, but what was
measured does, because it is written as it is found. On the next start of the
installation screen, a box whose `tv.json` says `detection_complete: false`
(and whose wake or sleep technique came from a detection that never finished)
is offered, ahead of the ordinary menu:

> A TV setup was interrupted.
> Answer below: resume, or start over.

Both choices run the CEC detection procedure from its first step — there is
no step-level resume. Every step re-measures its technique from a live human
regardless of what a previous run found, so nothing already-correct is lost
by re-asking, only time.

## Adding a procedure later

A procedure is a name in `installation.PROCEDURES`, a `Hardware`-shaped
protocol describing exactly what it needs from the real world, and a `run(env)`
function built the same way as the two above: pure logic over that protocol,
tested against a fake environment, never touching CEC/output/Zigbee directly.
`modes.py` is the only place that wraps the real hardware and wires a new
entry into `installation.start()`'s dispatch.

## Testing

- **A procedure's own logic**, against a fake `Environment` — for CEC
  detection, a small model television (a power state, a displayed input, a
  policy consuming frames) rather than mocked function calls, so a test reads
  "after this step the set is on and showing the tuner" instead of "these
  calls happened in this order". Every branch, every failure page, every
  abandon path.
- **The answer channel**, on its own: the duplicate press, the late press, a
  stale id, the timeout, the three-unanswered-questions abandon.
- **The pages, actually rendered** (`api/tests/test_page_render.py`): no
  crash on a long body or a long TV name, the checklist, the legend order and
  colouring by position.
- **The pages, actually drawn on a console** (`api/tests/test_screen_drawn.py`),
  the way the diagnostic page is already tested: run the real `screen.sh`
  against stubbed binaries and read what landed.

## Out of scope

- Volume. CEC has no absolute volume, only step up and step down, and that is
  a separate design.
- Choosing which HDMI input the box uses. It is read, not chosen.
