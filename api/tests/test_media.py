# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Music and photos on a board with one slow core.

Both lists are the box's own — shuffled here, held here, played one item at a
time — and that is what makes "next", "previous" and "set this one aside"
possible. What draws and what sounds are deliberately small: `fbi` for a photo,
`mpg123` for a track. mpv did both before and cost four to five times as much,
which on this board was the difference between a mode that works and one that
stutters.
"""

import json
import os
import pathlib
import socket
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import media
import media_config
from conftest import AUTH_HEADERS
from main import app

client = TestClient(app, headers=AUTH_HEADERS)


@pytest.fixture(autouse=True)
def _nothing_playing(tmp_path):
    """No leftover state, and nothing written outside the test's own folder."""
    media._photo_stop.set()
    media._photos = []
    media._music = None
    media._music_playing = False
    media._tracks = []
    media._music_at = 0
    media._folder_playing = None
    with patch.object(media, "RUNTIME_DIR", str(tmp_path / "run")), \
         patch.object(media, "SLIDESHOW_REQUEST", str(tmp_path / "run" / "slideshow")), \
         patch.object(media, "SLIDESHOW_RUNNING", str(tmp_path / "run" / "slideshow-running")), \
         patch.object(media, "RUNTIME_DIR", str(tmp_path / "run")):
        yield
    media._photo_stop.set()


def pretend_to_be_mpv(draws_after=None):
    """A socket that answers a loadfile the way the photo player does, and
    announces the frame once it has been "drawn" — or never, for a photo that
    will not come. Returns the directory to point RUNTIME_DIR at, the listener
    to close afterwards, and the list of commands it was given.

    Its directory is a short one of its own: a Unix socket path is capped at
    about a hundred characters and pytest's tmp_path overruns it.
    """
    directory = pathlib.Path(tempfile.mkdtemp(dir="/tmp"))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(directory / "photos.sock"))
    listener.listen(1)
    asked = []

    def serve():
        link, _ = listener.accept()
        for line in link.recv(4096).split(b"\n"):
            if line.strip():
                asked.append(json.loads(line)["command"])
        link.sendall(b'{"error":"success"}\n')
        if draws_after is not None:
            time.sleep(draws_after)
            link.sendall(b'{"event":"playback-restart"}\n')
        # Held open far longer than any test is willing to wait. Closing it
        # early was a real weakness: _show stops on a closed socket too, so
        # every wait ended at the same moment whatever the reason, and a test
        # meant to prove that a press cuts a photo short passed with the check
        # for the press deleted.
        time.sleep(20)
        link.close()

    threading.Thread(target=serve, daemon=True).start()
    return directory, listener, asked


def _hold_the_lock(seconds: float) -> None:
    """Hold the media lock, the way a press waking a television does.

    Written for `test_the_status_never_waits_behind_a_press`, which called it
    from a thread without it ever having been defined: the thread died on a
    NameError that pytest only reports as a warning, nothing held the lock,
    and the test passed against an empty room for as long as it existed.
    """
    with media._lock:
        time.sleep(seconds)


def _library(tmp_path, monkeypatch, photos=2, tracks=2):
    """A media folder with files at the top and in sub-folders."""
    pictures = tmp_path / "pictures"
    (pictures / "holidays").mkdir(parents=True)
    for i in range(photos):
        target = pictures if i % 2 else pictures / "holidays"
        (target / f"photo{i}.jpg").write_text("photo")
    (pictures / "._photo0.jpg").write_text("mac litter")
    music = tmp_path / "music"
    (music / "Tri Yann").mkdir(parents=True)
    for i in range(tracks):
        target = music if i % 2 else music / "Tri Yann"
        (target / f"track{i}.mp3").write_text("music")
    monkeypatch.setattr(media, "PICTURES_DIR", str(pictures))
    monkeypatch.setattr(media, "MUSIC_DIR", str(music))
    monkeypatch.setattr(media, "ARCHIVE_DIR", str(tmp_path / "archive"))
    return pictures, music


class TestWhatThereIsToPlay:
    def test_files_at_the_top_and_in_sub_folders_are_one_list(self, tmp_path, monkeypatch):
        """The owner may keep albums in folders or drop everything in loose."""
        pictures, music = _library(tmp_path, monkeypatch, photos=4, tracks=4)
        found = media._files(str(pictures), media.IMAGE_EXTENSIONS)
        assert len(found) == 4
        assert any("holidays" in path for path in found)
        assert any(path.endswith("photo1.jpg") for path in found)

    def test_the_macs_companion_files_are_left_out(self, tmp_path, monkeypatch):
        """"._photo.jpg" carries a photo's extension and holds no photo."""
        pictures, _ = _library(tmp_path, monkeypatch)
        assert all("._" not in path for path in
                   media._files(str(pictures), media.IMAGE_EXTENSIONS))

    def test_heic_is_not_offered_on_this_board(self):
        """Its FFmpeg is too old to decode it; the photo would be a blank."""
        assert ".heic" not in media.IMAGE_EXTENSIONS

    def test_only_folders_holding_music_are_offered(self, tmp_path, monkeypatch):
        _, music = _library(tmp_path, monkeypatch)
        (music / "Empty").mkdir()
        assert media.music_folders() == ["Tri Yann"]

    @pytest.mark.parametrize("asked", ["../../etc", "/etc", "Empty", "nope"])
    def test_a_folder_that_is_not_there_is_refused(self, asked, tmp_path, monkeypatch):
        """Compared with what is really on the disk, so no name can point a
        player outside the media folder."""
        _, music = _library(tmp_path, monkeypatch)
        (music / "Empty").mkdir()
        with pytest.raises(media.MediaError, match="no such music folder"):
            media._music_dir_for(asked)

    def test_nothing_to_play_is_said_rather_than_started(self, tmp_path, monkeypatch):
        monkeypatch.setattr(media, "PICTURES_DIR", str(tmp_path / "none"))
        monkeypatch.setattr(media, "MUSIC_DIR", str(tmp_path / "none"))
        with pytest.raises(media.MediaError, match="nothing to play"):
            media._start_players()


class TestKnowingWhichPhotoIsUp:
    """fbi owns the list and never says what it is showing; it does not even
    keep the file open — measured, /proc/<pid>/fd holds nothing between turns.
    But it opens each photo at the moment it puts it up, and the kernel reports
    that: one IN_OPEN per turn, exactly at the change. That is how "next",
    "previous" and "set this one aside" came back."""

    def test_what_is_up_is_what_the_kernel_saw_opened(self):
        media._photos = ["/a.jpg", "/b.jpg"]
        with patch("media.photos_playing", return_value=True), \
             patch.object(media, "_photo_showing", "/b.jpg"):
            assert media.now_playing("pictures") == "/b.jpg"

    def test_nothing_is_claimed_before_a_photo_has_been_seen(self):
        """A box that named a photo it had never watched being opened would be
        guessing, and archiving on a guess is how family photographs are
        lost."""
        media._photos = ["/a.jpg"]
        with patch("media.photos_playing", return_value=True), \
             patch.object(media, "_photo_showing", None):
            assert media.now_playing("pictures") is None

    def test_a_new_slideshow_does_not_inherit_the_last_photo(self):
        """Found in the audit. What is on screen is only known once the kernel
        has reported fbi opening it, and photos_playing() is true before that —
        so a slideshow that started while the previous session's photo was
        still remembered would set aside a photo from the session before."""
        with patch("media._ask_for_slideshow"), \
             patch("media._watch_what_is_shown"), \
             patch.object(media, "_photo_showing", "/from-the-last-session.jpg"):
            media._start_photos(["/a.jpg", "/b.jpg"])
            assert media._photo_showing is None

    def test_next_and_previous_are_keys_the_screen_presses(self):
        """The consoles belong to root and the API does not, so it asks."""
        with patch("media.photos_playing", return_value=True), \
             patch("media.time.sleep"), \
             patch("media.now_playing", return_value="/b.jpg"), \
             patch("media._press_in_the_slideshow") as pressed:
            media.skip("pictures")
            assert pressed.call_args[0][0] == media.PHOTO_NEXT
            media.skip("pictures", backwards=True)
            assert pressed.call_args[0][0] == media.PHOTO_PREVIOUS
    def test_setting_aside_moves_the_file_and_moves_on(self, tmp_path):
        """Moved, never deleted: a mistake must cost nothing. fbi's own delete
        key is an unlink with no backup and no trash — tested on copies, and
        not used here for exactly that reason."""
        photo = tmp_path / "one.jpg"
        photo.write_text("photo")
        with patch.object(media, "ARCHIVE_DIR", str(tmp_path / "archive")), \
             patch("media.now_playing", return_value=str(photo)), \
             patch("media.skip", return_value={"now_playing": "/next.jpg"}):
            answer = media.archive_current("pictures")
        assert not photo.exists(), "the original must have moved"
        assert os.path.exists(answer["moved_to"]), "and still exist elsewhere"
        assert answer["now_playing"] == "/next.jpg"
class TestTheRoutes:
    def test_status_says_what_is_playing_and_what_there_is(self, tmp_path, monkeypatch):
        _library(tmp_path, monkeypatch)
        answer = client.get("/media/status").json()
        assert answer["players"] == {"pictures": "stopped", "music": "stopped"}
        assert answer["photos"] == 2 and answer["music"] == 2
        assert answer["music_folders"] == ["Tri Yann"]

    def test_next_previous_and_archive_reach_the_right_player(self):
        with patch("media.skip", return_value={"player": "music"}) as skip:
            assert client.post("/media/music/next").status_code == 200
            skip.assert_called_with("music")
            client.post("/media/music/previous")
            skip.assert_called_with("music", backwards=True)
            client.post("/media/pictures/next")
            skip.assert_called_with("pictures")
            client.post("/media/pictures/previous")
            skip.assert_called_with("pictures", backwards=True)
        with patch("media.archive_current", return_value={"player": "pictures"}) as archive:
            assert client.post("/media/pictures/archive").status_code == 200
            archive.assert_called_with("pictures")
            client.post("/media/music/archive")
            archive.assert_called_with("music")

    def test_a_player_that_is_not_running_answers_409_not_500(self):
        with patch("media.skip", side_effect=media.MediaError("the music player is not running")):
            answer = client.post("/media/music/next")
        assert answer.status_code == 409
        assert "not running" in answer.json()["detail"]

    def test_start_passes_the_folder_on(self):
        with patch("modes.music", return_value={"mode": "music"}) as start:
            client.post("/mode/music?folder=Tri%20Yann")
        start.assert_called_once_with("Tri Yann")

    def test_swagger_offers_the_folders_on_the_disk_as_a_choice(self):
        """The list cannot live in the code: a folder dropped on the share has
        to show up without restarting the service."""
        with patch("media.music_folders", return_value=["Irish", "Tri Yann"]):
            schema = client.get("/openapi.json").json()
        parameter = next(p for p in schema["paths"]["/mode/music"]["post"]["parameters"]
                         if p["name"] == "folder")
        branches = parameter["schema"].get("anyOf", [parameter["schema"]])
        assert ["Irish", "Tri Yann"] in [branch.get("enum") for branch in branches]

    def test_an_empty_library_offers_no_choice_rather_than_an_empty_one(self):
        with patch("media.music_folders", return_value=[]):
            schema = client.get("/openapi.json").json()
        parameter = next(p for p in schema["paths"]["/mode/music"]["post"]["parameters"]
                         if p["name"] == "folder")
        branches = parameter["schema"].get("anyOf", [parameter["schema"]])
        assert all("enum" not in branch for branch in branches)


class TestStoppingWhenTheTelevisionGoesOff:
    """Switched off by anything other than the two buttons — its own remote
    above all — the set tells the box nothing, and the players would carry on:
    music into a set that is off, photos for nobody."""

    @pytest.mark.parametrize("status,answered,expected", [
        ("standby", True, True),
        ("off", True, True),
        ("in transition from on to standby", True, True),
        ("on", True, False),
        ("in transition from standby to on", True, False),
        # Silence counts only once the set has answered during this session:
        # an unplugged cable is silent too.
        ("unknown", True, True),
        ("unknown", False, False),
    ])
    def test_how_a_reading_is_read(self, status, answered, expected):
        assert media._television_went_off(status, answered) is expected

    def test_two_readings_in_a_row_stop_the_players(self, monkeypatch):
        """One is a hiccup on the bus; two is the set being off."""
        monkeypatch.setattr(media, "WATCH_INTERVAL_SECONDS", 0.01)
        states = iter(["on", "standby", "standby"])
        media._watch_stop.clear()
        with patch("media.is_playing", return_value=True), \
             patch("media.cec._safe_power_status", side_effect=lambda: next(states, "standby")), \
             patch("modes.television") as stop:
            media._watch_television()
        stop.assert_called_once()

    def test_it_asks_and_never_tells(self, monkeypatch):
        """Re-sending to a set that is shutting down wakes it back up."""
        monkeypatch.setattr(media, "WATCH_INTERVAL_SECONDS", 0.01)
        media._watch_stop.clear()
        with patch("media.is_playing", return_value=True), \
             patch("media.cec._safe_power_status", return_value="standby"), \
             patch("media.cec.standby") as never_sent, \
             patch("media.cec.power_on") as never_sent_either, \
             patch("modes.television"):
            media._watch_television()
        never_sent.assert_not_called()
        never_sent_either.assert_not_called()

    def test_it_gives_up_once_nothing_is_playing(self, monkeypatch):
        monkeypatch.setattr(media, "WATCH_INTERVAL_SECONDS", 0.01)
        media._watch_stop.clear()
        with patch("media.is_playing", return_value=False), \
             patch("media.cec._safe_power_status") as asked:
            media._watch_television()
        asked.assert_not_called()

    def test_it_runs_with_the_players_and_stops_with_them(self, tmp_path, monkeypatch):
        _library(tmp_path, monkeypatch)
        with patch("media._start_photos"), patch("media._start_music"), \
             patch("media._watch_television", lambda: media._watch_stop.wait(5)):
            media._start_players()
            assert media._watcher is not None and media._watcher.is_alive()
            media._stop_players()
        assert media._watcher is None


class TestWhatTheAuditFound:
    """One test per defect found on 2026-09-20, so none of them comes back."""

    def test_the_unit_survives_a_media_folder_that_is_not_there(self):
        """systemd REFUSES TO START a service whose ReadWritePaths does not
        exist (measured on the Pi 5 box with a throwaway unit), so an install
        without the media component would have left the box with no API and no
        buttons at all."""
        from pathlib import Path
        unit = Path(__file__).resolve().parents[2] / "systemd" / "maman-api.service"
        line = next(line for line in unit.read_text().splitlines()
                    if line.startswith("ReadWritePaths="))
        assert line == "ReadWritePaths=-__MEDIA_DIR__", line

    def test_the_status_never_waits_behind_a_press(self, tmp_path, monkeypatch):
        """It is the route called when the box seems stuck, and a press waking
        a television holds the lock for up to a minute."""
        _library(tmp_path, monkeypatch)
        holder = threading.Thread(target=lambda: _hold_the_lock(0.6), daemon=True)
        holder.start()
        time.sleep(0.05)
        started = time.monotonic()
        media.status()
        assert time.monotonic() - started < 0.3
class TestVolume:
    """The music mode's own volume — mpg123's own gain, independent of the
    television's, which CEC cannot set in absolute terms anyway (see
    docs/development/installation-screen.md's "out of scope" note)."""

    def test_default_is_100(self):
        assert media_config.load()["volume"] == 100

    def test_applied_whenever_the_player_starts(self, monkeypatch):
        """Every (re)start passes through this, including a revive after a
        crash — the player is not a standing process any more, so there is
        no single "at API startup" moment to hook instead."""
        told = []
        process = MagicMock()
        process.poll.return_value = None
        process.stdout = iter([])
        process.stdin.write = lambda s: told.append(s)
        process.stdin.flush = lambda: None
        monkeypatch.setattr(media.subprocess, "Popen", lambda *a, **kw: process)
        media_config.set_volume(42)
        media._ensure_music_player()
        assert any("VOLUME 42" in line for line in told)

    def test_set_volume_persists(self):
        media.set_volume(30)
        assert media_config.load()["volume"] == 30

    def test_set_volume_applies_live_when_a_player_is_running(self):
        media._music = MagicMock()
        media._music.poll.return_value = None
        with patch("media._say_to_music") as told:
            media.set_volume(55)
        told.assert_called_once_with("VOLUME 55")

    def test_set_volume_is_quiet_when_nothing_is_running(self):
        """A config change must succeed even with no player up — the next
        one to start picks the new value up on its own."""
        media._music = None
        result = media.set_volume(70)
        assert result["volume"] == 70

    def test_a_live_apply_failure_does_not_lose_the_saved_value(self):
        media._music = MagicMock()
        media._music.poll.return_value = None
        with patch("media._say_to_music", side_effect=media.MediaError("gone")):
            result = media.set_volume(80)
        assert result["volume"] == 80
        assert media_config.load()["volume"] == 80

    def test_out_of_range_is_refused(self):
        with pytest.raises(ValueError):
            media.set_volume(150)
        with pytest.raises(ValueError):
            media.set_volume(-1)

    def test_the_routes_read_and_write_it(self):
        assert client.get("/media/config").json()["volume"] == 100
        answer = client.put("/media/config/volume?percent=60")
        assert answer.status_code == 200
        assert answer.json()["volume"] == 60
        assert client.get("/media/config").json()["volume"] == 60

    def test_the_route_refuses_an_out_of_range_value(self):
        assert client.put("/media/config/volume?percent=150").status_code == 422


class TestTheMusicPlayerDyingUnderUs:
    """Found in the field: the player died with the sound card taken by
    something else, the music stopped, the box went on showing photos as though
    nothing had happened, and the journal said nothing at all. A box that goes
    quiet without a word is the failure this project refuses — nobody
    investigates a thing that merely stopped."""

    def test_a_death_while_playing_brings_the_music_back(self):
        media._music_playing = True
        process = MagicMock(stdout=iter([]))       # the stream simply ends
        with patch("media._revive_music") as revived:
            media._follow_music(process)
        revived.assert_called_once()

    def test_a_death_after_a_stop_is_left_alone(self):
        """Closing the player at shutdown must not look like a fault."""
        media._music_playing = False
        process = MagicMock(stdout=iter([]))
        with patch("media._revive_music") as revived:
            media._follow_music(process)
        revived.assert_not_called()

    def test_what_the_player_complains_about_reaches_the_journal(self, caplog):
        """Its own messages were dropped, which is why the death was silent."""
        media._music_playing = False
        process = MagicMock(stdout=iter(["Cannot open device: Device busy\n"]))
        with caplog.at_level("WARNING"):
            media._follow_music(process)
        assert "Device busy" in caplog.text

    def test_it_resumes_the_track_it_died_on(self):
        media._music_playing = True
        media._tracks = ["/a.mp3", "/b.mp3", "/c.mp3"]
        media._music_at = 2                        # /b.mp3 was playing
        with patch("media._ensure_music_player"), \
             patch("media._sleep_unless_stopped", return_value=False), \
             patch("media._say_to_music") as told:
            media._revive_music()
        assert told.call_args[0][0] == "LOAD /b.mp3"

    def test_a_player_that_will_not_start_is_given_up_on(self, caplog):
        """One core: a player that cannot start at all must not be restarted in
        a tight loop for ever."""
        media._music_playing = True
        with patch("media._ensure_music_player",
                   side_effect=media.MediaError("no sound card")), \
             patch("media._sleep_unless_stopped", return_value=False) as waited, \
             caplog.at_level("ERROR"):
            media._revive_music()
        assert waited.call_count == media.MUSIC_REVIVE_ATTEMPTS
        assert "giving up" in caplog.text
        # And it backs off: the last wait is longer than the first.
        assert waited.call_args_list[-1][0][0] > waited.call_args_list[0][0][0]
