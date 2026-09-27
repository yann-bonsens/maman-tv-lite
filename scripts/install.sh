#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens
#
# Install Maman TV Lite on a freshly written Raspberry Pi OS Lite card.
#
# The script asks which parts you want, then installs only those. Answers are
# saved to /etc/maman-tv-lite/install.conf, so re-running it neither re-asks nor
# undoes anything. A future prebuilt image can ship that same file to install
# unattended.
#
# Written for and tested on a Raspberry Pi 1 (ARMv6), 32-bit Raspberry Pi OS
# Lite. It should adapt to other models, but the Node.js and Zigbee2MQTT steps
# are pinned to versions that still support ARMv6 — on 64-bit hardware you can
# use the official packages instead.
#
# Run it as a normal user (not root) from the root of the cloned repository.
# The script calls sudo itself where needed, so you authenticate once.
#
#   git clone <repository-url> ~/maman-tv-lite-src && cd ~/maman-tv-lite-src
#   ./scripts/install.sh
#
# Safe to re-run: every step checks before acting, and nothing you configured
# afterwards (Zigbee pairings, API password, tunnel credentials) is touched.
#
# If prebuilt artefacts are present in cache/ (Node.js, a compiled
# Zigbee2MQTT), they are reused to avoid roughly 20 minutes of building on a
# slow CPU. Otherwise everything is built from source.
#
# cache/ is deliberately NOT in the repository: compiled binaries have no
# business in git, where nobody can tell what they were built from. Cloning
# and running this script compiles locally. The prebuilt path exists for the
# image build, which is how someone skips the wait.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="$HOME/maman-tv-lite"
CONF_DIR="/etc/maman-tv-lite"
CONF_FILE="$CONF_DIR/install.conf"
API_ENV="$CONF_DIR/api.env"

# The systemd units and the polkit rule ship with __MAMAN_USER__ and
# __MAMAN_HOME__ placeholders rather than a hard-coded account, so the
# repository does not assume any particular username. They are substituted
# below with whoever runs this script.
MAMAN_USER="$(id -un)"
MAMAN_HOME="$HOME"

NODE_VERSION="20.20.2"
NODE_TARBALL="node-v${NODE_VERSION}-linux-armv6l.tar.xz"
NODE_CACHE="$SCRIPT_DIR/cache/$NODE_TARBALL"
NODE_URL="https://unofficial-builds.nodejs.org/download/release/v${NODE_VERSION}/$NODE_TARBALL"
NODE_SHA_URL="https://unofficial-builds.nodejs.org/download/release/v${NODE_VERSION}/SHASUMS256.txt"

# Two roads to Zigbee2MQTT, chosen by the architecture of the userland — not
# the CPU: a Pi 4 on the 32-bit image runs a 64-bit kernel under armhf
# programs, and `uname -m` says aarch64 for a system that cannot run one.
#
#   armhf (every Pi on the 32-bit image, the Pi 1 included): Node.js 20 from
#   the unofficial ARMv6 builds, which run on every 32-bit Pi, and Zigbee2MQTT
#   2.12.0 — the last release that accepts Node 20 — compiled on the board,
#   twenty minutes on a Pi 1, unless a prebuilt archive sits in cache/.
#
#   anything else (arm64 on the 64-bit image): Node.js from NodeSource, as
#   Zigbee2MQTT's own Linux guide does, and a newer Zigbee2MQTT whose native
#   modules come prebuilt.
DPKG_ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
if [ "$DPKG_ARCH" = "armhf" ]; then
    Z2M_VERSION="2.12.0"
    NODE_BIN=/opt/nodejs/bin/node
else
    Z2M_VERSION="2.14.1"
    # Zigbee2MQTT 2.14.1's "engines" accepts ^22.2 || ^24. Pinned to a major
    # rather than "lts", which would move on before Zigbee2MQTT does.
    NODE_MAJOR=24
    NODE_BIN=/usr/bin/node
fi
Z2M_CACHE="$SCRIPT_DIR/cache/zigbee2mqtt-${Z2M_VERSION}-armv6l-node20.tar.gz"
Z2M_REPO="https://github.com/Koenkk/zigbee2mqtt.git"

# --- Answers ----------------------------------------------------------------
# Precedence, strongest first: command-line flag, environment variable, saved
# answer file, interactive question, default below.
MAMAN_ZIGBEE="${MAMAN_ZIGBEE:-}"
MAMAN_TAILSCALE="${MAMAN_TAILSCALE:-}"
MAMAN_CLOUDFLARE="${MAMAN_CLOUDFLARE:-}"
# No MAMAN_SCREEN here any more. The screen service stopped being optional
# when it started drawing the installation screen — the box's only way to be
# configured without a laptop — but only half of that change was made: the
# unit became unconditional (see unit_wanted below) and the question stayed.
# Answering "no" printed "TV screen : no" in the summary and installed it
# anyway. An older answers file may still carry the key, or MAMAN_NETSTATUS
# before it; both are simply ignored now.
MAMAN_MEDIA="${MAMAN_MEDIA:-}"
MAMAN_SHARE="${MAMAN_SHARE:-}"
MAMAN_SHARE_LAN="${MAMAN_SHARE_LAN:-}"
MAMAN_SHARE_PASSWORD="${MAMAN_SHARE_PASSWORD:-}"
MAMAN_API_USER="${MAMAN_API_USER:-}"
MAMAN_API_PASSWORD="${MAMAN_API_PASSWORD:-}"

# What the saved answers said about the file share, before any flag or
# environment variable overrode it. This is how the installer tells setting
# the share up from merely updating a box that already has it.
SAVED_SHARE=""

ASSUME_YES=0
RECONFIGURE=0
# Kept for the log header: the parsing loop below consumes "$@".
ORIGINAL_ARGS="$*"

usage() {
    cat <<'EOF'
Usage: ./scripts/install.sh [options]

Installs Maman TV Lite. With no options it asks what you want, then remembers the
answers in /etc/maman-tv-lite/install.conf.

Options:
  -y, --yes            do not ask anything; use saved answers or the defaults
      --reconfigure    ask again even though answers were already saved
      --with-NAME      force a component on
      --without-NAME   force a component off
      --api-user NAME  username for the API (default: maman)
  -h, --help           show this message

Components (NAME above), with their defaults:
  zigbee      yes   buttons: Zigbee2MQTT, MQTT broker, Node.js (slow to build)
  tailscale   yes   remote administration over SSH, across a network you do
                    not control
  cloudflare  no    expose the API on the internet through an outbound tunnel;
                    needs a Cloudflare account and a domain name
  media       yes   music and photos on the TV (mpg123 for the sound, fbi
                    for the pictures), with the "music" and "tv" buttons
  share       no    the media folder as a network share over Tailscale only,
                    to drop files in from a Mac or a PC (needs tailscale)

The API password is asked for, never generated. Set MAMAN_API_PASSWORD in the
environment for an unattended install, and MAMAN_SHARE_PASSWORD for the file
share's own password. Neither is ever accepted as a command-line flag, because
command lines are visible to every user on the machine.

Both are asked for only when they are being set up for the first time. Running
this script again to update a box does not ask for either: change the API
password by editing /etc/maman-tv-lite/api.env, and the share password with
  sudo smbpasswd -a <user>

Examples:
  ./scripts/install.sh --without-zigbee --without-cloudflare
  ./scripts/install.sh --yes                 # unattended, defaults
  MAMAN_API_PASSWORD=hunter2 ./scripts/install.sh --yes
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)        ASSUME_YES=1; shift ;;
        --reconfigure)   RECONFIGURE=1; shift ;;
        --with-zigbee)      MAMAN_ZIGBEE=yes; shift ;;
        --without-zigbee)   MAMAN_ZIGBEE=no; shift ;;
        --with-tailscale)   MAMAN_TAILSCALE=yes; shift ;;
        --without-tailscale) MAMAN_TAILSCALE=no; shift ;;
        --with-cloudflare)  MAMAN_CLOUDFLARE=yes; shift ;;
        --without-cloudflare) MAMAN_CLOUDFLARE=no; shift ;;
        --with-media)       MAMAN_MEDIA=yes; shift ;;
        --without-media)    MAMAN_MEDIA=no; shift ;;
        --with-share)       MAMAN_SHARE=yes; shift ;;
        --without-share)    MAMAN_SHARE=no; shift ;;
        --with-share-lan)   MAMAN_SHARE_LAN=yes; shift ;;
        --without-share-lan) MAMAN_SHARE_LAN=no; shift ;;
        --media-dir)        MAMAN_MEDIA_DIR="$2"; shift 2 ;;
        --api-user)      MAMAN_API_USER="$2"; shift 2 ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; echo "Try --help." >&2; exit 1 ;;
    esac
done

# No terminal means no questions to ask: a piped or automated run must never
# block forever waiting on stdin. Said out loud when --reconfigure was asked
# for, because silently ignoring an explicit request is how people end up
# believing they changed something they did not.
if [ ! -t 0 ]; then
    if [ "$RECONFIGURE" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
        echo "NOTE: no terminal, so --reconfigure cannot ask anything." >&2
        echo "Set the components with --with-NAME / --without-NAME instead." >&2
    fi
    ASSUME_YES=1
fi

# Read previously saved answers. Only fills in what is still unset, so a flag,
# an environment variable or the card keeps priority over the file.
if [ -f "$CONF_FILE" ] && [ "$RECONFIGURE" -eq 0 ]; then
    # shellcheck disable=SC1090
    while IFS='=' read -r key value; do
        case "$key" in
            MAMAN_ZIGBEE|MAMAN_TAILSCALE|MAMAN_CLOUDFLARE|MAMAN_API_USER|MAMAN_MEDIA|MAMAN_SHARE|MAMAN_SHARE_LAN|MAMAN_MEDIA_DIR)
                [ -z "${!key-}" ] && printf -v "$key" '%s' "$value" ;;
        esac
        # Recorded whatever a flag may say, because what matters here is what
        # the previous run installed, not what this one is being asked for.
        [ "$key" = "MAMAN_SHARE" ] && SAVED_SHARE="$value"
    done < "$CONF_FILE"
    SAVED_ANSWERS=1
else
    SAVED_ANSWERS=0
fi

# Lowercase without ${var,,}, which needs bash 4. The Pi has a recent bash,
# but an installer that dies on an older one with "bad substitution" tells the
# user nothing useful.
lowercase() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

ask_yes_no() {
    # ask_yes_no VARNAME DEFAULT "question"
    local name="$1" default="$2" question="$3" reply
    if [ -n "${!name-}" ]; then
        return  # already answered by a flag, the environment or the saved file
    fi
    if [ "$ASSUME_YES" -eq 1 ]; then
        printf -v "$name" '%s' "$default"
        return
    fi
    local hint="[Y/n]"
    [ "$default" = "no" ] && hint="[y/N]"
    while true; do
        read -rp "  $question $hint " reply
        reply="$(lowercase "$reply")"
        case "$reply" in
            "")       printf -v "$name" '%s' "$default"; return ;;
            y|yes|o|oui) printf -v "$name" '%s' "yes"; return ;;
            n|no|non) printf -v "$name" '%s' "no"; return ;;
            *)        echo "  Answer y or n." ;;
        esac
    done
}

enabled() { [ "${!1-}" = "yes" ]; }

# Everything from here is written to a log as well as to the screen. An
# install takes half an hour, and the interesting part is rarely the last
# screenful: a transient network error during pip, a warning from a build, a
# service that refused to start. Over SSH the session can also end before
# anyone has read any of it.
#
# Appended, with a header per run, so re-runs accumulate rather than erase the
# evidence of the one that failed. Nothing secret goes through here: the
# password is typed without echo and never printed back.
INSTALL_LOG="${MAMAN_INSTALL_LOG:-$HOME/maman-tv-lite-install.log}"
{
    echo
    echo "=============================================================="
    echo " Install run started $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo " Arguments: ${ORIGINAL_ARGS:-(none)}"
    echo "=============================================================="
} >> "$INSTALL_LOG"
exec > >(tee -a "$INSTALL_LOG") 2>&1

echo "=============================================================="
echo " Maman TV Lite - installation"
echo "=============================================================="
if [ "$SAVED_ANSWERS" -eq 1 ]; then
    echo
    echo "Using the answers saved in $CONF_FILE."
    echo "Run with --reconfigure to change them."
else
    echo
    echo "A few questions. Press Enter to accept the default in brackets."
    echo
fi

ask_yes_no MAMAN_ZIGBEE yes \
    "Zigbee buttons (press a button to turn the TV on and off)?"
ask_yes_no MAMAN_TAILSCALE yes \
    "Remote administration over SSH from anywhere (Tailscale)?"
ask_yes_no MAMAN_CLOUDFLARE no \
    "Expose the API on the internet (Cloudflare Tunnel, needs a domain)?"
ask_yes_no MAMAN_MEDIA yes \
    "Music and photos on the TV (a button plays them)?"
if enabled MAMAN_MEDIA; then
    ask_yes_no MAMAN_SHARE no \
        "Share the media folder, to drop files in from a computer?"
    if enabled MAMAN_SHARE; then
        ask_yes_no MAMAN_SHARE_LAN no \
            "  ...reachable from the local network too (a laptop plugged in by cable)?"
    fi
else
    MAMAN_SHARE="${MAMAN_SHARE:-no}"
fi
MAMAN_SHARE_LAN="${MAMAN_SHARE_LAN:-no}"

# Who the share will even answer. Loopback and Tailscale always; the local
# network only when it was asked for.
#
# "Local network" here has to include 169.254.0.0/16 and fe80::/10: a box with
# no network at all — the case this exists for — is reached by plugging a
# laptop straight into it with a cable, and both ends then give themselves a
# link-local address and find each other by name over mDNS. The private ranges
# cover a box that does sit on a home network.
SHARE_ALLOW="127.0.0.1 ::1 100.64.0.0/10 fd7a:115c:a1e0::/48"
if enabled MAMAN_SHARE_LAN; then
    SHARE_ALLOW="$SHARE_ALLOW 169.254.0.0/16 fe80::/10 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"
fi
MAMAN_API_USER="${MAMAN_API_USER:-maman}"
MAMAN_MEDIA_DIR="${MAMAN_MEDIA_DIR:-/medias}"

# One folder, one name, nothing clever: the share, the players and the service
# unit all take it from here. Checked rather than trusted, because it ends up
# in a systemd unit and in smb.conf.
# An absolute path of letters, digits, dot, dash, underscore and slash: it is
# substituted into a systemd unit and into smb.conf, where a space or a quote
# would produce a file that parses as something else entirely.
if ! printf '%s' "$MAMAN_MEDIA_DIR" | grep -qE '^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$'; then
    echo "ERROR: '$MAMAN_MEDIA_DIR' is not a usable media folder." >&2
    echo "It must be an absolute path holding letters, digits, dot, dash," >&2
    echo "underscore and slash only - for instance /medias." >&2
    exit 1
fi

# Without Tailscale AND without the local network, the share would answer
# nobody at all.
if enabled MAMAN_SHARE && ! enabled MAMAN_TAILSCALE && ! enabled MAMAN_SHARE_LAN; then
    echo
    echo "NOTE: the file share would accept connections from Tailscale only,"
    echo "which is not being installed, and the local network was not allowed."
    echo "Turning the share off. Use --with-share-lan to reach it by cable."
    MAMAN_SHARE=no
fi

# Asked, never invented. One mechanism per path: installing by hand means
# answering questions, and the password is a question like the others. A
# generated password has to be written down before it scrolls away, and a box
# whose password nobody chose is a box whose password nobody changes.
#
# Not asked when credentials already exist: overwriting them would lock out a
# phone or a script already using them.
if [ ! -f "$API_ENV" ] && [ -z "$MAMAN_API_PASSWORD" ]; then
    if [ "$ASSUME_YES" -eq 1 ]; then
        echo "ERROR: no API password." >&2
        echo "Either set MAMAN_API_PASSWORD in the environment, or run without" >&2
        echo "--yes so it can be asked for." >&2
        exit 1
    fi
    echo
    echo "The API is password-protected. Choose one now."
    while true; do
        read -rsp "  API password (8 characters or more): " MAMAN_API_PASSWORD; echo
        read -rsp "  Again: " password_again; echo
        if [ "$MAMAN_API_PASSWORD" != "$password_again" ]; then
            echo "  They do not match."
        elif [ "${#MAMAN_API_PASSWORD}" -lt 8 ]; then
            echo "  Too short."
        else
            break
        fi
    done
fi

# Asked, never invented, exactly like the API password. Asked here, before
# anything is installed, because this is where the questions are — and answered
# without sudo, which is only taken further down.
#
# **Asked when the share is being set up, never on an update.** This script is
# how a box is updated as well as installed, and a question about a password
# that already works is worse than noise: the Mac on the other side has
# remembered it, and anybody who types something new here breaks that mount
# without meaning to. The API password is guarded the same way, by the
# existence of its env file; the share's equivalent is the answer the previous
# run saved. `--reconfigure` asks again, because that flag means "change my
# answers".
SHARE_IS_NEW=1
if [ "$SAVED_SHARE" = "yes" ] && [ "$RECONFIGURE" -eq 0 ]; then
    SHARE_IS_NEW=0
fi

if enabled MAMAN_SHARE && [ "$SHARE_IS_NEW" -eq 1 ] && [ -z "$MAMAN_SHARE_PASSWORD" ]; then
    if [ "$ASSUME_YES" -eq 1 ]; then
        echo "NOTE: the file share is being set up and no MAMAN_SHARE_PASSWORD" >&2
        echo "was given, so $MAMAN_USER has none and the share will refuse" >&2
        echo "every connection. Set one with:" >&2
        echo "  sudo smbpasswd -a $MAMAN_USER" >&2
    else
        echo
        echo "The file share needs its own password, for account $MAMAN_USER."
        echo "Press Enter to skip; you can set one later with smbpasswd."
        while true; do
            read -rsp "  Share password (8 characters or more): " MAMAN_SHARE_PASSWORD; echo
            if [ -z "$MAMAN_SHARE_PASSWORD" ]; then
                echo "  Left as it is."
                break
            fi
            read -rsp "  Again: " share_again; echo
            if [ "$MAMAN_SHARE_PASSWORD" != "$share_again" ]; then
                echo "  They do not match."
            elif [ "${#MAMAN_SHARE_PASSWORD}" -lt 8 ]; then
                echo "  Too short."
            else
                break
            fi
        done
    fi
fi

echo
echo "Installing:"
echo "  Zigbee buttons     : $MAMAN_ZIGBEE"
echo "  Tailscale          : $MAMAN_TAILSCALE"
echo "  Cloudflare Tunnel  : $MAMAN_CLOUDFLARE"
echo "  Music and photos   : $MAMAN_MEDIA"
enabled MAMAN_MEDIA && echo "  Media folder       : $MAMAN_MEDIA_DIR"
echo "  File share         : $MAMAN_SHARE${MAMAN_SHARE_LAN:+ (local network: $MAMAN_SHARE_LAN)}"
echo "  API user           : $MAMAN_API_USER"
echo
if enabled MAMAN_ZIGBEE && [ "$DPKG_ARCH" = "armhf" ] && [ ! -f "$Z2M_CACHE" ]; then
    echo "This will take a while. Zigbee2MQTT is compiled from source, which is"
    echo "around 20 minutes on a first-generation Pi, and longer on a slow card."
    echo "If it gets interrupted, run this script again: it picks the build"
    echo "up from the start and keeps anything already paired."
else
    echo "This takes a few minutes."
fi
if [ "$ASSUME_YES" -eq 0 ]; then
    read -rp "Proceed? [Y/n] " reply
    case "$(lowercase "$reply")" in n|no|non) echo "Cancelled."; exit 0 ;; esac
fi

# Ask for the sudo password once, here, then keep the timestamp alive in the
# background. Without this the build steps outlast sudo's 15-minute timeout
# and the install stops at a password prompt, minutes after the user walked
# away, with no sound and no obvious reason.
echo
echo "Administrator password needed once, now:"
sudo -v
while true; do
    sudo -n true 2>/dev/null
    sleep 50
    kill -0 "$$" 2>/dev/null || exit
done &
SUDO_KEEPALIVE_PID=$!
trap 'kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true' EXIT

# Total is computed here so the step counter matches what will actually run.
TOTAL=8
enabled MAMAN_ZIGBEE && TOTAL=$((TOTAL + 2))
enabled MAMAN_MEDIA && TOTAL=$((TOTAL + 1))
enabled MAMAN_SHARE && TOTAL=$((TOTAL + 1))
{ enabled MAMAN_TAILSCALE || enabled MAMAN_CLOUDFLARE; } && TOTAL=$((TOTAL + 1))
STEP=0
log() { STEP=$((STEP + 1)); echo -e "\n=== [$STEP/$TOTAL] $* ==="; }

# Every Raspberry Pi is meant to work. Said, not asked: this used to stop and
# ask whether to continue on anything but a Pi 1, which read as "unsupported"
# to the owner of every other Pi.
case "$DPKG_ARCH" in
    armhf) echo "Architecture: armhf (32-bit) - Zigbee2MQTT is compiled on the board." ;;
    arm64) echo "Architecture: arm64 (64-bit) - Zigbee2MQTT installs without compiling." ;;
    *)     echo "NOTE: architecture '$DPKG_ARCH' - this project is written for a Raspberry Pi." >&2 ;;
esac

# The installer copies its own directory into INSTALL_DIR. Clone the repository
# straight into that path and the two become the same place: `cp` is then asked
# to copy files onto themselves, which fails with "are the same file" in the
# middle of an otherwise healthy run. Caught here, where the message can say
# what to do, rather than 500 lines later.
if [ "$SCRIPT_DIR" = "$INSTALL_DIR" ]; then
    echo "This copy of the project sits at $INSTALL_DIR, which is also where" >&2
    echo "the installer deploys to, so it would copy files onto themselves." >&2
    echo "Keep the source somewhere else, for instance:" >&2
    echo "  mv \"$INSTALL_DIR\" \"${INSTALL_DIR}-src\" && \"${INSTALL_DIR}-src/scripts/$(basename "$0")\"" >&2
    exit 1
fi

mkdir -p "$INSTALL_DIR"

# --- System packages --------------------------------------------------------
log "System packages (apt)"
# avahi-daemon is already present on Raspberry Pi OS, but listing it makes the
# `<hostname>.local` name the diagnostic screen prints work on a plain Debian
# too — and that name is the easiest way to reach the box on a local network.
# curl and ca-certificates are used further down to fetch Node.js, Tailscale
# and cloudflared. They are present on Raspberry Pi OS, but a minimal Debian
# may lack them, and the failure would come 10 minutes into the run rather
# than here.
#
# fbi, python3-pil and fonts-dejavu-core draw the installation screen — the
# box's only way to be configured without a laptop, so unlike mpg123/alsa
# below they are never behind a component question. python3-pil rather than
# `pip install pillow`: Pillow has no prebuilt wheel for armv6l, so pip would
# compile it from source on a Pi 1, slow and needing its own build tooling,
# where the Debian package is prebuilt for the board's actual architecture
# and installs instantly. fonts-dejavu-core gives the renderer a real
# TrueType font at a fixed, known path rather than depending on Pillow's own
# scalable default font, whose size argument is not there in every version
# this has to survive.
# v4l-utils provides `cec-ctl`, which is how the box talks to the television:
# every CEC frame, the adapter's own state and the physical address go through
# it. Not optional, and easy to miss because a development machine tends to have
# it already.
#
# cec-utils (libCEC's `cec-client`) is deliberately NOT installed. Nothing uses
# it, and the two tools cannot share the adapter: whichever of them configures a
# logical address takes it from the other, so a cec-client left running — or
# started by hand to debug something — stops the box talking to the television.
PACKAGES=(avahi-daemon ca-certificates curl fbi fonts-dejavu-core git
         python3-pil python3-pip python3-venv v4l-utils)
# The MQTT broker and its client tools are only useful with the buttons: the
# broker is what Zigbee2MQTT publishes to, and setup-zigbee.py needs the
# clients.
enabled MAMAN_ZIGBEE && PACKAGES+=(mosquitto mosquitto-clients)
# What Zigbee2MQTT's Linux guide installs alongside Node.js on the arm64 road:
# a compiler in case a native module has no prebuilt binary after all, and
# libsystemd-dev for its systemd integration. The armhf road has always built
# without asking for them.
if enabled MAMAN_ZIGBEE && [ "$DPKG_ARCH" != "armhf" ]; then
    PACKAGES+=(make g++ gcc libsystemd-dev)
fi
# mpg123 plays the music. Chosen by measurement against mpv on this board:
# 4.5 times cheaper for a track. See CLAUDE.md.
#
# alsa-utils is not a player: it brings `aplay -L`, which is the only safe way
# to find out what MAMAN_AUDIO_DEVICE should be set to. The documentation used
# to send people to mpg123 for that list, and mpg123 with no file to play reads
# its standard input instead and sits at 100% of this board's one core.
enabled MAMAN_MEDIA && PACKAGES+=(mpg123 alsa-utils)
enabled MAMAN_SHARE && PACKAGES+=(samba samba-common-bin)
sudo apt-get update
sudo apt-get install -y --no-install-recommends "${PACKAGES[@]}"

# --- Boot settings in config.txt --------------------------------------------
log "Boot settings (CEC, camera)"
CONFIG_TXT=/boot/firmware/config.txt
# Stops the board from broadcasting an automatic CEC "Active Source" message
# at boot, which otherwise makes the TV switch input on every restart. This
# keeps CEC usable for our own commands, unlike hdmi_ignore_cec=1 — never use
# that one, it disables CEC entirely.
if ! grep -q "^hdmi_ignore_cec_init=1" "$CONFIG_TXT"; then
    echo "hdmi_ignore_cec_init=1" | sudo tee -a "$CONFIG_TXT" > /dev/null
fi
# No CSI camera attached, so skip a pointless probe at boot.
if grep -q "^camera_auto_detect=1" "$CONFIG_TXT"; then
    sudo sed -i "s/^camera_auto_detect=1/camera_auto_detect=0/" "$CONFIG_TXT"
elif ! grep -q "^camera_auto_detect=" "$CONFIG_TXT"; then
    echo "camera_auto_detect=0" | sudo tee -a "$CONFIG_TXT" > /dev/null
fi

# --- Hardware watchdog and persistent journal -------------------------------
log "Hardware watchdog and persistent journal"
# The watchdog on this SoC is fixed at 60 s (SETTIMEOUT is unsupported), so the
# exact value matters little as long as it stays under that; systemd adapts to
# the real hardware timeout.
#
# IMPORTANT: no trailing comment on that line. /etc/systemd/*.conf does not
# accept them, and the whole line would be ignored in silence.
if ! grep -q "^RuntimeWatchdogSec=" /etc/systemd/system.conf; then
    sudo sed -i "s/^#RuntimeWatchdogSec=off/RuntimeWatchdogSec=55/" /etc/systemd/system.conf
fi
sudo mkdir -p "/var/log/journal/$(cat /etc/machine-id)"
sudo chown root:systemd-journal "/var/log/journal/$(cat /etc/machine-id)"
sudo chmod 2755 "/var/log/journal/$(cat /etc/machine-id)"
if ! grep -q "^Storage=persistent" /etc/systemd/journald.conf; then
    sudo sed -i "s/#Storage=auto/Storage=persistent/" /etc/systemd/journald.conf
fi
# Without an explicit limit journald may grow to 10% of the disk, which is far
# too much on a small card.
#
# **200 MB, not the 5 MB this used to set.** That number was chosen from
# "observed usage after many test reboots stayed under 2 MB", which measured
# a box nobody was using rather than one in service. On 2026-09-22 the API's
# own lines for the hour that mattered had already been discarded by the time
# anybody looked, and the failure had to be reconstructed from file
# timestamps. This box goes to a place with no internet, where the journal and
# the television's own diagnostic page are the only two ways to find out what
# happened — so a cap that throws the evidence away is the expensive kind of
# saving. 200 MB is under 3% of the free space on a 16 GB card and holds
# weeks.
JOURNAL_CAP="${MAMAN_JOURNAL_CAP:-200M}"
if grep -q "^SystemMaxUse=" /etc/systemd/journald.conf; then
    sudo sed -i "s/^SystemMaxUse=.*/SystemMaxUse=$JOURNAL_CAP/" /etc/systemd/journald.conf
else
    sudo sed -i "s/#SystemMaxUse=/SystemMaxUse=$JOURNAL_CAP/" /etc/systemd/journald.conf
fi
sudo systemctl daemon-reexec
sudo systemctl restart systemd-journald

if enabled MAMAN_ZIGBEE; then
    # --- Node.js ------------------------------------------------------------
    if [ "$DPKG_ARCH" = "armhf" ]; then
        # ARMv6 is not served by official Node.js builds, hence the unofficial
        # ones (from cache when available). They run on every 32-bit Pi.
        log "Node.js v${NODE_VERSION} (ARMv6, unofficial build)"
        if [ ! -x /opt/nodejs/bin/node ] || [ "$(/opt/nodejs/bin/node --version 2>/dev/null)" != "v${NODE_VERSION}" ]; then
            if [ -f "$NODE_CACHE" ]; then
                echo "Using the local cache: $NODE_CACHE"
                NODE_TARBALL_PATH="$NODE_CACHE"
            else
                echo "No cache, downloading from unofficial-builds.nodejs.org"
                tmpdir=$(mktemp -d)
                curl -sLo "$tmpdir/$NODE_TARBALL" "$NODE_URL"
                curl -sLo "$tmpdir/SHASUMS256.txt" "$NODE_SHA_URL"
                expected=$(grep "$NODE_TARBALL" "$tmpdir/SHASUMS256.txt" | awk '{print $1}')
                actual=$(sha256sum "$tmpdir/$NODE_TARBALL" | awk '{print $1}')
                if [ "$expected" != "$actual" ]; then
                    echo "ERROR: Node.js checksum mismatch, aborting." >&2
                    exit 1
                fi
                NODE_TARBALL_PATH="$tmpdir/$NODE_TARBALL"
            fi
            sudo mkdir -p /opt/nodejs
            sudo tar -xJf "$NODE_TARBALL_PATH" -C /opt/nodejs --strip-components=1
            sudo ln -sf /opt/nodejs/bin/node /usr/local/bin/node
            sudo ln -sf /opt/nodejs/bin/npm /usr/local/bin/npm
            sudo ln -sf /opt/nodejs/bin/npx /usr/local/bin/npx
            sudo ln -sf /opt/nodejs/bin/corepack /usr/local/bin/corepack
            [ -n "${tmpdir:-}" ] && rm -rf "$tmpdir"
        else
            echo "Node.js v${NODE_VERSION} already installed."
        fi
    else
        log "Node.js $NODE_MAJOR (NodeSource)"
        if ! node --version 2>/dev/null | grep -q "^v$NODE_MAJOR\."; then
            ns_tmp="$(mktemp -d)"
            curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" -o "$ns_tmp/setup.sh"
            sudo -E bash "$ns_tmp/setup.sh"
            rm -rf "$ns_tmp"
            sudo apt-get install -y nodejs
        fi
        node --version | grep -q "^v$NODE_MAJOR\." \
            || { echo "ERROR: Node.js $NODE_MAJOR did not install ($(node --version 2>&1))." >&2; exit 1; }
    fi
    node --version

    # --- Zigbee2MQTT ---------------------------------------------------------
    # Pinned on both roads: raising Z2M_VERSION at the top and re-running the
    # installer is how it gets updated. The armhf one cannot go further,
    # because later releases need a Node.js with no ARMv6 build.
    log "Zigbee2MQTT v${Z2M_VERSION}"
    # Compare the built version, not merely the presence of a build. Checking
    # for dist/ alone meant that raising Z2M_VERSION here updated nothing: the
    # old build stayed, and the script said it was fine.
    # Two helpers, because replacing the tree happens on both the cache path
    # and the build path, and a half-copied difference between them would be
    # the kind of bug nobody notices until a pairing is lost. The data
    # directory holds the Zigbee network key and the paired devices: losing it
    # means pairing every button again.
    z2m_clear_keeping_data() {
        [ -d "$INSTALL_DIR/zigbee2mqtt" ] || return 0
        if [ -d "$INSTALL_DIR/zigbee2mqtt/data" ]; then
            mv "$INSTALL_DIR/zigbee2mqtt/data" "$INSTALL_DIR/zigbee2mqtt-data.keep"
        fi
        rm -rf "$INSTALL_DIR/zigbee2mqtt"
    }
    z2m_restore_data() {
        [ -d "$INSTALL_DIR/zigbee2mqtt-data.keep" ] || return 0
        rm -rf "$INSTALL_DIR/zigbee2mqtt/data"
        mv "$INSTALL_DIR/zigbee2mqtt-data.keep" "$INSTALL_DIR/zigbee2mqtt/data"
        echo "Kept the existing Zigbee network and paired devices."
    }

    # Compare the built version, not merely the presence of a build. Checking
    # for dist/ alone meant that raising Z2M_VERSION here updated nothing: the
    # old build stayed and the script reported success.
    INSTALLED_Z2M=""
    if [ -f "$INSTALL_DIR/zigbee2mqtt/package.json" ]; then
        INSTALLED_Z2M="$(python3 -c "
import json, sys
try:
    print(json.load(open(sys.argv[1])).get('version', ''))
except Exception:
    print('')
" "$INSTALL_DIR/zigbee2mqtt/package.json" 2>/dev/null)"
    fi

    if [ -d "$INSTALL_DIR/zigbee2mqtt/dist" ] && [ "$INSTALLED_Z2M" = "$Z2M_VERSION" ]; then
        echo "Zigbee2MQTT v$Z2M_VERSION already present and built, nothing to do."
    else
        if [ -n "$INSTALLED_Z2M" ] && [ "$INSTALLED_Z2M" != "$Z2M_VERSION" ]; then
            echo "Zigbee2MQTT v$INSTALLED_Z2M installed, v$Z2M_VERSION wanted: replacing."
        fi
        if [ "$DPKG_ARCH" = "armhf" ] && [ -f "$Z2M_CACHE" ]; then
            echo "Using the prebuilt cache: $Z2M_CACHE (saves ~20 min of building)"
            z2m_clear_keeping_data
            tar -xzf "$Z2M_CACHE" -C "$INSTALL_DIR"
            z2m_restore_data
        else
            echo "Building from source (about 20 min on a Pi 1, a few on a 64-bit Pi)"
            # A previous run interrupted mid-build leaves a directory with no
            # dist/ in it, and `git clone` refuses to write into one. Without
            # this the installer could not simply be run again after a dropped
            # connection, which is exactly when someone runs it again.
            z2m_clear_keeping_data
            git clone --depth 1 --branch "$Z2M_VERSION" "$Z2M_REPO" "$INSTALL_DIR/zigbee2mqtt"
            z2m_restore_data
            cd "$INSTALL_DIR/zigbee2mqtt"
            # esbuild (a vitest dependency, not needed at runtime) ships no
            # ARMv6 binary and crashes during installation, so it is removed
            # from the list of packages allowed to run their install scripts.
            # Only on armhf: on arm64 every one of them has its binary.
            [ "$DPKG_ARCH" = "armhf" ] && python3 - <<'PYEOF'
import json
path = "package.json"
with open(path) as f:
    data = json.load(f)
data["pnpm"]["onlyBuiltDependencies"] = ["@serialport/bindings-cpp", "unix-dgram"]
with open(path, "w") as f:
    json.dump(data, f, indent=4)
    f.write("\n")
PYEOF
            # `corepack enable` is deliberately NOT called. It only creates
            # shims in /usr/local/bin, which a normal user cannot write, so it
            # printed a frightening "Internal Error: EACCES" in the middle of
            # an otherwise healthy install. `corepack pnpm` needs no shims.
            #
            # The prompt variable matters more than it looks: without it
            # corepack asks for confirmation before downloading pnpm, and an
            # install left running unattended would wait for a keypress.
            export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
            corepack pnpm install --frozen-lockfile
            corepack pnpm run build
            cd "$SCRIPT_DIR"
        fi
    fi

    # NEVER overwrite an existing configuration.yaml. Zigbee2MQTT writes the
    # generated network key back into this same file, and setup-zigbee.py
    # writes the serial port into it. Replacing it would invalidate the Zigbee
    # network and force every paired device to be paired again.
    Z2M_CONFIG="$INSTALL_DIR/zigbee2mqtt/data/configuration.yaml"
    if [ -f "$Z2M_CONFIG" ]; then
        echo "Existing Zigbee2MQTT configuration kept (network key and pairings)."
    else
        mkdir -p "$(dirname "$Z2M_CONFIG")"
        cp "$SCRIPT_DIR/zigbee2mqtt-config/configuration.yaml" "$Z2M_CONFIG"
        echo "Default Zigbee2MQTT configuration written."
    fi
fi

# --- Python API -------------------------------------------------------------
log "Python API (FastAPI)"
# A virtualenv records absolute paths in the shebang of every script it
# installs, so moving or renaming the directory silently breaks pip and
# uvicorn while the venv's own python keeps working. Re-running `python3 -m
# venv` over the top does not rewrite those shebangs either. The same thing
# happens when the system Python is upgraded underneath it.
#
# So test the thing that actually breaks, a console script, and rebuild from
# scratch when it fails. Costs a few minutes on a slow board, and only when
# something is already wrong.
if [ -d "$INSTALL_DIR/venv" ] && ! "$INSTALL_DIR/venv/bin/pip" --version >/dev/null 2>&1; then
    echo "The Python environment is broken (moved directory, or a Python upgrade)."
    echo "Rebuilding it from scratch."
    rm -rf "$INSTALL_DIR/venv"
fi
# --system-site-packages: the installation screen's page renderer uses the
# Debian-packaged python3-pil (see PACKAGES above), not a pip-installed
# Pillow. Venv-installed packages still take precedence on sys.path, so this
# changes nothing for fastapi/uvicorn/paho-mqtt below.
python3 -m venv --system-site-packages "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$SCRIPT_DIR/api/requirements.txt"
mkdir -p "$INSTALL_DIR/api" "$INSTALL_DIR/scripts"
cp "$SCRIPT_DIR"/api/*.py "$INSTALL_DIR/api/"
cp "$SCRIPT_DIR"/scripts/screen.sh "$SCRIPT_DIR"/scripts/setup-zigbee.py \
    "$INSTALL_DIR/scripts/"
chmod +x "$INSTALL_DIR/scripts/screen.sh" "$INSTALL_DIR/scripts/setup-zigbee.py"
# The shell commands, on everybody's PATH: "what is this box doing?" and
# "which television is this set up for?" must both be answerable by somebody
# who has just ssh'd in and knows nothing else.
sudo install -m 0755 "$SCRIPT_DIR/scripts/maman-tv" /usr/local/bin/maman-tv
sudo install -m 0755 "$SCRIPT_DIR/scripts/tv-profile" /usr/local/bin/tv-profile
# The CEC bus recorder. Installed always, started never: it is a diagnostic for a
# measurement session, and it is the unit's own job to stay off.
sudo install -m 0755 "$SCRIPT_DIR/scripts/cec-monitor.sh" /usr/local/bin/cec-monitor
# The library of measured televisions goes with it. Copied rather than left in
# the source tree, so `tv-profile` finds them whether or not the clone this
# was installed from is still around.
if [ -d "$SCRIPT_DIR/profiles" ]; then
    mkdir -p "$INSTALL_DIR/profiles"
    cp "$SCRIPT_DIR"/profiles/*.json "$SCRIPT_DIR"/profiles/README.md \
        "$INSTALL_DIR/profiles/" 2>/dev/null || true
fi

# API credentials (HTTP Basic). Written once, then left untouched by later
# re-runs so that anything already configured elsewhere (a phone, a script)
# keeps working. Stored outside the repository: never committed.
if [ ! -f "$API_ENV" ]; then
    # root:$MAMAN_USER, setgid (2775): the installation screen's own
    # button-pairing procedure now writes buttons.json into this directory
    # from inside the running API, as the service account — see the media
    # folder block below for the same reasoning. api.env itself stays
    # root:root 600 regardless; setgid only sets a *new* file's group, not
    # the mode of files already there.
    sudo install -d -o root -g "$MAMAN_USER" -m 2775 "$CONF_DIR"
    # Written with every setting spelled out, commented. This file is the only
    # place these live, and a knob nobody can find is a knob nobody uses: the
    # television-dependent ones below were invisible until someone read the
    # source or the README.
    sudo tee "$API_ENV" >/dev/null <<EOF
# Maman TV Lite - service configuration.
#
# Read by systemd when maman-api starts, so nothing here takes effect until:
#   sudo systemctl restart maman-api
#
# Keep this file root-owned and mode 600: it holds the API password.

# --- API credentials (HTTP Basic) ------------------------------------------
MAMAN_API_USER=$MAMAN_API_USER
MAMAN_API_PASSWORD=$MAMAN_API_PASSWORD

# --- Television behaviour ---------------------------------------------------
# These two depend on your television and can only be found by trying them.
# GET /tv/config shows what is in effect; PATCH /tv/config changes it on the
# running service so each attempt costs one request instead of a restart. That
# change is temporary: once a value works, write it here.


# Path to the CEC adapter. Unset, the box picks the HDMI port the television is
# on (a Pi 4 or 5 has two). Set it only to force one; empty leaves cec-ctl to
# its own default.
#CEC_DEVICE=/dev/cec1
EOF
    sudo chown root:root "$API_ENV"
    sudo chmod 600 "$API_ENV"
    echo "API credentials written to $API_ENV (user: $MAMAN_API_USER)."
else
    echo "API credentials already present ($API_ENV) - kept as they are."
fi

# --- Remote access ----------------------------------------------------------
if enabled MAMAN_TAILSCALE || enabled MAMAN_CLOUDFLARE; then
    log "Remote access"

    # Tailscale: SSH access from anywhere, independent of Cloudflare. Verified
    # to run on ARMv6. Installed but NOT authenticated — that needs an account,
    # see docs/tailscale.md.
    if enabled MAMAN_TAILSCALE && ! command -v tailscale >/dev/null 2>&1; then
        # ID is "raspbian" on 32-bit Pi OS and "debian" on the 64-bit build, so
        # read it rather than hard-coding either one.
        TS_ID=$(. /etc/os-release && echo "$ID")
        TS_CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
        curl -fsSL "https://pkgs.tailscale.com/stable/$TS_ID/$TS_CODENAME.noarmor.gpg" \
            | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
        curl -fsSL "https://pkgs.tailscale.com/stable/$TS_ID/$TS_CODENAME.tailscale-keyring.list" \
            | sudo tee /etc/apt/sources.list.d/tailscale.list >/dev/null
        sudo apt-get update -qq
        sudo apt-get install -y tailscale
        echo "Tailscale installed, not yet authenticated."
    elif enabled MAMAN_TAILSCALE; then
        echo "Tailscale already installed."
    fi

    # cloudflared: exposes the API on the Internet through an outbound tunnel,
    # with no router change. Verified to run on ARMv6, unlike some other ARM
    # binaries. The service stays INACTIVE until a tunnel is created by hand —
    # see docs/cloudflare-tunnel.md.
    if enabled MAMAN_CLOUDFLARE; then
        if ! command -v cloudflared >/dev/null 2>&1; then
            case "$(dpkg --print-architecture)" in
                arm64)       CF_ARCH="arm64" ;;
                armhf|armel) CF_ARCH="arm" ;;
                amd64)       CF_ARCH="amd64" ;;
                *)           CF_ARCH="" ;;
            esac
            if [ -n "$CF_ARCH" ]; then
                # Downloaded into a private directory, not /tmp/cloudflared.
                # That path is guessable and /tmp is world-writable, so another
                # account could plant or swap the file between the download and
                # the install, and it would land in /usr/local/bin as root.
                #
                # Worth naming an asymmetry: the Node.js download above is
                # verified against a published SHA-256, this one is not.
                # Cloudflare publishes no checksum at a stable URL for the
                # "latest" alias, so the trust here rests on HTTPS to GitHub
                # alone. Pinning a version would allow verifying it.
                cf_tmp="$(mktemp -d)"
                curl -fsSL -o "$cf_tmp/cloudflared" \
                    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH"
                sudo install -o root -g root -m 755 "$cf_tmp/cloudflared" /usr/local/bin/cloudflared
                rm -rf "$cf_tmp"
                echo "cloudflared installed, tunnel not yet created."
            else
                echo "Unrecognised architecture - cloudflared not installed."
            fi
        else
            echo "cloudflared already installed."
        fi
        # Mode 700: this directory will hold the tunnel credentials.
        sudo mkdir -p /etc/cloudflared
        sudo chown root:root /etc/cloudflared
        sudo chmod 700 /etc/cloudflared
    fi
fi

# --- Hardening --------------------------------------------------------------
# --- The media folder, and the share -----------------------------------------
if enabled MAMAN_MEDIA; then
    log "Media folder ($MAMAN_MEDIA_DIR)"
    # Owned by the service account, which is also the account the share logs
    # in as, so a file dropped from a Mac is readable by the players without
    # anyone thinking about permissions.
    #
    # The folders themselves, never their contents: a recursive chown over a
    # library of thousands of photos costs minutes on this board's card, on
    # every single run, and changes nothing. setgid (2775) is what keeps a file
    # dropped over the share readable by the service that plays it.
    for sub in "" pictures music movies; do
        sudo install -d -o "$MAMAN_USER" -g "$MAMAN_USER" -m 2775 \
            "$MAMAN_MEDIA_DIR${sub:+/$sub}"
    done
    # ARMv6 is not ARM: a package that installs may still die with "Illegal
    # instruction" on this board. Each player is therefore run once, here,
    # where the failure is loud and next to its cause.
    for player in mpg123 fbi; do
        "$player" --help >/dev/null 2>&1
        status=$?
        # 0 or 1 is a program that ran and printed its usage; anything higher
        # on this board means "Illegal instruction" (132) or a missing library.
        if [ "$status" -le 1 ]; then
            echo "  $player runs."
        else
            echo "  WARNING: $player exited $status on --help: it may not run" >&2
            echo "  on this board (ARMv6). Music or photos would then fail." >&2
        fi
    done
    echo "Drop photos in $MAMAN_MEDIA_DIR/pictures and music in $MAMAN_MEDIA_DIR/music."
    echo "Prepare the photos on a Mac first, with scripts/prepare-photos.sh:"
    echo "it converts HEIC, turns them upright and shrinks them to the screen,"
    echo "none of which this board can do for itself."
fi

if enabled MAMAN_SHARE; then
    log "File share over Tailscale (Samba)"
    SMB_TEMPLATE="$SCRIPT_DIR/config/smb.conf.template"
    if [ ! -f "$SMB_TEMPLATE" ]; then
        echo "ERROR: $SMB_TEMPLATE is missing." >&2
        exit 1
    fi
    sed -e "s|__MEDIA_DIR__|$MAMAN_MEDIA_DIR|g" \
        -e "s|__MAMAN_USER__|$MAMAN_USER|g" \
        -e "s|__SHARE_ALLOW__|$SHARE_ALLOW|g" "$SMB_TEMPLATE" \
        | sudo tee /etc/samba/smb.conf >/dev/null
    # testparm reads the file the way Samba will; a typo here would otherwise
    # surface as a service that fails to start minutes later.
    if ! sudo testparm -s /etc/samba/smb.conf >/dev/null 2>&1; then
        echo "ERROR: Samba refused the configuration just written." >&2
        sudo testparm -s /etc/samba/smb.conf || true
        exit 1
    fi
    # NetBIOS name announcements are useless here — names do not cross
    # Tailscale — and nmbd is one daemon and one open port fewer on a board
    # with 512 MB.
    sudo systemctl disable --now nmbd >/dev/null 2>&1 || true
    if [ -n "$MAMAN_SHARE_PASSWORD" ]; then
        # Read from the descriptor, not typed on a command line: a command
        # line is visible to every account on the machine.
        printf '%s\n%s\n' "$MAMAN_SHARE_PASSWORD" "$MAMAN_SHARE_PASSWORD" \
            | sudo smbpasswd -a -s "$MAMAN_USER" >/dev/null
        echo "  Share password set for $MAMAN_USER."
    else
        # Nothing to set. On an update that is the whole point — the account
        # keeps the password the Mac on the other side has remembered, and it
        # is not asked about again. On a box where one was never set, the share
        # refuses every connection with an authentication error, and nothing in
        # that symptom points at the cause. So it is said here, where sudo is
        # available to ask Samba's own database, rather than discovered from a
        # Mac that will not mount it.
        if sudo pdbedit -L -u "$MAMAN_USER" >/dev/null 2>&1; then
            echo "  Share password left as it is."
        else
            echo "  WARNING: $MAMAN_USER has no share password, so the share" >&2
            echo "  will refuse every connection. Set one with:" >&2
            echo "    sudo smbpasswd -a $MAMAN_USER" >&2
        fi
    fi
    sudo systemctl enable --now smbd >/dev/null 2>&1 || true
    sudo systemctl restart smbd
    allowed="$(sudo testparm -s --parameter-name="hosts allow" 2>/dev/null)"
    if printf '%s' "$allowed" | grep -q "100.64.0.0/10"; then
        echo "  Answers: $allowed"
    else
        echo "  WARNING: the share is not limited to the intended addresses." >&2
    fi
    if enabled MAMAN_SHARE_LAN; then
        echo "From a Mac on the same network, or plugged in by cable:"
        echo "  smb://$(hostname).local/medias"
        echo "From Windows:   \\\\$(hostname)\\medias"
    else
        echo "From a Mac:     smb://<tailscale address>/medias"
        echo "From Windows:   \\\\<tailscale address>\\medias"
    fi
    echo "User $MAMAN_USER, with the share password."
elif [ -f /etc/samba/smb.conf ] && systemctl is-enabled smbd >/dev/null 2>&1; then
    # Turning the component off must actually turn it off.
    echo "File share not selected - stopping Samba."
    sudo systemctl disable --now smbd >/dev/null 2>&1 || true
fi

log "Hardening (sshd, polkit)"
# sshd: root unreachable. Dropped in as a separate file rather than editing
# sshd_config, so it stays reversible — delete the file to undo.
if [ -d "$SCRIPT_DIR/ssh" ]; then
    sudo cp "$SCRIPT_DIR"/ssh/*.conf /etc/ssh/sshd_config.d/
    sudo chmod 644 /etc/ssh/sshd_config.d/*.conf
    # Never restart sshd without validating first: a broken config would cut
    # remote access, and on a machine you cannot easily reach that costs a
    # physical trip.
    if sudo sshd -t; then
        sudo systemctl restart ssh
    else
        echo "WARNING: invalid sshd configuration, restart cancelled."
    fi
fi

# polkit rule: the API runs with NoNewPrivileges=true, which forbids sudo, and
# logind answers "challenge" with no interactive session. This grants exactly
# reboot and power-off to the API account. Without it, /system/reboot returns an
# explicit error rather than failing silently.
if [ -d "$SCRIPT_DIR/polkit" ]; then
    for rule in "$SCRIPT_DIR"/polkit/*.rules; do
        sed "s|__MAMAN_USER__|$MAMAN_USER|g" "$rule" \
            | sudo tee "/etc/polkit-1/rules.d/$(basename "$rule")" >/dev/null
    done
    sudo systemctl restart polkit
fi

# --- Save the answers -------------------------------------------------------
# Written after everything is installed and BEFORE the services start: the API
# reads MAMAN_ZIGBEE from it at startup. Written at the very end, it was not
# there yet when the API first started, so a box installed without the buttons
# took itself for one with buttons and none paired, and opened the
# installation screen (measured on a Pi 5, 2026-10-02). A run that fails while
# starting the services is re-run with these same answers, which is what they
# are for. This file is also what a prebuilt image would ship to install
# unattended.
sudo install -d -o root -g "$MAMAN_USER" -m 2775 "$CONF_DIR"
printf '%s\n' \
    "# Written by scripts/install.sh. Re-run with --reconfigure to change." \
    "MAMAN_ZIGBEE=$MAMAN_ZIGBEE" \
    "MAMAN_TAILSCALE=$MAMAN_TAILSCALE" \
    "MAMAN_CLOUDFLARE=$MAMAN_CLOUDFLARE" \
    "MAMAN_MEDIA=$MAMAN_MEDIA" \
    "MAMAN_MEDIA_DIR=$MAMAN_MEDIA_DIR" \
    "MAMAN_SHARE=$MAMAN_SHARE" \
    "MAMAN_SHARE_LAN=$MAMAN_SHARE_LAN" \
    "MAMAN_API_USER=$MAMAN_API_USER" \
    | sudo tee "$CONF_FILE" >/dev/null
sudo chmod 644 "$CONF_FILE"

# --- systemd services -------------------------------------------------------
log "systemd services"

# Which units this installation wants, given the answers above.
unit_wanted() {
    case "$1" in
        maman-api.service)        return 0 ;;
        # No longer gated on MAMAN_SCREEN: this service also draws the
        # installation screen now, the box's only way to be configured
        # without a laptop, so it is core like maman-api and CEC control
        # rather than an optional component.
        maman-screen.service)     return 0 ;;
        # Installed on every box and enabled on none: a recorder somebody can
        # switch on for an evening when a television is behaving oddly, and that
        # is useless if it has to be copied over first.
        maman-cec-monitor.service) return 0 ;;
        zigbee2mqtt.service)      enabled MAMAN_ZIGBEE ;;
        cloudflared.service)      enabled MAMAN_CLOUDFLARE ;;
        *)                        return 0 ;;
    esac
}

if [ -d "$SCRIPT_DIR/systemd" ] && [ -n "$(ls -A "$SCRIPT_DIR/systemd" 2>/dev/null)" ]; then
    for unit in "$SCRIPT_DIR"/systemd/*.service; do
        name="$(basename "$unit")"
        if ! unit_wanted "$name"; then
            # Turning a component off must actually turn it off, otherwise
            # re-running with --without-X would leave the old service running
            # and the answer file would be a lie.
            if systemctl list-unit-files "$name" >/dev/null 2>&1 \
               && [ -f "/etc/systemd/system/$name" ]; then
                echo "  $name not selected - disabling and removing."
                sudo systemctl disable --now "$name" >/dev/null 2>&1 || true
                sudo rm -f "/etc/systemd/system/$name"
            else
                echo "  $name not selected - skipped."
            fi
            continue
        fi
        sed -e "s|__MAMAN_USER__|$MAMAN_USER|g" \
            -e "s|__MAMAN_HOME__|$MAMAN_HOME|g" \
            -e "s|__NODE_BIN__|$NODE_BIN|g" \
            -e "s|__MEDIA_DIR__|$MAMAN_MEDIA_DIR|g" "$unit" \
            | sudo tee "/etc/systemd/system/$name" >/dev/null
    done
    # ------------------------------------------------------------------
    # Who gets the core when they all want it
    # ------------------------------------------------------------------
    # One core, and in a day the background services burnt five times what the
    # product did: cloudflared 1644 s, smbd 1547 s, tailscaled 1029 s, against
    # 283 s for maman-api. Nothing told the kernel which of them the owner is
    # looking at, so a photo that normally reaches the screen in 3 s took
    # 20.3 s at load 5.4 while Samba was starting, and the music broke into
    # ALSA underruns in the same seconds.
    #
    # The order matters as much as the throttling, and getting it wrong was
    # worse than not doing it at all. Highest is Zigbee2MQTT and its broker:
    # ASH has hard acknowledgement deadlines, and a first attempt that put
    # Zigbee2MQTT at 20 dropped the adapter fifteen seconds into a track — the
    # buttons then did nothing whatsoever. Music that stutters is a poor
    # evening; a button that does nothing is a box that cannot be used.
    #
    # Then the API, then the way in: ssh and the user sessions keep a share of
    # their own, because the same first attempt made the box unreachable for
    # two minutes and this is a machine nobody can walk up to. Last come the
    # three that are genuinely elsewhere.
    #
    # Drop-ins rather than edits: these units belong to their packages and are
    # replaced on upgrade. Written only for units that exist, because every one
    # of them is optional. A weight is not a cap — none of them is slowed at
    # all unless it is competing with the buttons, the music and the photos.
    # `resilient` (4th arg, default 0): also widen the startup timeout and
    # retry on failure. A low CPUWeight does not just slow a service down
    # once it is running — it can starve its own startup check. Measured in
    # the field: on a boot busy with cloud-init, Zigbee2MQTT and the rest,
    # userspace alone took just under 5 minutes on a Pi 1, and smbd's own
    # `ExecCondition` (parsing smb.conf) blew through systemd's default 90 s
    # timeout in the middle of it. Debian's smbd.service sets no `Restart=`
    # at all, so the unit stayed `failed` for the eleven hours until somebody
    # noticed the share was unreachable and restarted it by hand — on a box
    # nobody can walk up to.
    #
    # A flat retry interval would fix that but trades it for the exact fault
    # CLAUDE.md already records for Zigbee2MQTT: 318 consecutive restarts
    # pinning a Pi 1 at load 2 until the hardware watchdog rebooted it twice.
    # A service that is genuinely broken (not just caught in a boot storm),
    # left retrying every 30 s forever, would sit at a steady few percent of
    # the one core for good. `RestartSteps=`/`RestartMaxDelaySec=` back the
    # interval off instead: quick to retry while the boot storm it was built
    # for is still plausible, backing off to a half-hour once it plainly
    # is not one. Needs systemd >=254, which the supported platform has:
    # Raspberry Pi OS Lite 13 (Trixie) ships 257. An older Debian would
    # log "Unknown key name" and ignore both, leaving the flat retry
    # interval this paragraph exists to avoid.
    #
    # Every step still lands outside systemd's own 10 s burst-counting window
    # (`StartLimitIntervalUSec`), so none of this ever trips "start-limit-hit"
    # and leaves the unit failed again regardless.
    write_weight() {
        local unit="$1" weight="$2" why="$3" resilient="${4:-0}"
        systemctl list-unit-files "$unit" >/dev/null 2>&1 || return 0
        sudo mkdir -p "/etc/systemd/system/$unit.d"
        {
            printf '%s\n' \
                "# Written by maman-tv-lite: see scripts/install.sh." \
                "# $why" \
                "[Service]" \
                "CPUWeight=$weight"
            if [ "$resilient" -eq 1 ]; then
                printf '%s\n' \
                    "TimeoutStartSec=300" \
                    "Restart=on-failure" \
                    "RestartSec=30" \
                    "RestartSteps=5" \
                    "RestartMaxDelaySec=1800"
            fi
        } | sudo tee "/etc/systemd/system/$unit.d/maman-tv-lite.conf" >/dev/null
        echo "  $unit: CPUWeight=$weight ($why)"
    }

    # The broker carries every press; Zigbee2MQTT's own weight is in its unit.
    write_weight mosquitto.service 10000 "the buttons come before everything"
    # The way in. This box is not one anybody can walk up to.
    write_weight ssh.service 1000 "administration must stay possible"
    sudo mkdir -p /etc/systemd/system/user.slice.d
    printf '%s\n' \
        "# Written by maman-tv-lite: see scripts/install.sh." \
        "# A logged-in administrator must keep a share of the core." \
        "[Slice]" \
        "CPUWeight=1000" \
        | sudo tee /etc/systemd/system/user.slice.d/maman-tv-lite.conf >/dev/null
    # Genuinely elsewhere: a file share, a tunnel, a mesh VPN. The same low
    # weight that gives way to the music and photos is what can leave one of
    # these starved past its own startup timeout on a busy boot — see
    # write_weight's own comment above.
    for background in smbd nmbd samba-dcerpcd cloudflared tailscaled; do
        write_weight "$background.service" 20 "gives way to the music and photos" 1
    done

    # Units this version replaced. Left behind they would keep running the
    # old code — the per-second screen loop, and a unit that forced the HDMI
    # connector off — beside the new ones.
    for gone in maman-netstatus.service maman-hdmi-hidden.service; do
        if [ -f "/etc/systemd/system/$gone" ]; then
            echo "  $gone belongs to an older version - removing it."
            sudo systemctl disable --now "$gone" >/dev/null 2>&1 || true
            sudo rm -f "/etc/systemd/system/$gone"
        fi
    done

    sudo systemctl daemon-reload

    for unit in "$SCRIPT_DIR"/systemd/*.service; do
        name="$(basename "$unit")"
        unit_wanted "$name" || continue
        # cloudflared cannot start until the tunnel has been created by hand
        # (a Cloudflare account and a domain are required), so enabling it here
        # would only produce a restart loop. It is installed but left inactive.
        if [ "$name" = "cloudflared.service" ]; then
            echo "  $name installed but NOT enabled (see docs/cloudflare-tunnel.md)."
            continue
        fi
        # Not a service at all: the API starts it to hide the box's HDMI signal
        # while the television goes off, and stops it to bring the signal back.
        # Restarted here, it would cut the picture — and "enable" means nothing
        # to a unit that has no [Install] section. Installed, never touched.
        if [ "$name" = "maman-hdmi-blank.service" ] \
           || [ "$name" = "maman-hdmi-disconnect.service" ]; then
            echo "  $name installed (started by the API to stop driving the screen)."
            continue
        fi
        # A diagnostic, and the whole point is that it is not running: it writes
        # to the card and listens on the bus for as long as it is up. Enabling it
        # here would make every box pay for a measurement nobody asked for.
        if [ "$name" = "maman-cec-monitor.service" ]; then
            echo "  $name installed but NOT enabled (sudo systemctl start maman-cec-monitor)."
            continue
        fi
        # zigbee2mqtt used to be enabled only when a serial adapter was
        # already present at install time — crash-looping otherwise, measured
        # at ~50s of CPU per attempt and 318 consecutive attempts once
        # pinning a Pi 1 at load 2 until the hardware watchdog rebooted it
        # twice. The unit's own ConditionPathExistsGlob= now makes that
        # impossible regardless of when the adapter shows up: with none
        # plugged in, systemd never even attempts to start it — "inactive
        # (condition failed)", not a failure, nothing retried. So it is
        # enabled unconditionally here, the same as everything else, and the
        # installation screen's own bootstrap (api/button_pairing.py) can
        # safely restart it once it finds an adapter, on this boot or a
        # later one, without anyone needing to run setup-zigbee.py by hand.
        # enable, then restart rather than `enable --now`: on a service that
        # is already running, --now does nothing at all. The new code would be
        # copied into place and the old one would keep serving until somebody
        # rebooted, which quietly breaks the whole update story of "pull, then
        # run this script again".
        sudo systemctl enable "$name" >/dev/null 2>&1
        sudo systemctl restart "$name"
    done
    echo "Services enabled and started (startup may take 20-30s on a slow CPU)."
else
    echo "No systemd unit files in this repository - step skipped."
fi

# --- Final cleanup ----------------------------------------------------------
log "Cleanup"
sudo apt-get autoremove -y
sudo apt-get clean
rm -rf ~/.cache/node-gyp ~/.cache/pip ~/.cache/node

# --- Summary ----------------------------------------------------------------
log "Done"
df -h /
echo
echo "Answers saved to $CONF_FILE (re-run with --reconfigure to change)."
echo "Full log of this run: $INSTALL_LOG"
echo
echo "Remaining manual steps. None can be automated: they need an account, a"
echo "domain, or a piece of hardware."
echo
NEXT=1
if enabled MAMAN_ZIGBEE; then
    echo "  Zigbee - nothing manual needed. Plug the adapter in and put batteries"
    echo "  in the two buttons (any time, before or after this reboot): the"
    echo "  installation screen finds the adapter and pairs them on its own the"
    echo "  next time the box starts with no button bound yet, which it always"
    echo "  does on a fresh install. scripts/setup-zigbee.py is still there for"
    echo "  anything the screen does not cover (a third button, say)."
    echo
fi
if enabled MAMAN_TAILSCALE; then
    echo "  $NEXT. Tailscale - remote SSH access:"
    echo "       sudo tailscale up --ssh --accept-dns=false"
    echo "     Then, in the Tailscale console, DISABLE key expiry for this"
    echo "     machine, otherwise access will vanish in a few months."
    echo
    NEXT=$((NEXT + 1))
fi
if enabled MAMAN_CLOUDFLARE; then
    echo "  $NEXT. Cloudflare Tunnel (exposes the API): see docs/cloudflare-tunnel.md"
    echo
    NEXT=$((NEXT + 1))
fi
if enabled MAMAN_ZIGBEE; then
    echo "  $NEXT. The television - let the box work it out on the television itself:"
    echo "       curl -u USER:PASS -X POST http://localhost:8000/mode/installation"
    echo "     It asks questions on the screen and you answer with the two"
    echo "     buttons. About a quarter of an hour, and it measures three things"
    echo "     on your actual set: how to wake it, how to switch it off, and how"
    echo "     to give the viewer their programmes back. A box with no button"
    echo "     paired opens it by itself at startup."
    echo "     Until then a fresh box uses the two most standard CEC frames:"
    echo "     Image View On to wake it, plain Standby to switch it off. To set a"
    echo "     technique by hand instead:"
    echo "       curl -u USER:PASS -X PUT \"http://localhost:8000/tv/config/wake?technique=text_view_on\""
    echo "     The names it accepts are listed by GET /tv/config."
else
    echo "  $NEXT. The television - probably nothing to do. Without the buttons the"
    echo "     box uses the two most standard CEC frames, Image View On to wake the"
    echo "     set and plain Standby to switch it off, which most televisions obey."
    echo "     If yours does not, docs/without-buttons.md shows how to try the"
    echo "     other techniques from the browser, at /docs."
fi
echo
NEXT=$((NEXT + 1))
echo "  $NEXT. The screen - the box does not draw on the television unless it is"
echo "     asked to, so its HDMI input shows nothing at rest. That is deliberate:"
echo "     it is what stops a set from coming back on the box instead of on its"
echo "     programmes. To see the diagnostic page:"
echo "       maman-tv screen        (or POST /mode/diagnostic)"
echo "     It ends by itself after five minutes and switches the TV off."
echo "     maman-tv status and maman-tv report answer from a shell."
echo
NEXT=$((NEXT + 1))
echo "  $NEXT. Reboot: hdmi_ignore_cec_init, camera_auto_detect and the hardware"
echo "     watchdog only take effect after a restart: sudo reboot"
echo
# The address comes from the machine, never from this repository: the
# hostname is whatever was chosen in Raspberry Pi Imager.
BOX_NAME="$(hostname | tr '[:upper:]' '[:lower:]')"
echo "Once it has restarted, from a phone or a computer on the same network:"
echo "  http://$BOX_NAME.local:8000         the remote control"
echo "  http://$BOX_NAME.local:8000/docs    the whole API"
