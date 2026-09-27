# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The configuration file, the HDMI output, and the state the box publishes.

Three small modules that between them carry the two lessons of September 2026:
a box must not change its own configuration while somebody is using it, and it
must never claim to have done something to a screen without looking.
"""

import json
import re
from pathlib import Path

import pytest

import hdmi_output
import state
import tv_config

# Anchored on this file, not on the working directory: CI runs pytest from
# api/, where a relative "api/..." path does not exist.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Captured before the fixtures replace it: these tests are about the real one.
REAL_STATE = hdmi_output.state


class TestTheConfigurationFile:
    def test_a_box_that_has_never_been_configured_uses_the_standard_frames(self):
        config = tv_config.load()
        assert config["wake"]["technique"] == "image_view_on"
        assert config["sleep"]["technique"] == "standby"
        assert config["wake"]["source"] == "default"

    def test_it_says_where_each_answer_came_from(self):
        """A box that has never been configured must look unconfigured. The
        previous file could not tell a measured answer from a guess."""
        tv_config.set_technique("wake", "text_view_on", source="detection")
        config = tv_config.load()
        assert config["wake"] == {"technique": "text_view_on", "source": "detection"}
        assert config["sleep"]["source"] == "default"

    def test_an_unknown_provenance_is_not_taken_at_face_value(self):
        Path(tv_config.PATH).write_text(json.dumps(
            {"wake": {"technique": "power_key", "source": "wishful thinking"}}))
        assert tv_config.load()["wake"]["source"] == "manual"

    def test_a_file_that_cannot_be_read_does_not_stop_the_box(self):
        """A box always has an answer: a television nobody can switch on is
        worse than one switched on with the wrong frame."""
        Path(tv_config.PATH).write_text("{ this is not json")
        assert tv_config.load()["wake"]["technique"] == "image_view_on"

    def test_the_television_it_was_made_for_is_recorded(self):
        tv_config.set_television({"manufacturer": "SAM", "name": "SAMSUNG"})
        assert tv_config.load()["television"]["name"] == "SAMSUNG"

    def test_an_older_installation_keeps_the_techniques_it_had(self):
        """"Pull, then run the installer again" must not cost somebody the
        answer an evening of measurement produced."""
        Path(tv_config.LEGACY_PATH).write_text(json.dumps({
            "wake": {"technique": "text_view_on", "confirmed": True},
            "sleep": {"technique": "standby", "confirmed": True,
                      "rejected": ["power_off_function"]},
        }))
        config = tv_config.load()
        assert config["wake"]["technique"] == "text_view_on"
        # "manual", not "detection": the mechanism that wrote those files no
        # longer exists, and half of what it recorded was wrong.
        assert config["wake"]["source"] == "manual"

    def test_it_is_written_whole_or_not_at_all(self):
        """This box is switched off by pulling its plug. A configuration that
        exists only in the page cache is one an unplug takes away."""
        source = (REPO_ROOT / "api" / "tv_config.py").read_text()
        assert "os.fsync" in source and "os.replace" in source

    def test_a_change_is_seen_by_a_completely_separate_load(self):
        """What "persisted and picked up after a reboot" actually reduces
        to: load() never caches anything in memory, so a fresh call — the
        same shape as what happens after the process restarts — sees
        whatever the last save() wrote, with nothing to reload."""
        tv_config.set_technique("wake", "power_key", source="manual")
        tv_config.set_hdmi_sleep("connector")
        tv_config.set_detection_seconds(step_seconds=45, cycle_seconds=120)

        fresh = tv_config.load()

        assert fresh["wake"]["technique"] == "power_key"
        assert fresh["hdmi_sleep"] == "connector"
        assert fresh["detection_step_seconds"] == 45
        assert fresh["detection_cycle_seconds"] == 120


class TestClearing:
    def test_resets_the_learned_techniques(self):
        tv_config.set_technique("wake", "text_view_on", source="detection")
        tv_config.set_technique("sleep", "power_off_function", source="detection")
        tv_config.set_detection_complete(True)
        tv_config.set_television({"manufacturer": "SAM"})

        config = tv_config.clear()

        assert config["wake"] == {"technique": tv_config.DEFAULT_WAKE, "source": "default"}
        assert config["sleep"] == {"technique": tv_config.DEFAULT_SLEEP, "source": "default"}
        assert config["television"] == {}
        assert config["detection_complete"] is False

    def test_release_also_resets_to_its_default(self):
        tv_config.set_technique("release", "inactive_source", source="detection")
        config = tv_config.clear()
        assert config["release"] == {"technique": tv_config.DEFAULT_RELEASE, "source": "default"}

    def test_leaves_hdmi_sleep_and_the_detection_timings_alone(self):
        """Independent preferences about this board and this television,
        not part of "forget the TV" — a box that lost a deliberately-tuned
        hdmi_sleep choice along with its CEC techniques would be a worse
        surprise than keeping it."""
        tv_config.set_hdmi_sleep("connector")
        tv_config.set_detection_seconds(step_seconds=60)
        tv_config.set_technique("wake", "text_view_on", source="detection")

        config = tv_config.clear()

        assert config["hdmi_sleep"] == "connector"
        assert config["detection_step_seconds"] == 60

    def test_persists(self):
        tv_config.set_technique("wake", "text_view_on", source="detection")
        tv_config.clear()
        assert tv_config.load()["wake"]["source"] == "default"

    def test_leaves_tv_button_switches_off_alone(self):
        tv_config.set_tv_button_switches_off(True)
        tv_config.clear()
        assert tv_config.load()["tv_button_switches_off"] is True


class TestTvButtonSwitchesOff:
    def test_defaults_to_false(self):
        assert tv_config.load()["tv_button_switches_off"] is False

    def test_changes_and_persists(self):
        tv_config.set_tv_button_switches_off(True)
        assert tv_config.load()["tv_button_switches_off"] is True

    def test_a_malformed_stored_value_is_coerced_not_trusted_verbatim(self):
        Path(tv_config.PATH).write_text(json.dumps({"tv_button_switches_off": "yes please"}))
        # A non-empty string is truthy in Python — coerced with bool(), not
        # rejected, the same forgiving handling detection_complete already
        # gets: this setting is a plain flag, not something a malformed
        # file should be able to crash the box over.
        assert tv_config.load()["tv_button_switches_off"] is True


# The real reading. The autouse `box` fixture replaces hdmi_output.state with a
# pretend one for every test, so the tests of the reading itself keep their own.
REAL_STATE = hdmi_output.state


class TestTheHdmiOutput:
    def test_sleeping_and_waking_go_through_the_one_unit(self, box):
        hdmi_output.sleep("test")
        # stop-then-start — see TestPuttingTheOutputToSleepActuallyReapplies
        # for what `start` alone does to a oneshot systemd already considers
        # active, and why `restart` is not an option here.
        assert box.hdmi_calls[-2:] == ["stop", "start"]
        hdmi_output.wake("test")
        assert box.hdmi_calls[-1] == "stop"

    def test_it_reads_the_screen_back_rather_than_assuming(self, monkeypatch):
        """A version that claimed a cut which had not happened read as a
        working fix in the journal for a quarter of an hour."""
        monkeypatch.setattr(hdmi_output, "_systemctl", lambda verb, unit: True)
        monkeypatch.setattr(hdmi_output, "outputs", lambda: {
            "card0-HDMI-A-1": {"status": "connected", "dpms": "On",
                               "enabled": "enabled"}})
        monkeypatch.setattr(hdmi_output, "state", lambda: hdmi_output.AWAKE)
        assert hdmi_output.sleep("test") is False, \
            "the output is still driven; saying otherwise would be a lie"

    def test_an_empty_second_port_has_no_say(self, monkeypatch):
        """A Pi 5 read back "unknown" on every transition, because its empty
        port counted as asleep next to the one driving the set."""
        def ports(dpms):
            return lambda: {
                "card1-HDMI-A-1": {"status": "connected", "dpms": dpms, "enabled": "enabled"},
                "card1-HDMI-A-2": {"status": "disconnected", "dpms": "On",
                                   "enabled": "disabled"}}
        monkeypatch.setattr(hdmi_output, "outputs", ports("On"))
        assert REAL_STATE() == hdmi_output.AWAKE
        monkeypatch.setattr(hdmi_output, "outputs", ports("Off"))
        assert REAL_STATE() == hdmi_output.ASLEEP

    def test_a_connector_forced_off_still_reads_asleep(self, monkeypatch):
        monkeypatch.setattr(hdmi_output, "outputs", lambda: {
            "card1-HDMI-A-1": {"status": "disconnected", "dpms": "Off", "enabled": "disabled"},
            "card1-HDMI-A-2": {"status": "disconnected", "dpms": "On", "enabled": "disabled"}})
        assert REAL_STATE() == hdmi_output.ASLEEP

    def test_a_box_without_the_unit_says_so_instead_of_pretending(self, monkeypatch):
        monkeypatch.setattr(hdmi_output, "_systemctl", lambda verb, unit: False)
        assert hdmi_output.sleep("test") is False
        assert state.snapshot()["hdmi"] == hdmi_output.UNKNOWN

    def test_the_two_ways_of_stopping_the_picture_use_their_own_unit(self, box):
        """Blanking is the default; forcing the connector off is the fallback
        for a set that ignores it, and it costs the kernel's copy of the
        television's EDID."""
        hdmi_output.sleep("test")
        assert box.hdmi["unit"] == hdmi_output.UNITS["blank"]
        tv_config.set_hdmi_sleep("connector")
        hdmi_output.sleep("test")
        assert box.hdmi["unit"] == hdmi_output.UNITS["connector"]

    def test_none_never_touches_a_unit_at_all(self, box):
        """A deliberate no-op, not a fallback: hdmi_sleep="none" trades away
        rule 2 of modes.py on purpose (see tv_config.py's own note on it)."""
        tv_config.set_hdmi_sleep("none")
        assert hdmi_output.sleep("test") is True
        assert box.hdmi_calls == []

    def test_none_reports_the_output_as_awake(self, box):
        tv_config.set_hdmi_sleep("none")
        hdmi_output.sleep("test")
        assert state.snapshot()["hdmi"] == hdmi_output.AWAKE

    def test_waking_stops_both_of_them(self, box, monkeypatch):
        """The method can have changed while the output was asleep, and a unit
        left active would keep the screen dark for ever."""
        stopped = []
        monkeypatch.setattr(hdmi_output, "_systemctl",
                            lambda verb, unit: stopped.append((verb, unit)) or True)
        monkeypatch.setattr(hdmi_output, "state", lambda: hdmi_output.AWAKE)
        hdmi_output.wake("test")
        assert sorted(unit for verb, unit in stopped if verb == "stop") \
            == sorted(hdmi_output.UNITS.values())

    def test_an_unknown_method_is_refused_rather_than_guessed(self):
        with pytest.raises(ValueError):
            tv_config.set_hdmi_sleep("unplug the cable")

    def test_dpms_is_what_it_looks_at(self):
        """`enabled` stays "enabled" while the output sleeps and `status` only
        echoes what was forced. Measured on 2026-09-23: with the output
        asleep, status=connected, enabled=enabled, dpms=Off."""
        source = (REPO_ROOT / "api" / "hdmi_output.py").read_text()
        assert '"dpms"' in source
        assert "enabled" not in source.split("def outputs")[1].split("def ")[0] \
            or "dpms" in source.split("def outputs")[1].split("def ")[0]

    @pytest.mark.parametrize("seen,expected", [
        ({}, hdmi_output.UNKNOWN),
        ({"a": {"status": "connected", "dpms": "Off"}}, hdmi_output.ASLEEP),
        ({"a": {"status": "connected", "dpms": "On"}}, hdmi_output.AWAKE),
        # The connector forced off: the set sees no source, and the box must
        # read that as asleep even though dpms says nothing useful any more.
        ({"a": {"status": "disconnected", "dpms": "Off"}}, hdmi_output.ASLEEP),
        ({"a": {"status": "connected", "dpms": "On"},
          "b": {"status": "connected", "dpms": "Off"}}, hdmi_output.UNKNOWN),
    ])
    def test_how_the_screens_are_read(self, monkeypatch, seen, expected):
        """No television plugged in is "unknown", not "asleep": a box that
        believed its output was off when nothing was measuring it would report
        a fix it never applied."""
        monkeypatch.setattr(hdmi_output, "outputs", lambda: seen)
        assert REAL_STATE() == expected


class TestWhatTheBoxPublishes:
    def test_the_state_file_is_what_the_screen_reads(self, box):
        state.set_mode(state.MUSIC)
        published = json.loads(Path(state.PATH).read_text())
        assert published["mode"] == state.MUSIC
        assert published["configuration"]["wake"]["technique"] == "image_view_on"

    def test_events_carry_both_clocks(self, box):
        """This board has no clock of its own: after a power cut it starts
        with the last time it knew and jumps when the network answers, which
        made two boots overlap in the journal and cost an hour of wrong
        conclusions. Seconds since boot never lie."""
        state.note("something happened", detail="x")
        event = state.snapshot()["events"][-1]
        assert "clock" in event["at"] and "since_boot" in event["at"]

    def test_only_the_last_events_are_kept(self, box):
        for i in range(state.EVENTS_KEPT + 10):
            state.note(f"event {i}")
        assert len(state.snapshot()["events"]) == state.EVENTS_KEPT

    def test_a_runtime_directory_that_is_not_writable_costs_nothing(self, monkeypatch):
        """Losing this file costs a screen that shows nothing, never a press
        that does nothing."""
        monkeypatch.setattr(state, "PATH", "/proc/not-writable/state.json")
        monkeypatch.setattr(state, "RUNTIME_DIR", "/proc/not-writable")
        state.note("still fine")  # must not raise


class TestWhatTheBoxPublishesAboutItself:
    """`state.snapshot()` is what the API, the shell command and the
    television screen all render, so a setting missing from it is missing
    from all three at once."""

    def test_every_setting_reaches_the_published_state(self):
        """It used to name four keys by hand, and everything added after them
        was invisible from then on: the diagnostic page printed
        "output=asleep (?)" for ever because `hdmi_sleep` was never published,
        and the release technique appeared nowhere at all."""
        published = state.snapshot()["configuration"]
        for setting in tv_config.defaults():
            assert setting in published, \
                f"{setting} is configured but never published to the screen"

    def test_it_says_where_the_configuration_lives(self):
        assert state.snapshot()["configuration"]["file"] == tv_config.PATH

    def test_the_values_are_the_ones_in_force(self):
        tv_config.set_technique("release", "inactive_source", source="detection")
        tv_config.set_hdmi_sleep("connector")
        published = state.snapshot()["configuration"]
        assert published["release"] == {"technique": "inactive_source",
                                        "source": "detection"}
        assert published["hdmi_sleep"] == "connector"


class TestPuttingTheOutputToSleepActuallyReapplies:
    """The one mechanism the whole product rests on.

    Both sleep units are oneshots with RemainAfterExit=yes, so systemd
    treats `start` on an already-active one as a no-op: it does not re-run
    ExecStart, and it reports success. Anything that touches the framebuffer
    behind them — fbi drawing an installation page, a chvt, the slideshow —
    resets /sys/class/graphics/fb0/blank, and from then on the unit says
    "active" while the output is awake and every later sleep does nothing.

    Measured on the board on 2026-09-26:

        blank before      : 1  dpms=On
        after start (noop): 1  dpms=On
        after restart     : 1  dpms=Off
    """

    def test_sleeping_restarts_the_unit_rather_than_starting_it(self, box, monkeypatch):
        verbs = []
        monkeypatch.setattr(hdmi_output, "_systemctl",
                            lambda verb, unit: verbs.append((verb, unit)) or True)
        monkeypatch.setattr(hdmi_output, "state", lambda: hdmi_output.ASLEEP)
        hdmi_output.sleep("a test")
        # stop-then-start, not `restart`: the polkit rule grants exactly
        # those two verbs, and asking for restart is refused outright.
        assert verbs == [("stop", hdmi_output.UNITS["blank"]),
                        ("start", hdmi_output.UNITS["blank"])], verbs

    def test_waking_still_stops_both_units(self, box, monkeypatch):
        verbs = []
        monkeypatch.setattr(hdmi_output, "_systemctl",
                            lambda verb, unit: verbs.append((verb, unit)) or True)
        monkeypatch.setattr(hdmi_output, "state", lambda: hdmi_output.AWAKE)
        hdmi_output.wake("a test")
        assert [verb for verb, _ in verbs] == ["stop", "stop"]

    def test_the_unit_is_stopped_before_it_is_started(self):
        """`start` alone is the trap: on a oneshot systemd already considers
        active it succeeds and does nothing. The stop is what makes the
        start re-run ExecStart."""
        source = (REPO_ROOT / "api" / "hdmi_output.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                        if not line.lstrip().startswith("#"))
        sleeping = code[code.index('if verb == "start":'):]
        sleeping = sleeping[:sleeping.index("else:")]
        assert sleeping.index('_systemctl("stop"') < sleeping.index('_systemctl("start"')

    def test_only_the_verbs_polkit_grants_are_used(self):
        """The rule says "the API cannot restart, enable or mask anything",
        and `restart` really is refused: "Interactive authentication
        required", measured on the board."""
        source = (REPO_ROOT / "api" / "hdmi_output.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                        if not line.lstrip().startswith("#"))
        rule = (REPO_ROOT / "polkit" / "50-maman-tv-lite-power.rules").read_text()
        for verb in re.findall(r'_systemctl\("(\w+)"', code):
            assert f'verb === "{verb}"' in rule, f"polkit does not grant {verb!r}"
