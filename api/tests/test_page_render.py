# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""The installation screen's page renderer: draw() produces a well-formed
image for every page shape, and save() writes it the way the screen expects
to find it (write, fsync, rename — never a half-drawn PNG)."""

import os
from pathlib import Path

from PIL import Image

import page_render

# Anchored on this file, not on the working directory: CI runs pytest from
# api/, where a relative "api/..." path does not exist.
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_a_plain_body_draws_something(tmp_path):
    image = page_render.draw(page_render.PageSpec(body="Bonjour."))
    assert image.size == (page_render.WIDTH, page_render.HEIGHT)
    # Not a blank canvas: text was actually painted.
    assert image.getcolors(maxcolors=2) is None or len(image.getcolors(2)) > 1


def test_the_margin_is_respected():
    """Old sets crop the edges; nothing meaningful may sit in the outer 5%."""
    image = page_render.draw(page_render.PageSpec(body="X" * 5))
    pixels = image.load()
    for x in range(page_render.MARGIN):
        for y in (0, page_render.HEIGHT - 1):
            assert pixels[x, y] == page_render.BACKGROUND


def test_a_title_is_drawn():
    image = page_render.draw(page_render.PageSpec(body="body", title="E1 — TITLE"))
    assert image.size == (page_render.WIDTH, page_render.HEIGHT)


def test_a_step_counter_is_drawn_without_crashing():
    spec = page_render.PageSpec(body="body", step=(3, 5))
    page_render.draw(spec)  # must not raise


def test_a_legend_is_drawn_without_crashing():
    spec = page_render.PageSpec(body="body",
                                legend={"white": "it happened", "red": "nothing"})
    page_render.draw(spec)  # must not raise


def test_a_very_long_body_does_not_overflow_or_crash():
    spec = page_render.PageSpec(body="A very long word. " * 200,
                                legend={"white": "yes", "red": "no"})
    page_render.draw(spec)  # must not raise, and must not throw off PIL


def test_a_long_television_name_in_the_title_wraps_rather_than_crashing():
    spec = page_render.PageSpec(
        body="body",
        title="E1 — " + "AN EXTREMELY LONG TELEVISION NAME " * 5)
    page_render.draw(spec)  # must not raise


def test_save_writes_the_file_atomically(tmp_path):
    path = str(tmp_path / "page.png")
    image = page_render.draw(page_render.PageSpec(body="body"))
    page_render.save(image, path)
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp"), "the temp file must be renamed away"
    with Image.open(path) as reopened:
        assert reopened.size == (page_render.WIDTH, page_render.HEIGHT)


def test_save_reads_the_module_level_path_at_call_time(tmp_path, monkeypatch):
    """A default argument bound at import time would not see this — the same
    reasoning state.py and tv_config.py already rely on for their own PATH."""
    target = str(tmp_path / "elsewhere" / "page.png")
    monkeypatch.setattr(page_render, "PAGE_PATH", target)
    page_render.save(page_render.draw(page_render.PageSpec(body="body")))
    assert os.path.exists(target)


def test_fonts_fall_back_without_crashing_when_none_are_found(monkeypatch):
    monkeypatch.setattr(page_render, "BOLD_CANDIDATES", ("/no/such/font.ttf",))
    monkeypatch.setattr(page_render, "REGULAR_CANDIDATES", ("/no/such/font.ttf",))
    page_render._font_cache.clear()
    page_render.draw(page_render.PageSpec(body="body", title="title",
                                          legend={"white": "oui"}))
    page_render._font_cache.clear()


def test_the_legend_never_runs_off_the_edge(monkeypatch):
    """The one line that must never be lost: it says which button does what.

    Its labels carry a button's configured name, which is whatever somebody
    typed at pairing. Measured before this: "LE GRAND BOUTON DU SALON :
    nothing happened" beside its twin needed about 2000 px of a 1728 px
    content width and ran off the edge, past the overscan margin.
    """
    from PIL import ImageDraw
    canvas = ImageDraw.Draw(page_render.draw(page_render.PageSpec(body="x")))
    content_width = page_render.WIDTH - 2 * page_render.MARGIN
    labels = ["LE GRAND BOUTON DU SALON : nothing happened",
              "LE PETIT BOUTON BLANC : it happened"]
    font, gap = page_render._fit_legend(canvas, labels, content_width)
    width = sum(canvas.textlength(label, font=font) for label in labels)
    width += gap * (len(labels) - 1)
    assert width <= content_width, f"the legend needs {width:.0f} of {content_width}"


def test_the_legend_is_drawn_in_one_colour():
    """It used to be white then red, by position — left from a time when the
    two tokens really were "white" and "red". Once a button's name became
    whatever somebody typed, that could only mislead: "BOUTON VERT" printed
    in red, and the affirmative answer marked with the colour every other
    interface uses for stop."""
    source = (REPO_ROOT / "api" / "page_render.py").read_text(encoding="utf-8")
    legend = source[source.index("if page.legend:"):]
    assert "WHITE_ACCENT" not in legend
    assert "RED_ACCENT" not in legend, "red belongs to a failure title, not to a button"


def test_pages_are_encoded_for_speed_not_for_size():
    """They live in a tmpfs fbi reads back out of RAM, and the board takes a
    visible moment to put one up."""
    source = (REPO_ROOT / "api" / "page_render.py").read_text(encoding="utf-8")
    assert "compress_level=1" in source
