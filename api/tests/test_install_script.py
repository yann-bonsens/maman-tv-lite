# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens

"""Tests for scripts/install.sh.

Not a run of the installer — that needs a Raspberry Pi — but a guard on the
convention it depends on. Adding a component means touching four places, and
forgetting one of them broke every single run, fresh machines included.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "install.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_no_setting_is_read_before_it_is_given_a_value():
    """The rule that keeps the installer alive under `set -euo pipefail`.

    Reading a variable that was never set aborts the whole run, and it does so
    on a clean machine too. It happened three times over: the media component
    was added to the questions without being initialised, MAMAN_SHARE_LAN was
    initialised six lines *after* the question that reads it, and
    MAMAN_SHARE_PASSWORD was documented as an environment variable and never
    declared at all. Each one killed every single install.

    So: every MAMAN_* setting must be assigned before the first line that reads
    it. The block at the top of the script is where they all belong.
    """
    lines = SOURCE.splitlines()
    assigned: dict[str, int] = {}
    read: dict[str, int] = {}
    for number, line in enumerate(lines, start=1):
        code = re.sub(r"#.*", "", line)
        for name in re.findall(r"^\s*(MAMAN_\w+)=", code):
            assigned.setdefault(name, number)
        # A read that carries its own default — ${X:-...} — is safe under
        # `set -u` and is how an optional environment variable is meant to be
        # picked up. Only a bare read can abort the run.
        for match in re.finditer(r"\$\{?(MAMAN_\w+)(.?.?)", code):
            name, following = match.group(1), match.group(2)
            if following[:2] in (":-", ":=", ":?", ":+") or following[:1] in ("-", "=", "?", "+"):
                continue
            read.setdefault(name, number)

    too_late = {
        name: (where, assigned.get(name))
        for name, where in read.items()
        if assigned.get(name) is None or assigned[name] > where
    }
    assert not too_late, (
        "read before being set: "
        + "; ".join(f"{n} read at line {r}, set at {a}" for n, (r, a) in sorted(too_late.items()))
        + '. Declare it with the others: MAMAN_X="${MAMAN_X:-}".'
    )


def test_every_component_asked_about_is_declared_first():
    """The other half of the same rule.

    A variable passed to ask_yes_no is read by *name*, through an indirect
    expansion inside the function, so it never appears as $MAMAN_X anywhere and
    the check above cannot see it. Both tests are needed: this one caught
    MAMAN_SHARE_LAN, initialised six lines after the question that reads it.
    """
    asked = set(re.findall(r"^\s*ask_yes_no\s+(MAMAN_\w+)", SOURCE, re.MULTILINE))
    asked |= set(re.findall(r"^\s*enabled\s+(MAMAN_\w+)", SOURCE, re.MULTILINE))
    assert asked, "the installer should still be a menu"

    first_question = SOURCE.index("ask_yes_no ")
    declared = set(re.findall(r"^(MAMAN_\w+)=\"\$\{\1:-",
                              SOURCE[:first_question], re.MULTILINE))

    missing = asked - declared
    assert not missing, (
        f"asked about but not declared above the questions: {sorted(missing)}. "
        'Add MAMAN_X="${MAMAN_X:-}" beside the others.'
    )


def test_indirect_expansions_tolerate_an_unset_variable():
    """The belt to that braces: a forgotten variable must produce a question,
    not kill the installation. `${!x}` aborts under `set -u`; `${!x-}` does not.
    """
    bare = re.findall(r"\$\{!\w+\}", SOURCE)
    assert not bare, (
        f"these abort the install when the variable is unset: {bare}. "
        "Write ${!name-} instead."
    )


def test_conf_dir_is_created_group_writable_not_a_bare_mkdir():
    """buttons.json now has two writers: scripts/setup-zigbee.py (run by
    hand, via sudo) and the API process itself (the installation screen's
    button-pairing procedure). A bare `mkdir -p` leaves the directory
    root:root — the running service, hardened under ProtectSystem=strict,
    could never write there. It must be setgid (2775) and group-owned by
    $MAMAN_USER, matching the media-folder block's own precedent."""
    lines = [line for line in SOURCE.splitlines() if '"$CONF_DIR"' in line
             and ("mkdir" in line or "install -d" in line)]
    assert lines, "no line creates $CONF_DIR at all"
    for line in lines:
        assert "install -d" in line, f"still a bare mkdir: {line!r}"
        assert re.search(r'-o\s+root\s+-g\s+"\$MAMAN_USER"\s+-m\s+2775', line), (
            f"not owner root, group $MAMAN_USER, mode 2775: {line!r}"
        )


def test_maman_api_service_can_write_the_binding_file():
    """The service is ProtectSystem=strict; without an explicit
    ReadWritePaths entry, api/button_pairing.py's write to buttons.json
    would fail silently against a read-only /etc."""
    unit = (REPO_ROOT / "systemd" / "maman-api.service").read_text(encoding="utf-8")
    assert re.search(r"^ReadWritePaths=-/etc/maman-tv-lite\s*$", unit, re.MULTILINE), (
        "missing (or missing its leading '-') ReadWritePaths for "
        "/etc/maman-tv-lite — a brand-new install, before this directory "
        "exists, must still be able to start"
    )


def test_maman_api_service_can_write_the_zigbee2mqtt_config():
    """api/zigbee_pairing.py's installation-screen bootstrap writes the
    adapter's serial path directly into Zigbee2MQTT's own configuration —
    without this, ProtectHome=read-only refuses it silently."""
    unit = (REPO_ROOT / "systemd" / "maman-api.service").read_text(encoding="utf-8")
    assert re.search(
        r"^ReadWritePaths=-__MAMAN_HOME__/maman-tv-lite/zigbee2mqtt/data\s*$",
        unit, re.MULTILINE), (
        "missing (or missing its leading '-') ReadWritePaths for "
        "zigbee2mqtt/data — a box where MAMAN_ZIGBEE was declined has no "
        "such directory at all, and the service must still start"
    )
    assert re.search(
        r"^Environment=MAMAN_Z2M_CONFIG=__MAMAN_HOME__/maman-tv-lite/"
        r"zigbee2mqtt/data/configuration\.yaml\s*$", unit, re.MULTILINE)


def test_zigbee2mqtt_is_enabled_unconditionally():
    """It used to be enabled only when a serial adapter was already
    present at install time. zigbee2mqtt.service's own
    ConditionPathExistsGlob= now makes the crash-loop that gate existed to
    prevent impossible regardless of when the adapter shows up, so there
    is no reason left to special-case it — and every reason not to: a box
    whose adapter appears only after the first boot needs the unit enabled
    for the installation screen's own bootstrap to `systemctl restart` it
    into life."""
    assert "no Zigbee adapter detected" not in SOURCE
    assert "ls /dev/serial/by-id" not in SOURCE


def test_zigbee2mqtt_never_starts_without_an_adapter():
    unit = (REPO_ROOT / "systemd" / "zigbee2mqtt.service").read_text(encoding="utf-8")
    assert re.search(r"^ConditionPathExistsGlob=/dev/serial/by-id/\*\s*$",
                     unit, re.MULTILINE)


def test_the_zigbee_polkit_rule_is_scoped_to_start_stop_restart_only():
    """No enable/disable/mask, and no unit besides zigbee2mqtt.service —
    the same narrow, single-unit shape as the existing power/hdmi rule."""
    rule = (REPO_ROOT / "polkit" / "51-maman-tv-lite-zigbee.rules").read_text(encoding="utf-8")
    assert "manage-unit-files" not in rule, \
        "must not grant enable/disable/mask, only start/stop/restart"
    assert '"start"' in rule and '"stop"' in rule and '"restart"' in rule
    assert rule.count('unit === "') >= 1
    assert "zigbee2mqtt.service" in rule
    # Every unit named must be zigbee2mqtt.service - no other service name
    # anywhere in the rule.
    named_units = re.findall(r'unit === "([^"]+)"', rule)
    assert named_units and set(named_units) == {"zigbee2mqtt.service"}


def _weight(unit_file):
    unit = (REPO_ROOT / "systemd" / unit_file).read_text(encoding="utf-8")
    return int(re.search(r"^CPUWeight=(\d+)", unit, re.MULTILINE).group(1))


def _installer_weights():
    """{unit: weight} for everything the installer writes a drop-in for."""
    found = dict(re.findall(r'write_weight "?([\w.$]+)"? (\d+)', SOURCE))
    loop = re.search(r'for background in ([^\n;]+); do', SOURCE)
    if loop and "$background.service" in found:
        weight = found.pop("$background.service")
        for name in loop.group(1).split():
            found[f"{name}.service"] = weight
    return {unit: int(weight) for unit, weight in found.items()}


def test_nothing_on_the_box_outranks_the_buttons():
    """ASH, between Zigbee2MQTT and the adapter, has hard acknowledgement
    deadlines. Starved, Node misses them and the adapter gives up — and the
    buttons then do nothing at all until the service has restarted, which costs
    about a minute of CPU on this board.

    Measured, not theoretical: Zigbee2MQTT at 20 beside an API at 10000 dropped
    the adapter fifteen seconds into a track, and the next press was never
    received. Music that stutters is a poor evening; a button that does nothing
    is a box that cannot be used.
    """
    weights = _installer_weights()
    buttons = _weight("zigbee2mqtt.service")
    assert weights["mosquitto.service"] >= buttons > _weight("maman-api.service"), \
        "the buttons must outrank the music"


def test_the_way_in_keeps_a_share_of_its_own():
    """The same first attempt made the box unreachable over ssh for two
    minutes. Nobody can walk up to this machine."""
    weights = _installer_weights()
    assert weights["ssh.service"] > weights["smbd.service"]
    assert "user.slice.d" in SOURCE and "CPUWeight=1000" in SOURCE


def test_the_background_services_give_way():
    """One core, and in a day they burnt five times what the product did:
    cloudflared 1644 s, smbd 1547 s, tailscaled 1029 s, against 283 s for
    maman-api. A photo that normally reaches the screen in 3 s took 20.3 s at
    load 5.4 while Samba was starting."""
    weights = _installer_weights()
    api = _weight("maman-api.service")
    for service in ("smbd.service", "cloudflared.service", "tailscaled.service"):
        assert weights[service] < api, f"{service} is a measured top-three consumer"


def test_the_buttons_are_never_throttled_by_the_background_list():
    """The regression that broke the box: they were in it."""
    loop = re.search(r'for background in ([^\n;]+); do', SOURCE).group(1).split()
    assert "zigbee2mqtt" not in loop and "mosquitto" not in loop


def test_the_background_services_survive_a_starved_startup():
    """Measured in the field: on a Pi 1 boot busy with cloud-init and
    Zigbee2MQTT, smbd's own ExecCondition (parsing smb.conf) blew through
    systemd's default 90 s startup timeout — the same low CPUWeight that
    gives way to the music and photos starved its own startup check, not
    just its running state. Debian's smbd.service sets no Restart= at all,
    so it stayed "failed" for eleven hours until somebody noticed the share
    was unreachable and restarted it by hand, on a box nobody can walk up
    to. The `resilient` flag on write_weight() is what fixes that.
    """
    loop = SOURCE[SOURCE.index("for background in "):]
    loop = loop[:loop.index("\n    done")]
    assert re.search(r'write_weight "\$background\.service" \d+ "[^"]*" 1', loop), \
        "the background services must be written with the resilient flag set"

    helper = SOURCE[SOURCE.index("write_weight() {"):]
    helper = helper[:helper.index("\n    }")]
    assert "TimeoutStartSec=300" in helper
    assert "Restart=on-failure" in helper
    # Spaced past systemd's own StartLimitIntervalUSec (10s default), or a
    # retry storm during a bad boot trips the burst limiter and the unit
    # ends up "failed" again regardless.
    assert "RestartSec=30" in helper


def test_a_persistently_broken_background_service_backs_off():
    """A flat retry interval fixes the boot-storm case but reintroduces the
    exact fault CLAUDE.md already records for Zigbee2MQTT: 318 consecutive
    restarts pinning a Pi 1 at load 2 until the hardware watchdog rebooted
    it twice. A background service that is genuinely broken, not just caught
    in a slow boot, must not retry every 30 s forever."""
    helper = SOURCE[SOURCE.index("write_weight() {"):]
    helper = helper[:helper.index("\n    }")]
    assert "RestartSteps=" in helper
    assert "RestartMaxDelaySec=" in helper


def test_the_drop_ins_are_written_for_services_that_exist():
    """Every one of them is optional — a box without Tailscale, without the
    share, without the tunnel — and `systemctl` must not be handed a unit that
    was never installed."""
    helper = SOURCE[SOURCE.index("write_weight() {"):]
    helper = helper[:helper.index("\n    }")]
    assert "list-unit-files" in helper and "return 0" in helper


def _packages_block() -> str:
    block = SOURCE[SOURCE.index("PACKAGES=("):]
    return block[:block.index("\nsudo apt-get update")]


def test_the_installation_screens_packages_are_never_behind_a_component():
    """fbi, python3-pil and fonts-dejavu-core draw the installation screen —
    the box's only way to be configured without a laptop — so unlike
    mpg123/alsa-utils (music-only) they must never be conditional on
    `enabled MAMAN_...`."""
    block = _packages_block()
    for package in ("fbi", "python3-pil", "fonts-dejavu-core"):
        line = next((l for l in block.splitlines() if package in l), None)
        assert line is not None, f"{package} is not installed at all"
        assert "enabled" not in line, f"{package} must not be behind a component question"


def test_pillow_is_not_pip_installed_over_the_system_package():
    """python3-pil is prebuilt for the board's actual architecture; a pip
    Pillow on top would try to compile from source on a Pi 1 (no armv6l
    wheel exists) and the two could drift to different versions."""
    requirements = (REPO_ROOT / "api" / "requirements.txt").read_text(encoding="utf-8")
    assert not re.search(r"(?i)^pillow", requirements, re.MULTILINE)


def test_the_venv_sees_the_system_pillow():
    assert "python3 -m venv --system-site-packages" in SOURCE


def test_the_screen_service_is_no_longer_an_optional_component():
    """It draws the installation screen now, not only the diagnostic one, so
    it is core like maman-api rather than gated behind MAMAN_SCREEN."""
    unit_wanted = SOURCE[SOURCE.index("unit_wanted() {"):]
    unit_wanted = unit_wanted[:unit_wanted.index("\n}")]
    line = next(l for l in unit_wanted.splitlines() if "maman-screen.service" in l)
    assert "MAMAN_SCREEN" not in line


def _share_password_question() -> str:
    """The block that asks for the share password, with its guard."""
    start = SOURCE.index("SHARE_IS_NEW=1")
    return SOURCE[start:SOURCE.index("\necho\necho \"Installing:", start)]


def test_the_share_password_is_asked_for_only_when_the_share_is_new():
    """This script installs a box and updates it, with the same command.

    Asking again on every update is not merely noise: the Mac on the other
    side has remembered the password, and anybody who types something new at
    a prompt they did not expect breaks that mount without meaning to. The API
    password is guarded by the existence of its env file; the share's
    equivalent is what the previous run saved.
    """
    question = _share_password_question()
    assert 'SAVED_SHARE" = "yes"' in question, \
        "the guard must read what the previous run installed"
    assert '"$SHARE_IS_NEW" -eq 1' in question, \
        "the question must be behind the guard, not merely near it"
    assert "RECONFIGURE" in question, \
        "--reconfigure means 'change my answers', so it must ask again"


def test_the_previous_share_answer_is_read_whatever_the_flags_say():
    """`--with-share` on an update must not make the box look new.

    The loop that reads the saved answers only fills in what is still unset,
    so MAMAN_SHARE alone cannot tell a first setup from an update once a flag
    has overridden it. SAVED_SHARE records what the file said, regardless.
    """
    assert re.search(r'SAVED_SHARE=""', SOURCE), "SAVED_SHARE must be declared at the top"
    assert '[ "$key" = "MAMAN_SHARE" ] && SAVED_SHARE="$value"' in SOURCE
    declared = SOURCE.index('SAVED_SHARE=""')
    assert declared < SOURCE.index("SHARE_IS_NEW=1"), \
        "declared before it is read, like every other answer"


def test_a_box_with_no_share_password_is_told_so():
    """Skipping the question leaves an account Samba will refuse every
    connection for, and nothing in that symptom points at the cause. Checked
    against Samba's own database, where sudo is available, rather than guessed
    from the answers."""
    # Anchored on the log line, because `if enabled MAMAN_SHARE` also opens
    # the question block three hundred lines earlier.
    share_block = SOURCE[SOURCE.index('log "File share over Tailscale (Samba)"'):]
    share_block = share_block[:share_block.index("\nelif ")]
    assert "pdbedit -L -u" in share_block
    assert "smbpasswd -a" in share_block


def test_the_share_password_is_never_saved_to_the_answers_file():
    """It would land in a 644 file. The API password does not go there either."""
    saved = SOURCE[SOURCE.index('"# Written by scripts/install.sh'):]
    saved = saved[:saved.index("| sudo tee")]
    assert "MAMAN_SHARE_PASSWORD" not in saved
    assert "MAMAN_API_PASSWORD" not in saved


def test_the_screen_component_question_is_gone():
    """The screen service stopped being optional when it started drawing the
    installation screen — the box's only way to be configured without a
    laptop. Only half of that change was made: `unit_wanted` dropped the
    condition and the question stayed, so answering "no" printed
    "TV screen : no" in the summary and installed the service anyway.

    An installer that asks a question it then ignores is worse than one that
    does not ask: it reports back the opposite of what it did.
    """
    # The comment explaining the removal names the variable, so this looks at
    # the shell that runs rather than at the prose around it.
    code = "\n".join(line for line in SOURCE.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "MAMAN_SCREEN" not in code, "nothing reads it any more"
    assert "--with-screen" not in code and "--without-screen" not in code
    assert "TV screen" not in code, "the summary must not report a component nobody chose"


def test_the_profile_command_and_its_library_are_installed():
    """`tv-profile` runs on the box, not from a laptop over SSH: switching
    televisions is something somebody does while standing next to the thing,
    and the product must not assume an administrator's account exists."""
    assert "/usr/local/bin/tv-profile" in SOURCE
    assert '"$SCRIPT_DIR"/profiles/*.json' in SOURCE, \
        "the measured televisions have to reach the box too"


def test_nothing_in_the_shipped_scripts_assumes_an_admin_account():
    """The service account is substituted at install time; an account someone
    added for themselves is not part of the product."""
    for name in ("maman-tv", "tv-profile"):
        source = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "claude@" not in source, f"{name} names a personal account"
        assert "ssh " not in source, f"{name} reaches out over SSH"


def test_the_card_backup_finds_a_card_in_a_built_in_reader():
    """A Mac's built-in SD slot reports the card as "Device Location:
    Internal". Measured with a card in the reader: `diskutil list external
    physical` returned nothing at all, and refusing anything internal refused
    the card itself. What separates a card from the Mac's own disk is
    "Removable Media: Removable" against "Fixed".
    """
    source = (REPO_ROOT / "scripts" / "backup-card.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "Removable Media: *Removable" in code
    assert "diskutil list external" not in code, \
        "a built-in reader is internal; that filter finds nothing"
    assert "Device Location:.*Internal" not in code, \
        "refusing internal media refuses the card itself"


def test_the_card_backup_refuses_the_disk_the_mac_boots_from():
    """It only ever reads, so the cost of the wrong disk is a useless image
    rather than a lost Mac — but the guard is cheap and the confirmation
    prompt is the last thing between a tired person and a long wait."""
    source = (REPO_ROOT / "scripts" / "backup-card.sh").read_text(encoding="utf-8")
    assert "root_disks" in source
    assert "Part of Whole" in source and "Physical Store" in source, \
        "on APFS the container backing / is synthesized; its store counts too"


def test_the_card_backup_only_reads():
    """Restoring is the dangerous direction and is deliberately not here."""
    source = (REPO_ROOT / "scripts" / "backup-card.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#") and "echo" not in line)
    assert "of=/dev/" not in code, "this script must never write to a disk"
    assert 'if="/dev/r' in code, \
        "the raw device, which is several times faster on a card this size"

def test_the_installer_installs_what_the_box_talks_to_the_television_with():
    """`cec-ctl` comes from v4l-utils, and every CEC frame goes through it.

    A near miss worth pinning: this list once named only cec-utils, and a
    development box had v4l-utils already, so nothing showed it — a clean
    install would have come up with no CEC at all, which on this product means
    two buttons that do nothing.

    And cec-utils must stay out. The two tools cannot share the adapter:
    whichever configures a logical address takes it from the other, so one left
    running stops the box talking to the television.
    """
    source = (Path(__file__).resolve().parents[2] / "scripts" / "install.sh").read_text()
    packages = source[source.index("PACKAGES=("):]
    packages = packages[:packages.index(")")]
    assert "v4l-utils" in packages, "cec-ctl would be missing"
    assert "cec-utils" not in packages, \
        "libCEC would be installed alongside the tool that cannot share with it"


# ---------------------------------------------------------------------------
# The CEC bus recorder
# ---------------------------------------------------------------------------

def _repo():
    return Path(__file__).resolve().parents[2]


def _without_comments(text: str) -> str:
    """Only the lines that actually do something.

    Matched against the code and not the prose, because a comment explaining why
    something was avoided contains the very word a naive test looks for. That
    mistake is already in this file's history: the first version of
    `test_the_docs_never_send_the_reader_to_a_command_the_box_lacks` passed on
    "mpv" appearing in a comment about dropping mpv.
    """
    return "\n".join(line for line in text.splitlines()
                      if line.strip() and not line.strip().startswith("#"))


class TestTheCecBusRecorder:
    """A diagnostic that is installed on every box and enabled on none."""

    def test_it_is_installed_and_the_installer_never_enables_it(self):
        installer = (_repo() / "scripts" / "install.sh").read_text()
        assert "/usr/local/bin/cec-monitor" in installer, \
            "the recorder is not installed anywhere"
        assert "maman-cec-monitor.service) return 0" in installer, \
            "the unit would be removed as an unwanted component"
        # The enable loop skips it by name, the way cloudflared is skipped.
        assert 'if [ "$name" = "maman-cec-monitor.service" ]; then' in installer
        assert "NOT enabled" in installer
        # It has an [Install] section, so it CAN be enabled by hand — the
        # installer simply never does it.
        unit = (_repo() / "systemd" / "maman-cec-monitor.service").read_text()
        assert "[Install]" in unit

    def test_it_only_ever_listens(self):
        """The one property that makes it safe to run beside the box. A second
        talker on the bus takes the box's own logical address away — measured,
        and it left the box unable to reach the television at all."""
        script = _without_comments((_repo() / "scripts" / "cec-monitor.sh").read_text())
        assert "--monitor" in script
        for transmits in ("--to ", "--standby", "--image-view-on", "--text-view-on",
                          "--active-source", "--user-control-pressed", "--poll",
                          "--record", "--playback", "--tv"):
            assert transmits not in script, \
                f"the recorder could put {transmits!r} on the bus"

    def test_it_cannot_fill_the_card(self):
        script = _without_comments((_repo() / "scripts" / "cec-monitor.sh").read_text())
        assert "CAP_BYTES" in script and "52428800" in script, \
            "no size cap, on a box whose journal has one for a reason"
        assert "mv -f" in script, "nothing rotates the file"

    def test_it_checks_its_size_without_a_timer(self):
        """A shell loop that forks on a timer is a permanent tax on this board:
        `sleep 1` alone was measured at 1150 ms of CPU per minute."""
        script = _without_comments((_repo() / "scripts" / "cec-monitor.sh").read_text())
        assert "CHECK_EVERY_LINES" in script
        assert "sleep" not in script

    def test_the_unit_gives_way_to_the_product(self):
        unit = _without_comments(
            (_repo() / "systemd" / "maman-cec-monitor.service").read_text())
        assert "CPUWeight=20" in unit, \
            "a diagnostic must never compete with the buttons or the music"
        # systemd creates the directory, so the unit cannot fail for a missing
        # path — ReadWritePaths= on one that does not exist stops a service from
        # starting at all.
        assert "LogsDirectory=maman-tv-lite" in unit
        assert "ReadWritePaths" not in unit


# ---------------------------------------------------------------------------
# Every Raspberry Pi
# ---------------------------------------------------------------------------

def test_no_kernel_is_ever_removed():
    """The installer used to purge every -v7 and -v8 kernel to save space on a
    Pi 1. Those are the kernels a Pi 2, 3, Zero 2 and 4 boot from on the
    32-bit image, so the reboot it asks for at the end would have left one of
    those with no kernel to start."""
    code = _without_comments(SOURCE)
    assert not re.search(r"apt-get\s+(purge|remove)[^\n]*linux-image", code)
    assert "unwanted_kernels" not in code


def test_no_architecture_is_asked_to_be_confirmed():
    """Every Pi is meant to work, so nothing stops to ask "continue anyway?"
    on anything but a Pi 1."""
    assert "Continue anyway" not in SOURCE


def _road(arch: str, tmp_path) -> dict:
    """Run the architecture block alone, with a dpkg that answers `arch`."""
    block = SOURCE[SOURCE.index('DPKG_ARCH="$(dpkg'):SOURCE.index("Z2M_CACHE=")]
    fake = tmp_path / "dpkg"
    fake.write_text(f"#!/bin/sh\necho {arch}\n")
    fake.chmod(0o755)
    out = subprocess.run(
        ["bash", "-euc", block + 'echo "$Z2M_VERSION $NODE_BIN ${NODE_MAJOR:-}"'],
        capture_output=True, text=True, check=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"}).stdout.split()
    return {"z2m": out[0], "node": out[1], "major": out[2] if len(out) > 2 else ""}


def test_the_32_bit_image_keeps_the_armv6_road(tmp_path):
    road = _road("armhf", tmp_path)
    assert road == {"z2m": "2.12.0", "node": "/opt/nodejs/bin/node", "major": ""}


def test_the_64_bit_image_takes_the_nodesource_road(tmp_path):
    road = _road("arm64", tmp_path)
    assert road["node"] == "/usr/bin/node"
    assert road["major"] == "24"
    assert road["z2m"] != "2.12.0"


def test_the_road_is_chosen_by_the_userland_not_the_cpu():
    """A Pi 4 on the 32-bit image runs a 64-bit kernel: `uname -m` says
    aarch64 for a system that can only run armhf programs."""
    block = SOURCE[SOURCE.index('DPKG_ARCH="$(dpkg'):SOURCE.index("Z2M_CACHE=")]
    assert "dpkg --print-architecture" in block
    assert not re.search(r'"\$\(uname -m\)"\s*!?=', _without_comments(SOURCE))


def test_the_zigbee_unit_runs_the_node_the_installer_chose():
    unit = (REPO_ROOT / "systemd" / "zigbee2mqtt.service").read_text()
    assert re.search(r"^ExecStart=__NODE_BIN__ index\.js$", unit, re.M)
    assert '-e "s|__NODE_BIN__|$NODE_BIN|g"' in SOURCE


def test_the_armv6_esbuild_workaround_stays_on_armhf():
    code = SOURCE[SOURCE.index("esbuild (a vitest"):]
    assert code.index('[ "$DPKG_ARCH" = "armhf" ] && python3 - <<') < code.index("onlyBuiltDependencies")


def test_the_answers_are_saved_before_the_api_starts():
    """The API reads MAMAN_ZIGBEE from the answers file when it starts. Saved
    at the very end, the file was not there yet: a box installed without the
    buttons took itself for one with buttons and none paired, and opened the
    installation screen (measured on a Pi 5, 2026-10-02)."""
    code = _without_comments(SOURCE)
    assert code.index('sudo tee "$CONF_FILE"') < code.index('sudo systemctl restart "$name"')
