# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The installation screen's own logic: the legend's fixed left/right order,
and which procedure `start()` reaches before anything else can be shown.
"""

from pathlib import Path

import cec_detection
import installation
import procedure_support
import tv_config

# Anchored on this file, not on the working directory: CI runs pytest from
# api/, where a relative "api/..." path does not exist.
REPO_ROOT = Path(__file__).resolve().parents[2]


class TestLegendOrder:
    """Navigate/negative (music) always on the left, validate/positive (tv)
    always on the right — regardless of the order a procedure happened to
    build its accepts dict in. Position in the returned dict is what
    page_render.py draws left-to-right, so the order of `_legend()`'s
    output keys is the thing under test."""

    def test_music_before_tv_when_given_tv_first(self):
        legend = installation._legend({"tv": "choose", "music": "next line"})
        assert list(legend.keys()) == ["music", "tv"]

    def test_music_before_tv_when_given_music_first(self):
        legend = installation._legend({"music": "next line", "tv": "choose"})
        assert list(legend.keys()) == ["music", "tv"]

    def test_a_single_entry_is_unaffected(self):
        legend = installation._legend({"tv": "ok"})
        assert list(legend.keys()) == ["tv"]


class FakePairingHardware:
    def __init__(self, *, any_button: bool):
        self._any_button = any_button

    def has_any_button(self) -> bool:
        return self._any_button


class TestBootstrapDispatch:
    """start() must reach button_pairing.run_bootstrap() before the menu,
    the interrupted-CEC-session check, or the guided "Pair the buttons"
    procedure — a box with no working button cannot navigate any of them.
    Once a button exists, none of that applies and today's menu-driven flow
    is untouched."""

    def test_bootstrap_runs_when_nothing_is_bound(self, monkeypatch):
        calls = []
        monkeypatch.setattr("button_pairing.run_bootstrap", lambda env: calls.append("bootstrap"))
        monkeypatch.setattr(installation, "_run_menu", lambda env: None)
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=False),
        })
        assert calls == ["bootstrap"]

    def test_bootstrap_is_skipped_once_a_button_exists(self, monkeypatch):
        calls = []
        monkeypatch.setattr("button_pairing.run_bootstrap", lambda env: calls.append("bootstrap"))
        monkeypatch.setattr(installation, "_run_menu", lambda env: None)
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=True),
        })
        assert calls == []

    def test_the_menu_never_runs_if_bootstrap_leaves_the_mode(self, monkeypatch):
        """abandon() during run_bootstrap() (someone switched modes while it
        was waiting for a press) must stop start() there — not fall through
        into the ordinary menu with a channel that already gave up. A real
        run_bootstrap() also returns False in this case (see its own
        docstring), which is what actually stops start() below — the
        explicit abandon() call here is what a real abandonment would also
        do, kept for realism."""
        menu_calls = []

        def fake_bootstrap(env):
            installation.channel.abandon()
            return False

        monkeypatch.setattr("button_pairing.run_bootstrap", fake_bootstrap)
        monkeypatch.setattr(installation, "_run_menu", lambda env: menu_calls.append("menu"))
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=False),
        })
        assert menu_calls == []

    def test_the_menu_never_runs_after_a_genuine_give_up(self, monkeypatch):
        """Distinct from abandonment: run_bootstrap()'s own ~20-minute
        deadline can run out with nobody having left the mode at all — its
        return value, not menu_env.abandoned(), is what must stop start()
        here, since there is still no button to show a menu to."""
        menu_calls = []
        monkeypatch.setattr("button_pairing.run_bootstrap", lambda env: False)
        monkeypatch.setattr(installation, "_run_menu", lambda env: menu_calls.append("menu"))
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=False),
        })
        assert menu_calls == []

    def test_the_menu_runs_once_bootstrap_finishes_normally(self, monkeypatch):
        menu_calls = []
        monkeypatch.setattr("button_pairing.run_bootstrap", lambda env: True)
        monkeypatch.setattr(installation, "_run_menu", lambda env: menu_calls.append("menu") or None)
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=False),
        })
        assert menu_calls == ["menu"]


class TestReturnsToTheMenuAfterAProcedure:
    """Finishing a procedure must land back on the menu, not close the
    mode — only "Quit" (or the mode being left from outside, which
    _run_menu already reports as None the same way) should do that."""

    def test_after_cec_the_menu_runs_again(self, monkeypatch):
        menu_choices = iter(["cec", None])
        menu_calls = []

        def fake_menu(env):
            menu_calls.append(1)
            return next(menu_choices)

        cec_calls = []
        monkeypatch.setattr(installation, "_run_menu", fake_menu)
        monkeypatch.setattr("cec_detection.run", lambda env: cec_calls.append(1))
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=True),
        })
        assert cec_calls == [1]
        assert len(menu_calls) == 2, "the menu must be shown again after the procedure ends"

    def test_after_pairing_the_menu_runs_again(self, monkeypatch):
        menu_choices = iter(["buttons", None])
        menu_calls = []

        def fake_menu(env):
            menu_calls.append(1)
            return next(menu_choices)

        pairing_calls = []
        monkeypatch.setattr(installation, "_run_menu", fake_menu)
        monkeypatch.setattr("button_pairing.run", lambda env: pairing_calls.append(1))
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=True),
        })
        assert pairing_calls == [1]
        assert len(menu_calls) == 2

    def test_quit_actually_stops_the_loop(self, monkeypatch):
        """Guards against an infinite loop: a plain None from the menu on
        the very first call must return at once."""
        monkeypatch.setattr(installation, "_run_menu", lambda env: None)
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=True),
        })  # must return; a hang here would fail the test on its own timeout

    def test_an_interrupted_session_returns_to_the_menu_too(self, monkeypatch):
        """The interrupted-CEC-session prompt is a special case that used
        to `return` unconditionally after resuming — it must now rejoin
        the same loop as everything else.

        The channel's own ask()/is_abandoned() are stubbed directly rather
        than going through a real blocking wait: the interrupted-session
        prompt has no button bound to answer it in this test (only
        run_menu is stubbed by the other tests here), so a real
        channel.ask() would block forever waiting for an answer nothing
        in this test ever sends. The mocked cec_detection.run() marks the
        session complete, the same as a real successful run would (via
        env.finish()) — leaving it as a no-op left `interrupted` true
        forever, since nothing ever cleared it, which is a flaw in an
        over-simplified mock, not a real hang in installation.py: a
        genuine run either finishes (clearing it) or raises Abandoned
        (caught by start()'s own outer handler, which returns immediately
        rather than looping).
        """
        tv_config.set_technique("wake", "text_view_on", source="detection")

        menu_calls = []
        monkeypatch.setattr(installation.channel, "ask", lambda *a, **kw: cec_detection.YES)
        monkeypatch.setattr(installation.channel, "is_abandoned", lambda: False)
        monkeypatch.setattr(installation, "_run_menu",
                            lambda env: menu_calls.append(1) or None)
        monkeypatch.setattr("cec_detection.run",
                            lambda env: tv_config.set_detection_complete(True))
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=True),
        })
        assert menu_calls == [1], "the menu must run once the resumed session finishes"


class TestTheInterruptedSessionPrompt:
    """Start over, or quit — and the answer is acted on.

    It used to offer "resume" or "start over" and then read neither: both
    answers fell through to the same `cec_detection.run()` from step 1,
    because there is no step-level resume to jump to. A question whose two
    answers run the same code is worse than a question with one answer.
    """

    def _interrupted_box(self):
        tv_config.set_technique("wake", "text_view_on", source="detection")

    def test_quitting_leaves_the_screen_without_running_the_procedure(self, monkeypatch):
        self._interrupted_box()
        ran, menu_calls = [], []
        monkeypatch.setattr(installation.channel, "ask",
                            lambda *a, **kw: cec_detection.NO)
        monkeypatch.setattr(installation.channel, "is_abandoned", lambda: False)
        monkeypatch.setattr(installation, "_run_menu",
                            lambda env: menu_calls.append(1) or None)
        # Raises rather than returning: if "quit" is ever ignored, start()
        # would otherwise loop here for ever — `interrupted` stays true
        # because a stub cannot clear it — and the test would hang instead
        # of failing. Measured: it did exactly that before this.
        def never_reached(env):
            ran.append(1)
            raise procedure_support.Abandoned("the procedure should not have run")
        monkeypatch.setattr("cec_detection.run", never_reached)
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=True),
        })
        assert ran == [], "quitting must not start the procedure over"
        assert menu_calls == [], "quitting leaves the screen, it does not fall to the menu"

    def test_starting_over_runs_the_procedure(self, monkeypatch):
        self._interrupted_box()
        ran = []
        monkeypatch.setattr(installation.channel, "ask",
                            lambda *a, **kw: cec_detection.YES)
        monkeypatch.setattr(installation.channel, "is_abandoned", lambda: False)
        monkeypatch.setattr(installation, "_run_menu", lambda env: None)
        monkeypatch.setattr("cec_detection.run",
                            lambda env: ran.append(1) or tv_config.set_detection_complete(True))
        installation.start({
            "cec": lambda: object(),
            "buttons": lambda: FakePairingHardware(any_button=True),
        })
        assert ran == [1]

    def test_it_never_offers_to_resume_something_it_cannot_resume(self):
        source = (REPO_ROOT / "api" / "installation.py").read_text(encoding="utf-8")
        block = source[source.index("if interrupted:"):]
        block = block[:block.index("continue")]
        # The accepts mapping itself, not the comment above it — which says
        # the word "resume" precisely to explain why it is not offered.
        accepts = next(line for line in block.splitlines()
                       if "cec_detection.YES:" in line and "cec_detection.NO:" in line)
        assert "resume" not in accepts, accepts
        assert '"start over"' in accepts and '"quit"' in accepts, accepts
        assert "answer != cec_detection.YES" in block, "the answer must be acted on"
