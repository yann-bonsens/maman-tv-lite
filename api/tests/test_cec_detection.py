# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The CEC-detection procedure, against a model television.

A small object with a policy of which techniques "work", consuming the
frames the procedure sends — so a test reads "after this step the box knows
how to wake, sleep and release the set" rather than asserting a sequence of
calls. Matches docs/development/installation-screen.md's own testing section.

`answer_fn` is matched against each question's `accepts` values (the meaning
shown next to each button, e.g. "it happened"/"nothing") rather than its
body text: every question cec_detection.py asks has a distinct pair of
meanings, which is a much more robust thing for a test to key off than a
sentence. Tests that need to inspect the body itself (the running checklist,
the ready gate) read `env.bodies`, populated alongside `answer_fn`.
"""

from pathlib import Path

import pytest

import cec_detection as cd

# Anchored on this file, not on the working directory: CI runs pytest from
# api/, where a relative "api/..." path does not exist.
REPO_ROOT = Path(__file__).resolve().parents[2]

YES, NO = cd.YES, cd.NO


class FakeEnvironment:
    """A model television: which techniques it obeys, and what was actually
    sent and saved. `answer_fn(accepts) -> token | None` decides every
    question; a None simulates an unanswered (timed out) question."""

    def __init__(self, answer_fn, *, works_wake=(), works_sleep=(),
                works_release=(), identity=None):
        self.answer_fn = answer_fn
        self.works_wake = set(works_wake)
        self.works_sleep = set(works_sleep)
        self.works_release = set(works_release)
        self.identity = {"name": "MODEL TV"} if identity is None else identity
        self._abandoned = False
        self.sent: "list[tuple[str, str]]" = []
        self.saved: dict = {}
        self.output_asleep = False
        self.bodies: "list[str]" = []
        self.titles: "list[str | None]" = []
        self.steps: "list[tuple[int, int] | None]" = []
        self.claim_input_calls = 0

    # --- cec_detection.Environment ---------------------------------------

    def ask(self, body, accepts, seconds=None, title=None, step=None):
        # `title` and `step` are drawn by the renderer's own header
        # rather than written into the body, so a page's prose no
        # longer carries "STEP n OF m" or a failure code. Recorded
        # here so a test can still assert on either.
        self.titles.append(title)
        self.steps.append(step)
        self.bodies.append(body)
        return self.answer_fn(accepts)

    def abandoned(self) -> bool:
        return self._abandoned

    def step_seconds(self) -> float:
        return 1

    def cycle_seconds(self) -> float:
        return 1

    def wake_techniques(self):
        return ("a", "b", "c")

    def sleep_techniques(self):
        return ("x", "y")

    def release_techniques(self):
        return ("r1", "r2")

    def send_wake(self, technique: str) -> None:
        self.sent.append(("wake", technique))

    def send_sleep(self, technique: str) -> None:
        self.sent.append(("sleep", technique))

    def send_release(self, technique: str) -> None:
        self.sent.append(("release", technique))

    def claim_input(self) -> None:
        self.claim_input_calls += 1

    def begin_release_power_cycle(self) -> None:
        self.sent.append(("release", "power_cycle"))
        self.output_asleep = True

    def end_release_power_cycle(self) -> None:
        self.output_asleep = False

    def television_identity(self) -> dict:
        return dict(self.identity)

    def save_wake(self, technique: str) -> None:
        self.saved["wake"] = technique

    def save_sleep(self, technique: str) -> None:
        self.saved["sleep"] = technique

    def save_release(self, technique: str) -> None:
        self.saved["release"] = technique

    def save_television(self, identity: dict) -> None:
        self.saved["television"] = identity

    def finish(self) -> None:
        self.saved["finished"] = True


def _last_sent(env: FakeEnvironment, kind: str) -> "str | None":
    for sent_kind, technique in reversed(env.sent):
        if sent_kind == kind:
            return technique
    return None


def answering(*, works_wake=(), works_sleep=(), works_release=(),
             on_resync=YES, on_summary=YES, on_e5=YES):
    """A reasonably complete default policy: confirms whichever technique was
    just sent if it is in the corresponding `works_*` set, accepts the
    preparation and step 4 pages, and takes the given side of the summary /
    E5 questions. Good enough to drive a full run; edge cases build their own
    `answer_fn` instead.
    """
    def answer_fn(env: FakeEnvironment):
        def respond(accepts):
            values = set(accepts.values())
            if values == {"done"}:
                return YES
            if values == {"ready"}:
                return YES
            if values == {"it happened", "nothing"}:
                kind, technique = env.sent[-1]
                works = {"wake": works_wake, "sleep": works_sleep,
                        "release": works_release}[kind]
                return YES if technique in works else NO
            if values == {"came back on its own", "had to switch back by hand"}:
                return on_resync
            if values == {"got them back", "still nothing"}:
                return YES if "power_cycle" in works_release else NO
            if values == {"try again", "give up"}:
                return NO  # do not loop forever in a test
            if values == {"try again", "go on without it"}:
                # E2/E3's own retry, and (same token mapping, on purpose —
                # see cec_detection.py) E5's wake&sleep-worked question too:
                # a test that wants to say "try again" specifically at E5
                # builds its own answer_fn instead of using this default.
                return NO
            if values == {"go on without it"}:
                return on_e5
            if values == {"save", "start over"}:
                return on_summary
            raise AssertionError(f"unexpected question: {accepts}")
        return respond
    return answer_fn


def run_with(**policy) -> "tuple[cd.Result, FakeEnvironment]":
    env = FakeEnvironment(None, works_wake=policy.get("works_wake", ()),
                          works_sleep=policy.get("works_sleep", ()),
                          works_release=policy.get("works_release", ()))
    env.answer_fn = answering(**policy)(env)
    result = cd.run(env)
    return result, env


class TestAFullSuccess:
    def test_it_finds_a_technique_for_every_direction(self):
        result, env = run_with(works_wake={"b"}, works_sleep={"y"},
                               works_release={"r2"})
        assert result.completed is True
        assert result.wake == "b"
        assert result.sleep == "y"
        assert result.release == "r2"

    def test_it_stops_at_the_first_working_technique(self):
        """Best-first: "a" and "b" both work, but "a" is tried first."""
        result, env = run_with(works_wake={"a", "b"}, works_sleep={"x"},
                               works_release={"r1"})
        assert result.wake == "a"
        assert [t for k, t in env.sent if k == "wake"] == ["a"]

    def test_it_saves_progressively_with_source_detection(self):
        _, env = run_with(works_wake={"a"}, works_sleep={"x"}, works_release={"r1"})
        assert env.saved["wake"] == "a"
        assert env.saved["sleep"] == "x"
        assert env.saved["release"] == "r1"
        assert env.saved["finished"] is True

    def test_it_reads_the_television_identity_twice_in_step_one(self):
        """Once before the prep page, once after — the second read is the
        gate that decides E1."""
        calls = []
        env = FakeEnvironment(None, works_wake={"a"}, works_sleep={"x"},
                              works_release={"r1"})
        real_identity = env.television_identity
        env.television_identity = lambda: (calls.append(1), real_identity())[1]
        env.answer_fn = answering(works_wake={"a"}, works_sleep={"x"},
                                  works_release={"r1"})(env)
        cd.run(env)
        assert len(calls) == 2


class TestReadTimeAndVisibility:
    """Feedback from testing on real hardware: give the person time to read
    and get the TV into position before a timed search's first probe goes
    out, and show what has been tried so far as a search runs."""

    def test_a_ready_question_precedes_the_first_wake_probe(self):
        """The first technique must not be sent before the ready page is
        answered — wasted on a TV the person hasn't turned off yet."""
        env = FakeEnvironment(None, works_wake={"a"}, works_sleep={"x"},
                              works_release={"r1"})
        default = answering(works_wake={"a"}, works_sleep={"x"},
                            works_release={"r1"})(env)
        seen_ready_with_nothing_sent_yet = []

        def answer_fn(accepts):
            if set(accepts.values()) == {"ready"} and not env.sent:
                seen_ready_with_nothing_sent_yet.append(True)
            return default(accepts)

        env.answer_fn = answer_fn
        cd.run(env)
        assert seen_ready_with_nothing_sent_yet, \
            "no ready question was asked before anything was sent"

    def test_the_checklist_shows_earlier_attempts_but_not_the_current_one(self):
        result, env = run_with(works_wake={"c"}, works_sleep={"x"},
                               works_release={"r1"})
        # "c" is third in FakeEnvironment.wake_techniques() = ("a", "b", "c"),
        # so "a" and "b" must have been tried and shown as failed first.
        assert result.wake == "c"
        # The question asked right before "c" succeeds must already list a
        # and b as failed attempts.
        last_wake_body = [b for b in env.bodies if "a: no" in b or "b: no" in b]
        assert last_wake_body, "the checklist never showed a prior failed attempt"
        assert "a: no" in last_wake_body[-1]
        assert "b: no" in last_wake_body[-1]
        assert "c:" not in last_wake_body[-1], \
            "the technique being tried right now must not already show a result"

    def test_a_retry_gets_its_own_ready_page_labelled_again(self):
        env = FakeEnvironment(None, works_wake=(), works_sleep={"x"},
                              works_release={"r1"})
        retries = {"n": 0}

        def answer_fn(accepts):
            values = set(accepts.values())
            if values in ({"done"}, {"ready"}):
                return YES
            if values == {"it happened", "nothing"}:
                kind, technique = env.sent[-1]
                if kind == "wake":
                    # Fails the first pass, succeeds once retried once.
                    return YES if retries["n"] >= 1 and technique == "a" else NO
                if kind == "sleep":
                    return YES if technique in env.works_sleep else NO
                return YES if technique in env.works_release else NO
            if values == {"try again", "go on without it"}:
                retries["n"] += 1
                return YES if retries["n"] == 1 else NO
            if values == {"came back on its own", "had to switch back by hand"}:
                return YES
            if values == {"got them back", "still nothing"}:
                return NO
            if values == {"go on without it"}:
                return YES
            if values == {"save", "start over"}:
                return YES
            raise AssertionError(accepts)

        env.answer_fn = answer_fn
        result = cd.run(env)
        assert result.wake == "a"
        assert any("(again)" in b for b in env.bodies), \
            "a retried ready page must say so, not look identical to the first"

    def test_a_found_technique_is_shown_on_the_next_page(self):
        """Real-hardware feedback: which technique worked is the single most
        important thing to see, and used to be visible only as an entry in
        a later failure checklist (or not at all, when the next attempt
        with a different table never fails). It must be shown up front,
        without needing an extra press to see it."""
        result, env = run_with(works_wake={"a"}, works_sleep={"x"},
                               works_release={"r1"})
        assert result.wake == "a"
        assert result.sleep == "x"
        assert any("Turning it on worked: a" in b for b in env.bodies), \
            "the wake result must be shown before the sleep step starts"
        assert any("Turning it off worked: x" in b for b in env.bodies), \
            "the sleep result must be shown before the release step starts"


class TestFailurePages:
    def test_e1_nothing_answers_on_the_bus(self):
        """No identity at all -> E1 -> abandon (NO = "give up")."""
        env = FakeEnvironment(None, identity={})

        def answer_fn(accepts):
            if set(accepts.values()) == {"done"}:
                return YES
            if set(accepts.values()) == {"try again", "give up"}:
                return NO
            raise AssertionError(f"should not be asked: {accepts}")

        env.answer_fn = answer_fn
        result = cd.run(env)
        assert result.completed is False
        assert "finished" not in env.saved

    def test_e1_retry_can_recover(self):
        """The set answers on the second inventory read."""
        env = FakeEnvironment(None, works_wake={"a"}, works_sleep={"x"},
                              works_release={"r1"})
        attempts = {"n": 0}
        real_identity = env.television_identity

        def flaky_identity():
            attempts["n"] += 1
            return {} if attempts["n"] <= 2 else real_identity()

        env.television_identity = flaky_identity
        default = answering(works_wake={"a"}, works_sleep={"x"},
                            works_release={"r1"})(env)

        def answer_fn(accepts):
            if set(accepts.values()) == {"try again", "give up"}:
                return YES
            return default(accepts)

        env.answer_fn = answer_fn
        result = cd.run(env)
        assert result.completed is True

    def test_e2_no_wake_technique_continues_without_one(self):
        result, env = run_with(works_wake=(), works_sleep={"x"}, works_release={"r1"})
        assert result.wake is None
        assert "wake" not in env.saved
        assert result.completed is True

    def test_e2_offers_one_retry(self):
        """"try again" tries the whole table again, once."""
        env = FakeEnvironment(None, works_wake=(), works_sleep={"x"},
                              works_release={"r1"})
        default = answering(works_wake=(), works_sleep={"x"},
                            works_release={"r1"})(env)
        asked_retry = {"n": 0}

        def answer_fn(accepts):
            if set(accepts.values()) == {"try again", "go on without it"}:
                asked_retry["n"] += 1
                return YES if asked_retry["n"] == 1 else NO
            return default(accepts)

        env.answer_fn = answer_fn
        cd.run(env)
        # One search of 3, then a full retry of 3 more: 6 wake probes.
        assert len([1 for k, _ in env.sent if k == "wake"]) == 6

    def test_e3_no_sleep_technique_continues_without_one(self):
        result, env = run_with(works_wake={"a"}, works_sleep=(), works_release={"r1"})
        assert result.sleep is None
        assert result.completed is True


class TestStepFiveAndItsFailurePages:
    def test_the_power_cycle_candidate_puts_the_output_to_sleep_and_back(self):
        result, env = run_with(works_wake={"a"}, works_sleep={"x"},
                               works_release={"power_cycle"})
        assert result.release == "power_cycle"
        assert env.output_asleep is False, "the output must be reclaimed afterwards"

    def test_e5_continuing_without_a_release_technique(self):
        result, env = run_with(works_wake={"a"}, works_sleep={"x"},
                               works_release=(), on_e5=YES)
        assert result.release is None
        assert result.completed is True

    def test_e5_offers_to_retry_just_the_release_step(self):
        """"try again" at E5 redoes only the release step (5 + 5 bis) —
        wake and sleep, already confirmed, are neither re-asked nor
        disturbed. Same YES="try again" mapping as E2/E3 on purpose, so a
        test can't tell them apart by their accepts alone in a scenario
        where wake/sleep never fail — which is exactly why this test keeps
        its own hand-written policy instead of `answering()`."""
        env = FakeEnvironment(None, works_wake={"a"}, works_sleep={"x"})
        attempts = {"n": 0}

        def answer_fn(accepts):
            values = set(accepts.values())
            if values in ({"done"}, {"ready"}):
                return YES
            if values == {"it happened", "nothing"}:
                kind, technique = env.sent[-1]
                if kind == "wake":
                    return YES if technique in env.works_wake else NO
                if kind == "sleep":
                    return YES if technique in env.works_sleep else NO
                # release: fails on the first attempt, succeeds on the retry
                return YES if attempts["n"] >= 1 and technique == "r1" else NO
            if values == {"came back on its own", "had to switch back by hand"}:
                return YES
            if values == {"got them back", "still nothing"}:
                return NO
            if values == {"try again", "go on without it"}:
                attempts["n"] += 1
                return YES  # this can only be E5 here: wake/sleep never fail
            if values == {"save", "start over"}:
                return YES
            raise AssertionError(accepts)

        env.answer_fn = answer_fn
        result = cd.run(env)
        assert result.completed is True
        assert result.release == "r1"
        assert result.wake == "a"
        assert result.sleep == "x"

    def test_e5_without_wake_or_sleep_only_offers_to_continue(self):
        result, env = run_with(works_wake=(), works_sleep=(), works_release=())
        assert result.completed is True
        assert result.release is None


class TestAbandonment:
    def test_explicit_abandonment_stops_the_procedure_at_once(self):
        env = FakeEnvironment(None, works_wake={"a"})

        def answer_fn(accepts):
            env._abandoned = True
            return None

        env.answer_fn = answer_fn
        with pytest.raises(cd.Abandoned):
            cd.run(env)

    def test_three_unanswered_questions_abandon(self):
        """Only questions outside a technique search count — see
        cec_detection._Asker's own docstring. Step 1's preparation page has
        no other question before it, so three silent answers there abandon
        directly."""
        env = FakeEnvironment(None)
        env.answer_fn = lambda accepts: None
        with pytest.raises(cd.Abandoned):
            cd.run(env)

    def test_a_full_table_search_going_unanswered_does_not_abandon(self):
        """The normal shape of "nothing worked": every wake technique times
        out, and the procedure reaches E2 rather than raising Abandoned."""
        env = FakeEnvironment(None, works_sleep={"x"}, works_release={"r1"})

        def answer_fn(accepts):
            values = set(accepts.values())
            if values in ({"done"}, {"ready"}):
                return YES
            if values == {"it happened", "nothing"}:
                kind, technique = env.sent[-1]
                if kind == "wake":
                    return None  # every wake probe goes unanswered
                return YES if technique in env.works_sleep | env.works_release else NO
            if values == {"try again", "go on without it"}:
                return NO
            if values == {"came back on its own", "had to switch back by hand"}:
                return YES
            if values == {"got them back", "still nothing"}:
                return NO
            if values == {"go on without it"}:
                return YES
            if values == {"save", "start over"}:
                return YES
            raise AssertionError(accepts)

        env.answer_fn = answer_fn
        result = cd.run(env)
        assert result.completed is True
        assert result.wake is None


class TestTheSummary:
    def test_declining_the_summary_restarts_the_whole_procedure(self):
        env = FakeEnvironment(None, works_wake={"a"}, works_sleep={"x"},
                              works_release={"r1"})
        attempts = {"n": 0}
        default = answering(works_wake={"a"}, works_sleep={"x"},
                            works_release={"r1"})(env)

        def answer_fn(accepts):
            if set(accepts.values()) == {"save", "start over"}:
                attempts["n"] += 1
                return NO if attempts["n"] == 1 else YES
            return default(accepts)

        env.answer_fn = answer_fn
        result = cd.run(env)
        assert result.completed is True
        assert attempts["n"] == 2


class TestTheDeprecatedActiveSourceCheck:
    """`_deprecated_check_active_source_alone()` is no longer called from
    `run()` (see its own docstring for why), but it is kept rather than
    deleted, so it is still exercised directly here rather than left
    completely untested code."""

    def test_has_a_ready_gate_before_the_timed_question(self):
        env = FakeEnvironment(None)
        env.answer_fn = lambda accepts: YES
        asker = cd._Asker(env)
        cd._deprecated_check_active_source_alone(env, asker, sleep="x")
        assert len(env.bodies) == 2, \
            "a ready page, then the timed observation question"

    def test_claims_the_input_before_the_observation_question(self):
        env = FakeEnvironment(None)
        env.answer_fn = lambda accepts: YES
        asker = cd._Asker(env)
        cd._deprecated_check_active_source_alone(env, asker, sleep="x")
        assert env.claim_input_calls == 1

    def test_the_observation_question_never_counts_toward_abandon(self):
        """Either answer is a result, not a miss — nothing downstream reads
        it, so a timeout here must never tip the three-unanswered-questions
        budget on its own."""
        env = FakeEnvironment(None)

        def answer_fn(accepts):
            if set(accepts.values()) == {"ready"}:
                return YES
            return None  # the observation question always times out

        env.answer_fn = answer_fn
        asker = cd._Asker(env)
        cd._deprecated_check_active_source_alone(env, asker, sleep="x")
        # No Abandoned raised despite the timeout: a single miss here is not
        # one of the three that would trip _Asker's own counter.
        assert asker._misses == 0


class TestTheStepCounterAndFailureCodes:
    """Drawn by the renderer's header and title, never written into the prose.

    The first version spelled "STEP n OF 4" out by hand in eight separate
    strings, against three numbered steps — and `PageSpec.step`/`.title`,
    built and tested for exactly this, were never passed anything.
    """

    def test_no_page_spells_its_own_step_number(self):
        _, env = run_with(works_wake={"b"}, works_sleep={"y"}, works_release={"r2"})
        for body in env.bodies:
            assert "STEP " not in body, f"a step number written into the prose: {body!r}"

    def test_each_numbered_step_declares_itself_to_the_renderer(self):
        _, env = run_with(works_wake={"b"}, works_sleep={"y"}, works_release={"r2"})
        seen = [step for step in env.steps if step is not None]
        assert seen, "no page ever carried a step counter"
        assert {n for n, _ in seen} == {1, 2, 3}, seen
        assert {total for _, total in seen} == {cd.TOTAL_STEPS}, seen

    def test_the_total_is_one_constant(self):
        source = (REPO_ROOT / "api" / "cec_detection.py").read_text(encoding="utf-8")
        body = source[source.index("def run("):]
        assert "TOTAL_STEPS" in body
        assert " OF 4" not in body and " OF 3" not in body

    def test_a_failure_code_goes_in_the_title_not_the_body(self):
        """Where the renderer draws it in red, above the body."""
        # Nothing works anywhere, so every search runs out and each failure
        # page is reached; the default policy then declines every retry.
        _, env = run_with(works_wake=set(), works_sleep=set(), works_release=set())
        codes = [title for title in env.titles if title]
        assert any(title.startswith("E2") for title in codes), codes
        for body in env.bodies:
            assert not body.startswith("E"), f"a failure code left in the body: {body!r}"
