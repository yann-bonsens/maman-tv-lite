# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""scripts/uninstall.sh, run for real against a directory tree.

It deletes things, on a machine that may also carry another project under the
same unit names — so what it removes and what it leaves is
exercised, not read. A script that stripped cards was removed from this
project once after growing two bugs around `rm -rf` in a day.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "uninstall.sh"
MARKER = ("# Installed by maman-tv-lite's scripts/install.sh; "
          "scripts/uninstall.sh removes what carries this line.")

STUB_SUDO = '#!/bin/sh\n[ "$1" = -v ] && exit 0\nexec "$@"\n'
STUB_LOG = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_CALLS"\n'


class Box:
    """A machine with Maman TV Lite installed, under one directory."""

    def __init__(self, tmp: Path):
        self.root = tmp / "root"
        self.home = tmp / "home"
        self.bin = tmp / "bin"
        self.calls = tmp / "calls"
        for directory in (self.home, self.bin):
            directory.mkdir(parents=True)
        for name, body in (("sudo", STUB_SUDO), ("systemctl", STUB_LOG),
                           ("sshd", STUB_LOG)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

        self.write("etc/maman-tv-lite/install.conf",
                   "MAMAN_ZIGBEE=yes\nMAMAN_MEDIA_DIR=/medias\n")
        self.write("etc/maman-tv-lite/api.env", "MAMAN_API_PASSWORD=x\n")
        self.write("var/lib/maman-tv-lite/tv.json", "{}\n")
        self.write("var/log/maman-tv-lite/cec-bus.log", "")
        for unit in ("maman-api", "maman-screen", "zigbee2mqtt", "cloudflared"):
            self.write(f"etc/systemd/system/{unit}.service", f"{MARKER}\n[Unit]\n")
        self.write("etc/systemd/system/mosquitto.service.d/maman-tv-lite.conf", "x")
        self.write("etc/systemd/system/user.slice.d/maman-tv-lite.conf", "x")
        self.write("etc/polkit-1/rules.d/50-maman-tv-lite-power.rules", "x")
        self.write("etc/ssh/sshd_config.d/60-maman-tv-lite.conf", "x")
        self.write("usr/local/bin/maman-tv", f"#!/bin/sh\n{MARKER}\n")
        self.write("usr/local/bin/tv-profile", f"#!/bin/sh\n{MARKER}\n")
        self.write("etc/samba/smb.conf", f"{MARKER}\n[medias]\n")
        self.write("opt/nodejs/bin/node", "")
        (self.root / "usr/local/bin/node").symlink_to("/opt/nodejs/bin/node")
        self.write("medias/pictures/grandchildren.jpg", "photo")
        for part in ("api/main.py", "scripts/screen.sh", "venv/bin/python",
                     "zigbee2mqtt/dist/index.js", "zigbee2mqtt/data/configuration.yaml"):
            path = self.home / "maman-tv-lite" / part
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        (self.home / "maman-tv-lite-install.log").write_text("log")

    def write(self, path: str, text: str) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def has(self, path: str) -> bool:
        return os.path.lexists(self.root / path)

    def uninstall(self, *args: str) -> str:
        done = subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True,
            env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
                 "MAMAN_UNINSTALL_ROOT": str(self.root),
                 "STUB_CALLS": str(self.calls)})
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


class TestByDefault:
    def test_the_services_and_commands_go(self, box):
        box.uninstall("--yes")
        for path in ("etc/systemd/system/maman-api.service",
                     "etc/systemd/system/zigbee2mqtt.service",
                     "etc/systemd/system/mosquitto.service.d/maman-tv-lite.conf",
                     "etc/systemd/system/user.slice.d",
                     "etc/polkit-1/rules.d/50-maman-tv-lite-power.rules",
                     "etc/ssh/sshd_config.d/60-maman-tv-lite.conf",
                     "usr/local/bin/maman-tv", "usr/local/bin/tv-profile"):
            assert not box.has(path), path

    def test_each_service_is_stopped_before_its_file_goes(self, box):
        box.uninstall("--yes")
        calls = box.calls.read_text()
        assert "systemctl disable --now maman-api.service" in calls
        assert "systemctl daemon-reload" in calls

    def test_the_settings_are_kept(self, box):
        box.uninstall("--yes")
        assert box.has("etc/maman-tv-lite/api.env")
        assert box.has("var/lib/maman-tv-lite/tv.json")

    def test_the_compiled_parts_and_the_zigbee_network_are_kept(self, box):
        box.uninstall("--yes")
        installed = box.home / "maman-tv-lite"
        assert (installed / "venv/bin/python").exists()
        assert (installed / "zigbee2mqtt/dist/index.js").exists()
        assert (installed / "zigbee2mqtt/data/configuration.yaml").exists()
        assert box.has("opt/nodejs/bin/node")

    def test_the_code_itself_goes(self, box):
        box.uninstall("--yes")
        installed = box.home / "maman-tv-lite"
        assert not (installed / "api").exists()
        assert not (installed / "scripts").exists()

    def test_the_share_is_stopped_but_its_configuration_kept(self, box):
        box.uninstall("--yes")
        assert "systemctl disable --now smbd nmbd" in box.calls.read_text()
        assert box.has("etc/samba/smb.conf")

class TestWhatIsNotOurs:
    def test_a_unit_without_the_marker_is_left_and_named(self, box):
        """`cloudflared service install` writes a unit of this very name,
        and another project can use the same names too."""
        box.write("etc/systemd/system/cloudflared.service", "[Unit]\n# theirs\n")
        out = box.uninstall("--yes")
        assert box.has("etc/systemd/system/cloudflared.service")
        assert "left alone: cloudflared.service" in out

    def test_another_projects_files_are_left(self, box):
        maman_tv = "# Installed by maman-tv's scripts/install.sh\n[Unit]\n"
        box.write("etc/systemd/system/maman-api.service", maman_tv)
        box.write("usr/local/bin/maman-tv", "#!/bin/sh\n# maman-tv\n")
        box.write("etc/polkit-1/rules.d/50-maman-tv-power.rules", "x")
        box.uninstall("--purge", "--yes")
        assert box.has("etc/systemd/system/maman-api.service")
        assert box.has("usr/local/bin/maman-tv")
        assert box.has("etc/polkit-1/rules.d/50-maman-tv-power.rules")

    def test_a_samba_configuration_of_somebody_elses_is_left(self, box):
        box.write("etc/samba/smb.conf", "[global]\n# the owner's own\n")
        box.uninstall("--purge", "--yes")
        assert box.has("etc/samba/smb.conf")
        assert "smbd" not in (box.calls.read_text() if box.calls.exists() else "")

    def test_a_node_link_that_points_elsewhere_is_left(self, box):
        (box.root / "usr/local/bin/node").unlink()
        (box.root / "usr/local/bin/node").symlink_to("/usr/bin/node")
        box.uninstall("--purge", "--yes")
        assert box.has("usr/local/bin/node")


class TestEverything:
    def test_purge_removes_settings_and_compiled_parts(self, box):
        box.uninstall("--purge", "--yes")
        for path in ("etc/maman-tv-lite", "var/lib/maman-tv-lite",
                     "var/log/maman-tv-lite", "opt/nodejs", "usr/local/bin/node",
                     "etc/samba/smb.conf"):
            assert not box.has(path), path
        assert not (box.home / "maman-tv-lite").exists()
        assert not (box.home / "maman-tv-lite-install.log").exists()

    def test_purge_never_touches_the_photos(self, box):
        """These are somebody's photographs: no option of an uninstaller
        reaches them, not even "remove everything"."""
        box.uninstall("--purge", "--yes")
        assert box.has("medias/pictures/grandchildren.jpg")


class TestTheQuestions:
    def _answer(self, box, replies: str) -> str:
        done = subprocess.run(
            ["bash", str(SCRIPT)], input=replies, capture_output=True, text=True,
            env={"PATH": f"{box.bin}:/usr/bin:/bin", "HOME": str(box.home),
                 "MAMAN_UNINSTALL_ROOT": str(box.root), "STUB_CALLS": str(box.calls)})
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    def test_everything_is_a_question_whose_default_is_no(self, box):
        self._answer(box, "\ny\n")          # Enter, then Proceed
        assert box.has("etc/maman-tv-lite/api.env")
        assert not box.has("etc/systemd/system/maman-api.service")

    def test_yes_to_everything_removes_the_settings(self, box):
        self._answer(box, "y\ny\n")        # everything, proceed
        assert not box.has("etc/maman-tv-lite")

    def test_no_to_proceed_does_nothing(self, box):
        self._answer(box, "y\nn\n")
        assert box.has("etc/systemd/system/maman-api.service")


def test_every_installed_unit_and_command_carries_the_marker():
    """Without it the uninstaller cannot tell this project's files from the
    another project's, and leaves them."""
    files = sorted((REPO_ROOT / "systemd").glob("*.service")) + [
        REPO_ROOT / "scripts" / name for name in ("maman-tv", "tv-profile", "cec-monitor.sh")
    ] + [REPO_ROOT / "config" / "smb.conf.template"]
    missing = [f.name for f in files if MARKER not in f.read_text().splitlines()]
    assert missing == []


def test_the_script_and_the_installer_agree_on_the_marker():
    assert f'MARKER="{MARKER}"' in SCRIPT.read_text()


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
