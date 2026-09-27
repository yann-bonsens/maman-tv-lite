# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""What every installation-screen procedure shares: the abandon rule.

Split out of `cec_detection.py` once a second procedure (`button_pairing.py`)
needed the exact same three-unanswered-questions logic — one copy, not two
that could drift apart. Pure logic, like the procedures themselves: this
module only talks to the same minimal `ask`/`abandoned` shape every
procedure's own `Environment` protocol already provides structurally, never
imports a specific procedure's protocol.
"""

from typing import Optional, Protocol

# Three questions in a row with no answer abandon the procedure: a button
# with a flat battery must not leave the box stuck on a page for ever.
MAX_MISSES = 3


class AskEnvironment(Protocol):
    """The one shape every procedure's own `Environment` protocol carries,
    structurally — no procedure needs to inherit from this explicitly."""

    def ask(self, body: str, accepts: "dict[str, str]",
           seconds: "float | None" = None,
           title: "str | None" = None,
           step: "tuple[int, int] | None" = None) -> "Optional[str]": ...
    def abandoned(self) -> bool: ...


class Abandoned(Exception):
    """The procedure stopped: asked to leave, or silence for three questions
    running. Either way nothing further is written."""


def _check_abandoned(env: AskEnvironment) -> None:
    if env.abandoned():
        raise Abandoned("the mode was left")


class _Asker:
    """Wraps `env.ask` for a procedure's own decision points: counts
    unanswered questions, raises on the third in a row and on explicit
    abandonment. A step function only ever sees a real token.

    Deliberately **not** used inside a search's per-technique probes: trying
    several techniques in turn is expected to collect several silent windows
    on the way to a working one (or to a failure page, if none works) — that
    is the normal shape of a search, not a sign that nobody is in the room
    any more. The three-in-a-row rule instead protects the real decision
    points: is the preparation done, did something happen, do you want to
    try again.
    """

    def __init__(self, env: AskEnvironment) -> None:
        self._env = env
        self._misses = 0

    def ask(self, body: str, accepts: "dict[str, str]",
           seconds: "Optional[float]" = None,
           title: "Optional[str]" = None,
           step: "Optional[tuple[int, int]]" = None) -> str:
        answer = self._env.ask(body, accepts, seconds, title=title, step=step)
        _check_abandoned(self._env)
        if answer is None:
            self._misses += 1
            if self._misses >= MAX_MISSES:
                raise Abandoned("three questions in a row went unanswered")
            return self.ask(body, accepts, seconds, title=title, step=step)
        self._misses = 0
        return answer
