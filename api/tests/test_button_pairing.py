# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The button-pairing procedure, against a fake bridge — the same shape
test_cec_detection.py uses for a fake television: a FakeEnvironment records
what was sent/bound and answers questions through a policy function, so a
test reads "TV ended up bound to this device" rather than asserting a
sequence of calls.
"""

import button_pairing as bp
from cec_detection import NO, YES


class FakeEnvironment:
    def __init__(self, answer_fn, *, bridge_ready=True, presses=None,
                rename_ok=True, actions_by_device=None,
                adapters=None, adapter_appears_after=0, bring_up_result=True):
        self.answer_fn = answer_fn
        self._bridge_ready = bridge_ready
        # queue of (device, action) per role, in pairing order, or None for
        # "no press this attempt" (a timeout).
        self._presses = list(presses or [])
        self._rename_ok = rename_ok
        self._actions_by_device = actions_by_device or {}
        self._abandoned = False
        self.bodies: "list[str]" = []
        self.asked: "list[tuple[str, dict]]" = []
        self.bound: "dict[str, tuple[str, list[str]]]" = {}
        self.reload_calls = 0
        self.pairing_open = False
        self.pairing_cycles = 0
        self._any_button = False
        # An adapter list a test can have appear after N empty polls,
        # simulating someone plugging the dongle in partway through.
        self._adapters = list(adapters) if adapters is not None else []
        self._adapter_appears_after = adapter_appears_after
        self._list_adapters_calls = 0
        self._bring_up_result = bring_up_result
        self.bring_up_calls: "list[str]" = []
        self.titles: "list[str | None]" = []
        self.steps: "list[tuple[int, int] | None]" = []
        self.stop_bridge_calls = 0

    # --- button_pairing.Environment ---------------------------------------

    def ask(self, body, accepts, seconds=None, title=None, step=None):
        # `title` and `step` are drawn by the renderer's own header
        # rather than written into the body, so a page's prose no
        # longer carries "STEP n OF m" or a failure code. Recorded
        # here so a test can still assert on either.
        self.titles.append(title)
        self.steps.append(step)
        self.bodies.append(body)
        self.asked.append((body, accepts))
        return self.answer_fn(accepts) if self.answer_fn else None

    def abandoned(self) -> bool:
        return self._abandoned

    def step_seconds(self) -> float:
        return 1

    def bridge_ready(self) -> bool:
        return self._bridge_ready

    def has_any_button(self) -> bool:
        return self._any_button

    def open_pairing(self) -> bool:
        self.pairing_open = True
        self.pairing_cycles += 1
        return True

    def close_pairing(self) -> None:
        self.pairing_open = False

    def capture_press(self):
        if not self._presses:
            return None
        return self._presses.pop(0)

    def rename_device(self, old, new) -> bool:
        return self._rename_ok

    def device_actions(self, device):
        return self._actions_by_device.get(device, [])

    def write_binding(self, role, device, actions) -> None:
        self.bound[role] = (device, list(actions))
        self._any_button = True

    def reload_bindings(self) -> None:
        self.reload_calls += 1

    def list_adapters(self):
        self._list_adapters_calls += 1
        if self._list_adapters_calls > self._adapter_appears_after:
            return list(self._adapters)
        return []

    def bring_bridge_up(self, port) -> bool:
        self.bring_up_calls.append(port)
        if self._bring_up_result:
            self._bridge_ready = True
        return self._bring_up_result

    def stop_bridge(self) -> None:
        self.stop_bridge_calls += 1


def answering(*, on_retry=NO, on_summary=YES):
    """A default policy good enough to drive a full run: ready pages always
    proceed, a retry offer declines by default (do not loop forever in a
    test), and the summary takes the given side."""
    def respond(accepts):
        values = set(accepts.values())
        if values == {"ready"}:
            return YES
        if values == {"try again", "go on without it"}:
            return on_retry
        if values == {"ok"}:
            return YES
        if values == {"save", "start over"}:
            return on_summary
        raise AssertionError(f"unexpected question: {accepts}")
    return respond


class TestNoBridge:
    def test_one_page_and_nothing_written(self):
        env = FakeEnvironment(answering(), bridge_ready=False)
        result = bp.run(env)
        assert result.paired == {}
        assert result.completed is False
        assert any("NO ZIGBEE BRIDGE" in b for b in env.bodies)

    def test_reload_is_never_called(self):
        env = FakeEnvironment(answering(), bridge_ready=False)
        bp.run(env)
        assert env.reload_calls == 0


class TestASuccessfulPairing:
    def test_both_roles_get_bound(self):
        env = FakeEnvironment(
            answering(),
            presses=[("0xa4c1", "single"), ("0xa4c2", "single")],
            actions_by_device={"TV": ["single", "double"], "Music": ["single"]})
        result = bp.run(env)
        assert result.completed is True
        assert result.paired == {"tv": "TV", "music": "Music"}
        assert env.bound["tv"] == ("TV", ["single", "double"])
        assert env.bound["music"] == ("Music", ["single"])

    def test_reload_happens_once_after_pairing(self):
        env = FakeEnvironment(answering(),
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run(env)
        assert env.reload_calls == 1

    def test_pairing_is_always_closed_after_a_press(self):
        env = FakeEnvironment(answering(),
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run(env)
        assert env.pairing_open is False

    def test_falls_back_to_the_observed_action_when_none_are_advertised(self):
        """device_actions() can come back empty (an unreadable definition) —
        the press just seen must still be bound, not nothing at all."""
        env = FakeEnvironment(answering(),
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run(env)
        assert env.bound["tv"] == ("TV", ["single"])

    def test_a_failed_rename_keeps_the_devices_own_name(self):
        env = FakeEnvironment(answering(), rename_ok=False,
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        result = bp.run(env)
        assert result.paired == {"tv": "0xa4c1", "music": "0xa4c2"}


class TestNoPress:
    def test_try_again_retries_the_same_role(self):
        attempts = {"n": 0}

        def answer_fn(accepts):
            values = set(accepts.values())
            if values == {"ready"}:
                return YES
            if values == {"try again", "go on without it"}:
                attempts["n"] += 1
                return YES if attempts["n"] == 1 else NO
            if values == {"save", "start over"}:
                return YES
            raise AssertionError(accepts)

        env = FakeEnvironment(answer_fn, presses=[None, ("0xa4c1", "single"), None])
        result = bp.run(env)
        assert result.paired == {"tv": "TV"}
        assert any("(again)" in b for b in env.bodies)

    def test_go_on_without_it_skips_to_the_next_role(self):
        env = FakeEnvironment(answering(on_retry=NO),
                              presses=[None, ("0xa4c2", "single")])
        result = bp.run(env)
        assert "tv" not in result.paired
        assert result.paired == {"music": "Music"}

    def test_go_on_without_it_on_both_still_completes(self):
        env = FakeEnvironment(answering(on_retry=NO), presses=[])
        result = bp.run(env)
        assert result.paired == {}
        assert result.completed is True
        assert any("(not paired)" in b for b in env.bodies)


class TestBootstrapNote:
    def test_shown_while_nothing_is_bound_yet(self):
        env = FakeEnvironment(answering(), presses=[("0xa4c1", "single"), None])
        bp.run(env)
        ready_bodies = [b for b in env.bodies if "THE TV BUTTON" in b]
        assert any("answer this one from a phone" in b for b in ready_bodies)

    def test_gone_once_a_button_is_bound(self):
        env = FakeEnvironment(answering(),
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run(env)
        ready_bodies = [b for b in env.bodies if "THE MUSIC BUTTON" in b]
        assert not any("answer this one from a phone" in b for b in ready_bodies)


class TestSummary:
    def test_start_over_runs_the_whole_procedure_again(self):
        calls = {"n": 0}

        def answer_fn(accepts):
            values = set(accepts.values())
            if values == {"ready"}:
                return YES
            if values == {"save", "start over"}:
                calls["n"] += 1
                return NO if calls["n"] == 1 else YES
            raise AssertionError(accepts)

        env = FakeEnvironment(
            answer_fn,
            presses=[("0xa4c1", "single"), ("0xa4c2", "single"),
                    ("0xa4c1", "single"), ("0xa4c2", "single")])
        result = bp.run(env)
        assert result.completed is True
        assert calls["n"] == 2


class AbandonsAfter(FakeEnvironment):
    """Answers `abandoned()` True once `open_pairing()` has been called more
    than `after` times — models the mode being left partway through a
    silent retry loop, without needing a real thread or event."""

    def __init__(self, *args, after, **kwargs):
        super().__init__(*args, **kwargs)
        self._after = after

    def abandoned(self) -> bool:
        return self.pairing_cycles > self._after


class TestRunBootstrap:
    """No button exists yet, so no question can be asked — the flow used
    only when nothing is bound, per docs on run_bootstrap() itself."""

    def test_no_page_ever_offers_a_button_to_press(self):
        """Every page must have an empty accepts dict: there is nothing
        that could answer one, and offering a legend that goes nowhere
        would be worse than none."""
        env = FakeEnvironment(lambda accepts: None,
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run_bootstrap(env)
        assert env.asked, "no page was ever drawn"
        assert all(accepts == {} for _, accepts in env.asked)

    def test_both_roles_get_bound_with_fixed_names(self):
        env = FakeEnvironment(lambda accepts: None,
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run_bootstrap(env)
        assert env.bound["tv"] == ("TV", ["single"])
        assert env.bound["music"] == ("Music", ["single"])

    def test_reload_happens_once_at_the_end(self):
        env = FakeEnvironment(lambda accepts: None,
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run_bootstrap(env)
        assert env.reload_calls == 1

    def test_a_role_with_no_press_yet_is_retried_silently(self):
        """Two empty attempts, then a real press — never a question asked
        about whether to keep trying."""
        env = FakeEnvironment(lambda accepts: None,
                              presses=[None, None, ("0xa4c1", "single"),
                                      ("0xa4c2", "single")])
        bp.run_bootstrap(env)
        # TV: two empty attempts then a press (3 cycles); Music: one (4 total).
        assert env.pairing_cycles == 4
        assert env.bound["tv"] == ("TV", ["single"])
        assert not any("try again" in b.lower() for b in env.bodies)

    def test_reload_is_never_called_if_abandoned_before_any_press(self):
        env = AbandonsAfter(lambda accepts: None, presses=[], after=0)
        bp.run_bootstrap(env)
        assert env.bound == {}
        assert env.reload_calls == 0

    def test_stops_as_soon_as_abandoned_mid_retry(self):
        env = AbandonsAfter(lambda accepts: None,
                            presses=[None, None, None, None, None], after=2)
        bp.run_bootstrap(env)
        assert env.bound == {}
        assert env.pairing_cycles <= 3


class FakeClock:
    """A controllable time.monotonic(), so the GIVE_UP_SECONDS deadline can
    be tested without a real 20-minute wait."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestBridgeBootstrap:
    """run_bootstrap() must bring the Zigbee bridge up itself when it is
    not already running — nobody should need scripts/setup-zigbee.py or an
    SSH session just to get a fresh box's two buttons working."""

    def test_skips_straight_to_pairing_when_already_ready(self):
        """The existing behaviour, unchanged: no adapter-related page at
        all when the bridge is already up."""
        env = FakeEnvironment(lambda accepts: None, bridge_ready=True,
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run_bootstrap(env)
        assert not any("ADAPTER" in b for b in env.bodies)
        assert env.bound["tv"] == ("TV", ["single"])

    def test_finds_the_adapter_and_proceeds_to_pairing(self):
        env = FakeEnvironment(lambda accepts: None, bridge_ready=False,
                              adapters=["usb-Silicon_Labs-if00"],
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run_bootstrap(env)
        assert env.bring_up_calls == ["usb-Silicon_Labs-if00"]
        assert any("usb-Silicon_Labs-if00" in b for b in env.bodies)
        assert env.bound["tv"] == ("TV", ["single"])

    def test_polls_silently_until_an_adapter_appears(self):
        env = FakeEnvironment(lambda accepts: None, bridge_ready=False,
                              adapters=["usb-adapter"], adapter_appears_after=3,
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run_bootstrap(env)
        assert any("NO ZIGBEE ADAPTER FOUND" in b for b in env.bodies)
        assert env.bring_up_calls == ["usb-adapter"]
        assert env.bound["tv"] == ("TV", ["single"])

    def test_retries_the_whole_search_if_bring_up_fails(self):
        """An adapter is present but Zigbee2MQTT still cannot talk to it —
        not a reason to give up immediately, only after the deadline."""
        env = FakeEnvironment(lambda accepts: None, bridge_ready=False,
                              adapters=["usb-flaky"], bring_up_result=False)
        env._bridge_ready = False  # bring_bridge_up() must not flip this

        real_bring = env.bring_bridge_up
        calls = {"n": 0}

        def bring_up(port):
            calls["n"] += 1
            if calls["n"] >= 3:
                env._bring_up_result = True
            return real_bring(port)

        env.bring_bridge_up = bring_up
        env._presses = [("0xa4c1", "single"), ("0xa4c2", "single")]
        bp.run_bootstrap(env)
        assert calls["n"] == 3
        assert env.bound["tv"] == ("TV", ["single"])

    def test_gives_up_and_stops_the_bridge_after_the_deadline(self, monkeypatch):
        clock = FakeClock()
        monkeypatch.setattr(bp.time, "monotonic", clock)
        monkeypatch.setattr(bp, "GIVE_UP_SECONDS", 100)
        monkeypatch.setattr(bp, "ADAPTER_POLL_SECONDS", 20)

        def answer_fn(accepts):
            clock.advance(20)  # each empty poll costs real time
            return None

        env = FakeEnvironment(answer_fn, bridge_ready=False, adapters=[])
        bp.run_bootstrap(env)
        assert env.stop_bridge_calls == 1
        assert any("NO RESPONSE" in b for b in env.bodies)
        assert env.bound == {}

    def test_no_button_answers_no_question_during_the_adapter_search(self):
        """Nothing here can be answered — there is no button yet by
        definition, the same reasoning as the rest of run_bootstrap()."""
        env = FakeEnvironment(lambda accepts: None, bridge_ready=False,
                              adapters=["usb-adapter"],
                              presses=[("0xa4c1", "single"), ("0xa4c2", "single")])
        bp.run_bootstrap(env)
        assert all(accepts == {} for _, accepts in env.asked)

    def test_abandoning_during_the_adapter_search_stops_at_once(self):
        env = FakeEnvironment(lambda accepts: None, bridge_ready=False, adapters=[])
        env._abandoned = True
        bp.run_bootstrap(env)
        assert env.stop_bridge_calls == 0
        assert env.bring_up_calls == []
        assert env.bound == {}
