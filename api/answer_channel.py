# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The question/answer channel a guided procedure runs on.

A procedure is a list of steps; a step asks a question and waits for an
answer. This module is the only thing that knows how to ask and how to
answer — a procedure's own logic (see `cec_detection.py`) only ever calls
`ask()`, and never touches a screen, a button or HTTP directly.

Generic on purpose, not HTTP-specific: today only `POST /installation/answer`
calls `answer()`, but a Zigbee button pressed later would call exactly the
same function with exactly the same arguments.

Two rules carry the whole design, both already measured elsewhere in this
project rather than invented here:

- **An answer belongs to the question that was displayed when it arrived.**
  A press (or a request) naming a question id that is not the current one is
  dropped. A single Zigbee press reaching the bridge twice, 0.5 s apart, with
  two different link qualities, is what originally motivated this rule
  (`zigbee_bridge.py`'s own debounce) — the same id-matching mechanism
  reused here also protects an HTTP caller that resends a request it thinks
  timed out.
- **A press that arrives after a question has timed out is dropped**, for the
  same reason and with the same mechanism: once `ask()` gives up on a
  question, its id is no longer current.
"""

import dataclasses
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# How often ask()'s wait loop wakes up to check for an abandon request.
# threading.Event has no "wait on either of two events", so the wait is
# polled at a short interval instead of blocking on the answer alone with no
# way to notice an abandonment that arrives mid-wait.
POLL_SECONDS = 0.2


@dataclasses.dataclass(frozen=True)
class Question:
    id: int
    body: str
    accepts: "dict[str, str]"      # token -> what it means, e.g. {"tv": "it happened"}
    seconds: "float | None"        # how long to wait, or None for no deadline
    asked_at: float                # time.monotonic() when this question was published
    # Drawn by the renderer's own header and title, rather than written into
    # the body by hand: a counter spelled out in prose is one nobody can keep
    # right, and the first version of this procedure shipped "STEP n OF 4"
    # against three steps for exactly that reason.
    title: "str | None" = None           # a failure code, drawn in red above the body
    step: "tuple[int, int] | None" = None  # (n, total) -> "STEP n OF total"


class Channel:
    """One question at a time, exactly like the mode it belongs to."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._current: "Optional[Question]" = None
        self._answered = threading.Event()
        self._answer_token: "Optional[str]" = None
        self._abandon = threading.Event()

    def reset(self) -> None:
        """Start a fresh session: clears any leftover abandon flag."""
        with self._lock:
            self._current = None
            self._answer_token = None
        self._answered.clear()
        self._abandon.clear()

    def ask(self, body: str, accepts: "dict[str, str]",
            seconds: "float | None" = None,
            publish: "Optional[Callable[[Question], None]]" = None,
            title: "str | None" = None,
            step: "tuple[int, int] | None" = None) -> "str | None":
        """Publish a question and block until it is answered, times out, or
        the session is abandoned. Returns the accepted token, or None.

        `publish`, if given, runs with the new Question once it is current —
        the caller's hook for drawing the page, so this module never imports
        a renderer.
        """
        with self._lock:
            question = Question(id=self._next_id, body=body, accepts=dict(accepts),
                                seconds=seconds, asked_at=time.monotonic(),
                                title=title, step=step)
            self._next_id += 1
            self._current = question
            self._answer_token = None
        self._answered.clear()

        if publish is not None:
            publish(question)

        deadline = None if seconds is None else time.monotonic() + seconds
        while not self._abandon.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            wait_for = POLL_SECONDS if remaining is None else min(POLL_SECONDS, remaining)
            if self._answered.wait(wait_for):
                break

        with self._lock:
            if self._current is not question or self._abandon.is_set():
                self._current = None if self._current is question else self._current
                return None
            token = self._answer_token if self._answered.is_set() else None
            self._current = None
            return token

    def answer(self, id: int, token: str) -> bool:
        """Accept an answer to the current question. False when dropped."""
        with self._lock:
            current = self._current
            if current is None or current.id != id:
                logger.info("dropped answer %r for question %d (current is %s)",
                            token, id, current.id if current else None)
                return False
            if token not in current.accepts:
                logger.info("dropped answer %r to question %d: not one of %s",
                            token, id, sorted(current.accepts))
                return False
            self._answer_token = token
        self._answered.set()
        return True

    def abandon(self) -> None:
        """End the session at once. A blocked ask() wakes and returns None."""
        self._abandon.set()
        self._answered.set()

    def is_abandoned(self) -> bool:
        return self._abandon.is_set()

    def current(self) -> "Optional[Question]":
        with self._lock:
            return self._current
