# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Tests for config/api.env.example.

That file is the only place anybody setting this box up learns which settings
exist. A setting offered there that nothing reads is worse than no
documentation at all: it is read as a promise, set, and silently ignored.

Written after the move from mpv to mpg123 and fbi left three of them behind —
a console the photos are no longer drawn on, a wait that was mpv's
`--audio-wait-open` and has no equivalent, and an instruction to run a command
that is not installed any more.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "config" / "api.env.example"
TEXT = EXAMPLE.read_text(encoding="utf-8")

# Where a setting of the API's may be read from. Not the installer: its own
# answers (MAMAN_ZIGBEE, MAMAN_SHARE…) live in install.conf and are a
# different file's business.
READERS = [
    (REPO_ROOT / "api").rglob("*.py"),
    (REPO_ROOT / "scripts").glob("*.sh"),
    (REPO_ROOT / "scripts").glob("*.py"),
    (REPO_ROOT / "systemd").glob("*"),
]


def _code():
    out = []
    for group in READERS:
        for path in group:
            if path.is_file() and "tests" not in path.parts:
                out.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(out)


CODE = _code()


def test_every_setting_offered_is_actually_read():
    """Three were left behind by the move away from mpv, and each one reads as
    a promise: somebody sets it, nothing happens, and there is no way to tell
    that from a setting that simply had no effect on their television."""
    offered = set(re.findall(r"^#?(MAMAN_[A-Z0-9_]+)=", TEXT, re.MULTILINE))
    assert offered, "the example stopped offering anything at all"
    dead = sorted(name for name in offered if name not in CODE)
    assert not dead, f"offered but nothing reads them: {dead}"


# The package a command comes from, where the two names differ.
FROM_PACKAGE = {"cec-ctl": "v4l-utils", "aplay": "alsa-utils"}

# Files that tell somebody setting the box up to run something on it.
INSTRUCTIONS = [EXAMPLE,
                REPO_ROOT / "docs" / "media.md",
                REPO_ROOT / "docs" / "faq.md",
                REPO_ROOT / "docs" / "lessons.md",
                REPO_ROOT / "docs" / "sd-card-image.md",
                REPO_ROOT / "docs" / "installer.md",
                REPO_ROOT / "docs" / "television.md",
                REPO_ROOT / "docs" / "without-buttons.md",
                REPO_ROOT / "docs" / "architecture.md",
                REPO_ROOT / "README.md"]


def _installed():
    installer = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    packages = set()
    for line in re.findall(r"^\s*(?:PACKAGES=\(|.*PACKAGES\+=\()([^)]*)\)",
                           installer, re.MULTILINE):
        packages.update(line.split())
    assert "mpg123" in packages and "v4l-utils" in packages, \
        "the package lists stopped being readable, so this test proves nothing"
    return packages


def _commands_offered(text: str) -> "set[str]":
    """Every command a reader could take as an instruction.

    The first word of each line in a fenced block, and the first word of each
    inline code span, with the fenced blocks removed before the spans are looked
    for.

    The first version matched "`(command)...`" across the whole file and was
    unreliable in both directions: backtick parity shifts as soon as a fence
    appears, so it read commands out of the gaps between spans and missed some
    inside them. It caught two real faults anyway, by luck — and a guard that
    works by luck is worth rewriting.
    """
    offered: "set[str]" = set()
    for block in re.findall(r"^```[a-z]*\n(.*?)^```", text,
                            re.MULTILINE | re.DOTALL):
        for line in block.splitlines():
            words = line.strip().lstrip("$ ").split()
            if not words:
                continue
            offered.add(words[0])
            # `sudo x` and `time x` are about x.
            if words[0] in ("sudo", "time") and len(words) > 1:
                offered.add(words[1])
    prose = re.sub(r"^```[a-z]*\n.*?^```", "", text,
                   flags=re.MULTILINE | re.DOTALL)
    for span in re.findall(r"`([^`\n]+)`", prose):
        words = span.split()
        if words:
            offered.add(words[0])
    return offered


def test_the_docs_never_send_the_reader_to_a_command_the_box_lacks():
    """The example said `mpv --audio-device=help` long after mpv stopped being
    installed, so the one instruction for the setting that most often needs
    changing could not be followed on the machine it was written for.

    Matched against the package lists the installer builds, NOT against the
    text of install.sh: the first version of this test searched the whole file
    and passed on the word "mpv" appearing in a comment explaining why mpv was
    dropped. It would never have caught the thing it was written for.
    """
    packages = _installed()
    known = {"mpv", "fbi", "mpg123", "cec-client", "aplay", "ffmpeg", "sips"}
    for path in INSTRUCTIONS:
        for command in _commands_offered(path.read_text(encoding="utf-8")):
            if command in known and command != "sips":   # sips is the Mac's
                package = FROM_PACKAGE.get(command, command)
                assert package in packages, \
                    f"{path.name} says to run {command}, which nothing installs"


def test_nothing_tells_anybody_to_run_mpg123_without_a_file():
    """Both the env example and the troubleshooting table said `mpg123
    --list-devices` was how to find a sound device's name. Run on the real
    board it did not list anything: with no file to play, mpg123 reads its
    standard input instead and sits at 100% of the single core until it is
    killed — which is exactly what happened, on the box, in front of the owner.

    `aplay -L` is the answer, and it is why alsa-utils is installed.
    """
    for path in INSTRUCTIONS:
        text = path.read_text(encoding="utf-8")
        assert "mpg123 --list-devices" not in text, \
            f"{path.name} still names a command that pegs the core"
        for snippet in re.findall(r"`mpg123([^`]*)`", text):
            assert not snippet.strip().startswith("-") or "-R" in snippet, \
                f"{path.name} runs mpg123 with no file to play: `mpg123{snippet}`"
