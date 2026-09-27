# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The guided CEC-detection procedure: how a box learns to drive the
television in front of it, with a human watching the screen.

Pure logic. This module never imports `cec_controller`, `hdmi_output`,
`screen` or `tv_config` — it only talks to the `Environment` protocol it is
handed, which is what lets it be tested against a small model television (a
power state, a displayed input, a policy consuming frames) instead of real
hardware. `modes.py` is the only place that builds the real environment,
because it is the only place allowed to touch the box's own HDMI output
(`begin_release_power_cycle`/`end_release_power_cycle`, for the one release
candidate whose mechanism is the output going to sleep, not a CEC frame).

One principle carries the whole procedure, straight from the spec: **during a
search, the box sends only what it is measuring.** Everything the product
layers on top afterwards — claiming the input, sleeping the output — can only
improve on what was measured here, which is why there is no confirmation
round after each answer.

Known simplification against the spec: there is no step-level resume. An
interrupted session is offered "start over" or "quit", never "resume",
because every step re-measures its technique from a live human anyway — so
skipping straight to where the last one stopped would save time and nothing
else, and a question whose two answers run the same code is worse than a
question with one answer.
"""

import logging
from typing import Optional, Protocol

from procedure_support import _Asker, _check_abandoned
# Re-exported on purpose, in the `x as x` form a linter reads as deliberate:
# `Abandoned` is what this module's callers catch, and it would be odd to have
# to import it from somewhere else to catch what `run()` raises.
from procedure_support import Abandoned as Abandoned

logger = logging.getLogger(__name__)

# The numbered steps, drawn by the renderer's own header (PageSpec.step)
# rather than written into each page's prose. One constant, because a counter
# spelled out by hand in eight separate strings is one nobody can keep right.
#
# Three steps carry a number today — turn on, turn off, give the screen back —
# while the preparation page ahead of them carries none. The total stays 4,
# counting the preparation, which is what the pages have always shown; change
# this one number if it should count only the numbered ones.
TOTAL_STEPS = 4

YES = "tv"      # the tv button: it happened / go on / choose this line
NO = "music"    # the music button: no / nothing happened / next line


class Environment(Protocol):
    """What a step needs, and nothing more. Production wraps `cec_controller`
    and `hdmi_output` (via `modes.py`); tests wrap a model television."""

    def ask(self, body: str, accepts: "dict[str, str]",
           seconds: "float | None" = None,
           title: "str | None" = None,
           step: "tuple[int, int] | None" = None) -> "Optional[str]": ...
    def abandoned(self) -> bool: ...
    def step_seconds(self) -> float: ...
    def cycle_seconds(self) -> float: ...

    # Names only, best-first, in the order a search tries them — never the
    # functions themselves. Reading `cec_controller.WAKE_TECHNIQUES` and
    # friends is `modes.py`'s job when it builds the real environment; a
    # test's environment names whatever its model television consumes.
    def wake_techniques(self) -> "tuple[str, ...]": ...
    def sleep_techniques(self) -> "tuple[str, ...]": ...
    def release_techniques(self) -> "tuple[str, ...]": ...

    def send_wake(self, technique: str) -> None: ...
    def send_sleep(self, technique: str) -> None: ...
    def send_release(self, technique: str) -> None: ...

    # Best-effort claim of the input (Active Source), for step 4: on a
    # television where that alone is enough to bring the box's screen back,
    # nothing else in the step would ever send it.
    def claim_input(self) -> None: ...

    # The power_cycle release candidate: not a CEC frame at all, the box's
    # own output going to sleep is the mechanism. Split in two rather than
    # one call, because the output must STAY asleep for the whole question
    # that follows (the person is looking at their television, not at the
    # box's own page, which is exactly the point) and only come back once an
    # answer — or a timeout — ends the wait.
    def begin_release_power_cycle(self) -> None: ...
    def end_release_power_cycle(self) -> None: ...

    def television_identity(self) -> dict: ...

    def save_wake(self, technique: str) -> None: ...
    def save_sleep(self, technique: str) -> None: ...
    def save_release(self, technique: str) -> None: ...
    def save_television(self, identity: dict) -> None: ...
    def finish(self) -> None: ...


class Result:
    """What the procedure produced, for the summary page and for the caller."""

    def __init__(self) -> None:
        self.wake: "str | None" = None
        self.sleep: "str | None" = None
        self.release: "str | None" = None
        self.television: dict = {}
        self.completed = False


def _checklist(tried: "list[tuple[str, bool]]") -> str:
    """What has been tried so far, one line per technique — plain "OK"/"no"
    markers rather than a tick/cross glyph: the same tofu-box risk already
    ruled out a Unicode arrow for the menu's cursor earlier, and this runs
    on whatever font is actually installed, never assumed."""
    return "\n".join(f"  {name}: {'OK' if ok else 'no'}" for name, ok in tried)


def _search(env: Environment, names: "tuple[str, ...]",
           prompt: str, seconds: float, send,
           step: "tuple[int, int] | None" = None) -> "str | None":
    """Send each technique in `names`, in order, asking after each one.

    Returns the name of the first technique the human confirmed, or None if
    the whole table was tried with no answer — the caller decides what that
    means (a failure page). Going through the whole table unanswered is the
    normal, expected shape of a search and never counts toward the
    three-unanswered-questions abandon rule (see `_Asker`).

    Each question shows what has been tried so far (and how it went) below
    the fixed explanation, so a search of several techniques is never just
    one static, unmoving page for however long it takes.
    """
    tried: "list[tuple[str, bool]]" = []
    for name in names:
        send(name)
        checklist = _checklist(tried)
        body = f"{prompt}\n\n{checklist}" if checklist else prompt
        answer = env.ask(body, {YES: "it happened", NO: "nothing"}, seconds=seconds,
                         step=step)
        _check_abandoned(env)
        ok = answer == YES
        tried.append((name, ok))
        if ok:
            return name
    return None


def _search_until_satisfied(env: Environment, asker: _Asker, *,
                            names: "tuple[str, ...]", send,
                            ready_body: str, search_body: str,
                            fail_title: str, fail_body: str,
                            step: "tuple[int, int] | None" = None) -> "str | None":
    """A ready page, then a search, then — on failure — the choice to try
    the whole thing again or go on without it. Loops for as long as the
    person keeps choosing "try again": there is no limit of one retry, so
    "redo" is a real affordance here, not a single extra life.

    The ready page exists so the search's first probe is never wasted on a
    television that is still mid-remote-control while somebody reads the
    instructions — measured against a real run, where the first attempt of
    a search launched immediately was routinely spent before the set was
    even in the right starting state.
    """
    retry = False
    while True:
        asker.ask(f"{ready_body}{' (again)' if retry else ''}",
                  {YES: "ready"}, seconds=None, step=step)
        found = _search(env, names, search_body, env.step_seconds(), send, step=step)
        if found is not None:
            return found
        # The failure code goes in the title, where the renderer draws it in
        # red above the body, rather than as the body's own first line.
        again = asker.ask(fail_body, {YES: "try again", NO: "go on without it"},
                          seconds=None, title=fail_title, step=step)
        if again != YES:
            return None
        retry = True


def _release_step(env: Environment, asker: _Asker,
                  wake: "str | None", sleep: "str | None",
                  step: "tuple[int, int]" = (3, TOTAL_STEPS)) -> "str | None":
    """Step 4 and step 4 bis together, retried as a pair for as long as the
    person keeps choosing "try again" at E5 — the same "redo the current
    step, not the whole procedure" shape `_search_until_satisfied` gives
    wake/sleep, just not built on top of it directly: this step has its own
    extra phase (4 bis) and E5's message depends on whether wake/sleep
    already succeeded.
    """
    retry = False
    while True:
        # The set is off at this point — step 2 has just been measuring how to
        # switch it off — and everything below needs it on, on the box's input,
        # with the person able to see these pages. The page used to say none of
        # that and the search's own page assumed all of it; a person works it
        # out, but a procedure that depends on being guessed is a procedure that
        # fails differently for each installer.
        asker.ask(
            "GIVING YOU BACK YOUR PROGRAMMES"
            f"{' (again)' if retry else ''}\n\n"
            + (f"Turning it off worked: {sleep}.\n\n" if sleep else "")
            + "Switch the TV back on with its own remote, and put it on the "
            "box's HDMI input so you can read this screen.\n\n"
            "When you're ready, answer below and I'll start trying "
            f"several ways — one every {int(env.step_seconds())} "
            "seconds.",
            {YES: "ready"}, seconds=None, step=step)
        release = _search(env, env.release_techniques(),
                          "GIVING YOU BACK YOUR PROGRAMMES\n\n"
                          "As soon as you get your programmes back: answer "
                          "below, then switch back to the box's HDMI input "
                          "so you can see the next page.",
                          env.step_seconds(), env.send_release, step=step)

        if release is None:
            # Step 4 bis: the same, with the box's output put fully asleep
            # for the whole question — the one candidate whose mechanism IS
            # the output sleeping, not a CEC frame. The output stays asleep
            # across the ask() below (the person is watching their
            # television, not the box) and is only reclaimed afterwards,
            # whichever way it goes.
            asker.ask(
                "SECOND TRY\n\n"
                "I'm trying again, cutting the output completely this "
                "time. This is the last method.\n\n"
                "When you're ready, answer below.",
                {YES: "ready"}, seconds=None, step=step)
            env.begin_release_power_cycle()
            try:
                got_it_back = asker.ask(
                    "SECOND TRY\n\n"
                    "Answer below as soon as you get your programmes "
                    "back, then switch back to the box's HDMI input so "
                    "you can see the next page.",
                    {YES: "got them back", NO: "still nothing"},
                    seconds=env.cycle_seconds(), step=step)
            finally:
                env.end_release_power_cycle()
            if got_it_back == YES:
                release = "power_cycle"

        if release is not None:
            return release

        # E5: candidates 5/5bis reuse both wake and sleep, so their failure
        # does not by itself mean the television always keeps the box's
        # input — offer to redo whichever of the two is still in doubt.
        # "try again" on YES, "go on without it" on NO: the same mapping
        # E2/E3 already use, on purpose — the two buttons must not swap
        # meaning between one failure page and the next.
        if wake and sleep:
            redo = asker.ask(
                "Since turning on and off both worked, the problem may "
                "be with one of those two rather than the TV itself.",
                {YES: "try again", NO: "go on without it"}, seconds=None,
                title="E5 — NOTHING GIVES THE SCREEN BACK", step=step)
            if redo != YES:
                return None
        else:
            asker.ask(
                "Look for a hotel mode or a startup channel in the "
                "TV's menu — no setting on the box can replace that.",
                {YES: "go on without it"}, seconds=None,
                title="E5 — NOTHING GIVES THE SCREEN BACK", step=step)
            return None
        retry = True


def _deprecated_check_active_source_alone(env: Environment, asker: _Asker,
                                          sleep: "str | None") -> None:
    """DEPRECATED — no longer called from `run()`. Kept, not deleted, for a
    future recalcitrant television.

    Used to be step "coming back to this screen alone": switch to your
    programmes, wait, and say whether the screen came back on its own after
    an Active Source claim. Removed because its answer was never acted on
    and never could be. `modes.py`'s `_show_the_box()` already sends
    `cec.switch_to_pi()` (Active Source) unconditionally on every single
    mode entry — there is no table of candidates to pick from the way
    wake/sleep/release have (`cec_controller.WAKE_TECHNIQUES` and friends);
    Active Source is the one standardised CEC message for "I am the active
    source," so there was nothing this measurement could have changed even
    if it had been recorded. Making somebody switch away, wait, and answer
    a question that altered nothing was pure lost time.

    If a television ever needs the claim made conditional — skipped, or
    replaced with something else, on a set where it actively interferes —
    this is where that measurement used to live, and the shape (a ready
    gate, `env.claim_input()`, a direct `env.ask()` exempt from the
    three-unanswered-questions abandon count) is still correct. It would
    need two things to actually matter again: `run()` calling it, and
    something downstream reading the result instead of discarding it.
    """
    asker.ask(
        "STEP — COMING BACK ON ITS OWN\n\n"
        + (f"Turning it off worked: {sleep}.\n\n" if sleep else "")
        + "When you're ready, switch to your programmes with your "
        "remote, then answer below. I'll then try to bring this screen "
        "back on my own, without you touching anything.",
        {YES: "ready"}, seconds=None)
    env.claim_input()
    env.ask(
        "STEP — COMING BACK ON ITS OWN\n\n"
        "Answer below depending on what happened.",
        {YES: "came back on its own", NO: "had to switch back by hand"},
        seconds=env.step_seconds())
    _check_abandoned(env)


def run(env: Environment) -> Result:
    result = Result()
    asker = _Asker(env)

    # --- Step 1: preparation --------------------------------------------
    identity = env.television_identity()
    env.save_television(identity)
    result.television = identity
    name = identity.get("name") or identity.get("manufacturer") or "this TV"
    asker.ask(
        f"TV INSTALLATION — PREPARATION\n\n"
        f"TV detected: {name}\n\n"
        "Before starting, in your TV's menu:\n"
        "1. Turn on HDMI-CEC (Anynet+, Bravia Sync, SimpLink, EasyLink...).\n"
        "2. Turn off quick start / eco mode — otherwise the TV stops "
        "responding once it is off.\n"
        "3. If there is a hotel mode or a startup channel, set it to your "
        "usual channel.\n\n"
        "Take the TV's remote, and stay in front of this screen.",
        {YES: "done"}, seconds=None)

    # The inventory runs again, after the press: this is the gate. Nothing
    # answering means step 2 would spend its budget proving nothing.
    identity = env.television_identity()
    env.save_television(identity)
    result.television = identity
    while not identity:
        answer = asker.ask(
            "Turn on HDMI-CEC in the TV's menu (under the name its maker "
            "gives it) · try another HDMI socket, often only one carries "
            "it · press a key on the TV's own remote and try again.",
            {YES: "try again", NO: "give up"}, seconds=None,
            title="E1 — NOTHING ANSWERS")
        if answer == NO:
            return _abandon_incomplete(result)
        identity = env.television_identity()
        env.save_television(identity)
        result.television = identity

    # --- Step 2: how to wake the set -------------------------------------
    wake = _search_until_satisfied(
        env, asker,
        names=env.wake_techniques(), send=env.send_wake,
        ready_body=(
            "TURN THE TV ON\n\n"
            "Turn the TV off with its own remote, then leave it off.\n\n"
            "When you're ready, answer below and I'll start trying to "
            f"turn it on — one way every {int(env.step_seconds())} "
            "seconds."),
        search_body=(
            "TURN THE TV ON\n\n"
            "As soon as it turns on: answer below. Switch back to the "
            "right HDMI input afterwards if needed."),
        fail_title="E2 — NOTHING TURNED IT ON",
        fail_body=("Check the quick start / eco setting, the remote's "
                  "battery, and the distance to the box."),
        step=(1, TOTAL_STEPS))
    if wake:
        env.save_wake(wake)
        result.wake = wake

    # --- Step 3: how to switch the set off -------------------------------
    sleep = _search_until_satisfied(
        env, asker,
        names=env.sleep_techniques(), send=env.send_sleep,
        ready_body=(
            "TURN THE TV OFF\n\n"
            + (f"Turning it on worked: {wake}.\n\n" if wake else "")
            + "Switch back to your programmes with your remote.\n\n"
            "When you're ready, answer below and I'll start trying to "
            f"turn it off — one way every {int(env.step_seconds())} "
            "seconds."),
        search_body=(
            "TURN THE TV OFF\n\n"
            "As soon as it turns off: answer below."),
        fail_title="E3 — NOTHING TURNED IT OFF",
        fail_body=("Check the quick start / eco setting, the remote's "
                  "battery, and the distance to the box."),
        step=(2, TOTAL_STEPS))
    if sleep:
        env.save_sleep(sleep)
        result.sleep = sleep

    # Step "coming back to this screen alone" used to run here. Removed from
    # the live procedure — see _deprecated_check_active_source_alone()'s own
    # docstring for why — so the numbering below runs 1 to 4, not 1 to 5.

    # --- Step 4: giving the screen back -----------------------------------
    release = _release_step(env, asker, wake, sleep)
    if release:
        env.save_release(release)
        result.release = release
        # A release that worked has, by definition, just sent the television
        # away from the box's input — so the summary below would be drawn on
        # an input nobody is looking at, and it waits for an answer with no
        # deadline. On the set this box was built for that is the *expected*
        # path (it ignores every CEC release frame and lands on power_cycle),
        # so the procedure would have ended by hanging on an invisible page.
        #
        # The pages above say how to come back by hand, because a person with
        # a remote is the only thing that always works. This claim is the box
        # trying to spare them that: the same Active Source every ordinary
        # mode entry sends, and the only use left for `claim_input()`.
        env.claim_input()

    # --- Step 6: the summary ----------------------------------------------
    summary = (
        f"DONE — {name}\n\n"
        f"turn on: {result.wake or '(not found)'}\n"
        f"turn off: {result.sleep or '(not found)'}\n"
        f"give the screen back: {result.release or '(not found)'}\n"
    )
    accept = asker.ask(summary, {YES: "save", NO: "start over"},
                       seconds=None)
    if accept == NO:
        return run(env)
    env.finish()
    result.completed = True
    return result


def _abandon_incomplete(result: Result) -> Result:
    result.completed = False
    return result
