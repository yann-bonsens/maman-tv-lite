# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""media_config.py's own file handling — the same write/fsync/rename
discipline tv_config.py uses, pinned the same way."""

import json
from pathlib import Path

import pytest

import media_config

# Anchored on this file, not on the working directory: CI runs pytest from
# api/, where a relative "api/..." path does not exist.
REPO_ROOT = Path(__file__).resolve().parents[2]


class TestLoad:
    def test_a_fresh_box_defaults_to_full_volume(self):
        assert media_config.load() == {"volume": 100}

    def test_a_missing_file_does_not_stop_the_box(self):
        assert media_config.load()["volume"] == media_config.DEFAULT_VOLUME

    def test_a_file_that_cannot_be_read_falls_back_to_the_default(self):
        Path(media_config.PATH).write_text("{ this is not json")
        assert media_config.load()["volume"] == media_config.DEFAULT_VOLUME

    def test_an_out_of_range_stored_value_is_not_trusted(self):
        """A hand-edited or corrupted file must not hand back a volume this
        module would itself refuse to set."""
        Path(media_config.PATH).write_text(json.dumps({"volume": 500}))
        assert media_config.load()["volume"] == media_config.DEFAULT_VOLUME

    def test_a_non_numeric_stored_value_is_not_trusted(self):
        Path(media_config.PATH).write_text(json.dumps({"volume": "loud"}))
        assert media_config.load()["volume"] == media_config.DEFAULT_VOLUME

    def test_a_valid_stored_value_is_used(self):
        Path(media_config.PATH).write_text(json.dumps({"volume": 35}))
        assert media_config.load()["volume"] == 35


class TestSetVolume:
    def test_changes_and_persists(self):
        media_config.set_volume(20)
        assert media_config.load()["volume"] == 20

    def test_the_boundaries_are_accepted(self):
        media_config.set_volume(media_config.MIN_VOLUME)
        assert media_config.load()["volume"] == media_config.MIN_VOLUME
        media_config.set_volume(media_config.MAX_VOLUME)
        assert media_config.load()["volume"] == media_config.MAX_VOLUME

    def test_beyond_the_boundaries_is_refused(self):
        with pytest.raises(ValueError):
            media_config.set_volume(media_config.MAX_VOLUME + 1)
        with pytest.raises(ValueError):
            media_config.set_volume(media_config.MIN_VOLUME - 1)

    def test_a_refused_value_does_not_change_what_was_saved(self):
        media_config.set_volume(45)
        with pytest.raises(ValueError):
            media_config.set_volume(1000)
        assert media_config.load()["volume"] == 45


def test_it_is_written_whole_or_not_at_all():
    """This box is switched off by pulling its plug. A configuration that
    exists only in the page cache is one an unplug takes away."""
    source = (REPO_ROOT / "api" / "media_config.py").read_text()
    assert "os.fsync" in source and "os.replace" in source
