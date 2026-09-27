# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Tests for scripts/screen.sh.

Not a run of the screen — that needs a television and a console — but a guard
on what it costs and on the handful of behaviours that were paid for in hours.

The service exists because consoles belong to root and the API does not. It
draws only when asked, through a named pipe. Its predecessor came round once a
second for the life of the machine and repainted the television every five
minutes; that loop cost about 2% of this board's single core for ever, showed
technical text to somebody who had not asked for it, and held most of the
display faults of September 2026.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "screen.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")


def function(name: str) -> str:
    body = SOURCE[SOURCE.index(f"{name}() {{"):]
    return body[:body.index("\n}")]


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_it_does_nothing_until_it_is_asked():
    """No loop, no timer, no polling. The whole point of the rewrite."""
    serve = function("serve")
    assert "read -r line" in serve, "it blocks on the pipe"
    assert not re.search(r"^\s*sleep\s", SOURCE, re.MULTILINE), \
        "a sleep in here is a loop in disguise"
    for gone in ("--loop", "LOOP_DELAY", "LEARNED_POLL_SECONDS", "CONSOLE_BACK_AT"):
        assert gone not in SOURCE, f"{gone} belongs to the loop that was removed"


def test_the_pipe_is_opened_read_write():
    """A pipe opened read-only reports end of file every time its last writer
    closes, which would spin this loop at full speed — the very cost the
    service was rewritten to remove."""
    serve = function("serve")
    assert 'exec 3<>"$FIFO"' in serve
    assert "mkfifo" in serve


def test_every_request_the_api_can_send_is_handled():
    serve = function("serve")
    for request in ("diagnostic", "page", "photos", "key", "off"):
        assert re.search(rf"^\s+{request}\)", serve, re.MULTILINE), \
            f"the screen ignores {request!r}, which the API sends"
    assert "unknown request" in serve, "an unknown request must be said, not swallowed"


def test_fbi_is_never_tracked_by_its_process_id():
    """It forks and the parent exits at once: measured here, the shell was
    given pid 19457 while fbi ran as 19459. Everything that asked "is my fbi
    still alive?" therefore always heard no — which restarted it for the same
    photo every second and left nine of them alive at once."""
    assert "PHOTO_PID" not in SOURCE, "a pid of fbi's is never its own"
    assert "pkill -x fbi" in function("stop_slideshow")


def test_a_page_is_shown_once_with_no_timer_or_shuffle():
    """Unlike the slideshow, a page must never advance on its own: no -t, no
    -u, no -l — fbi shows the one image and sits until the next "page"
    request or the mode ends."""
    serve = function("serve_page")
    assert '"$PAGE_PATH"' in serve
    assert " -t " not in serve and " -u" not in serve and " -l " not in serve


def test_a_page_reuses_the_slideshows_console_and_cleanup():
    """Installation and the slideshow are mutually exclusive modes, so
    sharing PHOTO_VT and stop_slideshow's cleanup costs nothing and is one
    less console to manage."""
    serve = function("serve_page")
    assert 'chvt "$PHOTO_VT"' in serve
    assert "wait_for_fbi_to_go" in serve
    assert "hide_photo_console_text" in serve


def test_a_page_is_detached_from_whatever_terminal_started_it():
    serve = function("serve_page")
    assert "setsid fbi" in serve and "</dev/null" in serve


def test_the_television_is_moved_to_the_page_console_before_fbi_starts():
    serve = function("serve_page")
    assert serve.index('chvt "$PHOTO_VT"') < serve.index("setsid fbi")


def test_fbi_is_given_the_whole_list_and_paces_it_itself():
    """A fresh fbi per photo retakes the console, which BLANKS THE SCREEN for
    about eight tenths of a second before it paints. Given the list, fbi never
    retakes it: it decodes the next photo while the current one is up."""
    serve = function("serve_slideshow")
    assert "-l " in serve and "-u" in serve and "-t " in serve


def test_fbi_is_detached_from_whatever_terminal_started_it():
    """It refuses to start when it can see a terminal that is not a console —
    which is every service, and every ssh session: "Not started from linux
    console?"."""
    serve = function("serve_slideshow")
    assert "setsid fbi" in serve and "</dev/null" in serve


def test_the_television_is_moved_to_the_photo_console_before_fbi_starts():
    """Starting fbi and switching afterwards left tty1 on screen for the
    couple of seconds fbi takes to come up — and tty1 carries the login
    prompt, so the viewer saw a flash of console text before the first photo."""
    serve = function("serve_slideshow")
    assert serve.index('chvt "$PHOTO_VT"') < serve.index("setsid fbi")


def test_the_console_is_forced_back_into_text_mode():
    """fbi puts the console into graphics mode to draw and restores it when it
    is asked to stop — but not when it dies any other way. Measured on the
    board: KDGETMODE stayed at 1 long after fbi was gone, and from then on
    every byte written to the console went nowhere. Silent, permanent, and
    only fixable by forcing the mode rather than trusting it."""
    assert "0x4B3A" in SOURCE, "KDSETMODE is what puts it back"
    assert "restore_text_console" in function("stop_slideshow")
    assert "restore_text_console" in function("draw_page")


def test_a_key_is_pressed_in_the_console_fbi_reads():
    """On tty1 the key would reach agetty's login prompt instead — measured,
    fbi started there has no controlling terminal and is never the foreground
    process group. That is the whole reason the slideshow has a console of its
    own."""
    press = function("press_key")
    assert "TIOCSTI" in press, "there is no command for this ioctl"
    assert '/dev/tty$PHOTO_VT' in press, "fbi's console, not the login one"
    assert "pgrep -x fbi" in press, "and only while there is an fbi to hear it"


def test_the_keys_are_named_rather_than_spelled():
    """The API says "next", not "j": which character fbi listens to is the
    screen's business, not the API's."""
    press = function("press_key")
    assert "next)" in press and "previous)" in press


def test_the_slideshow_has_a_console_of_its_own():
    """Not tty1: that belongs to agetty. Above the sixth, because logind opens
    a login on the first NAutoVTs consoles the moment anything switches to
    them, and reserves the sixth."""
    vt = int(re.search(r'PHOTO_VT="\$\{PHOTO_VT:-(\d+)\}"', SOURCE).group(1))
    assert vt > 6, "logind would put a login on anything lower"


def test_fbi_cannot_print_on_the_television():
    """fbi writes some of what it says straight to its console, not to stdout:
    "trying fbdev: /dev/fb0" as it starts, "Ooops: Terminated" as it is
    killed. Measured — running it with its output in a file caught only the
    font line while those two still reached the screen. TERM is set because a
    service has none, and without it setterm applies nothing at all."""
    hide = function("hide_photo_console_text")
    assert "TERM=" in hide and "--foreground black" in hide and "--background black" in hide


def test_the_diagnostic_page_says_what_the_box_is_doing():
    """Not only the network. What was missing on site was the box's own
    state: which mode, whether its output is asleep, which techniques are in
    force and where they came from, and which television the configuration
    was made for."""
    lines = function("box_state_lines")
    assert "$STATE_FILE" in lines
    for shown in ("mode=", "output=", "on=%s", "off=%s", "back=%s", "TV"):
        assert shown in lines
    assert "hdmi_sleep" in lines, "which method puts the output to sleep"
    assert "detection_complete" in lines, \
        "a configuration nobody finished is not one nobody started"
    assert "state unavailable" in lines, "a missing state file must be said"
    assert "box_state_lines" in function("draw_page")


def test_leaving_the_screen_leaves_nothing_on_it():
    """The last thing anybody should see is a television going dark, not a
    login prompt or half a photo."""
    stop = function("stop_drawing")
    assert "stop_slideshow" in stop
    assert "blank_console" in stop


def test_it_calls_nothing_it_does_not_define():
    """The rewrite deleted ten functions. One call to one of them survived in
    the middle of the diagnostic page, where bash would have printed
    "command not found" onto the television and carried on — a fault nothing
    else here would have noticed."""
    defined = set(re.findall(r"^([a-z_][a-z_0-9]*)\(\) \{", SOURCE, re.M))
    # A call is a name in a substitution, or a name alone at the start of a
    # line. An assignment is not a call, hence the (?!=).
    called = set(re.findall(r"\$\(([a-z_][a-z_0-9]*)(?=[ )])", SOURCE))
    called |= set(re.findall(r"^\s*([a-z_][a-z_0-9]*)(?!=)(?=\s|$)", SOURCE, re.M))
    shell_builtins = {"local", "return", "echo", "printf", "read", "exec", "set",
                      "shift", "exit", "cat", "cut", "grep", "sed", "awk", "tr",
                      "wc", "ls", "rm", "mkdir", "mkfifo", "pgrep", "pkill",
                      "chvt", "setfont", "sleep", "python3", "setsid", "fbi",
                      "if", "fi", "then", "else", "elif", "for", "do", "done",
                      "while", "case", "esac", "in", "curl", "date", "hostname",
                      "getent", "systemctl", "ip", "timeout"}
    ours = {name for name in called if "_" in name} - shell_builtins
    missing = sorted(name for name in ours if name not in defined)
    assert not missing, f"called but never defined: {missing}"


def test_a_dying_fbi_is_waited_for_before_the_next_one_starts():
    """`pkill` returns as soon as the signal is queued, and fbi restores the
    console to KD_TEXT on its way out — so the old one could hand the console
    back AFTER the new one had taken it as KD_GRAPHICS. From then on every
    byte written to that console goes nowhere, silently and permanently.

    Bounded, because a refusal to die must not wedge the screen service.
    """
    wait = function("wait_for_fbi_to_go")
    assert "pkill -x fbi" in wait
    assert "pgrep -x fbi" in wait, "it must actually check, not just signal"
    assert "pkill -KILL -x fbi" in wait, "a deadline, not an unbounded wait"
    assert "restore_text_console" in wait


def test_the_wait_never_reads_the_request_pipe():
    """A sub-second wait must not consume a request somebody already sent —
    an "off" queued while a page was being drawn would vanish without trace.
    The pause uses a pipe of its own, opened read-write and never written."""
    pause = function("pause_briefly")
    assert "$FIFO" not in pause, "that is the request pipe"
    assert "exec 9<>" in pause and "-u 9" in pause
    assert not re.search(r"^\s*sleep\s", pause, re.MULTILINE), \
        "forking /bin/sleep is what this service was rewritten to stop doing"
