#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens
#
# Remove Maman TV Lite from this machine.
#
# By default it removes the product — its services, its commands, what it
# added to systemd, polkit and sshd, and its code — and keeps what is costly
# or impossible to recreate: the settings (API password, button bindings, the
# television's techniques, the Zigbee network and its paired buttons) and the
# compiled parts (the Python environment, Zigbee2MQTT, Node.js). Running
# scripts/install.sh again then brings the box back as it was, in minutes.
#
# It asks whether to remove everything instead. The media folder is never
# touched, not even then: those are somebody's photographs and songs, and no
# uninstaller gets to decide they are part of the product.
#
# Only files carrying the line below are removed. The unit names are common
# ones another project can use too, and `cloudflared service install`
# writes a cloudflared.service of its own: a file without the line is left
# where it is and named, never guessed about.
#
# Never touched: apt packages, Tailscale (removing it could cut off the very
# session running this), the tunnel credentials in /etc/cloudflared, the edits
# to config.txt and to journald's and systemd's own configuration, and the
# clone this script runs from.
#
# Usage: scripts/uninstall.sh [--purge | --keep] [--yes]

set -euo pipefail

MARKER="# Installed by maman-tv-lite's scripts/install.sh; scripts/uninstall.sh removes what carries this line."

# Every absolute path goes through ROOT, which only the test suite sets: it
# runs this whole script against a directory tree instead of a machine.
ROOT="${MAMAN_UNINSTALL_ROOT:-}"
INSTALL_DIR="$HOME/maman-tv-lite"
CONF_DIR="$ROOT/etc/maman-tv-lite"
STATE_DIR="$ROOT/var/lib/maman-tv-lite"
LOG_DIR="$ROOT/var/log/maman-tv-lite"
UNIT_DIR="$ROOT/etc/systemd/system"
BIN_DIR="$ROOT/usr/local/bin"
INSTALL_LOG="${MAMAN_INSTALL_LOG:-$HOME/maman-tv-lite-install.log}"

PURGE=""          # "", yes or no
ASSUME_YES=0

usage() {
    sed -n '4,27p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --purge        remove everything, settings and compiled parts included
  --keep         keep settings and compiled parts, without asking
  --yes          ask nothing (keeps everything unless --purge is given)
EOF
}

owned() { [ -f "$1" ] && grep -qxF "$MARKER" "$1"; }

ask() {   # ask "question" -> 0 for yes; the default is no
    local reply
    read -rp "$1 [y/N] " reply
    case "$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')" in
        y|yes|o|oui) return 0 ;;
        *) return 1 ;;
    esac
}

# A directory this script may delete outright: under ROOT, absolute, and
# never one of the handful whose loss would be a disaster.
safe_to_delete() {
    case "$1" in
        ""|/|"$ROOT"|"$ROOT/"|"$HOME"|"$HOME/") return 1 ;;
        /*) return 0 ;;
        *) return 1 ;;
    esac
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --purge)      PURGE=yes ;;
            --keep)       PURGE=no ;;
            --yes|-y)     ASSUME_YES=1 ;;
            -h|--help)    usage; exit 0 ;;
            *) echo "Unknown option: $1 (see --help)" >&2; exit 1 ;;
        esac
        shift
    done
    if [ "$(id -u)" -eq 0 ] && [ -z "$ROOT" ]; then
        echo "Run this as the account that installed the box, not as root." >&2
        exit 1
    fi

    # Read before anything is deleted, only to say where the media folder
    # is: the answers file is where it is recorded.
    local media_dir=""
    if [ -f "$CONF_DIR/install.conf" ]; then
        media_dir="$(sed -n 's/^MAMAN_MEDIA_DIR=//p' "$CONF_DIR/install.conf" | tail -1)"
    fi
    [ -n "$media_dir" ] && media_dir="$ROOT$media_dir"

    echo "This removes Maman TV Lite from $(hostname): its services, its commands"
    echo "and its code."
    if [ -z "$PURGE" ]; then
        if [ "$ASSUME_YES" -eq 1 ]; then
            PURGE=no
        else
            echo
            echo "By default the settings (password, buttons, television, Zigbee"
            echo "network) and the compiled parts are kept, so installing again"
            echo "takes minutes and pairs nothing."
            if ask "Remove EVERYTHING instead, 100%, as if it had never been installed?"; then
                PURGE=yes
            else
                PURGE=no
            fi
        fi
    fi

    echo
    if [ "$PURGE" = yes ]; then
        echo "Removing everything: settings, Zigbee network, compiled parts."
    else
        echo "Removing the product; keeping the settings and the compiled parts."
    fi
    if [ "$ASSUME_YES" -eq 0 ]; then
        ask "Proceed?" || { echo "Nothing done."; exit 0; }
    fi
    sudo -v 2>/dev/null || true

    echo
    echo "Services:"
    local unit name found=0
    for unit in "$UNIT_DIR"/*.service; do
        [ -f "$unit" ] || continue
        name="$(basename "$unit")"
        if owned "$unit"; then
            sudo systemctl disable --now "$name" >/dev/null 2>&1 || true
            sudo rm -f "$unit"
            echo "  removed $name"
            found=1
        fi
    done
    [ "$found" -eq 1 ] || echo "  none of ours installed"
    # The same names, without the line: installed by an older version of
    # this project, or by something else entirely.
    for name in maman-api.service maman-screen.service maman-hdmi-blank.service \
                maman-hdmi-disconnect.service maman-cec-monitor.service \
                zigbee2mqtt.service cloudflared.service; do
        if [ -f "$UNIT_DIR/$name" ] && ! owned "$UNIT_DIR/$name"; then
            echo "  left alone: $name has no maman-tv-lite marker (an older install"
            echo "    of this project - run install.sh once, then this again - or"
            echo "    not this project's at all)"
        fi
    done

    local dropin
    for dropin in "$UNIT_DIR"/*.d/maman-tv-lite.conf; do
        [ -f "$dropin" ] || continue
        sudo rm -f "$dropin"
        sudo rmdir "$(dirname "$dropin")" 2>/dev/null || true
        echo "  removed the CPU weight on $(basename "$(dirname "$dropin")" .d)"
    done
    sudo systemctl daemon-reload >/dev/null 2>&1 || true

    echo "System integration:"
    local rule
    for rule in "$ROOT"/etc/polkit-1/rules.d/*-maman-tv-lite-*.rules; do
        [ -f "$rule" ] || continue
        sudo rm -f "$rule"
        echo "  removed $(basename "$rule")"
    done
    local sshd_conf="$ROOT/etc/ssh/sshd_config.d/60-maman-tv-lite.conf"
    if [ -f "$sshd_conf" ]; then
        sudo rm -f "$sshd_conf"
        # Reloaded only once sshd accepts what is left: a refused
        # configuration on a box reached over ssh is a trip to the box.
        if sudo sshd -t >/dev/null 2>&1; then
            sudo systemctl reload ssh >/dev/null 2>&1 || true
        fi
        echo "  removed the sshd hardening (open sessions are not affected)"
    fi
    local command
    for command in maman-tv tv-profile cec-monitor; do
        if owned "$BIN_DIR/$command"; then
            sudo rm -f "$BIN_DIR/$command"
            echo "  removed $command"
        fi
    done
    local smb="$ROOT/etc/samba/smb.conf"
    if owned "$smb"; then
        sudo systemctl disable --now smbd nmbd >/dev/null 2>&1 || true
        echo "  stopped the file share"
        if [ "$PURGE" = yes ]; then
            sudo rm -f "$smb"
            echo "  removed its configuration"
        fi
    fi

    echo "Code:"
    if [ -d "$INSTALL_DIR" ] && safe_to_delete "$INSTALL_DIR"; then
        if [ "$PURGE" = yes ]; then
            rm -rf "$INSTALL_DIR"
            echo "  removed $INSTALL_DIR"
        else
            # venv/ and zigbee2mqtt/ are what make a reinstall fast, and
            # zigbee2mqtt/data is the Zigbee network: losing it means pairing
            # every button again.
            find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
                ! -name venv ! -name zigbee2mqtt -exec rm -rf {} +
            echo "  removed $INSTALL_DIR, except venv/ and zigbee2mqtt/"
        fi
    fi

    if [ "$PURGE" = yes ]; then
        echo "Settings and compiled parts:"
        local dir
        for dir in "$CONF_DIR" "$STATE_DIR" "$LOG_DIR" "$ROOT/opt/nodejs"; do
            if [ -e "$dir" ] && safe_to_delete "$dir"; then
                sudo rm -rf "$dir"
                echo "  removed $dir"
            fi
        done
        local link
        for link in node npm npx corepack; do
            if [ -L "$BIN_DIR/$link" ] && readlink "$BIN_DIR/$link" | grep -q "^/opt/nodejs/"; then
                sudo rm -f "$BIN_DIR/$link"
            fi
        done
        rm -f "$INSTALL_LOG"
    fi

    echo
    echo "Done."
    if [ "$PURGE" = no ]; then
        echo "Kept: $CONF_DIR, $STATE_DIR, $INSTALL_DIR/venv and"
        echo "$INSTALL_DIR/zigbee2mqtt (with the Zigbee network)."
    fi
    if [ -n "$media_dir" ] && [ -d "$media_dir" ]; then
        echo "The media folder is never touched: $media_dir"
    fi
    echo "Not touched: apt packages, Tailscale, /etc/cloudflared, config.txt and"
    echo "the journal and watchdog settings, and this clone. Reinstall with"
    echo "scripts/install.sh."
}

# Everything runs from here, once the whole file has been read: bash reads a
# script as it goes, and this one deletes files.
main "$@"
