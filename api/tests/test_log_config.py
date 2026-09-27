# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""How much the box says about the CEC bus: a setting, not a restart.

The one moment somebody wants more detail is the one moment they cannot afford
to restart the API — a restart loses the CEC session's state, re-reads the
configuration and pokes the television on the way through, which is to say it
destroys the evidence it was asked to collect. So this is applied to the live
logger, written down, and read back at start.
"""

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import log_config
from conftest import AUTH_HEADERS
from main import app

client = TestClient(app, headers=AUTH_HEADERS)


def _cec_logger():
    return logging.getLogger(log_config.CEC_LOGGER)


class TestTheSetting:
    def test_a_box_that_has_never_been_touched_says_the_ordinary_amount(self):
        assert log_config.load() == {"cec": "INFO"}

    def test_setting_it_applies_at_once(self):
        log_config.set_cec_level("DEBUG")
        assert _cec_logger().level == logging.DEBUG

    def test_setting_it_writes_it_down(self):
        log_config.set_cec_level("WARNING")
        written = json.loads(Path(log_config.PATH).read_text())
        assert written == {"cec": "WARNING"}

    def test_it_comes_back_after_a_restart(self):
        """The process is gone; the file is not. `apply()` runs at import, so a
        box comes up as verbose as it was left."""
        log_config.set_cec_level("DEBUG")
        _cec_logger().setLevel(logging.INFO)     # a fresh process
        log_config.apply()
        assert _cec_logger().level == logging.DEBUG

    @pytest.mark.parametrize("refused", ["LOUD", "", "notset", "CRITICAL", "10"])
    def test_a_level_that_means_nothing_is_refused(self, refused):
        """NOTSET reads as a level and behaves like a question, and CRITICAL on a
        diagnostic logger is indistinguishable from silence."""
        with pytest.raises(ValueError):
            log_config.set_cec_level(refused)

    def test_a_refused_level_changes_nothing(self):
        log_config.set_cec_level("DEBUG")
        with pytest.raises(ValueError):
            log_config.set_cec_level("LOUD")
        assert log_config.load()["cec"] == "DEBUG"
        assert _cec_logger().level == logging.DEBUG

    def test_any_case_is_accepted(self):
        log_config.set_cec_level("debug")
        assert log_config.load()["cec"] == "DEBUG"

    def test_a_file_somebody_hand_edited_into_nonsense_is_survived(self):
        Path(log_config.PATH).write_text("not json at all")
        assert log_config.load() == {"cec": "INFO"}
        Path(log_config.PATH).write_text('{"cec": "LOUD"}')
        assert log_config.load() == {"cec": "INFO"}
        Path(log_config.PATH).write_text('["cec"]')
        assert log_config.load() == {"cec": "INFO"}

    def test_a_write_that_cannot_happen_is_not_fatal(self, monkeypatch):
        """A box that cannot write its state must still answer the press that
        follows."""
        monkeypatch.setattr(log_config, "PATH", "/proc/nowhere/logging.json")
        assert log_config.save({"cec": "DEBUG"}) is False

    def test_only_the_cec_logger_moves(self):
        """Raising the root to DEBUG turns on every library in the process —
        paho's MQTT chatter above all — on a board whose journal is capped and
        whose card is the only place logs live."""
        was = logging.getLogger().level
        log_config.set_cec_level("DEBUG")
        assert logging.getLogger().level == was


class TestTheApiAppliesItAtStart:
    """A box comes up as verbose as it was left, including for whatever the
    startup itself says about the television.

    Checked against the source because by the time a test runs, the import has
    long since happened — there is no way to observe it from inside. The
    alternative was a test that called `apply()` itself and proved only that
    `apply()` works, which is what this used to be: removing the call from
    `main` left every test passing.
    """

    def test_main_applies_the_setting_before_anything_logs(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        assert "log_config.apply()" in source, \
            "a box would come back at the default level whatever was set"
        # Before the routes, before the lifespan, right after the handlers exist:
        # anything logged during startup must obey it too.
        assert source.index("log_config.apply()") < source.index("@app.get"), \
            "applied too late to cover what the startup itself says"


class TestOverTheApi:
    def test_it_reports_what_is_in_force_and_where(self):
        answer = client.get("/logging").json()
        assert answer["cec"] == "INFO"
        assert answer["file"] == log_config.PATH
        assert "DEBUG" in answer["levels"]
        assert answer["effective"] == "INFO"

    def test_it_can_be_turned_up_and_down(self):
        answer = client.put("/logging/cec?level=DEBUG")
        assert answer.status_code == 200, answer.text
        assert answer.json()["cec"] == "DEBUG"
        assert _cec_logger().level == logging.DEBUG
        assert client.get("/logging").json()["effective"] == "DEBUG"

        client.put("/logging/cec?level=INFO")
        assert _cec_logger().level == logging.INFO

    def test_a_level_that_means_nothing_is_refused_by_the_schema(self):
        answer = client.put("/logging/cec?level=LOUD")
        assert answer.status_code == 422, answer.text
        assert _cec_logger().level == logging.INFO

    def test_it_needs_the_password_like_everything_else(self):
        bare = TestClient(app)
        assert bare.get("/logging").status_code == 401
        assert bare.put("/logging/cec?level=DEBUG").status_code == 401


class TestTheWholeProcessLevel:
    """`MAMAN_LOG_LEVEL` is read at import, from a file the owner edits by hand.

    A name `logging` does not know used to raise there, which stopped the API
    from starting at all — and a box with no API has no buttons, on a service
    that restarts for ever. Found by comparing against what is deployed.
    """

    def test_a_name_that_is_not_a_level_does_not_stop_the_api(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        assert "getLevelNamesMapping()" in source, \
            "the level is passed to basicConfig unchecked again"
        assert 'else "INFO"' in source, "nothing falls back"

    def test_it_says_which_value_it_refused(self):
        """Silently using INFO would leave somebody wondering why DEBUG did
        nothing."""
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        assert "is not a level; using INFO" in source
