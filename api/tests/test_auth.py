# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

import base64
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import auth
from conftest import AUTH_HEADERS, TEST_PASSWORD, TEST_USER
from main import app

anonymous = TestClient(app)
authenticated = TestClient(app, headers=AUTH_HEADERS)


def _header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class TestParseBasicHeader:
    def test_valid_header(self):
        assert auth.parse_basic_header(_header("bob", "s3cret")["Authorization"]) == (
            "bob",
            "s3cret",
        )

    def test_password_may_contain_colons(self):
        assert auth.parse_basic_header(_header("bob", "a:b:c")["Authorization"]) == (
            "bob",
            "a:b:c",
        )

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "Bearer abcdef",  # different scheme
            "Basic ###not-base64###",
            "Basic " + base64.b64encode(b"no-colon-separator").decode(),
        ],
    )
    def test_rejects_malformed(self, header):
        assert auth.parse_basic_header(header) is None


class TestEndpointProtection:
    def test_anonymous_request_is_refused(self):
        response = anonymous.get("/tv/config")
        assert response.status_code == 401

    def test_refusal_triggers_the_browser_prompt(self):
        """Without this header a browser would not show a login prompt.

        It is what produces the expected behaviour, the equivalent of an
        .htaccess-protected page.
        """
        response = anonymous.get("/tv/config")
        assert response.headers["WWW-Authenticate"] == f'Basic realm="{auth.REALM}"'

    def test_wrong_password_is_refused(self):
        response = anonymous.get("/tv/config", headers=_header(TEST_USER, "mauvais"))
        assert response.status_code == 401

    def test_wrong_user_is_refused(self):
        response = anonymous.get("/tv/config", headers=_header("intrus", TEST_PASSWORD))
        assert response.status_code == 401

    def test_docs_are_protected_too(self):
        """Regression: /docs exposes the whole API surface, so it must not
        stay an open door."""
        assert anonymous.get("/docs").status_code == 401

    def test_welcome_page_is_protected_too(self):
        assert anonymous.get("/").status_code == 401

    def test_valid_credentials_pass(self):
        assert authenticated.get("/docs").status_code == 200


class TestFailsClosed:
    """Regression: with no credentials configured, the API must refuse
    everything. Staying silently open while believed to be protected would be
    the worst of both worlds."""

    def test_refuses_everything_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv(auth.USER_ENV, raising=False)
        monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
        response = authenticated.get("/tv/config")
        assert response.status_code == 503
        assert "api.env" in response.json()["detail"]

    def test_refuses_when_password_is_empty(self, monkeypatch):
        monkeypatch.setenv(auth.PASSWORD_ENV, "")
        assert authenticated.get("/tv/config").status_code == 503


class TestResistingAGuessedPassword:
    """The API is published through a tunnel, so a single password stands
    between the internet and /system/shutdown — which needs someone to travel
    to the box to undo. Constant-time comparison does nothing against simply
    trying passwords in a loop."""

    def setup_method(self):
        auth.reset_throttle()

    def teardown_method(self):
        auth.reset_throttle()

    def test_a_caller_is_refused_outright_after_enough_failures(self):
        for _ in range(auth.AUTH_MAX_FAILURES):
            auth.note_failure("1.2.3.4")
        assert auth.seconds_locked_out("1.2.3.4") > 0

    def test_one_mistake_costs_nothing(self):
        auth.note_failure("1.2.3.4")
        assert auth.seconds_locked_out("1.2.3.4") == 0

    def test_the_right_password_clears_the_slate(self):
        for _ in range(auth.AUTH_MAX_FAILURES):
            auth.note_failure("1.2.3.4")
        auth.note_success("1.2.3.4")
        assert auth.seconds_locked_out("1.2.3.4") == 0

    def test_one_caller_cannot_lock_out_another(self):
        """Counted per caller precisely so that nobody on the internet can lock
        the owner out of their own box by failing on purpose."""
        for _ in range(auth.AUTH_MAX_FAILURES * 2):
            auth.note_failure("evil")
        assert auth.seconds_locked_out("evil") > 0
        assert auth.seconds_locked_out("owner") == 0

    def test_a_lockout_that_has_run_its_course_starts_over(self):
        """Otherwise a caller sits one attempt away from locked for ever."""
        for _ in range(auth.AUTH_MAX_FAILURES):
            auth.note_failure("1.2.3.4")
        with patch("auth.AUTH_LOCKOUT_SECONDS", 0):
            assert auth.seconds_locked_out("1.2.3.4") == 0
            assert auth.note_failure("1.2.3.4") == 1

    def test_tracking_cannot_grow_without_bound(self):
        """An attack from many addresses must not be a way to exhaust memory."""
        for i in range(auth.AUTH_MAX_TRACKED_CLIENTS + 50):
            auth.note_failure(f"10.0.0.{i}")
        assert len(auth._failures) <= auth.AUTH_MAX_TRACKED_CLIENTS


class TestIdentifyingTheCaller:
    def test_tunnel_traffic_is_keyed_on_the_real_client(self):
        """Everything arriving through the tunnel connects from loopback, so
        the socket alone would lump every remote caller together — and one
        attacker would lock out the owner."""
        assert auth.client_key("127.0.0.1", "203.0.113.7") == "via-tunnel:203.0.113.7"

    def test_a_local_caller_cannot_claim_to_be_someone_else(self):
        """On the LAN that header is caller-supplied, and trusting it would be
        a way to dodge the count entirely."""
        assert auth.client_key("192.168.1.50", "203.0.113.7") == "192.168.1.50"

    def test_an_unknown_peer_still_gets_a_key(self):
        assert auth.client_key(None) == "unknown"


def test_every_rejection_costs_the_caller_a_pause():
    """Imperceptible when you mistype once, ruinous for a word list. Asserted
    here because the test suite otherwise runs with it switched off."""
    assert auth.AUTH_FAILURE_DELAY_SECONDS == 0, "the fixture should have cleared it"
    with patch.object(auth, "AUTH_FAILURE_DELAY_SECONDS", 0.5):
        client = TestClient(app)
        started = time.monotonic()
        assert client.get("/tv/status", headers={"Authorization": "Basic bm9wZTpub3Bl"}).status_code == 401
        assert time.monotonic() - started >= 0.4


class TestARequestWithoutAPasswordIsNotAGuess:
    """Measured on the Pi 5 box: opening the Swagger page, Safari asked for
    /apple-touch-icon.png four times with no credentials at all, and each one
    counted — four steps, of ten, towards locking the owner out of their own
    box. A request that carries no password cannot be guessing one."""

    def setup_method(self):
        auth.reset_throttle()

    def test_it_is_still_refused_and_still_asked_for_the_password(self):
        response = anonymous.get("/tv/status")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == f'Basic realm="{auth.REALM}"'

    def test_but_it_costs_the_caller_nothing(self):
        for _ in range(auth.AUTH_MAX_FAILURES * 3):
            anonymous.get("/apple-touch-icon.png")
        assert auth.seconds_locked_out("testclient") == 0.0
        assert authenticated.get("/openapi.json").status_code == 200

    def test_a_wrong_password_still_counts(self):
        anonymous.get("/openapi.json", headers=_header(TEST_USER, "wrong"))
        assert auth.note_failure("testclient") == 2
