# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The button-binding file's own data loading, independent of what a command
name actually does — see button_bindings.py's own docstring for why this is
split from zigbee_bridge.py."""

import json
import os
from pathlib import Path

import button_bindings as bb

# Anchored on this file, not on the working directory: CI runs pytest
# from api/, where a relative "api/..." path does not exist.
REPO_ROOT = Path(__file__).resolve().parents[2]


class TestLoadBindings:
    def test_missing_file_falls_back_to_the_default(self, tmp_path):
        assert bb.load_bindings(str(tmp_path / "absent.json")) == bb.DEFAULT_BINDINGS

    def test_malformed_file_falls_back_instead_of_raising(self, tmp_path):
        path = tmp_path / "buttons.json"
        path.write_text("{ this is not json")
        assert bb.load_bindings(str(path)) == bb.DEFAULT_BINDINGS

    def test_reads_a_valid_file(self, tmp_path):
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {"Red Button": {"on": "diagnostic"}}}))
        assert bb.load_bindings(str(path)) == {"Red Button": {"on": "diagnostic"}}

    def test_a_command_name_is_kept_as_is_whatever_it_says(self, tmp_path):
        """This module only knows the file's shape, not which command names
        are real — that whitelist lives with zigbee_bridge.COMMANDS, which
        is what actually keeps a binding file from naming arbitrary Python
        (see test_zigbee_bridge.py's own version of this regression test)."""
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {"red": {"on": "os.system"}}}))
        assert bb.load_bindings(str(path)) == {"red": {"on": "os.system"}}

    def test_a_non_string_command_is_dropped(self, tmp_path):
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {"red": {"on": 123}}}))
        assert bb.load_bindings(str(path)) == bb.DEFAULT_BINDINGS

    def test_an_empty_file_falls_back_to_the_default(self, tmp_path):
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {}}))
        assert bb.load_bindings(str(path)) == bb.DEFAULT_BINDINGS


class TestWriteBindings:
    def test_writes_and_reads_back(self, tmp_path):
        path = tmp_path / "buttons.json"
        bb.write_bindings({"bindings": {"Red Button": {"single": "tv"}}}, str(path))
        assert json.loads(path.read_text()) == {"bindings": {"Red Button": {"single": "tv"}}}

    def test_group_write_bit_is_set(self, tmp_path):
        """The directory is setgid (see scripts/install.sh) so a new file
        gets the right group automatically, but setgid says nothing about
        the mode bits — those still need the explicit chmod, or a second
        writer (the API, or scripts/setup-zigbee.py run by a different
        account) could find itself locked out."""
        path = tmp_path / "buttons.json"
        bb.write_bindings({"bindings": {}}, str(path))
        assert path.stat().st_mode & 0o664 == 0o664

    def test_no_tmp_file_left_behind(self, tmp_path):
        path = tmp_path / "buttons.json"
        bb.write_bindings({"bindings": {}}, str(path))
        assert sorted(p.name for p in tmp_path.iterdir()) == ["buttons.json"]

    def test_unknown_keys_survive_a_read_raw_write_round_trip(self, tmp_path):
        """read_raw(), not load_bindings(), is what the write path reads —
        a key nothing here understands (an installation-specific "colors"
        block, say) must not be silently dropped by a rewrite."""
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({
            "bindings": {"Red Button": {"single": "tv"}},
            "colors": {"Red Button": "red"},
        }))
        document = bb.read_raw(str(path))
        bb.write_bindings(document, str(path))
        assert json.loads(path.read_text())["colors"] == {"Red Button": "red"}


class TestSetBinding:
    def test_creates_a_new_binding(self, tmp_path):
        path = tmp_path / "buttons.json"
        bb.set_binding("tv", "Red Button", ["single", "double"], str(path))
        assert bb.load_bindings(str(path)) == {
            "Red Button": {"single": "tv", "double": "tv"},
        }

    def test_moving_a_role_to_a_new_device_removes_it_from_the_old_one(self, tmp_path):
        """A role belongs to at most one button. Re-pairing "tv" onto a
        different physical device (choosing "start over" partway through
        pairing, say) must not leave the old device still answering — and
        still being the one device_for_command() finds and prints."""
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {
            "Old Button": {"single": "tv", "double": "tv"},
        }}))
        bb.set_binding("tv", "New Button", ["single"], str(path))
        bindings = bb.load_bindings(str(path))
        assert "Old Button" not in bindings
        assert bindings == {"New Button": {"single": "tv"}}

    def test_leaves_an_unrelated_device_untouched(self, tmp_path):
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {
            "Music Button": {"single": "music"},
        }}))
        bb.set_binding("tv", "TV Button", ["single"], str(path))
        bindings = bb.load_bindings(str(path))
        assert bindings["Music Button"] == {"single": "music"}
        assert bindings["TV Button"] == {"single": "tv"}

    def test_preserves_an_unrelated_action_on_the_same_device_being_moved(self, tmp_path):
        """A device bound to two different roles (unusual, but possible)
        loses only the action(s) that matched this command, not the whole
        entry."""
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {
            "Old Button": {"single": "tv", "hold": "relearn_tv_methods"},
        }}))
        bb.set_binding("tv", "New Button", ["single"], str(path))
        bindings = bb.load_bindings(str(path))
        assert bindings["Old Button"] == {"hold": "relearn_tv_methods"}

    def test_preserves_unknown_document_keys(self, tmp_path):
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {}, "colors": {"x": "y"}}))
        bb.set_binding("tv", "Red Button", ["single"], str(path))
        assert json.loads(path.read_text())["colors"] == {"x": "y"}


class TestReload:
    def test_replaces_the_module_level_bindings(self, tmp_path, monkeypatch):
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {"Red Button": {"single": "tv"}}}))
        monkeypatch.setattr(bb, "BINDINGS_PATH", str(path))
        bb.reload()
        assert bb.BINDINGS == {"Red Button": {"single": "tv"}}

    def test_a_name_imported_before_the_reload_still_sees_it(self, tmp_path, monkeypatch):
        """The real-hardware bug this pins: zigbee_bridge.py does
        `from button_bindings import BINDINGS`, which captures the dict
        *object*, not a live link to the name. A pairing procedure that
        replaced `BINDINGS` with a new dict (`BINDINGS = load_bindings(...)`)
        left zigbee_bridge.py holding the old, now-stale object — the file
        was correct, the screen showed the right name, but the button
        itself stayed unrecognised until the process restarted. reload()
        must mutate the existing object in place so every earlier importer
        of the name sees the update too, not only button_bindings.BINDINGS
        itself."""
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {}}))
        monkeypatch.setattr(bb, "BINDINGS_PATH", str(path))

        captured_by_another_module = bb.BINDINGS  # simulates the `from ... import BINDINGS` copy

        path.write_text(json.dumps({"bindings": {"Red Button": {"single": "tv"}}}))
        bb.reload()

        assert captured_by_another_module == {"Red Button": {"single": "tv"}}
        assert captured_by_another_module is bb.BINDINGS


class TestHasAnyButton:
    def test_false_on_the_catch_all_default(self, monkeypatch):
        monkeypatch.setattr(bb, "BINDINGS", dict(bb.DEFAULT_BINDINGS))
        assert bb.has_any_button() is False

    def test_true_once_tv_is_bound(self, monkeypatch):
        monkeypatch.setattr(bb, "BINDINGS", {"Red Button": {"single": "tv"}})
        assert bb.has_any_button() is True

    def test_true_once_music_is_bound(self, monkeypatch):
        monkeypatch.setattr(bb, "BINDINGS", {"Blue Button": {"single": "music"}})
        assert bb.has_any_button() is True

    def test_false_when_only_an_unrelated_command_is_bound(self, monkeypatch):
        monkeypatch.setattr(bb, "BINDINGS", {"Third Button": {"single": "diagnostic"}})
        assert bb.has_any_button() is False


class TestClear:
    def test_writes_an_empty_bindings_map(self, tmp_path, monkeypatch):
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {"Red Button": {"single": "tv"}}}))
        monkeypatch.setattr(bb, "BINDINGS_PATH", str(path))
        bb.clear()
        assert json.loads(path.read_text())["bindings"] == {}

    def test_reloads_so_the_effect_is_immediate(self, tmp_path, monkeypatch):
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({"bindings": {"Red Button": {"single": "tv"}}}))
        monkeypatch.setattr(bb, "BINDINGS_PATH", str(path))
        bb.clear()
        assert bb.has_any_button() is False

    def test_preserves_unknown_document_keys(self, tmp_path, monkeypatch):
        path = tmp_path / "buttons.json"
        path.write_text(json.dumps({
            "bindings": {"Red Button": {"single": "tv"}},
            "colors": {"Red Button": "red"},
        }))
        monkeypatch.setattr(bb, "BINDINGS_PATH", str(path))
        bb.clear()
        assert json.loads(path.read_text())["colors"] == {"Red Button": "red"}


class TestDeviceForCommand:
    def test_finds_the_device_bound_to_a_command(self):
        bindings = {"Red Button": {"single": "tv"}, "Green Button": {"single": "music"}}
        assert bb.device_for_command("tv", bindings) == "Red Button"
        assert bb.device_for_command("music", bindings) == "Green Button"

    def test_never_returns_the_catch_all(self):
        bindings = {bb.ANY_DEVICE: {"single": "tv"}}
        assert bb.device_for_command("tv", bindings) is None

    def test_none_when_nothing_is_bound_to_it(self):
        bindings = {"Red Button": {"single": "tv"}}
        assert bb.device_for_command("music", bindings) is None

    def test_reads_the_module_level_bindings_by_default(self, monkeypatch):
        monkeypatch.setattr(bb, "BINDINGS", {"Yellow Button": {"single": "diagnostic"}})
        assert bb.device_for_command("diagnostic") == "Yellow Button"


def test_the_binding_file_survives_an_unplug(tmp_path, monkeypatch):
    """Write, fsync, rename, then fsync the DIRECTORY — a rename that only
    exists in the page cache is one an unplug takes away, and this box is
    switched off by pulling its plug. `tv_config.save()` has always done
    this; the buttons are what the whole interface rests on."""
    import button_bindings
    source = (REPO_ROOT / "api" / "button_bindings.py").read_text(encoding="utf-8")
    write = source[source.index("def write_bindings("):]
    write = write[:write.index("\ndef ", 1)]
    assert "os.fsync(handle.fileno())" in write, "the file itself"
    assert "os.O_RDONLY" in write and "os.fsync(directory)" in write, \
        "the directory the rename happened in"

    synced: "list[int]" = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))
    button_bindings.write_bindings({"bindings": {"a": {"single": "tv"}}},
                                   str(tmp_path / "buttons.json"))
    assert len(synced) == 2, "both the file and its directory"
