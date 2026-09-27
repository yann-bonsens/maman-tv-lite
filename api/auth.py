# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

import base64
import binascii
import os
import secrets
import threading
import time

REALM = "Maman TV Lite"

# ============================================================================
# Resisting a password guessed one attempt at a time
# ----------------------------------------------------------------------------
# The API is published through a Cloudflare tunnel, so the whole of it — the
# television, and /system/shutdown, which needs someone to physically travel to
# the box to undo — is reachable from anywhere behind a single password.
# Constant-time comparison stops timing attacks; it does nothing against simply
# trying passwords in a loop.
#
# Two measures, both cheap:
#
#   - every rejection costs the caller a fixed pause. Imperceptible when you
#     mistype once, ruinous for anything working through a word list;
#   - after MAX_FAILURES rejections a caller is refused outright for
#     LOCKOUT_SECONDS, without the password even being looked at.
#
# Counted per caller rather than globally, deliberately: a global counter would
# let anyone on the internet lock the owner out of their own box by failing on
# purpose.
# ============================================================================
AUTH_MAX_FAILURES = int(os.environ.get("MAMAN_AUTH_MAX_FAILURES", "10"))
AUTH_LOCKOUT_SECONDS = float(os.environ.get("MAMAN_AUTH_LOCKOUT_SECONDS", "60"))
AUTH_FAILURE_DELAY_SECONDS = float(os.environ.get("MAMAN_AUTH_FAILURE_DELAY", "0.5"))

# An attack from many addresses must not grow this without bound. Well past any
# plausible number of real callers for one television.
AUTH_MAX_TRACKED_CLIENTS = 1024

_failures: "dict[str, tuple[int, float]]" = {}   # key -> (count, last failure)
_failures_lock = threading.Lock()


def client_key(peer: "str | None", forwarded_for: "str | None" = None) -> str:
    """Identify the caller for throttling.

    Requests arriving through the tunnel all connect from the loopback address,
    so the socket alone would lump every remote caller together — and one
    attacker would then lock out the owner. Cloudflare states the real client in
    CF-Connecting-IP, and its edge overwrites whatever the caller tried to put
    there, so it is worth trusting for loopback traffic specifically.

    Anything arriving from a real address is keyed on that address, and the
    header is ignored: on the local network it is caller-supplied and would
    otherwise be a way to dodge the count.
    """
    peer = peer or "unknown"
    if forwarded_for and peer in ("127.0.0.1", "::1", "localhost"):
        return f"via-tunnel:{forwarded_for.split(',')[0].strip()}"
    return peer


def seconds_locked_out(key: str) -> float:
    """How long this caller must wait, or 0 if it may try now."""
    with _failures_lock:
        count, last = _failures.get(key, (0, 0.0))
    if count < AUTH_MAX_FAILURES:
        return 0.0
    remaining = AUTH_LOCKOUT_SECONDS - (time.monotonic() - last)
    return max(0.0, remaining)


def note_failure(key: str) -> int:
    """Record a rejected attempt and return how many this caller has made."""
    now = time.monotonic()
    with _failures_lock:
        count, last = _failures.get(key, (0, 0.0))
        # A lockout that has run its course starts the caller over, rather than
        # leaving them one attempt away from locked forever.
        if count >= AUTH_MAX_FAILURES and now - last >= AUTH_LOCKOUT_SECONDS:
            count = 0
        count += 1
        _failures[key] = (count, now)
        if len(_failures) > AUTH_MAX_TRACKED_CLIENTS:
            oldest = min(_failures, key=lambda k: _failures[k][1])
            del _failures[oldest]
        return count


def note_success(key: str) -> None:
    """Forget a caller's failures: they have proved they know the password."""
    with _failures_lock:
        _failures.pop(key, None)


def reset_throttle() -> None:
    """Forget every caller. For tests, and for an operator locked out."""
    with _failures_lock:
        _failures.clear()

# Names of the environment variables read by the service. They come from
# /etc/maman-tv-lite/api.env (EnvironmentFile in maman-api.service), deliberately
# kept outside the repository: a password must never be committed.
USER_ENV = "MAMAN_API_USER"
PASSWORD_ENV = "MAMAN_API_PASSWORD"

CONFIG_HINT = (
    "Authentication is not configured: set "
    f"{USER_ENV} and {PASSWORD_ENV} in /etc/maman-tv-lite/api.env "
    "(see config/api.env.example), then run "
    "`sudo systemctl restart maman-api`."
)


class AuthNotConfigured(Exception):
    """No credentials are configured on the server side.

    Every request is then refused rather than left open: an API believed to be
    protected but silently reachable is the worst of both worlds.
    """


def _expected_credentials() -> tuple[str, str]:
    user = os.environ.get(USER_ENV, "")
    password = os.environ.get(PASSWORD_ENV, "")
    if not user or not password:
        raise AuthNotConfigured(CONFIG_HINT)
    return user, password


def parse_basic_header(header: str | None) -> tuple[str, str] | None:
    """Extract (user, password) from an `Authorization` header.

    Returns None when the header is missing, uses another scheme, or is
    malformed.
    """
    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    user, separator, password = decoded.partition(":")
    if not separator:
        return None
    return user, password


def is_authorized(header: str | None) -> bool:
    """Validate an `Authorization` header against the configured credentials.

    Raises AuthNotConfigured when the server has no credentials set.
    """
    expected_user, expected_password = _expected_credentials()
    provided = parse_basic_header(header)
    if provided is None:
        return False
    # Both comparisons always run, with no short-circuit, so that response
    # timing does not reveal whether it was the user or the password that was
    # wrong.
    user_ok = secrets.compare_digest(provided[0], expected_user)
    password_ok = secrets.compare_digest(provided[1], expected_password)
    return user_ok and password_ok
