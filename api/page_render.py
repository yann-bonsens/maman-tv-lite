# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Draws one installation-screen page as an image, for `fbi` to show.

Real graphics, not the plain text console the diagnostic screen uses — but
drawn as a **static image per step**, never redrawn on a timer: a page
changes every 30 seconds to several minutes, so even a few hundred
milliseconds of Pillow rendering is negligible, the same reasoning that
already makes `fbi` cheap enough for the photo slideshow on this board (about
0.55 s of CPU per still image, measured).

Canvas is FULL-HD, matching the project's photo-library default; `fbi -a`
scales it to fit whatever the television's actual resolution is.

Fonts come from the system's DejaVu package
(`/usr/share/fonts/truetype/dejavu/`), not from Pillow's own scalable default
font: that one's `size=` argument needs Pillow >=10.1, which is not a version
worth depending on when a real TrueType file sits at a known path. A short
candidate list is tried in order — mirroring
`scripts/screen.sh`'s own `set_big_font()` — with `ImageFont.load_default()`
(no size) as the last resort, so rendering never crashes even off-hardware.
"""

import dataclasses
import logging
import os

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 1920, 1080
# Old sets crop the edges (overscan), and the line that gets lost first is
# the one that says what to do — see docs/development/installation-screen.md.
MARGIN = round(WIDTH * 0.05)

BACKGROUND = (12, 14, 18)
FOREGROUND = (240, 240, 240)
MUTED = (150, 155, 165)
# Failure pages only (PageSpec.title), where red means what it usually means.
RED_ACCENT = (219, 68, 68)

HEADER_TEXT = "SETUP IN PROGRESS"

BOLD_CANDIDATES = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",)
REGULAR_CANDIDATES = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",)

_font_cache: "dict[tuple, ImageFont.FreeTypeFont]" = {}


def _font(candidates: "tuple[str, ...]", size: int) -> "ImageFont.FreeTypeFont":
    key = (candidates, size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    else:
        logger.warning("no usable font in %s; falling back to Pillow's built-in one",
                       candidates)
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _bold(size: int) -> "ImageFont.FreeTypeFont":
    return _font(BOLD_CANDIDATES, size)


def _regular(size: int) -> "ImageFont.FreeTypeFont":
    return _font(REGULAR_CANDIDATES, size)


@dataclasses.dataclass
class PageSpec:
    body: str
    title: "str | None" = None                    # a short pre-header, e.g. a failure code
    step: "tuple[int, int] | None" = None          # (n, total) -> "STEP n OF total"
    legend: "dict[str, str] | None" = None         # {token: "already-formatted label"}


def _wrap(draw: "ImageDraw.ImageDraw", text: str, font, max_width: int) -> "list[str]":
    """Word-wrap `text` to `max_width`, keeping blank lines as paragraph breaks."""
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if not current or draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


# Largest first: tried in order, and the first size whose wrapped body fits
# the space above the legend is kept. A page's body is French prose composed
# by cec_detection.py, not a fixed layout, so shrinking to fit is what keeps
# a longer page (the preparation step, an E-page with three bullet points)
# from silently losing its last lines instead of stretching an unusually
# long one off the bottom of the screen.
BODY_FONT_SIZES = (52, 46, 40, 36, 32)
BODY_LINE_SPACING = 1.23  # close to DejaVu Sans's own natural line height


def _fit_body(canvas: "ImageDraw.ImageDraw", body: str, max_width: int,
             available_height: int) -> "tuple[list[str], ImageFont.FreeTypeFont, int]":
    """The largest body font size (and its wrapped lines) that fits, or the
    smallest one if even that overflows — draw() still truncates as a last
    resort, but only after every size has been tried."""
    chosen = None
    for size in BODY_FONT_SIZES:
        font = _regular(size)
        line_height = round(size * BODY_LINE_SPACING)
        lines = _wrap(canvas, body, font, max_width)
        chosen = (lines, font, line_height)
        if len(lines) * line_height <= available_height:
            return chosen
    return chosen


# The legend is the one line that must never be lost: it is what says which
# button does what. Its labels carry a button's configured name, which is
# whatever somebody typed at pairing — measured, "LE GRAND BOUTON DU SALON :
# nothing happened" beside its twin needs about 2000 px of a 1728 px content
# width and ran off the edge, past the overscan margin, invisible. The body
# had shrink-to-fit from the start; this gives the legend the same, and a
# last-resort narrower gap before the smallest size.
LEGEND_FONT_SIZES = (44, 40, 36, 32, 28, 24)
LEGEND_GAPS = (80, 40)


def _fit_legend(canvas: "ImageDraw.ImageDraw", labels: "list[str]",
                max_width: int) -> "tuple[ImageFont.FreeTypeFont, int]":
    """The largest legend font (and gap) whose labels fit side by side.

    Falls back to the smallest combination rather than raising: a legend
    slightly too wide is still readable, whereas no legend at all leaves
    somebody in front of a television with no idea which button to press.
    """
    smallest = None
    for gap in LEGEND_GAPS:
        for size in LEGEND_FONT_SIZES:
            font = _bold(size)
            width = sum(canvas.textlength(label, font=font) for label in labels)
            width += gap * (len(labels) - 1)
            smallest = (font, gap)
            if width <= max_width:
                return smallest
    return smallest


def draw(page: PageSpec) -> "Image.Image":
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    canvas = ImageDraw.Draw(image)
    left, right = MARGIN, WIDTH - MARGIN
    content_width = right - left
    y = MARGIN

    header_font = _bold(38)
    canvas.text((left, y), HEADER_TEXT, font=header_font, fill=MUTED)
    if page.step:
        n, total = page.step
        step_text = f"STEP {n} OF {total}"
        step_width = canvas.textlength(step_text, font=header_font)
        canvas.text((right - step_width, y), step_text, font=header_font, fill=MUTED)
    y += 70
    canvas.line([(left, y), (right, y)], fill=MUTED, width=2)
    y += 50

    if page.title:
        title_font = _bold(56)
        for line in _wrap(canvas, page.title, title_font, content_width):
            canvas.text((left, y), line, font=title_font, fill=RED_ACCENT)
            y += 68
        y += 30

    legend_height = 150 if page.legend else 0
    max_body_bottom = HEIGHT - MARGIN - legend_height
    lines, body_font, line_height = _fit_body(canvas, page.body, content_width,
                                              max_body_bottom - y)
    for line in lines:
        if y + line_height > max_body_bottom:
            logger.warning("a page overflowed its body area even at the smallest "
                           "size; the rest was dropped: %r", page.body)
            break
        canvas.text((left, y), line, font=body_font, fill=FOREGROUND)
        y += line_height

    if page.legend:
        # Each value is already the label to draw in full (e.g. "BOUTON VERT
        # : it happened") — installation.py is the one that knows a button's
        # real configured name, this module never assumes one.
        #
        # All one colour. They used to be drawn white then red, by position,
        # left over from a time when the two tokens really were "white" and
        # "red". Once a button's name became whatever somebody typed at
        # pairing, that colouring could only mislead — "BOUTON VERT" printed
        # in red, and the affirmative answer marked with the colour every
        # other interface uses for "stop".
        labels = list(page.legend.values())
        legend_font, gap = _fit_legend(canvas, labels, content_width)
        legend_y = HEIGHT - MARGIN - 90
        canvas.line([(left, legend_y - 20), (right, legend_y - 20)], fill=MUTED, width=2)
        x = left
        for label in labels:
            canvas.text((x, legend_y), label, font=legend_font, fill=FOREGROUND)
            x += canvas.textlength(label, font=legend_font) + gap

    return image


RUNTIME_DIR = os.environ.get("RUNTIME_DIRECTORY", "/run/maman-tv-lite")
PAGE_PATH = os.path.join(RUNTIME_DIR, "page.png")


def save(image: "Image.Image", path: "str | None" = None) -> None:
    """Write the page where the screen service can pick it up.

    Write, fsync, rename: the screen must never open a half-drawn PNG — the
    same discipline `state.py` and `tv_config.py` already apply to their own
    files, here applied to an image instead of JSON.

    `path` is read from the module-level `PAGE_PATH` at call time rather than
    as a default argument, so that tests can `monkeypatch.setattr(page_render,
    "PAGE_PATH", ...)` the way they already do for `state.PATH`/`tv_config.PATH`
    — a default argument is bound once, at import time, and would not see it.
    """
    if path is None:
        path = PAGE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    # compress_level=1 rather than zlib's default 6. Measured off-hardware,
    # drawing and encoding one page went from 21.8 ms to 14.4 ms — a third of
    # the whole cost, for a file that grows from 38 KiB to 90 KiB and lives in
    # a tmpfs that fbi reads back out of RAM. On a board where a page visibly
    # takes a moment to appear, that trade is the right way round.
    image.save(tmp, format="PNG", compress_level=1)
    with open(tmp, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(tmp, path)
