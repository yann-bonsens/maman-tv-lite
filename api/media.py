# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Music and photos on the television, on a board with one slow core.

Both lists are the box's own: shuffled here, held here, and played one item at
a time. That is what makes "what is playing", "next", "previous" and "set this
one aside" possible at all — a player that owned the list could answer none of
them.

What draws and what sounds are deliberately small. `mpg123` for a track,
`fbi` for a photo. mpv did both before, ported from the Pi 5 box where it is
the right answer, and on this board it cost four to five times as much:
decoding one track was 64 seconds of CPU against 14, and putting one photo on
screen 2.3 seconds against 0.55. The whole mode came to 54% of the single core,
which is what broke the music into ALSA underruns. It also lost one photo in
three: mpv reported the frame displayed while the display plane kept the
previous one, confirmed against the kernel's own state, and with both of its
video outputs.

The photo is not drawn from here, and that is a permission rather than a
choice: `fbi` needs a console, every console on this board is root:root 0600
and held by agetty, and no group grants access to it. The diagnostic screen is
root and already owns that console, so it draws the photo and this asks it to —
see PHOTO_REQUEST.
"""

import logging
import ctypes
import os
import random
import shutil
import struct
import subprocess
import threading
import time

import cec_controller as cec
import media_config
import screen

logger = logging.getLogger(__name__)

MEDIA_DIR = os.environ.get("MAMAN_MEDIA_DIR", "/medias")
PICTURES_DIR = os.path.join(MEDIA_DIR, "pictures")
MUSIC_DIR = os.path.join(MEDIA_DIR, "music")
# Where a photo or a track set aside goes. Beside what is played, never inside
# it, so nothing archived comes back in the next round. Moved, never deleted:
# sorting a library from an armchair means mistakes, and a mistake must cost
# nothing more than moving the file back.
ARCHIVE_DIR = os.path.join(MEDIA_DIR, "archive")

# Longer than the ten seconds of the Pi 5 box: this board decodes a photo from
# a camera in seconds, not milliseconds, and a slower turn leaves the processor
# time to breathe between two — which is also what keeps the Zigbee dongle's
# serial link alive under load.
PHOTO_SECONDS = float(os.environ.get("MAMAN_PHOTO_SECONDS", "15"))

# Left to ALSA's own default, which on this board is the HDMI output the
# television is on. Forced with MAMAN_AUDIO_DEVICE when a box has something
# else plugged in.
FORCED_AUDIO_DEVICE = os.environ.get("MAMAN_AUDIO_DEVICE", "")

# NOTE, and it is a gap rather than a setting: MAMAN_AUDIO_WAIT_SECONDS is
# gone. It fed mpv's --audio-wait-open, which with --audio-stream-silence held
# the first note back until the television had locked onto the HDMI stream —
# measured on the Pi 5 box, the room stayed silent for about ten seconds while
# mpv had been playing for three, so every first track began mid-phrase.
# mpg123 has no equivalent, and nothing replaces it: the first track of a
# session may still lose its opening seconds on a set that is slow to lock on.
# Tracks after it are safe, because the sound card stays open between them.

RUNTIME_DIR = os.environ.get("RUNTIME_DIRECTORY", "/run/maman-tv-lite")

# How long a photo is given to reach the screen before the slideshow gives up
# on it and says so. Measured with fbi on a prepared photo: about half a
# second. The cap only has to be comfortably past the worst honest case — it is
# there so that one unreadable photo cannot stall the slideshow in silence.
# Only to count what there is to play, and to build the photo list. HEIC is NOT
# in it: nothing on this board can decode it. scripts/prepare-photos.sh
# converts a library on a Mac before it is copied over, and turns the photos
# upright while it is there — fbi does not read the rotation tag either.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".ppm"}
# What mpg123 can actually play, and nothing more. It decodes MPEG audio only;
# a FLAC or an m4a offered to it is an error per track, played as a gap and a
# line in the log. Left out of the count instead, so the number of tracks the
# box reports is the number it can play.
AUDIO_EXTENSIONS = {".mp3", ".mp2", ".mpga"}

STOP_TIMEOUT_SECONDS = 3

# The screen looks for a requested key once a second, and fbi then has a photo
# to decode. This is how long "next" waits before naming the new photo.
PHOTO_KEY_SETTLE_SECONDS = 2.5

# How hard to try when the music player dies under us, and how long to wait
# between tries. Multiplied by the attempt, so the last wait is the longest.
MUSIC_REVIVE_ATTEMPTS = 5
MUSIC_REVIVE_BACKOFF_SECONDS = 2.0
# The music player must not be started twice: _start_music() asks for one, and
# so can _revive_music() recovering from a crash while the mode is still up.
_music_player_lock = threading.RLock()

# ============================================================================
# Watching the television while music and photos play
# ----------------------------------------------------------------------------
# The two buttons stop the players themselves. A television switched off any
# other way — its own remote above all — tells the box nothing, and the players
# would carry on: music into a set that is off, photos for nobody, and a viewer
# who switches the set back on landing in the middle of the slideshow instead
# of on their programmes (measured on the Pi 5 box, 2026-09-20).
#
# So while something is playing, the box asks the set how it is, every
# WATCH_INTERVAL_SECONDS. Only ever a question, never an order: re-sending to a
# set that is shutting down wakes it back up, which is the failure the whole
# CEC side exists to avoid.
#
# Two signals, because no single one works on every set: the set says it is off
# or on its way off, or it stops answering a bus that was answering a moment
# ago (the target HIGH ONE cuts its CEC circuit in standby; the Samsung used
# for development keeps answering and says "in transition from on to standby"
# for ever). Two readings in a row before acting, so one hiccup on the bus does
# not cut the music mid-track — and if it ever did, one press starts it again.
# ============================================================================
WATCH_INTERVAL_SECONDS = float(os.environ.get("MAMAN_TV_WATCH_SECONDS", "30"))
WATCH_OFF_READINGS = 2
# What "the set is off" reads like. "unknown" is deliberately not here: silence
# only counts once the bus has answered at least once (see _watch_television).
POWER_STATES_OFF = ("standby", "off", "in transition from on to standby")

_lock = threading.RLock()
_music: "subprocess.Popen | None" = None
# Whether the standing music player has been given something to play. It can
# no longer be read from the process, which now outlives every press.
_music_playing = False
# The shuffled tracks, and where we are in them. mpg123 is told one at a time.
_tracks: "list[str]" = []
_music_at = 0
_watcher: "threading.Thread | None" = None
_watch_stop = threading.Event()
_folder_playing: "str | None" = None

# The photo slideshow: our own list and our own place in it. There is no photo
# player here — the screen draws it; see PHOTO_REQUEST.
_photos: "list[str]" = []
_photo_stop = threading.Event()


class MediaError(Exception):
    """There is nothing to play, or a player could not be started or reached."""


# --- What there is to play ---------------------------------------------------

def _files(directory: str, extensions: set) -> "list[str]":
    """Everything worth playing under a folder, however deep.

    Files at the top and files in sub-folders are one single list: the owner
    may keep albums in folders or drop everything in loose, and both play
    shuffled together. Names starting with a dot are left out — a Mac leaves
    "._photo.jpg" companions beside copied files, which carry a photo's
    extension and hold no photo.
    """
    found = []
    for root, _, names in os.walk(directory):
        for name in names:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in extensions:
                found.append(os.path.join(root, name))
    return found


def music_folders() -> "list[str]":
    """Sub-folders of <media>/music holding music, relative to it.

    What the API offers as a choice, read from the disk each time: a folder
    dropped on the share is there at once, with nothing written down.
    """
    folders = []
    for root, _, names in os.walk(MUSIC_DIR):
        if root == MUSIC_DIR:
            continue
        if any(not name.startswith(".")
               and os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS
               for name in names):
            folders.append(os.path.relpath(root, MUSIC_DIR))
    return sorted(folders)


def _music_dir_for(folder: "str | None") -> str:
    """The music folder to play, checked against what is really there.

    Compared with the list rather than cleaned up: a name that is not one of
    the folders on the disk is refused, so nothing can point the player outside
    the media folder.
    """
    if not folder:
        return MUSIC_DIR
    if folder not in music_folders():
        raise MediaError(f"no such music folder: {folder!r}")
    return os.path.join(MUSIC_DIR, folder)


# --- The music, through mpg123 --------------------------------------------------

def _music_command() -> "list[str]":
    """The music player: mpg123, in its remote mode.

    Not mpv, and the difference is not a preference. Measured on this board,
    decoding one whole track cost mpv 64 seconds of CPU and mpg123 14 — four
    and a half times less, which on a single core is the difference between a
    mode that works and one that stutters. Four ways of making mpv cheaper were
    tried first and all four failed: the file's bitrate changes nothing, its
    fixed-point decoder costs the same, forcing integer output is worse, and
    nothing was being resampled.

    Remote mode (-R) reads commands on stdin and reports on stdout. It is the
    only shape that gives both control and continuity: mpg123 will not play a
    list of its own while in remote mode, and its terminal control keys need a
    terminal, which a service has not got. Driving it track by track costs
    nothing — measured, the sound card stays RUNNING straight across the load
    of the next track, so the television never has to re-lock onto the stream.
    """
    command = ["mpg123", "-R", "-q"]
    if FORCED_AUDIO_DEVICE:
        command += ["-a", FORCED_AUDIO_DEVICE.replace("alsa/", "")]
    return command


def _say_to_music(order: str) -> None:
    """One line to the music player's stdin."""
    if _music is None or _music.poll() is not None:
        raise MediaError("the music player is not running")
    try:
        _music.stdin.write(order + "\n")
        _music.stdin.flush()
    except OSError as exc:
        raise MediaError(f"cannot reach the music player: {exc}") from exc


def _follow_music(process: subprocess.Popen) -> None:
    """Read what mpg123 says; carry the list on, and notice if it dies.

    `@P 0` is the end of a track, and reacting to it is the main reason this
    thread exists: mpg123 in remote mode plays exactly what it is told and then
    stops, so somebody has to say what comes next — and that somebody is the
    box, because the box owns the shuffled list.

    `SILENCE` is sent at startup or this would be 2189 lines a minute instead
    of 12: mpg123 reports every frame otherwise.

    Anything that is not one of its `@` messages is mpg123 complaining, and it
    is logged. Dropping those was a real fault: the player died with the sound
    card taken by something else, the music stopped, the box went on showing
    photos as though nothing had happened, and the journal said nothing at all.
    """
    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        if line.startswith("@P 0"):
            if _music_playing:
                _play_next_track()
        elif line.startswith("@E"):
            logger.warning("music: %s", line[2:].strip())
            if _music_playing:
                _play_next_track()
        elif not line.startswith("@"):
            logger.warning("music: %s", line)

    # The stream ended, so the player is gone.
    if _music_playing:
        logger.warning("the music player died while it was playing; restarting")
        _revive_music()


def _revive_music() -> None:
    """Bring the music back after the player died under it.

    A box whose music stops without a word is the failure this project refuses:
    nobody investigates a thing that merely went quiet. Backed off and capped,
    because a player that cannot start at all — the sound card taken by
    something else, say — must not be restarted in a tight loop on a board
    with one core.
    """
    for attempt in range(1, MUSIC_REVIVE_ATTEMPTS + 1):
        if not _music_playing:
            return
        if _sleep_unless_stopped(attempt * MUSIC_REVIVE_BACKOFF_SECONDS):
            return
        try:
            _ensure_music_player()
            with _lock:
                # Back to the track it died on rather than the one after.
                global _music_at
                _music_at = max(0, _music_at - 1)
            _play_next_track()
            logger.info("the music is back (attempt %d)", attempt)
            return
        except MediaError as exc:
            logger.warning("could not bring the music back (attempt %d): %s",
                           attempt, exc)
    logger.error("the music player will not start; giving up after %d attempts",
                 MUSIC_REVIVE_ATTEMPTS)


def _sleep_unless_stopped(seconds: float) -> bool:
    """Wait, unless somebody asks for silence meanwhile. True means stop."""
    return _photo_stop.wait(seconds) and not _music_playing


def _play_next_track() -> None:
    """Put the track at our place in the list on, and move the place along."""
    global _music_at
    with _lock:
        if not _tracks:
            return
        _music_at %= len(_tracks)
        track = _tracks[_music_at]
        _music_at += 1
    try:
        _say_to_music(f"LOAD {track}")
    except MediaError as exc:
        logger.warning("could not put %s on: %s", track, exc)


def _ensure_music_player() -> None:
    """Start the music player if it is not up.

    mpg123 starts in milliseconds, unlike the twenty seconds mpv needed, so
    none of the machinery that used to hide that startup is needed here.
    """
    global _music
    with _music_player_lock:
        if _music is not None and _music.poll() is None:
            return
        try:
            process = subprocess.Popen(_music_command(), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True,
                                       bufsize=1)
        except OSError as exc:
            raise MediaError(f"cannot start mpg123: {exc}") from exc
        _music = process
        threading.Thread(target=_follow_music, args=(process,),
                         name="music-follow", daemon=True).start()
        _say_to_music("SILENCE")
        # Applied on every (re)start rather than once at the API's own
        # startup: the player is not a standing process any more (see
        # _stop_players()'s own note on why), so this is the one place a
        # fresh mpg123 always passes through, including a revive after it
        # died mid-track.
        _say_to_music(f"VOLUME {media_config.load()['volume']}")
        logger.info("music player up")


def _start_music(music_dir: str) -> None:
    """Shuffle the folder and start on the first track.

    The list is the box's, exactly as the photos' list is, and for the same
    reason: "what is playing", "next" and "set this one aside" all need the box
    to know what it chose. mpg123 is never shown more than one track.
    """
    global _tracks, _music_at, _music_playing
    tracks = _files(music_dir, AUDIO_EXTENSIONS)
    if not tracks:
        raise MediaError(f"no music in {music_dir}")
    random.shuffle(tracks)
    with _lock:
        _tracks, _music_at = tracks, 0
    _ensure_music_player()
    _music_playing = True
    _play_next_track()
    logger.info("music started from %s: %d tracks in random order",
                music_dir, len(tracks))


def _silence_music() -> None:
    """Stop the sound without stopping the player."""
    global _music_playing, _tracks
    _music_playing = False
    with _lock:
        _tracks = []
    if _music is None or _music.poll() is not None:
        return
    try:
        _say_to_music("STOP")
    except MediaError as exc:
        logger.warning("could not silence the music player: %s", exc)


# The API does not draw the photos and does not pace them: it writes the list
# it wants shown, and the diagnostic screen — which is root and owns the
# console — hands that list to fbi.
#
# Two separate reasons, and the second is the one that shaped this. fbi needs a
# console, and every console on this board is root:root 0600, held by agetty,
# with no group access; the service account cannot open one whatever groups its
# unit is given. And a fresh fbi per photo retakes the console every time,
# which BLANKS THE SCREEN for about eight tenths of a second before it paints —
# measured by sampling the framebuffer every 200 ms. Given the whole list, fbi
# never retakes it: the next photo is decoded while the current one is up, and
# they are swapped. Timed on the board at exactly one photo every fifteen
# seconds, and lighter than anything driven from here.
#
# The price is paid knowingly: fbi owns the list, so the box cannot say which
# photo is on screen. "Next", "previous" and "set this one aside" therefore do
# nothing for photos, and say so rather than pretending.
SLIDESHOW_REQUEST = os.path.join(RUNTIME_DIR, "slideshow")
SLIDESHOW_RUNNING = os.path.join(RUNTIME_DIR, "slideshow-running")
# One character for the screen to press in the slideshow. The consoles belong
# to root and this does not.
# Named, not spelled. Which character fbi listens to belongs to the screen,
# which is the only thing that can push one into its console.
PHOTO_NEXT, PHOTO_PREVIOUS = "next", "previous"

# Which photo is on the television, learned from the kernel rather than
# counted here — see _follow_what_is_shown().
_photo_showing: "str | None" = None

IN_OPEN = 0x00000020


def _ask_for_slideshow(photos: "list[str] | None") -> None:
    """Hand the screen a list of photos to show, or take the screen back.

    The list is written beside and renamed, never written in place: the screen
    must never catch half of it. Then the screen is woken through its pipe —
    it is not watching this file, it is blocked waiting to be asked, which is
    what removed the per-second loop that used to cost 2% of this core for the
    whole life of the machine.
    """
    if not photos:
        for path in (SLIDESHOW_REQUEST, SLIDESHOW_RUNNING):
            try:
                os.remove(path)
            except OSError:
                pass
        screen.off()
        return
    os.makedirs(os.path.dirname(SLIDESHOW_REQUEST), exist_ok=True)
    temporary = f"{SLIDESHOW_REQUEST}.new"
    with open(temporary, "w", encoding="utf-8") as listing:
        listing.write("\n".join(photos) + "\n")
    os.replace(temporary, SLIDESHOW_REQUEST)
    screen.photos()


def _slideshow_is_up() -> bool:
    return os.path.exists(SLIDESHOW_RUNNING)


def _follow_what_is_shown(directory: str) -> None:
    """Learn which photo is on the television, by watching the kernel.

    fbi owns the list and will not say what it is showing; it does not even
    keep the file open — measured, /proc/<pid>/fd holds nothing between turns.
    But it opens each photo at the moment it puts it up, and the kernel
    reports that: one IN_OPEN per turn, exactly at the change (measured at
    7-second intervals with a 6-second turn). So the box knows the current
    photo without counting anything and without a timer.

    This holds only while fbi is NOT reading ahead. With -readahead it would
    open the next photo while the current one is still up, and every answer
    here would be one photo early. The slideshow is started without it, and
    that is not an accident.

    KNOWN FAULT, and the reason is here rather than hidden: fbi does not
    re-open a photo it still has in memory, so stepping BACK produces no event
    at all and this keeps naming the photo before the step. Setting one aside
    straight after "previous" would therefore move the wrong file. Stepping
    forward is unaffected. `-cachemem 0` was tried and does not help — tested,
    the backward step still produced no open.
    """
    global _photo_showing
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    notify = libc.inotify_init()
    if notify < 0:
        logger.warning("cannot watch the photos: the box will not know which "
                       "one is on screen")
        return
    if libc.inotify_add_watch(notify, directory.encode(), IN_OPEN) < 0:
        logger.warning("cannot watch %s: %s", directory,
                       os.strerror(ctypes.get_errno()))
        os.close(notify)
        return
    try:
        while True:
            data = os.read(notify, 4096)
            at = 0
            while at + 16 <= len(data):
                _, _, _, length = struct.unpack_from("iIII", data, at)
                name = data[at + 16:at + 16 + length].split(b"\0", 1)[0]
                at += 16 + length
                if name:
                    _photo_showing = os.path.join(directory,
                                                  name.decode("utf-8", "replace"))
    except OSError as exc:
        logger.warning("stopped watching the photos: %s", exc)
    finally:
        os.close(notify)


def _press_in_the_slideshow(key: str) -> None:
    """Ask the screen to press a key in fbi.

    The key itself goes down the pipe. Consoles belong to root, so only the
    screen can push a character into the one fbi is reading.
    """
    screen.press(key)


def _close_player(process: "subprocess.Popen | None") -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _close_music_player() -> None:
    global _music, _music_playing
    process, _music = _music, None
    _music_playing = False
    _close_player(process)


_watcher_started = False


def _watch_what_is_shown() -> None:
    """Start following what fbi puts up, once for the life of the process."""
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True
    threading.Thread(target=_follow_what_is_shown, args=(PICTURES_DIR,),
                     name="photos-watch", daemon=True).start()


def _start_photos(photos: "list[str]") -> None:
    """Put the whole library on the screen and let fbi walk it.

    There is no slideshow thread here any more, and no per-photo pacing: fbi
    shuffles and turns the pages itself, which is what removed the black gap
    between photos and most of the cost. See SLIDESHOW_REQUEST.
    """
    global _photos, _photo_showing
    _photos = list(photos)
    # Forgotten deliberately. It names the last photo of the PREVIOUS session
    # until the kernel reports fbi opening the first of this one, and in that
    # gap photos_playing() is already true — so setting one aside would have
    # moved a photo from the session before.
    _photo_showing = None
    _photo_stop.clear()
    _watch_what_is_shown()
    _ask_for_slideshow(_photos)
    logger.info("photos started: %d in random order, %.0f s each",
                len(_photos), PHOTO_SECONDS)


def _stop_photos() -> None:
    global _photos
    _photo_stop.set()
    _photos = []
    _ask_for_slideshow(None)


def photos_playing() -> bool:
    """Whether the screen is showing photos.

    Asked of the screen, not of a thread of our own: it is the screen that
    runs fbi, and a box that believed it was showing photos while nothing was
    on the television would be worse than one that admits it does not know.
    """
    return bool(_photos) and _slideshow_is_up()


def music_playing() -> bool:
    """Whether music is actually coming out, not whether a player exists.

    The player is started once and kept for the life of the API, so its being
    alive says nothing. Both halves are checked: the flag alone would lie if
    the player had died underneath it.
    """
    return _music_playing and _music is not None and _music.poll() is None


def is_playing() -> bool:
    return music_playing() or photos_playing()


def _television_went_off(status: str, bus_answered: bool) -> bool:
    """Read one power state as "the set is off", or not.

    Silence is the awkward one: a set that cut its CEC circuit going into
    standby cannot answer, but neither can an unplugged cable or a bus that
    hiccuped. It counts only once the set has answered during this session.
    """
    if status in POWER_STATES_OFF:
        return True
    return status == "unknown" and bus_answered


def _watch_television() -> None:
    """Stop the players once the television has gone off without being asked."""
    bus_answered = False
    off_readings = 0
    while not _watch_stop.wait(WATCH_INTERVAL_SECONDS):
        if not is_playing():
            return
        status = cec._safe_power_status()
        if status not in ("unknown", ""):
            bus_answered = True
        if _television_went_off(status, bus_answered):
            off_readings += 1
        else:
            off_readings = 0
        if off_readings >= WATCH_OFF_READINGS:
            logger.info("the television is off (%r, %d readings): leaving the "
                        "music mode", status, off_readings)
            # Imported here: the mode machine drives this module, and a module
            # cycle would be a poor price for one call.
            import modes
            modes.television("the television was switched off by hand")
            return


def _start_watcher() -> None:
    global _watcher
    _watch_stop.clear()
    _watcher = threading.Thread(target=_watch_television, name="tv-watch", daemon=True)
    _watcher.start()


def _stop_watcher() -> None:
    global _watcher
    _watch_stop.set()
    watcher, _watcher = _watcher, None
    # Never join itself: the watcher is what calls stop() when it finds the set
    # off, and stop() comes through here.
    if (watcher is not None and watcher.is_alive()
            and watcher is not threading.current_thread()):
        watcher.join(timeout=STOP_TIMEOUT_SECONDS)


# --- Changing what is playing ------------------------------------------------

def now_playing(kind: str) -> "str | None":
    """The file that player is on, as an absolute path."""
    if kind == "pictures":
        # What the kernel saw fbi open, not what this counted: fbi owns the
        # order and never says a word. Read without the lock, because the
        # status route must never wait behind a press.
        showing = _photo_showing
        if not showing or not photos_playing():
            return None
        return showing
    # The track the box chose, not one the player is asked about: it is the
    # box that owns the list. Read without the lock, like the photos above,
    # because the status route must never wait behind a press.
    tracks, at = _tracks, _music_at
    if not tracks or not music_playing():
        return None
    return tracks[(at - 1) % len(tracks)]


def skip(kind: str, backwards: bool = False) -> dict:
    """Next (or previous) file on that player, staying in the same shuffle."""
    if kind == "pictures":
        if not photos_playing():
            raise MediaError("the photos are not playing")
        _press_in_the_slideshow(PHOTO_PREVIOUS if backwards else PHOTO_NEXT)
        # The screen looks for the key once a second, and fbi then has the
        # photo to decode. Long enough that the answer names the new one.
        time.sleep(PHOTO_KEY_SETTLE_SECONDS)
    else:
        global _music_at
        if not music_playing():
            raise MediaError("the music is not playing")
        with _lock:
            # _music_at already points past the track being played, so going
            # back means stepping over both it and the one before.
            _music_at += -2 if backwards else 0
        _play_next_track()
    try:
        playing = now_playing(kind)
    except MediaError:
        playing = None
    logger.info("%s moved %s: %s", kind, "back" if backwards else "on", playing)
    return {"player": kind, "now_playing": playing}


def _free_name(folder: str, name: str) -> str:
    """A path in `folder` that is not taken, keeping the original name when it
    is free: two photos of the same name from different folders must not
    silently overwrite one another."""
    stem, extension = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    attempt = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{stem}-{attempt}{extension}")
        attempt += 1
    return candidate


def archive_current(kind: str) -> dict:
    """Set the file being played aside, and move on to the next one."""
    playing = now_playing(kind)
    if not playing:
        raise MediaError(f"the {kind} player is not on a file")
    folder = os.path.join(ARCHIVE_DIR, kind)
    os.makedirs(folder, exist_ok=True)
    target = _free_name(folder, os.path.basename(playing))
    try:
        shutil.move(playing, target)
    except OSError as exc:
        raise MediaError(f"cannot set {playing} aside: {exc}") from exc
    logger.info("%s: %s set aside in %s", kind, playing, target)
    # Moved, never deleted: a mistake must cost nothing, and these are
    # somebody's family photographs. fbi's own delete key is an unlink with no
    # backup and no trash — tested, and not used here for exactly that reason.
    now = skip(kind)["now_playing"]
    return {"player": kind, "archived": playing, "moved_to": target,
            "now_playing": now}


def set_volume(percent: float) -> dict:
    """Change the music mode's own volume: persisted immediately, and
    applied live too, if a player happens to be up right now — a track
    already playing must not have to end before a change is heard.
    Silently a no-op on the live side when nothing is running: the next
    `_ensure_music_player()` picks the new value up on its own regardless,
    the same way a revive after a crash does.
    """
    config = media_config.set_volume(percent)
    if _music is not None and _music.poll() is None:
        try:
            _say_to_music(f"VOLUME {config['volume']}")
        except MediaError as exc:
            logger.warning("could not apply the new volume live: %s", exc)
    return config


# --- Starting and stopping what plays ----------------------------------------

def _start_players(folder: "str | None" = None) -> dict:
    """Start the music and the slideshow. Called only by the music mode."""
    global _folder_playing
    music_dir = _music_dir_for(folder)
    photos = _files(PICTURES_DIR, IMAGE_EXTENSIONS)
    tracks = _files(music_dir, AUDIO_EXTENSIONS)
    if not photos and not tracks:
        raise MediaError(f"nothing to play: no photos in {PICTURES_DIR}, "
                         f"no music in {music_dir}")
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    if photos:
        _start_photos(photos)
    else:
        logger.warning("no photos in %s: music only", PICTURES_DIR)
    if tracks:
        _start_music(music_dir)
    else:
        logger.warning("no music in %s: photos only", music_dir)
    _folder_playing = folder
    _start_watcher()
    logger.info("music and photos: %d photos, %d music files%s",
                len(photos), len(tracks), f" from {folder!r}" if folder else "")
    return {"pictures": len(photos), "music": len(tracks), "folder": folder}


def _stop_players() -> None:
    """Stop everything, and close the players rather than leave them standing.

    An earlier version kept one mpg123 alive for the life of the API, because
    mpv needed twenty seconds to start and a button press could not wait for
    it. mpg123 starts in milliseconds, so the standing player bought nothing
    and cost plenty: it held the HDMI sound device open at all times, it died
    whenever the output went away and had to be revived, and "is music
    playing?" could no longer be answered by asking whether a player existed.
    Nothing runs outside the music mode now.
    """
    _stop_watcher()
    _stop_photos()
    _silence_music()
    _close_music_player()
    logger.info("music and photos stopped")


def status() -> dict:
    """What is playing, read without taking the lock.

    Deliberately: this is the route somebody calls when they think the box has
    stopped answering, and a press that is busy waking a television holds the
    lock for up to a minute. Everything read here is a snapshot anyway — a
    count of files, a process that is alive or not.
    """
    playing = {}
    if photos_playing():
        playing["pictures"] = now_playing("pictures")
    if music_playing():
        try:
            playing["music"] = now_playing("music")
        except MediaError as exc:
            logger.info("the music player did not say what it is on: %s", exc)
    return {
        "players": {"pictures": "running" if photos_playing() else "stopped",
                    "music": "running" if music_playing() else "stopped"},
        "now_playing": playing,
        "media_dir": MEDIA_DIR,
        "photos": len(_files(PICTURES_DIR, IMAGE_EXTENSIONS)),
        "music": len(_files(MUSIC_DIR, AUDIO_EXTENSIONS)),
        "music_folders": music_folders(),
        "folder_playing": _folder_playing if is_playing() else None,
        "photo_seconds": PHOTO_SECONDS,
    }


def startup() -> None:
    """Clear what a crash may have left behind."""
    for leftover in (SLIDESHOW_REQUEST, SLIDESHOW_RUNNING):
        try:
            os.remove(leftover)
        except OSError:
            pass


def shutdown() -> None:
    """Stop the players with the API rather than leave them orphaned."""
    with _lock:
        _stop_players()
