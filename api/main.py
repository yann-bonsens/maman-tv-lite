# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from enum import Enum

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import auth
import button_bindings
import cec_controller as cec
import hdmi_output
import installation
import log_config
import media
import media_config
import modes
import state
import system_controller as system
import tv_config
import zigbee_bridge
from cec_controller import CECError
from system_controller import SystemCommandError
from systemd_watchdog import keep_loop_beat, notify_ready, start_watchdog_pings

# Without this, the root logger has no handler and Python falls back to its
# "last resort" handler, which only emits WARNING and above. Every INFO log in
# the project was therefore silently discarded — including "button pressed ->
# action", which is exactly what you need when someone reports that it "does
# not work" from a place you cannot visit.
# How much the whole process says. INFO unless somebody says otherwise in
# /etc/maman-tv-lite/api.env.
#
# A name `logging` does not know falls back to INFO rather than raising: this is
# read at import, so a typo in a file the owner edits by hand would stop the API
# from starting at all — and a box with no API has no buttons, on a service that
# restarts for ever. The wrong level is a bad evening; no product is a broken one.
_WANTED_LEVEL = os.environ.get("MAMAN_LOG_LEVEL", "INFO").upper()
_LEVEL = _WANTED_LEVEL if _WANTED_LEVEL in logging.getLevelNamesMapping() else "INFO"
logging.basicConfig(
    level=_LEVEL,
    format="%(levelname)s:     %(name)s - %(message)s",
)
if _LEVEL != _WANTED_LEVEL:
    logging.getLogger("maman_api").warning(
        "MAMAN_LOG_LEVEL=%r is not a level; using INFO", _WANTED_LEVEL)
# Before anything has a chance to log: a box comes back as verbose as it was
# left, including for whatever the startup itself says about the television.
log_config.apply()

logger = logging.getLogger("maman_api")


def _open_the_screen_at_startup() -> dict:
    """What the box shows the moment it starts: the installation screen for
    a box with no working button, its output asleep otherwise. Pulled out
    of `lifespan()` so it can be tested without driving the whole ASGI
    startup sequence.

    Governed by `button_bindings.has_any_button()` alone, not by whether
    the CEC configuration has ever been confirmed: a television that
    already answers to the default frames (image_view_on/standby) is a
    working box, and forcing the installation screen open on every single
    boot just because nobody has explicitly run the detection or set a
    technique by hand would make that screen a permanent nag rather than a
    one-time step. A button, by contrast, genuinely cannot be worked around
    — without one the box is only reachable over SSH or a phone, so its
    absence forces the screen open every time, not only the first.

    Unless the owner never wanted buttons: installed `--without-zigbee`, a
    box is meant to be driven from a phone, and a screen that starts by
    pairing a button it can never receive would come back at every boot.
    """
    configuration = tv_config.load()
    state.note("configuration in force",
               wake=f"{configuration['wake']['technique']} ({configuration['wake']['source']})",
               sleep=f"{configuration['sleep']['technique']} ({configuration['sleep']['source']})")
    if not button_bindings.buttons_installed():
        logger.info("installed without buttons: no installation screen")
    elif not button_bindings.has_any_button():
        # A fresh box, plugged into a television for the first time, or one
        # whose bindings were just cleared: this is what a "no signal"
        # screen at boot used to look like before the installation screen
        # existed. Opens it instead of sleeping the output — the whole
        # point of "entering a mode wakes the output" is that somebody who
        # has just plugged in an HDMI cable should see something.
        logger.info("no button bound: opening the installation screen")
        return modes.installation()
    # The default state, applied rather than assumed: a box that has just
    # started is not using the screen, so its output has no business being
    # awake under a television that may be about to be switched on.
    hdmi_output.sleep("the box has just started")
    return {"mode": state.TELEVISION}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # READY=1 FIRST, before anything below it. This is not tidiness, it is
    # load-bearing, and moving it to the end of this function broke a real
    # boot on 2026-09-25 in two separate ways at once.
    #
    # `maman-screen.service` is ordered `After=maman-api.service`, and this
    # unit is Type=notify — so "after" means after READY=1, not after the
    # process starts. With READY sent last, `_open_the_screen_at_startup()`
    # below ran with no screen service alive to hear it: measured on the box,
    # "the screen service is not listening; 'page' not sent" at 20:41:03,
    # against a screen service that became active at 20:41:05. A box with no
    # button paired showed the tail of its own boot messages instead of the
    # installation screen, which is its only way of being configured.
    #
    # The second failure was worse, because it is a priority inversion this
    # service creates itself. Everything ordered after this unit waits while
    # this function runs — two minutes and forty seconds on that boot, cold
    # cache — and this unit carries CPUWeight=5000 against polkit's default
    # 100 on a single core. So `hdmi_output.wake()`'s own `systemctl stop`
    # could not get polkit activated in time: "Failed to activate service
    # 'org.freedesktop.PolicyKit1': timed out" at 20:40:56, with polkit
    # finally coming up at 20:41:06. The box never took control of its own
    # output.
    #
    # What sending it last was meant to buy — TimeoutStartSec=300 covering
    # the slow work below — is real but theoretical: this board has been
    # killed mid-import from a cold page cache once. That gap is accepted.
    # Two demonstrated boot failures outrank one hypothetical one.
    notify_ready()
    start_watchdog_pings()
    # Proves the event loop is actually being scheduled, not just that the
    # process exists — see systemd_watchdog.py's own docstring for why a
    # ping thread alone cannot tell a stuck request handler from a healthy
    # one.
    loop_beat_task = asyncio.create_task(keep_loop_beat())
    media.startup()
    try:
        cec.get_cec_version()  # registers on the bus under the OSD name
    except CECError as exc:
        logger.warning("CEC unavailable at startup (harmless, retried on each call): %s", exc)
    # Said out loud at every start: during the September 2026 incident the box
    # had been running for hours with a configuration nobody had chosen, and
    # nothing in the journal said which one was in force.
    _open_the_screen_at_startup()
    # The Zigbee buttons are listened to inside this process, so a single
    # caller drives the CEC bus and cannot collide with HTTP requests. Started
    # after the installation-screen check above, so the mode is already
    # "installation" before any button press can arrive.
    mqtt_client = zigbee_bridge.start()
    yield
    zigbee_bridge.stop(mqtt_client)
    loop_beat_task.cancel()
    try:
        await loop_beat_task
    except asyncio.CancelledError:
        pass
    media.shutdown()
    cec.shutdown()


app = FastAPI(
    title="Maman TV Lite API",
    description="""Television control for a box with three buttons.

## 🔒 Authentication

Every route is password-protected (HTTP Basic). Credentials live in
`/etc/maman-tv-lite/api.env`, outside the repository; change them there and run
`sudo systemctl restart maman-api`. From the command line:
`curl -u user:password http://<host>:8000/state`.

⚠️ HTTP Basic encodes the password but does **not** encrypt it. On the local
network it guards against accidental access, not against someone listening to
that network. Published through a Cloudflare tunnel it travels inside HTTPS,
and guessing is throttled per caller.

That still leaves one password between the internet and `/system/shutdown`,
which needs someone to physically travel to the box to undo.

## 📺 One default state and three modes

The box is in **television** by default: it is not using the screen, the set
shows its own programmes, and the box's HDMI output is **asleep**. Three modes
take the screen, one at a time, and each is entered on request:

- **music** — music and photos
- **diagnostic** — the state of the box drawn on the television, for five
  minutes
- **installation** — a menu of guided procedures: CEC detection (working out
  how to drive a new television) and pairing the TV/Music buttons, with a
  human watching the screen and answering questions (`GET /installation`,
  `POST /installation/answer`)

Entering a mode wakes the output; leaving one puts it back to sleep and either
switches the set off or sends it back to its programmes. **It is never left
showing an input with nothing on it.**

The sleeping output is not a detail. A television that comes back on "the last
input used" cannot come back on an input that is silent when it wakes: it
falls back to its own programmes. That is why the box's output sleeps whenever
the box is idle, and it is the whole reason this arrangement exists.

## ⚙️ How the television is switched on and off

Two techniques, named in the box's own `tv.json`: one to wake the set, one
to switch it off. A press sends that frame, once, and nothing else. A fresh
installation uses the two most standard frames in the protocol — Image View On
and plain Standby — and says so: `GET /tv/config` reports where each answer
came from (`default`, `detection` or `manual`).

Nothing rewrites that file behind your back. An earlier version searched for a
working technique in the middle of an everyday press and recorded what it
believed; it settled on answers that did nothing, and an accidental press
could erase an evening's work. Changing a technique is now deliberate:
`PUT /tv/config/wake`, `PUT /tv/config/sleep`, or the installation mode's CEC
detection procedure.
""",
    version="0.2.0",
    lifespan=lifespan,
)


def _ask_for_credentials() -> JSONResponse:
    """The 401 that makes a browser show its password box."""
    return JSONResponse(
        status_code=401,
        content={"detail": "Credentials required."},
        headers={"WWW-Authenticate": f'Basic realm="{auth.REALM}"'},
    )


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    """Password-protect the WHOLE API (HTTP Basic).

    Applied as global middleware rather than per endpoint: with around twenty
    routes, one forgotten decorator would leave a hole. Any route added later
    is protected by default.

    Browsers show a login prompt, including on /docs; from a terminal use
    `curl -u user:password ...`.
    """
    key = auth.client_key(
        request.client.host if request.client else None,
        request.headers.get("cf-connecting-ip"),
    )

    # Checked before the password is even looked at: a caller being throttled
    # gets no information about whether their guess was close.
    waiting = auth.seconds_locked_out(key)
    if waiting > 0:
        logger.warning("Throttled %s on %s (%.0fs left)", key, request.url.path, waiting)
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many failed attempts. Try again in {waiting:.0f}s."},
            headers={"Retry-After": str(int(waiting) + 1)},
        )

    header = request.headers.get("Authorization")
    try:
        authorized = auth.is_authorized(header)
    except auth.AuthNotConfigured as exc:
        logger.error("Request refused on %s: %s", request.url.path, exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    if not authorized:
        # A request that carried no password at all is not a guess at one: it
        # is asked for the password and costs its caller nothing. Browsers do
        # this constantly — measured on the Pi 5 box, opening the Swagger page
        # from Safari asked for /apple-touch-icon.png four times with no
        # credentials, and each one counted, putting the owner four steps of
        # ten from being locked out of their own box. Guessing a password means
        # sending one, and that is still counted and still paused.
        if auth.parse_basic_header(header) is None:
            logger.info("No credentials offered from %s on %s (not counted)",
                        key, request.url.path)
            return _ask_for_credentials()
        count = auth.note_failure(key)
        logger.warning("Failed authentication from %s on %s (%d)",
                       key, request.url.path, count)
        # Awaited, not slept: this holds the caller, never the event loop, so
        # the buttons and everyone else keep working while an attacker waits.
        await asyncio.sleep(auth.AUTH_FAILURE_DELAY_SECONDS)
        return _ask_for_credentials()

    auth.note_success(key)
    return await call_next(request)


@app.exception_handler(CECError)
def cec_error_handler(request: Request, exc: CECError) -> JSONResponse:
    """Avoid a raw 500 when cec-ctl fails or hangs, for instance with the HDMI
    cable unplugged: report a clear error instead of a Python traceback."""
    logger.warning("CECError on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": f"TV unreachable over CEC: {exc}"},
    )


@app.exception_handler(SystemCommandError)
def system_error_handler(request: Request, exc: SystemCommandError) -> JSONResponse:
    """Same reasoning as for CECError: a refused system action must surface a
    readable error, not a raw 500."""
    logger.warning("SystemCommandError on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": f"System action not possible: {exc}"},
    )


@app.exception_handler(media.MediaError)
def media_error_handler(request: Request, exc: media.MediaError) -> JSONResponse:
    """Nothing to play, a folder that is not there, a player that is not
    running: the caller asked for something the box cannot do just now, which
    is a 409, not a crash."""
    logger.warning("MediaError on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# The family's remote control. Swagger can do everything and is no way to
# switch on somebody's television from a phone at the bus stop; this page does
# the four things a relative actually wants, and calls the same routes. It is
# behind the same password as everything else: the browser already holds it,
# so the fetches below need no credentials of their own.
_REMOTE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maman TV Lite</title>
<style>
  :root { color-scheme: light dark; --bg: #f6f4ef; --fg: #1d1d1b; --card: #fff;
          --accent: #2f6f4f; --muted: #6b6a66; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #161615; --fg: #ecebe7; --card: #232321; --muted: #a09f9a; }
  }
  body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
         font: 17px/1.4 system-ui, sans-serif; max-width: 480px; margin-inline: auto; }
  h1 { font-size: 1.3rem; margin: 8px 0 4px; }
  #state { color: var(--muted); margin: 0 0 20px; min-height: 1.4em; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  button { font: inherit; padding: 22px 12px; border: 0; border-radius: 14px;
           background: var(--card); color: var(--fg); cursor: pointer;
           box-shadow: 0 1px 3px rgb(0 0 0 / .15); }
  button.wide { grid-column: span 2; background: var(--accent); color: #fff; }
  .span { grid-column: span 2; }
  select { font: inherit; padding: 14px 12px; border: 0; border-radius: 14px;
           background: var(--card); color: var(--fg);
           box-shadow: 0 1px 3px rgb(0 0 0 / .15); }
  button:disabled { opacity: .5; }
  h2 { font-size: .9rem; text-transform: uppercase; letter-spacing: .05em;
       color: var(--muted); margin: 24px 0 8px; }
  footer { margin-top: 28px; font-size: .85rem; color: var(--muted); }
  a { color: inherit; }
</style>
</head>
<body>
<h1>Maman TV Lite</h1>
<p id="state">&nbsp;</p>
<div class="grid">
  <button class="wide" id="power" data-post="/tv/watch">Switch the TV on</button>
  <button data-post="/mode/music">Music &amp; photos</button>
  <button data-post="/tv/watch">TV</button>
</div>
<div id="folders" hidden>
<h2>One music folder</h2>
<div class="grid">
  <select class="span" id="folder" aria-label="Music folder"></select>
  <button class="span" data-post="/mode/music" data-with-folder>Play this folder</button>
</div>
</div>
<h2>While music plays</h2>
<div class="grid">
  <button data-post="/media/pictures/previous">Previous photo</button>
  <button data-post="/media/pictures/next">Next photo</button>
  <button data-post="/media/music/previous">Previous track</button>
  <button data-post="/media/music/next">Next track</button>
  <button data-post="/media/pictures/archive"
          data-confirm="Set this photo aside? It moves to the archive folder and is never deleted. Not straight after Previous photo: the box would set the one before aside.">Archive photo</button>
  <button data-post="/media/music/archive"
          data-confirm="Set this track aside? It moves to the archive folder and is never deleted.">Archive track</button>
</div>
<footer>Version __VERSION__ · <a href="/docs">Full API</a></footer>
<script>
const stateLine = document.getElementById("state");
let mode = "television", powerAnswer = "unknown";
async function refresh() {
  try {
    const s = await (await fetch("/state")).json();
    mode = s.mode;
    showPower();
    const what = {television: "Television", music: "Music and photos",
                  diagnostic: "Diagnostic screen",
                  installation: "Installation screen"}[s.mode] || s.mode;
    stateLine.textContent = "Now: " + what;
  } catch (e) { stateLine.textContent = "The box is not answering."; }
}
// The on/off button follows the set's own answer. Asked over CEC, which
// costs the bus a question, so only when the page opens, comes back into
// view, or has just done something — never on a timer. And only an explicit
// standby reads as off: a set that says nothing may well be on, so it gets
// the neutral label, and the tap then does what "TV" does.
//
// Switching off goes through the routes that already do it, so it uses the
// technique configured in tv.json: /tv/off on the programmes, and from the
// music /mode/television, which stops the players before the same standby.
// /mode/television alone sends nothing when the box is already on the
// programmes — it only puts the box's own output back to sleep.
const power = document.getElementById("power");
function showPower(answer) {
  if (answer !== undefined) powerAnswer = answer;
  answer = powerAnswer;
  if (answer === "on") {
    power.textContent = "TV is ON \u00b7 switch off";
    power.dataset.post = mode === "television" ? "/tv/off" : "/mode/television";
  } else {
    power.textContent = (answer === "standby" || answer === "off")
      ? "TV is OFF \u00b7 switch on" : "Switch the TV on";
    power.dataset.post = "/tv/watch";
  }
}
async function checkPower() {
  try {
    const r = await fetch("/tv/status");
    showPower(r.ok ? (await r.json()).power : "unknown");
  } catch (e) { showPower("unknown"); }
}
// Read from the disk each time, so a folder dropped on the share appears the
// next time the page comes into view. Hidden while there is none to choose.
const folderList = document.getElementById("folder");
async function loadFolders() {
  try {
    const r = await fetch("/media/folders");
    if (!r.ok) return;
    const chosen = folderList.value;
    folderList.replaceChildren(...(await r.json()).folders.map(name => {
      const option = document.createElement("option");
      option.value = option.textContent = name;
      return option;
    }));
    if (chosen) folderList.value = chosen;
    document.getElementById("folders").hidden = folderList.options.length === 0;
  } catch (e) { /* the section simply stays as it was */ }
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { refresh(); checkPower(); loadFolders(); }
});
for (const b of document.querySelectorAll("button[data-post]")) {
  b.addEventListener("click", async () => {
    // Archiving moves a file out of the slideshow or the playlist: a stray
    // tap must not be enough.
    if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
    b.disabled = true;
    stateLine.textContent = "Working on it...";
    try {
      let url = b.dataset.post;
      if ("withFolder" in b.dataset)
        url += "?folder=" + encodeURIComponent(folderList.value);
      const r = await fetch(url, {method: "POST"});
      if (!r.ok) {
        stateLine.textContent = (await r.json()).detail || ("Error " + r.status);
        return;
      }
      await refresh();
    } catch (e) { stateLine.textContent = "The box is not answering."; }
    finally { b.disabled = false; }
    // Twice: a set switching takes a few seconds to say where it ended up.
    checkPower();
    setTimeout(checkPower, 8000);
  });
}
refresh();
checkPower();
loadFolders();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def remote_control() -> str:
    return _REMOTE_PAGE.replace("__VERSION__", app.version)


class ModeResult(BaseModel):
    mode: str


# ============================================================================
# What the box is doing
# ============================================================================

@app.get("/state", tags=["state"],
         summary="Everything the box knows about itself, right now")
def box_state() -> dict:
    """The one object the API, the shell command and the television screen all
    render. They cannot disagree, because there is only one."""
    answer = state.snapshot()
    answer["hdmi_output"] = hdmi_output.state()
    answer["media"] = media.status()
    return answer


class LogLevel(str, Enum):
    """Offered as a list so the wrong value is refused by the schema rather than
    by a 400 somebody has to read — and so Postman and Swagger show the four
    that mean something. NOTSET and CRITICAL are left out on purpose: one reads
    as a level and behaves like a question, the other is indistinguishable from
    silence on a diagnostic logger."""

    debug = "DEBUG"
    info = "INFO"
    warning = "WARNING"
    error = "ERROR"


@app.get("/logging", tags=["logging"],
         summary="How much the box says about the CEC bus")
def logging_configuration() -> dict:
    """What is in force right now, and where it is written down."""
    answer = dict(log_config.load())
    answer["file"] = log_config.PATH
    answer["levels"] = list(log_config.LEVELS)
    answer["effective"] = logging.getLevelName(
        logging.getLogger(log_config.CEC_LOGGER).getEffectiveLevel())
    return answer


@app.put("/logging/cec", tags=["logging"],
         summary="Set how much the box says about the CEC bus")
def set_cec_logging(level: LogLevel = Query(
        ..., description="DEBUG records every frame and everything cec-ctl "
                         "printed about it, which is how a session is recorded "
                         "without putting a second process on the bus. INFO "
                         "keeps one line per frame. Applied at once — no "
                         "restart, which would destroy the evidence — and kept "
                         "across reboots.")) -> dict:
    """Takes effect immediately and survives a restart."""
    applied = dict(log_config.set_cec_level(level.value))
    applied["file"] = log_config.PATH
    return applied


@app.get("/report", tags=["state"],
         summary="Everything needed to diagnose the box from a distance")
def report() -> dict:
    """The state, plus what it costs a question on the CEC bus to answer.

    Separate from `/state` because this one talks to the television: it asks
    the adapter and the set for their version and their power state, which
    takes seconds and must never be on the path of something as ordinary as a
    screen refresh.
    """
    answer = box_state()
    answer["cec_adapter"] = cec.adapter_state()
    answer["television_identity"] = cec.television_identity()
    answer["television_power"] = cec._safe_power_status()
    return answer


# ============================================================================
# Modes
# ============================================================================

@app.post("/mode/television", tags=["modes"],
          summary="Leave whatever is running and go back to the programmes")
def mode_television(switch_off: bool = Query(
        True, description="Switch the set off (the default), or send it back "
                          "to its own programmes")) -> dict:
    return modes.television(why="asked over the API", switch_off=switch_off)


@app.post("/mode/music", tags=["modes"],
          summary="Music and photos: TV on, on the box's input, both playing")
def mode_music(folder: "str | None" = Query(
        None, description="Play one sub-folder of <media>/music instead of "
                          "everything. The photos are always the whole "
                          "library.")) -> dict:
    return modes.music(folder)


@app.post("/mode/diagnostic", tags=["modes"],
          summary="Draw the state of the box on the television for five minutes")
def mode_diagnostic() -> dict:
    """For whoever is standing in front of the set with no laptop.

    It ends by itself and switches the television off, so it can never be
    left on the box's input.
    """
    return modes.diagnostic()


@app.post("/mode/installation", tags=["modes"],
          summary="A menu of guided procedures: CEC detection, pairing the buttons")
def mode_installation() -> dict:
    """The place where the configuration is established, with a human
    watching the screen and answering questions — over the TV and Music
    buttons once they are bound, or `GET /installation` / `POST
    /installation/answer` before they are (the way the very first button
    ever gets paired, since nothing can answer with a button that does not
    exist yet).

    What CEC detection replaced — a search that ran in the middle of
    everyday presses and inferred success from silence on the bus — is
    gone: nothing outside this mode ever changes what the box knows about
    the television.

    Refused on a box installed without the buttons: every procedure on that
    screen is answered with them, and the first one pairs them. Such a box
    keeps the standard frames, or is configured by hand through
    `/tv/config` — see docs/without-buttons.md.
    """
    if not button_bindings.buttons_installed():
        raise HTTPException(
            status_code=409,
            detail="This box was installed without the Zigbee buttons, and the "
                   "installation screen is answered with them. Configure the "
                   "television through /tv/config instead: see "
                   "docs/without-buttons.md.")
    return modes.installation()


@app.get("/installation", tags=["modes"],
         summary="The current question of the installation procedure, if any")
def installation_question() -> dict:
    """Driven from a phone: poll this, answer with `POST /installation/answer`.

    `question` is `null` when nothing is running (not in the installation
    mode, or between two questions for an instant).
    """
    question = installation.channel.current()
    if question is None:
        return {"question": None}
    remaining = (None if question.seconds is None else
                max(0.0, question.seconds - (time.monotonic() - question.asked_at)))
    return {"question": {
        "id": question.id,
        "body": question.body,
        "accepts": question.accepts,
        "seconds": question.seconds,
        "seconds_remaining": remaining,
    }}


@app.post("/installation/answer", tags=["modes"],
          summary="Answer the current question of the installation procedure")
def installation_answer(
        id: int = Query(..., description="The question's id, from GET /installation"),
        token: str = Query(..., description="One of that question's accepted tokens, "
                           "e.g. tv or music")) -> dict:
    """An id that is not the current question's is dropped, not an error: it
    is how a late or duplicated answer is told apart from a real one."""
    return {"accepted": installation.channel.answer(id, token)}


@app.delete("/installation/buttons", tags=["modes"],
           summary="Forget both button bindings")
def installation_forget_buttons() -> dict:
    """Puts the box back into the state that makes it pair itself: the next
    time the installation screen opens (or, since `button_bindings.
    has_any_button()` is checked on every start, the very next restart) it
    skips straight to pairing the TV and Music buttons again, asking
    nothing, with no button required to reach that screen — exactly the
    state a box with no button at all is in already.
    """
    button_bindings.clear()
    return {"cleared": True}


# ============================================================================
# The television
# ============================================================================

@app.post("/tv/on", tags=["television"], summary="Switch the television on")
def tv_on() -> dict:
    return cec.power_on()


@app.post("/tv/off", tags=["television"], summary="Switch the television off")
def tv_off() -> dict:
    return cec.standby()


@app.post("/tv/toggle", tags=["television"],
          summary="What the tv button does: on, off, or back to the programmes")
def tv_toggle() -> dict:
    return modes.tv_button()


@app.post("/tv/watch", tags=["television"],
          summary="The television on, on its own programmes")
def tv_watch() -> dict:
    """What the phone page's "TV" does: leaves music and photos for the
    programmes, or wakes a set that is off. Never switches anything off, so
    pressing it twice changes nothing."""
    return modes.watch_television()


@app.get("/tv/status", tags=["television"],
         summary="The set's power state, asked over CEC")
def tv_status() -> dict:
    return {"power": cec.get_power_status()}


class TvConfigKind(str, Enum):
    """Which technique: switching on, switching off, or giving the viewer
    their programmes back when a mode ends. All three can be set by hand —
    a box installed without the buttons has no detection procedure to set
    them for it."""
    wake = "wake"
    sleep = "sleep"
    release = "release"


def _techniques(kind: str) -> "list[str]":
    """The names one kind accepts, best first. power_cycle is not in the
    release table — its mechanism is the box's own output, which only
    modes.py may touch — but it is the release that works on every set, so
    it closes the list."""
    if kind == "wake":
        return [name for name, _ in cec.WAKE_TECHNIQUES]
    if kind == "sleep":
        return [name for name, _ in cec.SLEEP_TECHNIQUES]
    return [name for name, _ in cec.RELEASE_TECHNIQUES] + [tv_config.DEFAULT_RELEASE]


# Built from the real tables rather than hand-listed, so Swagger's dropdown
# can never drift from what cec_controller.py actually implements. One enum
# for all three kinds (a technique name is unique to its own table — no wake
# name doubles as a sleep or a release name), which is what puts every valid choice into
# a single combo list; set_tv_configuration() below still checks a name
# against the *right* table for the kind given, since being a real
# technique name is not the same as being valid for this direction.
CecTechnique = Enum(
    "CecTechnique",
    {name: name for name in sorted(set(_techniques("wake")) | set(_techniques("sleep"))
                                   | set(_techniques("release")))},
    type=str)

# Likewise built from tv_config.py's own tuple, not duplicated here.
HdmiSleepMethod = Enum("HdmiSleepMethod",
                       {m: m for m in tv_config.HDMI_SLEEP_METHODS}, type=str)


@app.get("/tv/config", tags=["television"],
         summary="The three techniques in force, and where they came from")
def tv_configuration() -> dict:
    configuration = tv_config.load()
    configuration["available"] = {
        "wake": _techniques("wake"),
        "sleep": _techniques("sleep"),
        "release": _techniques("release"),
        "hdmi_sleep": list(tv_config.HDMI_SLEEP_METHODS),
    }
    configuration["file"] = tv_config.PATH
    return configuration


@app.delete("/tv/config", tags=["television"],
           summary="Forget the wake/sleep/release techniques and the recorded television")
def clear_tv_configuration() -> dict:
    """Puts the box back into the state a box nobody has ever run the CEC
    detection on is in. Does **not** by itself force the installation
    screen open at the next start — that is governed entirely by whether a
    button is bound, not by the CEC configuration — so a box with working
    buttons stays quietly on the default frames until "Set up the TV
    (CEC)" is opened deliberately from the menu. `hdmi_sleep` and the
    detection timings are left as they are — see `tv_config.clear()`'s own
    docstring.
    """
    return tv_config.clear()


@app.put("/tv/config/hdmi-sleep", tags=["television"],
         summary="How the box stops driving the television's screen")
def set_hdmi_sleep(method: HdmiSleepMethod = Query(
        ..., description="blank (the default), connector, or none")) -> dict:
    """`blank` puts the output to sleep and leaves the connector alone;
    `connector` forces the connector off, which makes the kernel forget the
    set's EDID and can leave the box unable to send a CEC frame to a
    television that unplugs its input while it sleeps — change it only for a
    set that ignores blanking. `none` never puts the output to sleep at all;
    see `tv_config.py`'s own note on what that trades away.

    Registered *before* the parameterised `/tv/config/{kind}` route below,
    deliberately: Starlette matches routes in registration order, not by
    how specific they are, so a `{kind}` route registered first would
    silently swallow every request here, taking `kind="hdmi-sleep"` and
    failing its own "must be wake or sleep" check instead. That is exactly
    what this route did until this fix — unreachable over HTTP, 404 on
    every call, and nothing caught it because the only test coverage was
    against `tv_config.set_hdmi_sleep()` directly, never through the actual
    router. `TestTheConfiguration` now calls both routes through the real
    FastAPI `TestClient`, the way a real caller does.
    """
    configuration = tv_config.set_hdmi_sleep(method.value)
    state.note("configuration changed", hdmi_sleep=method.value)
    return configuration


@app.put("/tv/config/tv-button-switches-off", tags=["television"],
        summary="What the tv button does while leaving a mode")
def set_tv_button_switches_off(enabled: bool = Query(
        ..., description="False (the default): give the programmes back. "
                         "True: switch off outright, the same as the music "
                         "button already does — simpler to predict at the "
                         "cost of an extra press to see the television "
                         "again.")) -> dict:
    """Registered before `/tv/config/{kind}` for the same reason
    `hdmi-sleep` above is: a literal path must come before a parameterised
    one, or Starlette's own routing swallows it."""
    configuration = tv_config.set_tv_button_switches_off(enabled)
    state.note("configuration changed", tv_button_switches_off=enabled)
    return configuration


@app.put("/tv/config/{kind}", tags=["television"],
         summary="Set one technique: wake, sleep or release")
def set_tv_configuration(kind: TvConfigKind, technique: CecTechnique = Query(
        ..., description="One of the names listed by GET /tv/config")) -> dict:
    """Deliberate, and recorded as `manual`.

    The box never changes these by itself: that is what made an installation
    switch a television off by claiming its HDMI input, so the set came back
    on the box at every wake.
    """
    known = _techniques(kind.value)
    if technique.value not in known:
        raise HTTPException(status_code=422,
                            detail=f"unknown {kind.value} technique {technique.value!r}; "
                                   f"one of {known}")
    configuration = tv_config.set_technique(kind.value, technique.value, source="manual")
    state.note("configuration changed", kind=kind.value, technique=technique.value)
    return configuration


@app.post("/tv/config/television", tags=["television"],
          summary="Record which television this configuration was made for")
def record_television() -> dict:
    """Reads the set's own identity over HDMI and CEC and stores it.

    Nothing decides anything from it. It answers "was this configuration made
    for the set in front of me?", which nobody could answer during the
    September 2026 incident.
    """
    identity = cec.television_identity()
    tv_config.set_television(identity)
    return identity


# ============================================================================
# Music and photos
# ============================================================================
# The mode itself is under /mode. What is left here is what only an API can
# offer: changing track, and setting aside a photo the owner does not want to
# see again.

@app.get("/media/status", tags=["media"],
         summary="Whether music and photos are playing, and what there is to play")
def media_status() -> dict:
    return media.status()


@app.get("/media/folders", tags=["media"],
         summary="The music folders that can be played on their own")
def media_folders() -> dict:
    return {"folders": media.music_folders(), "media_dir": media.MEDIA_DIR}


@app.get("/media/config", tags=["media"],
         summary="The music mode's own volume")
def media_configuration() -> dict:
    return media_config.load()


@app.put("/media/config/volume", tags=["media"],
        summary="Set the music mode's own volume")
def set_media_volume(percent: float = Query(
        ..., ge=media_config.MIN_VOLUME, le=media_config.MAX_VOLUME,
        description="0-100, mpg123's own gain — independent of the "
                    "television's own volume, which CEC cannot set in "
                    "absolute terms anyway.")) -> dict:
    return media.set_volume(percent)


@app.post("/media/music/next", tags=["media"], summary="Next track")
def media_music_next() -> dict:
    return media.skip("music")


@app.post("/media/music/previous", tags=["media"], summary="Previous track")
def media_music_previous() -> dict:
    return media.skip("music", backwards=True)


@app.post("/media/pictures/next", tags=["media"],
          summary="Next photo, without waiting for its turn")
def media_pictures_next() -> dict:
    return media.skip("pictures")


@app.post("/media/pictures/previous", tags=["media"],
          summary="Previous photo (see the note on setting one aside)")
def media_pictures_previous() -> dict:
    return media.skip("pictures", backwards=True)


@app.post("/media/music/archive", tags=["media"],
          summary="Set the track being played aside and move on")
def media_music_archive() -> dict:
    """Moves the file to `<media>/archive/music` — nothing is deleted, so a
    mistake is one move back through the file share."""
    return media.archive_current("music")


@app.post("/media/pictures/archive", tags=["media"],
          summary="Set the photo on screen aside and move on")
def media_pictures_archive() -> dict:
    """Moves the file to `<media>/archive/pictures` — nothing is deleted, so a
    mistake is one move back through the file share.

    **Known fault: do not use this straight after "previous photo."** Which
    photo is on screen is learned by watching the kernel report fbi opening it,
    and fbi does not re-open a photo it still has in memory. Going back
    therefore produces no event, the box still names the photo it was on before
    the step back, and this would set THAT one aside instead. Going forward is
    unaffected. Show one more photo before setting one aside, or step forward
    rather than back.
    """
    return media.archive_current("pictures")



# ============================================================================
# The machine
# ============================================================================

class SystemActionResult(BaseModel):
    action: str
    ok: bool
    detail: str


@app.post(
    "/system/reboot",
    tags=["system"],
    summary="Reboot the machine",
)
def system_reboot() -> SystemActionResult:
    """Reboot the box. The API is unreachable during the restart and comes
    back on its own, since the services use `Restart=always`.

    The command is delayed by a few seconds so this HTTP response can leave
    before the machine goes down; otherwise the client could not tell whether
    the request was accepted.
    """
    system.reboot()
    return SystemActionResult(
        action="reboot",
        ok=True,
        detail=f"Rebooting in {system.ACTION_DELAY_SECONDS:.0f} s.",
    )


@app.post(
    "/system/shutdown",
    tags=["system"],
    summary="⚠️ Power the machine off (physical restart required)",
)
def system_shutdown() -> SystemActionResult:
    """⚠️ **Use only with full awareness.** Once off, the box can only be
    started again on site by unplugging and replugging it: a Raspberry Pi has
    no power button and no Wake-on-LAN by default. Triggering this remotely
    means losing access until someone physically travels there.

    To simply return to a healthy state, prefer `/system/reboot`.
    """
    system.shutdown()
    return SystemActionResult(
        action="shutdown",
        ok=True,
        detail=(
            f"Powering off in {system.ACTION_DELAY_SECONDS:.0f} s. "
            "A physical restart will be required."
        ),
    )



# --- The music folders, as a list to pick from in the Swagger page -----------
#
# The choices are the folders on the disk, so they cannot live in the code: a
# folder dropped on the share has to show up without restarting the service.
# FastAPI builds the API description once and keeps it, so it is rebuilt at
# every request here — only the Swagger page ever asks for it.
_describe_api = app.openapi


def _openapi_with_what_is_on_the_disk() -> dict:
    schema = _describe_api()
    folders = media.music_folders()
    if folders:
        for parameter in schema.get("paths", {}).get("/mode/music", {}) \
                .get("post", {}).get("parameters", []):
            if parameter.get("name") != "folder":
                continue
            target = parameter.setdefault("schema", {})
            # An optional parameter's schema is a choice between a string and
            # null; the list of folders belongs on the string side.
            for branch in target.get("anyOf", [target]):
                if branch.get("type") == "string":
                    branch["enum"] = folders
    app.openapi_schema = None
    return schema


app.openapi = _openapi_with_what_is_on_the_disk
