# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

import re

from fastapi.testclient import TestClient

from conftest import AUTH_HEADERS
from main import app

authenticated = TestClient(app, headers=AUTH_HEADERS)


def _posted_paths(page: str) -> list[str]:
    return [p.split("?")[0] for p in re.findall(r'data-post="([^"]+)"', page)]


def _post_routes() -> set[str]:
    return {r.path for r in app.routes if "POST" in getattr(r, "methods", ())}


class TestRemotePage:
    def test_is_served_at_the_root(self):
        response = authenticated.get("/")
        assert response.status_code == 200
        assert "data-post" in response.text

    def test_every_button_names_a_route_that_exists(self):
        """A renamed route would leave a button on a relative's phone that
        answers 404 — at the one moment somebody is trying to help from afar."""
        paths = _posted_paths(authenticated.get("/").text)
        assert paths, "the page has no buttons left"
        missing = [p for p in paths if p not in _post_routes()]
        assert missing == []

    def test_offers_nothing_that_cannot_be_undone(self):
        """Reboot and shutdown stay in /docs: a stray thumb on a phone must
        not be able to take the box away from the person in front of it."""
        paths = _posted_paths(authenticated.get("/").text)
        assert not [p for p in paths if p.startswith("/system/")]

    def test_setting_a_file_aside_asks_first(self):
        """It moves a family photo out of the slideshow; a stray tap on a
        phone must not be enough."""
        page = authenticated.get("/").text
        for button in re.findall(r"<button[^>]*/archive\"[^>]*>", page):
            assert "data-confirm=" in button, button
        assert len(re.findall(r'data-post="[^"]*/archive"', page)) == 2

    def test_the_set_is_never_asked_on_a_timer(self):
        """Each answer is a question on the CEC bus, and a phone left open
        on this page would ask for ever."""
        page = authenticated.get("/").text
        assert "fetch(\"/tv/status\")" in page
        assert not re.search(r"setInterval\(\s*checkPower", page)

    def test_one_music_folder_can_be_played(self):
        """The same choice Swagger offers: the folders GET /media/folders
        lists, played with POST /mode/music?folder=."""
        page = authenticated.get("/").text
        assert 'fetch("/media/folders")' in page
        assert re.search(r'<button[^>]*data-post="/mode/music"[^>]*data-with-folder', page)
        assert '"?folder=" + encodeURIComponent(' in page
        assert "/media/folders" in {r.path for r in app.routes}
