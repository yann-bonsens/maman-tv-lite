# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The question/answer channel, on its own: the duplicate answer, the late
one, the timeout, and abandonment mid-wait — the rules
docs/development/installation-screen.md sets out for the channel, independent of who
answers it (a button, later; HTTP, today)."""

import threading
import time

import pytest

import answer_channel as ac


@pytest.fixture
def channel():
    return ac.Channel()


def test_a_question_gets_increasing_ids(channel):
    first = []
    channel.ask("one", {"white": "ok"}, seconds=0.01, publish=first.append)
    second = []
    channel.ask("two", {"white": "ok"}, seconds=0.01, publish=second.append)
    assert second[0].id == first[0].id + 1


def test_an_answer_to_the_current_question_is_accepted(channel):
    asked = threading.Event()
    result = {}

    def worker():
        result["token"] = channel.ask("body", {"white": "yes", "red": "no"},
                                      seconds=5, publish=lambda q: asked.set())

    thread = threading.Thread(target=worker)
    thread.start()
    assert asked.wait(2), "the question was never published"
    question = channel.current()
    assert channel.answer(question.id, "white") is True
    thread.join(timeout=2)
    assert result["token"] == "white"


def test_a_stale_id_is_dropped(channel):
    published = []
    asked = threading.Event()

    def worker():
        channel.ask("body", {"white": "yes"}, seconds=5,
                   publish=lambda q: (published.append(q), asked.set()))

    thread = threading.Thread(target=worker)
    thread.start()
    assert asked.wait(2)
    stale_id = published[0].id - 1
    assert channel.answer(stale_id, "white") is False
    channel.abandon()
    thread.join(timeout=2)


def test_a_late_answer_after_timeout_is_dropped(channel):
    """The spec's rule: a press that arrives after a question has timed out
    is dropped, the same mechanism as a duplicate."""
    token = channel.ask("body", {"white": "yes"}, seconds=0.05)
    assert token is None
    question_id = channel._next_id - 1  # the one that just timed out
    assert channel.answer(question_id, "white") is False


def test_an_unaccepted_token_is_dropped(channel):
    asked = threading.Event()
    result = {}

    def worker():
        result["token"] = channel.ask("body", {"white": "yes"}, seconds=2,
                                      publish=lambda q: asked.set())

    thread = threading.Thread(target=worker)
    thread.start()
    assert asked.wait(2)
    question = channel.current()
    assert channel.answer(question.id, "not-a-real-token") is False
    assert channel.answer(question.id, "white") is True
    thread.join(timeout=2)
    assert result["token"] == "white"


def test_a_duplicate_answer_a_second_later_changes_nothing(channel):
    """The scenario that motivated the rule: one press reaching the bridge
    twice must never answer the NEXT question."""
    asked = threading.Event()

    def worker():
        channel.ask("first", {"white": "yes"}, seconds=5,
                   publish=lambda q: asked.set())

    thread = threading.Thread(target=worker)
    thread.start()
    assert asked.wait(2)
    first_id = channel.current().id
    assert channel.answer(first_id, "white") is True
    thread.join(timeout=2)

    # The duplicate of the first press, arriving late: must not touch a
    # question that has since moved on.
    second_asked = threading.Event()

    def worker2():
        channel.ask("second", {"white": "yes"}, seconds=5,
                   publish=lambda q: second_asked.set())

    thread2 = threading.Thread(target=worker2)
    thread2.start()
    assert second_asked.wait(2)
    assert channel.answer(first_id, "white") is False
    channel.abandon()
    thread2.join(timeout=2)


def test_a_timeout_returns_none(channel):
    started = time.monotonic()
    token = channel.ask("body", {"white": "yes"}, seconds=0.1)
    assert token is None
    assert time.monotonic() - started < 1


def test_no_deadline_waits_until_answered(channel):
    asked = threading.Event()
    result = {}

    def worker():
        result["token"] = channel.ask("body", {"white": "yes"}, seconds=None,
                                      publish=lambda q: asked.set())

    thread = threading.Thread(target=worker)
    thread.start()
    assert asked.wait(2)
    thread.join(timeout=0.3)
    assert thread.is_alive(), "a question with no deadline must not time out"
    channel.answer(channel.current().id, "white")
    thread.join(timeout=2)
    assert result["token"] == "white"


def test_abandon_wakes_a_blocked_ask_at_once(channel):
    asked = threading.Event()
    result = {}

    def worker():
        result["token"] = channel.ask("body", {"white": "yes"}, seconds=None,
                                      publish=lambda q: asked.set())

    thread = threading.Thread(target=worker)
    thread.start()
    assert asked.wait(2)
    started = time.monotonic()
    channel.abandon()
    thread.join(timeout=2)
    assert time.monotonic() - started < 1, "abandon must not wait out a deadline"
    assert result["token"] is None
    assert channel.is_abandoned() is True


def test_reset_clears_abandonment_for_a_fresh_session(channel):
    channel.abandon()
    assert channel.is_abandoned() is True
    channel.reset()
    assert channel.is_abandoned() is False
    assert channel.current() is None
