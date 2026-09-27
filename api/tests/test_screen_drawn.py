# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The screen, actually run.

Every other test on `screen.sh` reads it. This one **executes** it: the real
script, with its real `render`, drawing a real page into a file that stands in
for the console. Everything it would touch on a live machine — the consoles,
fbi, systemctl, the network probes — is a stub on PATH.

It exists because reading a shell script does not catch what breaks it. Three
faults survived the rewrite and every reading test written for it:

- a call to `learned_technique`, deleted with the polling loop, left in the
  middle of the page;
- `set_big_font`, deleted by accident, still called by the page;
- `$cadence`, deleted with the auto-refresh, still printed in the title — and
  under `set -u` an unbound variable does not print an empty string, it kills
  the shell. The diagnostic screen drew nothing at all.

All three are what a person standing in front of the television would have
found, with no way to know why.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

import state
import tv_config

# Slow, and deliberately so — see pyproject.toml.
# Executes the real `screen.sh` against stub binaries on PATH: every test
# forks a shell and a dozen small programs, which is where its time goes.
pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "screen.sh"

# Everything the script calls that a test machine must not really run. `date`
# and `printf` are deliberately absent: the page should print a real time.
STUBS = {
    "chvt": "exit 0",
    "setfont": "exit 0",
    "setterm": "exit 0",
    "pkill": "exit 0",
    "pgrep": "exit 1",                       # no fbi running
    "fbi": "exit 0",
    "systemctl": 'echo active; exit 0',
    "curl": "exit 0",
    # A plausible wired box: one default route, one address. Enough for
    # render to take its normal path rather than the "no network" one.
    "ip": """case "$*" in
  *"route show default"*) echo "default via 192.168.2.1 dev eth0" ;;
  *"-4 -o addr show"*)    echo "2: eth0    inet 192.168.2.2/24 brd 192.168.2.255 scope global dynamic eth0" ;;
  *"-6 -o addr show"*)    echo "2: eth0    inet6 fe80::1/64 scope link" ;;
  *"-o link show"*)       echo "2: eth0: <BROADCAST,MULTICAST,UP>" ;;
esac
exit 0""",
    "getent": "exit 1",
    "tailscale": "exit 1",
    "nc": "exit 1",
    # Real pings are what made this test take two and a half seconds a run:
    # the gateway probe and the internet probe each wait for a timeout that
    # will never be answered on a machine with no network of this shape.
    "ping": "exit 1",
    "timeout": 'shift; exec "$@"',
}


def _publish_a_real_state(tmp_path) -> Path:
    """Write state.json the way the API writes it, not by hand.

    This fixture used to build the file itself, and that is how the page came
    to print "output=asleep (?)" on a real television for as long as it did:
    the handwritten dict carried `hdmi_sleep`, and the API's own
    `state.snapshot()` never published it. The test proved the renderer
    against a shape production does not produce.

    Going through `state.publish()` means the two can only drift apart if a
    test fails.
    """
    tv_config.PATH = str(tmp_path / "tv.json")
    tv_config.LEGACY_PATH = str(tmp_path / "cec-learned.json")
    state.RUNTIME_DIR = str(tmp_path)
    state.PATH = str(tmp_path / "state.json")
    tv_config.set_technique("wake", "text_view_on", source="detection")
    tv_config.set_technique("sleep", "standby", source="default")
    tv_config.set_technique("release", "power_cycle", source="detection")
    tv_config.set_television({"manufacturer": "SAM", "product": "0x7590",
                              "name": "SAMSUNG"})
    state._state.update({"mode": "diagnostic", "hdmi": "awake", "events": []})
    state.publish()
    return Path(state.PATH)


@pytest.fixture
def screen(tmp_path):
    """The script, ready to draw into a file, with the world stubbed out."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in STUBS.items():
        stub = binaries / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)

    console = tmp_path / "console"
    console.write_text("")
    state_file = _publish_a_real_state(tmp_path)

    def draw(page="diagnostic"):
        environment = dict(os.environ)
        environment["PATH"] = f"{binaries}:{environment['PATH']}"
        environment["STATE_FILE"] = str(state_file)
        environment["RUNTIME_DIR"] = str(tmp_path)
        done = subprocess.run(
            ["bash", str(SCRIPT), "--tty-path", str(console), "--once", page],
            capture_output=True, text=True, env=environment, timeout=60)
        return done, console.read_text(errors="replace")

    return draw


def test_the_page_is_drawn_at_all(screen):
    """Under `set -u`, one leftover variable is the difference between a page
    and a blank television."""
    done, page = screen()
    assert done.returncode == 0, done.stderr
    assert page.strip(), "nothing was drawn"
    assert "command not found" not in done.stderr, done.stderr
    assert "unbound variable" not in done.stderr, done.stderr


def test_it_shows_what_the_box_is_doing(screen):
    _, page = screen()
    assert "mode=diagnostic" in page
    assert "output=awake" in page
    assert "text_view_on(detection)" in page
    assert "standby(default)" in page
    assert "SAMSUNG" in page


def test_it_shows_the_way_in(screen):
    """The screen exists for somebody standing in front of the television with
    no keyboard: the address and the name to connect to are the point."""
    _, page = screen()
    assert "MAMAN TV LITE" in page
    assert "ssh" in page


def test_a_box_that_has_never_been_configured_says_so(screen, tmp_path):
    """A default must not look like a measurement."""
    # Published by the real code, on a box whose tv.json has never been
    # written — the defaults, and nothing claiming to have been measured.
    (tmp_path / "tv.json").unlink()
    state._state.update({"mode": "television", "hdmi": "asleep", "events": []})
    state.publish()
    _, page = screen()
    assert "image_view_on(default)" in page
    assert "unknown" in page, "an unconfigured box must say which set it does not know"
    assert "never completed" in page, \
        "a box that has never been set up must say so, not merely show defaults"


def test_a_missing_state_file_is_said_and_not_guessed(screen, tmp_path):
    (tmp_path / "state.json").unlink()
    done, page = screen()
    assert done.returncode == 0
    assert "state unavailable" in page


def test_the_detection_page_names_itself(screen):
    _, page = screen("detection")
    assert "DETECTION" in page


# ---------------------------------------------------------------------------
# The page request: how the whole installation screen reaches the television.
#
# Every test above drives `--once diagnostic`, which goes through `draw_page`.
# `serve_page` — the fbi path the installation screen actually uses — was
# reached by nothing executed, which is exactly the shape of the three faults
# this file was written for: the newest path was the least proven one.
# ---------------------------------------------------------------------------

@pytest.fixture
def running_screen(tmp_path):
    """The real service, serving its real pipe, with the world stubbed out."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in STUBS.items():
        stub = binaries / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    # fbi records how it was called, so the test can read the real argv the
    # service built rather than trusting the source to say what it would be.
    # setsid is what detaches fbi from the terminal that started it (fbi
    # refuses to run otherwise: "Not started from linux console?"). It does
    # not exist on macOS, where without this stub the whole invocation fails
    # into a backgrounded "command not found" that nothing can see.
    (binaries / "setsid").write_text('#!/bin/sh\nexec "$@"\n')
    (binaries / "setsid").chmod(0o755)
    calls = tmp_path / "fbi-calls"
    (binaries / "fbi").write_text(
        f'#!/bin/sh\necho "$@" >> {calls}\nexec sleep 30\n')
    (binaries / "fbi").chmod(0o755)
    # pgrep must answer truthfully about fbi for wait_for_fbi_to_go, and
    # nothing else runs here, so defer to the real one.
    (binaries / "pgrep").unlink()
    (binaries / "pkill").unlink()

    console = tmp_path / "console"
    console.write_text("")
    environment = dict(os.environ)
    environment["PATH"] = f"{binaries}:{environment['PATH']}"
    environment["RUNTIME_DIR"] = str(tmp_path)
    environment["STATE_FILE"] = str(tmp_path / "state.json")
    service = subprocess.Popen(
        ["bash", str(SCRIPT), "--tty-path", str(console)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=environment)
    fifo = tmp_path / "screen"
    for _ in range(200):
        if fifo.exists():
            break
        time.sleep(0.02)
    else:
        service.kill()
        raise AssertionError("the screen service never created its pipe")

    def send(request: str) -> None:
        with open(fifo, "w") as handle:
            handle.write(request + "\n")

    try:
        yield type("Screen", (), {"send": staticmethod(send), "calls": calls,
                                  "dir": tmp_path, "process": service})
    finally:
        service.terminate()
        try:
            service.wait(timeout=10)
        except subprocess.TimeoutExpired:
            service.kill()
        subprocess.run(["pkill", "-f", "sleep 30"], capture_output=True)


def _wait_for(path: Path, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text()
        time.sleep(0.02)
    return ""


def test_a_page_request_puts_the_image_on_the_television(running_screen):
    """The end of the chain: page_render writes the PNG, the API asks for
    "page", and this is what actually shows it."""
    image = running_screen.dir / "page.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    running_screen.send("page")
    called = _wait_for(running_screen.calls)
    assert called, "fbi was never started for the page"
    assert str(image) in called, called
    assert "-a" in called, "the page must be scaled to the television's resolution"
    assert "-d /dev/fb0" in called, "without this fbi goes looking for DRM"


def test_a_page_request_with_no_image_says_so_and_draws_nothing(running_screen):
    running_screen.send("page")
    time.sleep(0.5)
    assert not running_screen.calls.exists(), "fbi was started with nothing to show"


def test_a_second_page_replaces_the_first(running_screen):
    """Each step of a procedure is its own image. The old fbi has to be gone
    before the new one takes the console, or the console is left in graphics
    mode with nothing able to write to it."""
    image = running_screen.dir / "page.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    running_screen.send("page")
    assert _wait_for(running_screen.calls)
    running_screen.send("page")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if len(running_screen.calls.read_text().strip().splitlines()) >= 2:
            break
        time.sleep(0.02)
    assert len(running_screen.calls.read_text().strip().splitlines()) == 2


def test_the_page_names_the_sleep_method_it_is_actually_using(screen):
    """It printed "output=awake (?)" on a real television for as long as this
    fixture built its own state.json: the page reads `hdmi_sleep`, and the
    API's `state.snapshot()` never published it."""
    _, page = screen()
    assert "(blank)" in page, page
    assert "(?)" not in page, "the sleep method must be a value, not a fallback"


def test_the_page_shows_all_three_techniques(screen):
    """"back" decides what the tv button does while the music plays, and it
    was on no page anywhere."""
    _, page = screen()
    assert "on=text_view_on(detection)" in page
    assert "off=standby(default)" in page
    assert "back=power_cycle(detection)" in page


def test_a_completed_setup_does_not_nag(screen, tmp_path):
    tv_config.set_detection_complete(True)
    state.publish()
    _, page = screen()
    assert "never completed" not in page
