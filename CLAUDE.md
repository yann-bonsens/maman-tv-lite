<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Working on this project with Claude Code

Context for an AI assistant picking up this codebase. If you are a human, the
[README](README.md) is the better starting point — though the "Hard-won
lessons" section below is worth your time either way.

## What this is

A Raspberry Pi that lets an elderly person operate a television through a few
large wireless buttons. Press one: the TV turns on, on the right channel, at
the right volume. Press the other: it turns off. No remote, no menus, no
choices to make.

The guiding principle: **complexity belongs in the box, never in the
interface.** Every feature must survive the question "can someone who cannot
use a remote control still use this?"

## Conventions

- **Everything is written in English** — code, comments, documentation, commit
  messages. No exceptions.
- **Comments explain *why*, not *what*.** The codebase deliberately carries
  long comments where a decision would otherwise look arbitrary. Most of them
  record something that was measured and cost hours to find. Do not strip them
  as noise.
- Commit messages: one imperative line describing the intent, no body unless
  it genuinely adds something.
- Tests live in `api/tests/`, run with `pytest`. They are regression tests
  first: most exist because something actually broke.

  **The suite has two layers, and `pytest` runs only the fast one.** The unit
  layer is about 680 tests in six seconds. The integration layer — marked
  `integration`, excluded by `addopts` — executes real binaries and costs five
  times as much for a twentieth of the tests, nearly all of it process startup.
  Run it with `pytest -m integration` before committing anything that touches the
  CEC transport or the screen; CI runs both layers as separate steps, because a
  test nobody runs is worse than no test, and a default run says out loud what it
  skipped.

  `test_screen_drawn.py` executes the real `screen.sh` against stub binaries on
  PATH. `test_television_simulation.py` runs the real transport, the real mode
  machine and the real detection procedure against a fake `cec-ctl` and
  `television_model.py`, a model of one measured set — it is where a fault in the
  frames, the argv, the addressing, the acknowledgements or the link shows up,
  because everything else tests those one function at a time. The model encodes
  only what was captured, marks what was never isolated, and models the
  unisolated as doing nothing: a model that guesses in the product's favour turns
  a real fault into a passing test.

  `test_cec_output_parsing.py` is in the fast layer and holds verbatim output
  from a real adapter and a real television, asserting what the box concludes
  from it — the parsers there have twice been correct about a format nothing
  produces.

  **A test that passes when the thing it names is removed is not a test.** Two
  in that file asserted less than the test above them and survived deleting the
  guard they were written for; they were replaced by one that fails. Check a new
  test by breaking the code on purpose before believing it.
- **No code assumes a specific brand or a specific account.** The systemd units
  and the polkit rule carry `__MAMAN_USER__` and `__MAMAN_HOME__` placeholders
  that `scripts/install.sh` substitutes at install time. Do not replace them
  with a real username. No dongle, button or television model is assumed
  anywhere in the code.

  **`profiles/` is the deliberate exception, and it is data rather than an
  assumption.** Each file there is one television's measured answers, named
  after the set it was measured on — and the point of keeping them in the
  repository is that CEC varies so much between models that a quarter of an
  hour in front of a screen is the only way to find them. Somebody else's
  quarter of an hour is worth sharing. Nothing reads that directory at
  runtime; `scripts/tv-profile` copies one file onto a box when a human asks.
  See profiles/README.md.
- `docs/journal.md` is the private working log. It is gitignored and must stay
  out of the repository.
- **Declare every component answer at the top, and never write `${!x}`.** The
  installer runs under `set -euo pipefail`, where an indirect expansion on a
  variable that was never set aborts with "unbound variable". The media
  component was added to the questions without being added to the block that
  initialises the answers, and the installer then died on **every** run, clean
  machines included, at the first question it asked about it. Two tests in
  `api/tests/test_install_script.py` pin both halves, and both are needed: one
  checks that no `$MAMAN_X` is read before it is set, the other that every name
  passed to `ask_yes_no` or `enabled` is declared above the questions — those
  are read by *name*, through an indirect expansion, so they never appear as
  `$MAMAN_X` and the first test cannot see them. It happened five times over:
  `MAMAN_MEDIA`, `MAMAN_SHARE`, `MAMAN_SHARE_PASSWORD` never declared at all,
  and `MAMAN_SHARE_LAN` declared six lines *after* the question that reads it.
  A third test forbids a bare `${!x}` anywhere.
- **The installer is a menu, not a script with one path.** Five components are
  optional (`zigbee`, `tailscale`, `cloudflare`, `media`, `share`); the API,
  the CEC control and the screen service are not — the screen draws the
  installation screen, which is the only way to configure the box without a
  laptop. Answers come from, strongest first: a `--with-`/`--without-`
  flag, an environment variable, `/etc/maman-tv-lite/install.conf`, an interactive
  question, the default. Adding a component means touching all four places:
  the question, the flag, the `unit_wanted` table, and the saved file —
  and **removing one means touching all four too**. The screen was made
  unconditional in `unit_wanted` and left in the other three, so the installer
  went on asking a question it ignored and printing "TV screen : no" while
  installing it anyway.
- **No credential is ever generated.** The installer asks for the API
  password, and `--yes` without one stops rather than inventing it. A generated password has to be written down before it
  scrolls past, and a box whose password nobody chose is a box whose password
  nobody changes.
- **Anything hardware-specific is measured, never hard-coded.** Which action a
  button publishes varies by model, so a real press is observed and written to
  `/etc/maman-tv-lite/buttons.json` — by the installation screen's pairing
  procedure, or by `scripts/setup-zigbee.py` from a shell. Which CEC technique
  a television obeys is measured the same way, by the installation screen's
  **detection procedure**, with a person watching the set and answering with a
  button, and written to `/var/lib/maman-tv-lite/tv.json`.

  **Nothing else writes that file, and no press can change it.** An earlier
  version searched for a technique during an everyday press and kept the first
  that appeared to work; it inferred from silence what only eyes can see,
  converged on its own worst answer on a new set, and an accidental press
  erased an evening's measurements. See "What was removed, and why" below.
  `cec-learned.json` is that version's file, read once on upgrade and never
  written.

  When adding a technique, put it in the table at the position its quality
  deserves: the order is the answer to "which is best", not just the order a
  search happens to try. The box's own physical address is read from the
  adapter for the same reason — hard-coding it to the first HDMI socket made
  two frames announce an address that was not the box's on any other socket.
  Commands resolve
  through the `COMMANDS` whitelist in `zigbee_bridge.py`: a binding file is
  data and must never be able to name arbitrary Python. When adding a command,
  add it to that whitelist **and** to the duplicated list in the setup script.
  A test fails if the two drift apart.

## Architecture

```
Zigbee button ──> Zigbee2MQTT ──> MQTT ──┐
                                          ├─> FastAPI process ──> HDMI-CEC ──> TV
Phone / browser ──> HTTP ────────────────┘
```

**One default state and three modes, and every transition goes through
`modes.py`.** The box is in **television** by default: it is not using the
screen, the set shows its own programmes, and the box's HDMI output is
**asleep**. Three modes take the screen one at a time — **music**,
**diagnostic**, **detection** — and each is entered deliberately.

Four rules carry the whole design:

1. one mode at a time;
2. entering a mode wakes the output, leaving one puts it back to sleep;
3. leaving a mode never leaves the set on the box's input: either the
   television goes off, or it is sent back to its programmes;
4. nothing outside `modes.py` touches the output.

Rule 2 is not tidiness, it is the fix. A television that comes back on "the
last input used" cannot come back on an input that is silent when it wakes: it
falls back to its own programmes. Because the output sleeps whenever the box
is idle, every wake happens that way, with no sequence to get right. Two
evenings were spent trying to achieve the same thing by timing — cut the
signal, standby, wait two seconds, restore — and it failed in production every
time, because the restore had to happen before the next wake and there is no
moment at which that is true.

Order matters inside an entry: the output must be awake **before** any CEC
command that assumes the box is visible. The music mode brings the television
up in a thread to save time, and that thread wakes the output before it claims
the input.

The MQTT subscriber runs **inside the API process**, not as a separate
service. This is deliberate: a single process drives the CEC bus, so a button
press and an HTTP request cannot interleave their frames on the CEC bus. A
re-entrant lock in `cec_controller` serialises every access regardless of
caller.

Key modules:

| File | Role |
|---|---|
| `api/cec_controller.py` | All CEC access, through `cec-ctl`. Owns the serialisation lock and knows where the television answers |
| `api/zigbee_bridge.py` | MQTT subscriber, resolves button actions against the binding file |
| `scripts/setup-zigbee.py` | Interactive setup: finds the adapter, pairs a button, writes its binding |
| `api/auth.py` | HTTP Basic auth, applied globally as middleware |
| `api/system_controller.py` | Reboot / shutdown, via logind + a polkit rule |
| `api/media.py` | Music and photos: the two lists, the slideshow, the players |
| `api/modes.py` | The mode machine. Every transition goes through it |
| `api/state.py` | What the box is doing, published for the screen and the shell |
| `api/tv_config.py` | The three techniques, the sleep method, and where each came from |
| `api/hdmi_output.py` | The box's own HDMI output, asleep unless a mode needs it |
| `api/media_config.py` | The music mode's own volume |
| `api/log_config.py` | How much the CEC transport says, changed without a restart |
| `api/screen.py` | Asking the root screen service to draw something |
| `scripts/screen.sh` | That service: photos, the diagnostic page, an installation page, keys |
| `scripts/maman-tv` | `status` and `report`, from a shell |
| `api/systemd_watchdog.py` | READY=1 and the watchdog, gated on the event loop actually turning |

The installation screen is a second layer on top of that, and it has its own
document: [docs/development/installation-screen.md](docs/development/installation-screen.md).

| File | Role |
|---|---|
| `api/installation.py` | The menu of guided procedures. Owns the channel and the drawing |
| `api/answer_channel.py` | One question at a time; an answer belongs to the question it names |
| `api/procedure_support.py` | What every procedure shares: the three-unanswered-questions rule |
| `api/cec_detection.py` | The CEC procedure. Pure logic, testable against a model television |
| `api/button_pairing.py` | The button-pairing procedure, including the no-button bootstrap |
| `api/zigbee_pairing.py` | What that procedure needs from Zigbee2MQTT |
| `api/button_bindings.py` | The binding file itself, with no idea what a command name does |
| `api/page_render.py` | One page drawn as an image, for `fbi` to put on the screen |

Two shapes are worth knowing before changing any of them. A **procedure never
touches hardware**: it talks to an `Environment` protocol that `modes.py`
builds, which is what keeps `modes.py` the only place allowed near
`cec_controller` and `hdmi_output` (rule 4 above) and what lets a whole
detection run be tested against a model television. And a **page's prose never
carries its own step number or failure code**: those go through
`PageSpec.step` and `PageSpec.title`, because a counter spelled out by hand in
eight separate strings is one nobody can keep right — the first version
shipped "STEP n OF 4" against three numbered steps.

**The music and photos mode was ported back from the Pi 5 box, where mpv is the
right answer, and on this board mpv was the wrong one.** It has been replaced
by `mpg123` and `fbi`. The measurements, all on the real machine:

| | mpv | now | |
|---|---|---|---|
| decoding one whole track | 64 s of CPU | **14 s** | 4.5× |
| putting one photo on screen | 2.3 s of CPU | **0.55 s** | 4× |

The whole mode was 54% of the single core — the music player 36, the photos 13
— which is what broke the sound into ALSA underruns. And it lost one photo in
three: mpv announced the frame as displayed while the display plane still held
the previous one. That was confirmed against the kernel's own DRM state (the
plane kept one framebuffer for 42 seconds while photos changed every 18) and it
happened with **both** `--vo=drm` and `--vo=gpu`, so it was not the video
output. Four ways of making the audio half cheaper were tried and all four
failed — bitrate, FFmpeg's fixed-point decoder, integer output, resampling —
which is what finally settled it.

`fbi` draws once into the framebuffer and exits: no double buffering, no page
flip, so the lost-photo fault cannot happen by construction.

**The old note, kept because it is still true about the board:** An earlier note in this file said the opposite —
that this board runs the legacy graphics stack, that mpv had nothing to draw
on, and that photos therefore needed `fbi` straight into /dev/fb0. That was
wrong for the board as configured: `config.txt` carries `dtoverlay=vc4-kms-v3d`
and the kernel says `vc4-drm soc:gpu: [drm] fb0: vc4drmfb`. It runs KMS, and
mpv paints on it perfectly.

Two settings were what the port actually needed, and neither may be assumed:

- **The DRM connector.** The board has `HDMI-A-1` and a `Writeback-1` with no
  modes at all. Left to choose, mpv took the wrong one, drew nothing, and said
  only "Could not find any preferred mode" — no error, no exit. `media.py`
  detects the connector whose `/sys/class/drm/*/status` reads `connected`.
- **The audio device.** The code's comment claimed ALSA's default here is the
  HDMI output. It is not: this board defaults to `Headphones`, the 3.5 mm jack,
  so the music played perfectly into a socket with nothing in it. Forced with
  `MAMAN_AUDIO_DEVICE`.

Chasing the missing picture cost an hour in the wrong place. mpv's
`Can't open TTY for VT control` looked like the console quarrel that `fbi`
really does have — the getty owns tty1 and will not give it up — so the hunt
went to VT ownership. It was a harmless warning about terminal *switching*:
mpv had the display all along and simply had no mode to set. And the first
verdict of "still no image" was taken twelve seconds in, before this board had
finished painting its first photo. On hardware this slow, look longer before
concluding.

**What mpv's photo path taught, now that fbi has replaced it.** Kept short
and in the past tense on purpose: none of the code below exists any more, and
an assistant reading it as current would go looking for a socket, a
`PHOTO_SCREEN_MODE` and a `_show()` that are not there. What is worth keeping
is the shape of the bill, because it is the board's and not mpv's:

- **Drawing cost the screen's size, not the photo's.** Scaling into a 1920×1080
  software buffer was 11 s of CPU per photo against 3 s into 1280×720, whatever
  the photo's own dimensions — which is why `prepare-photos.sh` first
  recommended `HD`. That bill was mpv's software scaler and **does not carry
  over**: a whole library prepared at FULL-HD was measured by the owner on the
  real box as fast, so the default is FULL-HD and the docs no longer suggest
  giving up detail for a saving that is not there. Preparing on the Mac still
  matters, for the decode and for a board with 427 MB — just not at 720p.
- **A player's readiness is a connection that is accepted, never a file that
  exists.** mpv left its socket behind when killed, so the next player was
  declared ready the instant it was spawned and every photo failed with
  "connection refused" while the log blamed the IPC.
- **A photo's turn starts when it is on the screen, not when it was sent.**
  Returning as soon as the command had gone out made the slideshow fall behind
  itself. fbi owns the clock now, so this cannot recur — but anything that ever
  takes the pacing back has to wait for the screen, not for the send.

**The music player stands by for the life of the API.** Starting one per
press was the whole of the wait between the button and the first note. It is
started once, at API start, in a background thread, and a press only sends it a
`LOAD`. Standing by it costs nothing measurable.

The photo half is not symmetrical and must not be made so: fbi is started by
`screen.sh` for each slideshow and owns the list and the clock, because
consoles belong to root and the API is not root.

- **Stopping is a `STOP` to the standing player, closing is killing it.**
  `_stop_players()` silences and lowers the console flag; only `shutdown()`
  closes, so a restart leaves nothing orphaned.
- **"Music is playing" can no longer mean "a music process exists"**, because
  one always does. It is a flag the box keeps, checked together with the
  process being alive — the flag alone would lie if the player had died
  underneath it. This drives the buttons and the automatic stop when the set is
  switched off with its own remote, so it is where the tests are.

**The television and the players are brought up at the same time.** Measured on
a real press: 45 seconds before the music and 1 min 20 before the first photo,
because a CEC wake can spend half a minute searching for a technique and mpv
needs 20 to 35 just to load its 225 shared libraries — and the two were done
one after the other although neither needs anything from the other. `start()`
runs the CEC bring-up in its own thread and joins it after the players are
going, so a press costs whichever of the two is longer. The photo player is
warmed up from `_start_players` for the same reason, rather than from the
slideshow thread where it was started only once the first photo had already
been asked for.

**`ReadWritePaths=` without a leading `-` stops the service from starting at
all when the path is missing.** Measured on the Pi 5 box with a throwaway unit.
The media folder is exactly such a path — absent on an install without that
component — so the entry carries the `-`, and a test pins the line. Getting
this wrong leaves a box with no API and no buttons.

**A diagnostic route must never take a lock.** `GET /media/status` is what
somebody calls when the box seems stuck, and a press waking a television holds
the media lock for up to a minute. It reads snapshots without the lock, and so
does the photo side of `now_playing`.

**A television should look like it is switching off, not like the box is doing
something odd.** This used to need a counted hold on the console, so that a
power-off never uncovered a screenful of diagnostics a second before the set
went dark. The mode machine removed the whole problem: the screen only ever
draws when a mode asks it to, and leaving a mode stops the drawing before the
set is touched.

**Setting the photo on screen aside must not advance the slideshow.** Removing
it from the list already moves the next one into its place; advancing as well
showed one photo out of two (measured 2026-09-20, while sorting a library). And
whatever ends the slideshow — stopped, or the last photo set aside — has to
lower the flag that keeps the diagnostic screen away, or the screen never comes
back.

**A television takes seconds to lock onto a new HDMI audio stream, and eats
whatever plays meanwhile** (measured on the Pi 5 box, where mpv was playing 3 s
after the press and the room stayed silent for ten). mpv cured this with
`--audio-stream-silence` and `--audio-wait-open`; **mpg123 has no equivalent
and the symptom is back** — the first track of a session can start mid-phrase.
Known, accepted, and not worth a silence generator on this board. Do not add
`MAMAN_AUDIO_WAIT_SECONDS` back as a setting: it would promise a cure that is
not there.

**Only a request that carries a password counts as a guess at one.** A request
with no usable `Authorization` header is answered 401 and costs its caller
nothing: browsers ask for icons without credentials, and each one used to count
towards locking the owner out of their own box.

**A press that lands while something is running is a person, not an echo.** The
duplicate of one press arrives within a second; anything later is queued, and
further ones collapse into it. Dropping them all made the button feel dead
during a learning search.

**After the players stop, silence from the set is not "it is off".** The "tv"
button read `_safe_power_status() == "on"` and treated everything else,
including `unknown`, as a set to be woken. Measured in the field: the read took
15 seconds and came back unknown — stopping the players is itself a reason for
it, because the screen mode changes back and the set re-syncs — so the box woke
a television that was already on. On a set whose only wake technique claims the
input, that means a black screen and the diagnostic display a minute later,
instead of the programmes. Music was playing on that set a second earlier, so
only an explicit off state counts as off. The same asymmetry as everywhere else
in this file, applied in the one place it had been forgotten.

**A television switched off by its own remote tells the box nothing**, so
while something plays the box reads the set's power state every 30 seconds and
stops the players after two readings in a row saying it is off — the state
itself, or silence from a bus that was answering, the same asymmetry the
power-off verifier rests on. It only ever reads: re-sending to a set that is
shutting down wakes it back up. Measured on the Pi 5 box, where without it the
music went on playing into a set that was off.

**The slideshow belongs to fbi, not to the API.** An earlier design kept a
photo player alive and drove it from a thread in the API, so the box always
knew which photo was on screen. That went with mpv; see "fbi runs the slideshow
itself" below for what replaced it, what it cost in features, and why that
trade was the right one.

## Hard-won lessons

Each of these cost hours. Re-deriving them is waste.

**The media mode is now verified on this board, except its cost under a full
library.** Music, sound device, DRM connector, the kept-alive photo player and
the slideshow all work. What is not settled: the library is 275 photos of up to
4320×3240, and the measurements above were taken on three photos shrunk to
1440×1080 (`/medias/pictures_full` holds the originals, `/medias/pictures` the
test set). Shrinking the whole library to screen size is still to do — not for
the decode, which is only 2 s, but because a big photo makes mpv hold more
memory on a board with 427 MB and 83 MB already in swap.

**HDMI-CEC is not a reliable standard.** Behaviour varies wildly between
manufacturers. On the Samsung used for development, nearly every command
failed silently unless the Pi was the displayed source — which is why the two
sleep techniques that claim the input exist at all. That same TV also refused
all commands
(`Feature Abort`) until someone had physically navigated its menus after
power-on. Never assume a CEC command worked; verify state.

**Returning to the TV tuner is impossible over CEC.** The protocol reserves
`SET_STREAM_PATH` to the television itself. This is why the project depends on
a TV whose "Hotel Mode" sets the startup channel — the TV does it, not us.

**Hotel Mode is not enough when the set comes back on the last input used.**
The set at the installation site returned to the box's input after every
power-off from the music or the photos, Hotel Mode or not, and its menus offer
no setting for it. Every CEC route is closed: Set Stream Path came back as
Feature Abort, unrecognised opcode, and thirteen other frames were ignored
without a word (the five source-switch techniques of the day, Routing Change,
Active Source claimed for the television's own address, Select Broadcast Type
with four operands, Channel Up, a digit). What works is the signal itself, and
the rule is simple: **the box's output must be asleep when the set wakes.**
That is what the mode machine guarantees.

**Put the output to sleep; never force the connector off.** Both stop the
picture — the set reports an absent source either way — but they are not the
same thing to the kernel. Measured on 2026-09-23:

| | connector forced off | output asleep |
|---|---|---|
| picture | gone | gone |
| connector | reports disconnected | still connected |
| EDID kept by the kernel | **dropped** | kept |
| CEC address | kept on a set that stays connected in standby | kept |

The difference is the EDID. Forcing the connector off makes the kernel forget
it, and it can only read it again from a set that answers — which a television
that electrically unplugs its input while it sleeps never does. That is what
left the box unable to send a single frame at the installation site, with the
set having to be switched on by hand before it could be controlled again.
`echo 4 > /sys/class/graphics/fb0/blank` (FB_BLANK_POWERDOWN) never touches
the connector, so that failure cannot happen on any set.

**And the claim that a cut invalidates the CEC address is wrong** — it was
written here on 2026-09-22 and disproved the next day. With the connector
forced off for seventy minutes and the television asleep, the address stayed
`1.0.0.0`, the adapter stayed configured, and the box still woke the set. What
had really happened at the installation site is that *that* television drops
its HDMI hotplug in standby, and with the EDID already dropped the kernel had
nothing left to recompute the address from. It is a property of the
television, not of the cut. Whether a set stays connected while it sleeps is
worth measuring once per model.

**Which of the two a box uses is a configuration point**, `hdmi_sleep` in
`tv.json`: `blank` by default, `connector` for a television that ignores it.
Each has its own unit, and the polkit rule grants exactly those two. Whoever
switches to `connector` should know it is the one that can leave the box
unable to talk to the set.

**Read `dpms`, not `enabled` or `status`.** With the output asleep,
`status=connected`, `enabled=enabled` and `dpms=Off`. A version that checked
`enabled` reported a cut that had not happened, and the journal read as a
working fix for a quarter of an hour.

**There is no absolute volume in CEC.** Only step up and step down.

**Do not call `corepack enable` here.** It writes shims into /usr/local/bin,
which the installing user cannot write, so it prints "Internal Error: EACCES"
in the middle of a perfectly healthy build and makes people stop and worry.
`corepack pnpm` needs no shims. Keep `COREPACK_ENABLE_DOWNLOAD_PROMPT=0` too,
or corepack waits for a keypress before downloading pnpm and an unattended
install hangs.

**ARMv6 is not ARM.** On a Pi 1, Debian `armhf` packages assume ARMv7 and die
with `Illegal instruction`. Hit with both `adb` and `esbuild`. Test that a
binary *runs*, not just that it installed. Irrelevant on 64-bit Pi 4/5.

**A device's friendly name ends up in its MQTT topic, and names hold
spaces.** `mosquitto_sub -v` prints "topic payload" separated by a space, so a
press from "Bouton Jaune" was read as topic `zigbee2mqtt/Bouton` and payload
`Jaune {...}` — not JSON, dropped in silence, and the pairing script ended on
"no button press detected" (measured 2026-09-20). It had never shown up because
a button is renamed AFTER its first pairing, so the first press always came
from `0xa4c1...`. The script subscribes with `-F "%t\t%p"` and splits on the
tab; a test pins both halves.

**Zigbee2MQTT without its adapter will exhaust the machine.** It restarts
forever, ~50 s of CPU per attempt. 318 consecutive restarts once pinned a Pi 1
at load 2 until the hardware watchdog rebooted it twice. The installer only
enables it when an adapter is present.

**Televisions do not agree on how to be switched on or off — and the box no
longer tries to work it out by itself.** Image View On (0x04), Text View On
(0x0D), User Control power keys (0x44:6D, 0x40, 0x6B) and a bare active-source
announcement are all "the" way to wake a set, depending on the set. The tables
in `cec_controller` are a vocabulary; `tv_config` names one entry per
direction; a press sends exactly that, once.

**What was removed, and why, because it looked reasonable for months.** The
box used to try the techniques in order during an everyday press, verify with
`pow`, keep the first that worked as *provisional*, prove it alone on its next
use, strike it off if it did nothing, and search again. Every part of that
mechanism was measured into existence, and the whole was still wrong:

- **It inferred from silence what only eyes can see.** A set that answers
  nothing in standby is indistinguishable from one that did not obey. Silence
  means "it worked" for a power-off and "not yet" for a wake, and the box got
  that asymmetry right in the end — but it still could not see which input the
  television came back on, which is the thing that actually mattered.
- **It produced false positives.** On the development Samsung it credited
  techniques that did nothing. At the installation site it recorded
  `power_on_function` three times; sent alone later, that frame did nothing at
  all — the earlier entries of the search had been doing the work.
- **It could not be trusted on a new set.** On the High One it settled on
  `standby_after_active_source`, which claims the HDMI input before switching
  off — so the television went off showing the box and came back on it, every
  time. A search verifies only that the set went off, and that technique
  always passes that test. **The product converged on its own worst answer.**
- **It rewrote the configuration under the owner's feet.** On 2026-09-22 an
  accidental press on a button bound to `relearn_tv_methods` — one short press
  was enough, and the buttons do get pressed while being packed — erased an
  evening's measurements and reconfigured the box while it was being carried
  out of the building.

Discovery is now a mode with a human in front of the screen, entered
deliberately, and it is the only thing that writes the configuration. Nothing
about an everyday press can change what the box knows.

**One finding from that mechanism outlived it, and it still matters.**

A wake technique must send its own frame rather than ask a library for a
helpful "switch the television on". libCEC's `on 0` interrogated the set first
and gave up before sending anything at all against one that did not answer:
captured over a whole session, it emitted Give Vendor ID, a poll and Set OSD
Name, and not once Image View On. Everything in those tables transmits its frame
itself.

**The box talks to the television with `cec-ctl` (from v4l-utils), not with
libCEC.** A deliberate choice, and worth knowing before changing anything here,
because libCEC is what most projects reach for.

libCEC does two things by itself that this product cannot have. It re-announces
itself as the active source whenever the adapter's physical address is lost and
re-acquired — so on a television that drops its HDMI hotplug when it powers on,
the set is dragged onto the box's input every time, whatever the box asked for,
and there is no option to disable it. And it can only address a television at
logical address 0, while the specification allows 0 and 14 and at least one set
does move between them; every frame then goes unacknowledged while the box
reports success.

`cec-ctl` sends the frame it is given and nothing else: a thin wrapper over the
kernel's CEC ioctls, with no session to keep alive. About 0.5 s per frame, and
reading the set's power state went from up to 15.8 s to 0.5 s on the television
this was measured against.

**`cec-utils` is deliberately not installed.** Nothing uses it, and the two
tools cannot share the adapter: whichever of them configures a logical address
takes it from the other, so a libCEC client started by hand to debug something
stops the box talking to the television. Capture bus traffic with
`cec-ctl --monitor`, which listens and transmits nothing.

Three consequences, all load bearing:

- **Where the television answers is measured, not assumed.** `cec-ctl --to N
  --poll`, address 0 then 14, remembered; a frame that comes back
  unacknowledged makes the box look again and send it once more.
- **The adapter's own logical address is claimed while the set can still hear
  it**, before the box's output goes to sleep, never when a frame is due. Later
  than that the claim times out, and every frame goes out as `Unregistered` and
  unacknowledged with the journal recording it as sent.
- **An operation that was never attempted must not read as one that
  succeeded.** `cec-ctl` prints no "OK" for a frame it accepted, so the absence
  of a complaint is all there is to go on — and with the adapter's addresses
  cleared it transmits nothing and says nothing about that either. Every read of
  its output requires the line naming the transmission as well.

**Everything that needs the adapter happens once per frame.** "Is the link up"
and "do I hold an address" are the same reading of `cec-ctl`, and each reading is
a process: 0.15 s on this board. Asking twice made an ordinary press a third
slower than it had to be. `_run_unlocked()` reads once and passes it down;
`_send()` takes no reading at all. A test counts what one press costs, so a third
reader cannot be added without somebody noticing.

**A television in standby can take its HDMI link away entirely.** The adapter
then has no physical address (`f.f.f.f`), no logical address can be claimed, and
nothing can be sent at all. Waking the box's own output is what brings the link
back — forcing the connector to re-probe does not — so `bring_the_link_back()`
does that and hands the output straight back. It is the one place outside
`modes.py` that touches the output, because with the link down there is no other
lever.

Every branch of it says so at warning level, all beginning `cec link`, so
`journalctl -u maman-api | grep "cec link"` answers what happened days later —
and `/report` carries `link_down_seen` and `link_recovered` since boot.

**Every frame is logged with what became of it**, one line, always: `cec --to 0
--standby -> sent`, or `not acknowledged`, or `never sent`. A set that has moved
is said once, at warning level, on the frame that came back unacknowledged —
which is the only moment the box knows both where it was looking and where the
set actually is. Saying it inside `find_the_television()` looked tidier and was
nearly dead code: the retry clears the remembered address before looking, so by
then there is nothing left to compare against. Without it the
journal recorded that a technique had been sent and nothing about whether it
reached the television, which is exactly how an evening of presses that did
nothing looked like an evening of presses that worked. `PUT /logging/cec?level=DEBUG`
adds everything `cec-ctl` printed: applied to the live logger and written to
`logging.json`, so it needs no restart and survives a reboot. That is the point
of it being a setting — the one moment somebody wants more detail is the one
moment they cannot restart the API, which loses the CEC session's state, re-reads
the configuration and pokes the set on the way through. It moves the
`cec_controller` logger only: raising the root would turn on every library in the
process, paho above all, on a board whose journal is capped at 200 MB. And `maman-cec-monitor.service` records the
bus itself — installed on every box, enabled on none, listens only, capped at
50 MB, and last for the core. The
branch that can do nothing (the link is down and the output is already awake) is
the loudest: an earlier version returned from it in silence, which made the one
situation the box cannot get itself out of the one situation it said nothing
about.

**The techniques that claim the HDMI input are last in their tables.** The
viewer should see the set go off, not see it switch to the box first — and
captured on a real television, a claim was followed by the set going off and
straight back on, on the box's input. A set that does not need the claim must
never see it.

**Power Toggle (0x6B) is last in both tables.** It is the only frame with no
direction of its own: a set that obeys it can just as easily be sent the wrong
way, and reached in the middle of a search for how to switch *off* it would
switch the set back *on* and poison everything measured after it. Kept for a
television that answers nothing else, reached by nothing else.

**A television goes silent when off, and that means different things in each
direction.** The set this box is for answers nothing at all while in standby —
not even a Feature Abort. Turning off, silence from a bus that was answering is
success. Turning on, silence means "not up yet": counting it against the
two-reading failure budget rejected the technique that had just started the
wake, about four seconds in, and handed the credit to the next one. That is why
the recorded winner kept changing between runs — it was a race, not a
measurement.

**Check the state twice, spaced out.** A set can be put where you asked and
then moved straight back by something else on the bus. One reading is a
snapshot; `STABILITY_RECHECK_SECONDS` later, it is a result.

**Button actions never run in the MQTT callback.** They are handed to a single
worker thread. Running them inline blocked paho's network thread for as long as
the command took — up to a minute and a half for a search — so no further press
was even read from the socket. The reset button was therefore queued behind the
very search it was meant to stop and ran *after* it, wiping what had just been
learned (observed in the field). The same block also starves MQTT's keepalive.

One worker, so two presses still cannot interleave on the CEC bus. Nothing
runs on the spot in the MQTT thread any more: the one command that used to —
the button that stopped a search — went with the search itself.

**One press can reach the bridge twice.** Captured from a real session: two
MQTT publishes for a single press, in the same second, with *different* link
quality values — so two distinct radio receptions, not a duplicated log line.
Run twice, `power_toggle` turns the set on and straight back off, nothing
appears to happen, and the person presses again; the same logs show eight
presses in twenty seconds. `zigbee_bridge` therefore suppresses an identical
action from the same device inside `ZIGBEE_DEBOUNCE_SECONDS`.

The window is measured from the **end** of the previous identical action, not
its start. The MQTT callback runs the command synchronously, so a duplicate
that arrived 200 ms in waits in the socket buffer until the first finishes —
several seconds during a learning search. Measuring from the start would let it
through precisely when the command was slowest. That state is module-level and
`tests/conftest.py` clears it between tests, or a test inherits the previous
one's window and fails for an unrelated reason.

**A television can simply refuse to give up being the active source.**
Captured on the Samsung used for development, once it had been on for more
than about ten seconds without anyone touching the remote:

    << 1f:82:10:00    we broadcast Active Source = the box
    >> 0f:82:00:00    the TV broadcasts Active Source = itself, 0.26 s later
    << 10:36          we send Standby
    >> 01:00:36:01    Feature Abort, "not in correct mode to respond"

That is a closed loop: the set only obeys the active source, and it will not
let the box become one. Nothing sent over CEC breaks it. Tried and ignored:
re-broadcasting the raw Active Source frame, Image View On first, a status
poll first, and User Control "Power Off Function" (0x6C), which drew no reply
at all. Using the TV's own remote clears the state, because it changes
something CEC cannot reach.

Within a few seconds of power-on the same set accepts everything, so this is
a state it settles into rather than a fixed limitation. Do not spend hours
looking for the frame that fixes it; there is none. Choose a television that
does not do this — which is the deeper reason the project depends on Hotel
Mode rather than on CEC doing clever things.

**Claiming the input makes the set refuse the next command for about two
seconds.** Captured on the bus: a Standby sent 0.6 s after the claim came back
as `Feature Abort, not in correct mode to respond`, because the television was
still switching input; the routing change finished roughly two seconds later.
That is what `ACTIVE_SOURCE_SETTLE_SECONDS` is for.

**Zigbee2MQTT does not always recognise the adapter.** A widely sold EFR32
dongle on a CP210x bridge failed discovery outright: "No valid USB adapter
found. Specify valid 'adapter' and 'port'", crash-looping at ~47 s of CPU per
attempt. Relying on auto-detection and shipping a config without an `adapter:`
line was a regression. `setup-zigbee.py` now names each chipset in turn until
one answers, and `--adapter` skips straight to a known value. Do not hard-code
one in the shipped config: that would break every other chipset.

**The serial link to the Zigbee dongle drops when the CPU is busy.** ASH, the
protocol between host and an EFR32 adapter, has hard acknowledgement
deadlines. On a single-core Pi 1 under load the Node process is not scheduled
in time to acknowledge, and the adapter gives up:

    error: ash: Received ERROR from adapter, code=ERROR_EXCEEDED_MAXIMUM_ACK_TIMEOUT_COUNT
    error: ash: ASH disconnected | Adapter status: ASH_NCP_FATAL_ERROR
    error: z2m: Adapter disconnected, stopping

Zigbee2MQTT then stops itself, and pairing fails at "opening pairing mode" with
a message about the bridge not accepting requests — which points at the bridge
and hides the real cause. Observed at load 4.8, brought on by restarting the
API (~25 s of CPU each time on that board), restarting journald, and several
SSH logins, all while Zigbee2MQTT was starting.

So: **pair when the machine is idle.** Do not restart services, copy files or
open extra sessions during it. If it fails this way, wait for the load to fall
before retrying rather than changing anything — nothing is wrong with the
configuration, the dongle or the script.

**Zigbee2MQTT rewrites its own configuration.** Its port value was once left
split across two lines, which YAML joins with a space; the service then
reported ENOENT for a path that existed and restarted forever. Anything that
edits that file must handle a folded value, not just the first line.

**The Zigbee bridge reports itself online before it answers requests.**
Measured: it published `bridge/state` online three seconds before it logged
"started", and a `permit_join` sent in that gap is dropped in silence. The gap
only exists when something restarted the bridge moments earlier, so it breaks
the second run of the setup script and not the first. Anything that talks to
the bridge right after a restart must retry and read back proof, never trust
the send.

**Zigbee pairing windows are capped at 254 seconds** and the refusal lands on
a response topic nobody watches. Ask for more and pairing silently never
opens.

**The API needs more than systemd's default 90 s to start on a Pi 1.** Its
first boot attempt was killed mid-import from a cold page cache; hence
`TimeoutStartSec=300`.

**`.local` never crosses a router.** mDNS is multicast with TTL 1. From
another network the name simply will not resolve — this is protocol design,
not a bug to fix.

**A captive portal breaks every remote path.** Neither Tailscale nor a
Cloudflare tunnel can authenticate through one. The diagnostic screen on the
TV reports this explicitly, because when it happens nothing else will reach
the machine.

**A shell loop that forks is a permanent tax on this board.** The diagnostic
screen comes round once a second, because the blanking has to land before the
television has finished waking. `sleep 1` forks /bin/sleep every time, and that
alone was measured at 1150 ms of CPU per minute here — 2% of the single core,
for ever, on the machine whose spare CPU the music needs. The same wait done
with bash's own `read` on a fifo opened read-write (so it never reports end of
file) and then unlinked costs 90 ms. With the learned-state `stat` slowed from
every 5 s to every 60 s, the whole service went from 2.7% of the core to 0.28%,
measured before and after on the real machine. `tick()` keeps a `sleep`
fallback: a diagnostic screen that stopped working to save CPU would be a poor
trade.

The five-second poll was right while the CEC techniques were being worked out
on site, with somebody standing in front of the screen watching them land.
That is a setup activity, and it had no business staying on for ever.

**A journal cap chosen from an idle box loses the evidence from a working
one.** The installer used to set `SystemMaxUse=5M`, from "observed usage after
many test reboots stayed under 2 MB" — which measured a box nobody was using.
On 2026-09-22 the API's own lines for the hour that mattered had already been
discarded by the time anybody looked, and the failure had to be reconstructed
from file timestamps and from what had been copied to a laptop during the
session. It is 200 MB now: under 3% of the free space on a 16 GB card, and
this box lives somewhere with no internet, where the journal and the
television's own diagnostic page are the only two ways to find out what
happened.

**This board has no clock, and its timestamps lie across a boot.** With no
RTC and no network, systemd restores the last time it knew and jumps when NTP
answers. Two boots then overlap in the journal: on 2026-09-22 a box that
booted at 21:23 wrote its first three minutes stamped 20:26, which is exactly
when the previous boot was being unplugged. That cost an hour and a wrong
conclusion about where a configuration had been destroyed. Everything this
project prints now carries both the clock and the seconds since boot, and
`maman-tv report` puts them side by side.

**The buttons press themselves.** Captured at 21:36:26 and 21:36:27 on
2026-09-22: two different buttons published `hold`, 0.55 s apart, each with
its own battery level and link quality — two genuine radio receptions, while
the owner was not touching anything. They had been packed together. That is
how an installation lost its configuration, and it is why nothing destructive
is one press away any more.

**Two accidental presses occupied the box for twenty-six minutes.** The same
two: the first started a search that ran ten minutes, and the second waited
behind it in the queue and then ran its own. A press must never start
something that long — which is now true, because a press sends one frame.

**Logging was silently discarded** before `logging.basicConfig()` existed in
`main.py`: with no handler on the root logger, Python falls back to a
last-resort handler that only emits `WARNING` and above.

**Do not ship a script that strips a working card.** There was one; it was
removed. It missed Raspberry Pi Imager's seed files on the boot partition
(password hash, WiFi key, a Connect account token) and the entropy seed, and
it grew three bugs of its own in a day, two of them around `rm -rf` and a unit
name it collided with. Images for other people get built from a recipe that
never creates those files. docs/sd-card-image.md lists what such a recipe must avoid.

**A virtualenv cannot be moved.** It writes absolute paths into the shebang of
every script it installs, so renaming the install directory leaves `pip` and
`uvicorn` pointing at a path that no longer exists, while the venv's own
`python` keeps working and hides it. Re-running `python3 -m venv` over the top
does not rewrite them. The installer now tests a console script and rebuilds
the environment when it fails. The same breakage follows a system Python
upgrade.

**Updating is "pull, then run the installer again", so the installer has to
actually replace things.** Two ways it quietly did not: `systemctl enable
--now` does nothing to a service that is already running, so new code was
copied and the old code kept serving until a reboot; and Zigbee2MQTT was
skipped whenever `dist/` existed, so raising the pinned version updated
nothing while reporting success. Anything added here that writes a file must
also make the running system pick it up.

**The product has to outrank everything else on the box, and nothing said so.**
One core, and in a day the background services burnt five times what the
product did: cloudflared 1644 s, smbd 1547 s, tailscaled 1029 s, against 283 s
for `maman-api`. The consequence was visible from the sofa. Captured with the
slideshow's own timings: a photo that normally reaches the screen in 3 s took
**20.3 s at load 5.4**, holding it there for 35 s instead of 18, and the music
broke into ALSA underruns in the same seconds — all because `samba-dcerpcd` had
started three seconds earlier.

**The order matters as much as the throttling, and getting it wrong was worse
than not doing it at all.** The first attempt threw Zigbee2MQTT in with the
background services at `CPUWeight=20`. ASH, between it and the adapter, has
hard acknowledgement deadlines; starved, Node missed them and the adapter gave
up — `Adapter disconnected, stopping`, **fifteen seconds into a track** — and
the next press on the green button was never received at all. The same attempt
left ssh at the default against an API at 10000 and made the box unreachable
for two minutes, on a machine nobody can walk up to.

So the hierarchy, highest first:

| | weight | why |
|---|---|---|
| `zigbee2mqtt`, `mosquitto` | 10000 | the buttons **are** the interface |
| `maman-api` (and its two mpv children) | 5000 | what the buttons are for |
| `ssh`, `user.slice` | 1000 | the only way in |
| smbd, nmbd, samba-dcerpcd, cloudflared, tailscaled | 20 | genuinely elsewhere |

Music that stutters is a poor evening; a button that does nothing is a box that
cannot be used at all. Drop-ins rather than edits, because those units belong
to their packages and are replaced on upgrade, and guarded by
`list-unit-files` because every one of them is optional. Both halves are
needed — a high weight on the API means nothing while the others keep the
default — and tests pin the whole order, including that the buttons are never
in the throttled list.

Measured after, with Samba restarted in the middle of a slideshow on purpose:
nine photos between 2.9 s and 3.2 s, intervals between 17.8 s and 18.2 s, not
one slow turn, and three audio complaints where there had been dozens. It is a
weight and not a cap, so nothing is slowed down except when it is competing
with the music and the photos — the only moment the owner would notice.

Two things this depends on: the board runs cgroup v2 with the `cpu` controller
(`/sys/fs/cgroup/cgroup.controllers` lists it), and the two mpv players are
children of the API service, so they inherit its weight. Moving them out of
that cgroup would quietly undo all of it.

**Where this board's one core actually goes, measured.** Asked because 54% of
a core for "an MP3 and a photo every fifteen seconds" is hard to believe on a
machine that once ran XBMC. Per service, over 60-second windows:

| state | cost |
|---|---|
| nothing playing | ~4% (cloudflared 3, the API 2) |
| music and photos | **54%** — the music player 36, the photo player 13 |

So the pictures were never the expensive half. Of the music's 36%, **26% is the
MP3 decode itself**, and the rest is the HDMI output path.

Four ideas for making that 26% smaller were tried and **all four failed**, so
do not spend the afternoon again:

- **Bitrate makes no difference.** 64 kbps, 160 and 256 all cost 26%.
  Re-encoding the library would gain nothing.
- **FFmpeg's fixed-point MP3 decoder** (`--ad=lavc:mp3`) costs the same 26% as
  the float one, on a board with no fast FPU where it ought to have helped.
- **Forcing s16 output** makes it worse: 29%.
- The library is already 48 kHz, the rate the HDMI output wants, so **nothing
  is being resampled**.

The photo side can be made cheaper, and this one does work: `--vo=drm` is a
software output, and `--vo=gpu --gpu-context=drm --gpu-hwdec-interop=drmprime`
puts the scaling on the VideoCore — 2.3 s of CPU per photo becomes 1.7 s.
Worth about three points of the core. Plain `--vo=gpu` without the dmabuf
interop is barely better than software (2.1 s), which is why an earlier,
rougher test concluded the GPU did not help at all.

Why XBMC was comfortable on this hardware and this is not: it drew through the
firmware's own graphics stack (dispmanx/OpenMAX) with hardware scaling and
hardware decode. This board runs KMS — `dtoverlay=vc4-kms-v3d` — which is what
gives the DRM output the photos use, and which rules that firmware path out.
XBMC also did not share the board with Samba, Cloudflare and Tailscale.

**Never untar a Mac-made archive onto the media folder as root.** An archive
made with `tar -cf - .` carries the `./` entry itself, and `tar` as root applies
its owner and mode to the destination directory — so `/medias/pictures` came
out owned by UID 501 (the Mac's user), group root, mode 0755. The share then
silently refused every write from the Mac, and nothing in that symptom points
at the cause. The folders want `maman-tv:maman-tv` and **2775**: the setgid bit
is what keeps a file dropped over the share readable by the service that plays
it. Re-running the installer repairs it (`install -d` applies the mode to
existing directories), or `chown`/`chmod` by hand. Copy files in with `install`
rather than `tar`, or extract with `--no-same-owner` and no `.` entry.

**A fresh card runs things this box does not need, and they are not free.**
Measured on the real machine, per service, since boot: Raspberry Pi Connect's
daemon 5 min 34 s of CPU (Imager signs the machine in), and a Bluetooth
media-key proxy on a board that has no Bluetooth. Between them they also kept a
whole `systemd --user` session alive — 349 s of CPU — for an account nobody logs
into. docs/installer.md says how to turn them off.

What the same inventory says to leave alone: `avahi-daemon` (`.local` depends on
it), `polkit` (reboot and shutdown go through it), `systemd-timesyncd` (this
board has no clock of its own). And the periodic maintenance is a judgement
call rather than waste: `apt-daily-upgrade` cost 53 s of CPU in one run and
`man-db` 25 s, which on one core is a minute of competition — but the API is
published to the internet through a tunnel, so switching security updates off
is a trade, not a saving.

**mpv was tried again for the photos once the audio was cheap, and it still
loses one in three.** The idea was reasonable — the lost frames might have been
CPU starvation, since the audio used to take 36% of the core and now takes 7 —
so it was measured rather than argued: twelve photos through a standing mpv
while mpg123 played, and the display plane changed on seven of them. The same
rate as before. It is not the load; mpv's display path is simply wrong on this
board. `fbi` stays.

**What the whole mode costs now, measured through the product itself** — not
with the players driven by hand, which is a different thing:

| | mpv | mpg123 + fbi |
|---|---|---|
| photos | 13% of the core | **2%** |
| music | 36% | **11%** |
| both together | **54%** | **13%** |
| load average | 2.5 | **0.87** |

That is the whole point of the change, and it is why the music no longer breaks
into ALSA underruns: four fifths of the core is free where one half used to be.

**fbi runs the slideshow itself, and that decision cost two features on
purpose.** A fresh fbi per photo retakes the console every time, and measured
by sampling the framebuffer every 200 ms, that BLANKS THE SCREEN for about
eight tenths of a second before it paints: 60% black, then 100, 100, 100, 86,
then back. A fade can hide that black; nothing can remove it. Given the whole
list — `-u -t 15 -l <list>` — fbi never retakes the console: it decodes the
next photo while the current one is up and swaps them. Timed on the real board
at exactly one photo every fifteen seconds, and lighter than anything the box
can drive.

fbi owns the list and will not say what it is showing — it does not even keep
the file open; measured, `/proc/<pid>/fd` holds nothing between turns. **The
kernel answers instead**: fbi opens each photo at the moment it puts it up, and
one `IN_OPEN` arrives per turn, exactly at the change (measured at 7-second
intervals with a 6-second turn). `_follow_what_is_shown()` watches for that, so
the box knows the current photo with no timer and no counting.

**This holds only while fbi is not reading ahead.** With `-readahead` it opens
the next photo while the current one is still up, and every answer would be one
photo early. The slideshow is started without it, and that is not an accident.

**"Next" and "previous" are keys pressed in fbi's console.** `TIOCSTI` pushes
one character into the terminal fbi is reading, which is the whole reason the
slideshow has a console of its own: on tty1 it reaches agetty's login prompt
instead. The API cannot do it — consoles belong to root — so it writes the
character into `PHOTO_KEY` and the screen presses it, the same way it asks for
a slideshow at all.

**Known fault: setting a photo aside straight after "previous" moves the wrong
one.** fbi does not re-open a photo it still has in memory, so a backward step
produces no `IN_OPEN` at all and the box keeps naming the photo it was on
before. Stepping forward is unaffected. `-cachemem 0` was tried and does not
help: tested, the backward step still produced no open. It is written into the
Swagger description of `/media/pictures/archive`, because whoever presses that
button is the person who needs to know.

**This fault is accepted, and the obvious fix was refused on purpose.** It
would mean giving fbi the list in the box's own shuffled order, counting the
steps, and resynchronising on each `IN_OPEN`. That works, and it would also
cure `j` being dropped while fbi is decoding — but it puts the shuffle and the
pacing back inside the API, which is exactly the shape that cost 54% of the
core, lost one photo in three and blacked the screen between every picture.
The owner's decision, and the right one: fbi keeps the list and the clock, and
this one combination of two buttons is documented instead of being engineered
around.

**Setting a photo aside moves it, and never deletes it.** fbi's own delete key
is capital `D` with `-e`, and it is an `unlink`: tested on copies, the file went
and there was no backup and no trash anywhere. It is not used. A mistake must
cost nothing — these are somebody's family photographs.

**The television is moved to the slideshow's console before fbi starts, not
after.** Starting fbi and switching afterwards leaves tty1 on screen for the
couple of seconds fbi takes to come up, and tty1 carries the login prompt — so
the viewer sees a flash of console text before the first photo. Switching first,
while that console is still empty, shows black instead.

**Coming back to the diagnostic console is timed, not immediate.** It is
scheduled from the idle loop whenever the console flag goes away, not only when
a slideshow ends: switching the set off from the tv button holds the console
too, so the set goes dark on black, and that hold used to leave the screen
blank until the next full refresh — up to five minutes for anyone who put the
input back. Switching to
tty1 the moment the photos stop puts agetty's login prompt on the television
for the second or so the set takes to go dark — a screenful of technical text
as the last thing anybody sees, which is what all the blanking in this file
exists to avoid. Waiting for the ordinary five-minute refresh instead leaves a
black screen for anyone who switches the input back meanwhile. So the screen
stays on the slideshow's own console, empty once fbi has gone, and comes back
`CONSOLE_BACK_SECONDS` later, by which time the set is off and nobody sees the
change.

**fbi leaves the console in graphics mode when it dies badly, and nothing ever
recovers.** It switches the VT to KD_GRAPHICS to draw and restores it when it
is asked to stop — but not when it is killed any other way. Measured on the
board: `KDGETMODE` stayed at 1 long after fbi was gone, and from then on every
byte written to the console went nowhere at all. The television kept the last
photo for ever and the diagnostic screen wrote into the void; the box looked
fine from every angle except the one that mattered. `restore_text_console()`
forces KD_TEXT after every slideshow and before every repaint, rather than
trusting fbi to have done it — the failure is silent and permanent, which is
exactly the kind this project refuses.

**fbi will not start where it can see a terminal that is not a console**, which
is every service and every ssh session: it says "Not started from linux
console?" and exits. `setsid` and a closed stdin are what get past that. It
also needs `-d /dev/fb0` or it goes looking for DRM.

**`fbi` forks, so the pid a shell records is never its own.** Measured on the
board: the shell was handed 19457 while fbi ran as 19459, and 19457 was gone a
moment later. Everything that asked "is my fbi still alive?" therefore always
heard no — so `serve_photo` restarted fbi for the SAME photo every second. The
picture was redrawn once a second instead of once every fifteen, which is what
the flicker and the "ugly transition" were, and nine fbi processes were once
found alive at once. Nothing holds a pid now: what is on screen is remembered
by its path, "is one running" is asked of the process table, and a test forbids
the word `PHOTO_PID` from coming back.

**A player that dies must not take the music with it in silence.** Found in the
field: mpg123 died with the sound card held by something else, the music
stopped, the box went on showing photos as though nothing had happened, and the
journal said nothing at all — because everything mpg123 says that is not one of
its `@` messages was being dropped. Those are logged now, and `_follow_music()`
notices the stream ending: if the box still believes it is playing, it brings
the player back and resumes the track it died on. Backed off and capped at
`MUSIC_REVIVE_ATTEMPTS`, because a player that cannot start at all must not be
restarted in a tight loop on a board with one core.

**The standing music player holds the HDMI audio device for the life of the
API.** A consequence of keeping it alive, and the reason any experiment that
needs the sound card has to stop the service first.

**Never run `mpg123` with no file to play, and never document doing so.** The
env example and the troubleshooting table both said `mpg123 --list-devices`
was how to find the name for `MAMAN_AUDIO_DEVICE`. Run on the real board it
listed nothing and sat at 100% of the single core — with no file, mpg123 falls
back to reading its standard input — and it had to be killed from a second ssh
session while the box was too loaded to answer the first. `aplay -L` is the
answer; `alsa-utils` is installed for it and for nothing else.

Two things this cost that are worth remembering beyond the command itself.
Checking a documented command on the machine is right, but a command that is
wrong can be **destructive on a board with one core** — read it before running
it, or run it with `</dev/null` and a timeout. And a wrong instruction
propagates: it lived in `docs/media.md` for days and was copied into
`config/api.env.example` during an audit, by an audit that was checking
consistency rather than truth. `api/tests/test_env_example.py` now pins both —
every setting offered is read by something, and every command named is one the
installer installs.

**A slow turn no longer has anywhere to be reported from.** While the API
paced the slideshow, `_report_a_slow_turn()` separated the three causes — the
lock held by a button, a photo slow to draw, a thread not woken on time — and
printing the load average with them is what identified Samba. fbi owns the
clock now, so nothing sees a turn at all. If the photos ever look slow again,
that instrument has to be rebuilt somewhere, or the cause is unknowable from
the logs.

## Working method that paid off

- **Verify on the real device.** Repeatedly, assumptions that looked safe were
  wrong: a binary that installs but crashes, an API returning `ok: true` while
  the TV refused the command, a service reporting success it never checked.
- **Prefer one configuration point over duplicated code paths.** One mode
  machine replaced a power-off sequence written four different ways in two
  evenings, each of them correct in isolation.
- **Fail loudly rather than silently succeed.** A box that reports success
  while doing nothing is worse than one that errors: nobody investigates.

**A password is the only thing between the internet and `/system/shutdown`.**
The API is published through a Cloudflare tunnel, and that route needs someone
to physically travel to the box to undo. Constant-time comparison stops timing
attacks and does nothing against trying passwords in a loop, so `auth.py` adds
a fixed pause on every rejection and refuses a caller outright after
`AUTH_MAX_FAILURES`.

Counted **per caller**, deliberately: a global counter would let anyone on the
internet lock the owner out of their own box by failing on purpose. Tunnel
traffic all arrives from loopback, so the caller is taken from
`CF-Connecting-IP` for loopback requests only — Cloudflare's edge overwrites
what a caller puts there, while on the LAN the same header is caller-supplied
and trusting it would be a way to dodge the count. The test suite runs with the
pause set to zero, or rejecting on purpose dozens of times makes the whole run
ten times slower; one test asserts the pause is applied.

## Deliberate non-goals

- No streaming or video calling. This project is expected to stay what it is:
  a television and a photo album on modest hardware. Music and photos, once a
  non-goal, came back from the Pi 5 box once they worked there; see
  `api/media.py` and docs/media.md.
- SSH password authentication is **left enabled on purpose**, as a local
  fallback. It is only reachable from the local network.
- `/tv/turn-off` reports success without verifying it, *once a technique has
  been learned*. The search itself verifies — that is how it picks a winner —
  but a later press sends one frame and trusts it. `/tv/power-off-acknowledge`
  verifies every time.

**The instruments were removed, and that was the point of keeping them.** The
`-alt` power commands, the channel and mute commands, the source-switching
techniques and `/tv/power-off-acknowledge` existed to probe a television's CEC
behaviour from the installation site, without a laptop. They did their job:
what they measured is written in this file, and the detection mode is where
that work belongs now. `CEC_NEEDS_ACTIVE_SOURCE` went with them — it was a
setting for experiments, and the two sleep techniques that claim the input
cover the televisions that need it.

## Optional: a separate admin account

When an assistant administers the machine alongside a human, giving it its own
Unix account makes actions attributable in `journalctl`, in sudo logs and in
file ownership — and revocable independently. This is a workflow preference,
not part of the product, so the installer does not create one.

```bash
sudo useradd -m -s /bin/bash assistant
sudo usermod -aG "$(id -Gn "$(id -un)" | tr ' ' '\n' | grep -v "^$(id -un)$" | paste -sd, -)" assistant
sudo passwd -l assistant   # no password: key or identity-based access only
printf 'assistant ALL=(ALL) NOPASSWD:ALL\n' | sudo tee /tmp/sudoers.assistant >/dev/null
sudo visudo -cf /tmp/sudoers.assistant && \
  sudo install -o root -g root -m 0440 /tmp/sudoers.assistant /etc/sudoers.d/assistant
rm -f /tmp/sudoers.assistant
```

Always validate a sudoers file with `visudo -cf` **before** installing it: a
malformed file in `/etc/sudoers.d/` breaks `sudo` for everyone, which is
painful to repair on a remote machine.

Revoke with `sudo rm /etc/sudoers.d/assistant`.
