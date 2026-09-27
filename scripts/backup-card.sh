#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens
# Back up the box's SD card to images/, gzipped.
#
# Runs on the Mac, not on the Pi: the card has to be out of the box and in a
# reader. It is the same command that has always been used by hand —
#
#     diskutil unmountDisk /dev/diskN && sudo dd if=/dev/rdiskN bs=4m | gzip -c > ...
#
# — with the three things that are easy to get wrong done for you: picking the
# right disk, naming the file the way the others in images/ are named, and not
# leaving a half-written image behind under a name that looks finished.
#
# **This only ever reads the card.** Restoring is the dangerous direction and
# is deliberately not here; see images/images.md for that command.
#
# A card image carries every secret on the box — the API password, the SSH
# keys, the WiFi key, the Zigbee network key — which is why images/ is
# gitignored and why these files should not leave the Mac.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGES_DIR="$REPO_DIR/images"
NAME_PREFIX="maman-tv-lite"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--disk diskN] [--suffix WORD] [--yes]

  --disk diskN    the card reader's disk, e.g. disk4. Found automatically when
                  exactly one external physical disk is plugged in.
  --suffix WORD   added to the file name, e.g. "stable" ->
                  ${NAME_PREFIX}-$(date +%Y%m%d)-stable.img.gz
  --yes           do not ask for confirmation. Everything else is checked
                  first; this only skips the last question.

Writes to $IMAGES_DIR/
EOF
}

DISK=""
SUFFIX=""
ASSUME_YES=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --disk)   DISK="$2"; shift 2 ;;
        --suffix) SUFFIX="$2"; shift 2 ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: this uses diskutil, so it only runs on a Mac." >&2
    exit 1
fi

# --- Which disk -------------------------------------------------------------
# **Removable, not external.** A Mac's built-in SD slot reports the card as
# "Device Location: Internal" — measured: `diskutil list external physical`
# returns nothing at all with a card in the reader, and refusing anything
# internal refused the card itself. What actually separates a card from the
# Mac's own disk is "Removable Media: Removable" against "Fixed", and that
# holds for a USB reader just as well.
removable() { diskutil info "$1" 2>/dev/null | grep -q "Removable Media: *Removable"; }

# The whole disk behind / — refused whatever else it looks like. On an APFS
# Mac that is a synthesized container, so its physical store is refused too.
root_disks() {
    local container store
    container="$(diskutil info / 2>/dev/null \
        | awk -F': *' '/Part of Whole/ {print $2}' | tr -d ' ')"
    store="$(diskutil info "$container" 2>/dev/null \
        | awk -F': *' '/Physical Store/ {print $2}' | tr -d ' ' | sed 's/s[0-9]*$//')"
    echo "disk0 $container $store"
}

# "Built In SDXC Reader, Secure Digital, 15.9 GB" — enough for a person to
# recognise the thing before it is read. `paste -sd" / "` was tried and is a
# trap: the delimiter list cycles character by character, so three lines come
# back joined by a space and a slash rather than by two slashes.
describe() {
    local info name protocol size
    info="$(diskutil info "$1" 2>/dev/null)"
    name="$(printf '%s' "$info" | awk -F': *' '/Device \/ Media Name/ {print $2; exit}')"
    protocol="$(printf '%s' "$info" | awk -F': *' '/^ *Protocol/ {print $2; exit}')"
    size="$(printf '%s' "$info" | awk -F': *' '/^ *Disk Size/ {print $2; exit}' \
        | sed 's/ (.*//')"
    printf '%s, %s, %s' "${name:-unknown}" "${protocol:-unknown}" "${size:-unknown}"
}

if [[ -z "$DISK" ]]; then
    # Read into an array with a loop, not `mapfile`: macOS ships bash 3.2,
    # which does not have it — the same reason scripts/install.sh spells out
    # its own lowercase() instead of using ${var,,}.
    CANDIDATES=()
    while IFS= read -r candidate; do
        [ -n "$candidate" ] || continue
        removable "$candidate" && CANDIDATES+=("$candidate")
    done < <(diskutil list physical \
        | awk '/^\/dev\/disk/ {gsub("/dev/", "", $1); print $1}')
    if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
        echo "ERROR: no removable disk found. Put the card in the reader." >&2
        echo "       (\`diskutil list\` shows what this Mac can see.)" >&2
        exit 1
    fi
    if [[ ${#CANDIDATES[@]} -gt 1 ]]; then
        echo "Several removable disks are here:" >&2
        for candidate in "${CANDIDATES[@]}"; do
            echo "  $candidate  $(describe "$candidate")" >&2
        done
        echo "Say which one with --disk." >&2
        exit 1
    fi
    DISK="${CANDIDATES[0]}"
fi

if ! diskutil info "$DISK" >/dev/null 2>&1; then
    echo "ERROR: no such disk: $DISK" >&2
    exit 1
fi
for forbidden in $(root_disks); do
    if [[ "$DISK" == "$forbidden" ]]; then
        echo "ERROR: $DISK is this Mac's own disk. Refusing." >&2
        exit 1
    fi
done
if ! removable "$DISK"; then
    echo "ERROR: $DISK is not removable media. Refusing." >&2
    exit 1
fi

DESCRIPTION="$(describe "$DISK")"

# --- Where it goes ----------------------------------------------------------
mkdir -p "$IMAGES_DIR"
BASE="$NAME_PREFIX-$(date +%Y%m%d)${SUFFIX:+-$SUFFIX}"
TARGET="$IMAGES_DIR/$BASE.img.gz"
# Never overwrite an image that already exists: today's second backup is a
# different thing from today's first, and the first may be the good one.
attempt=2
while [[ -e "$TARGET" ]]; do
    TARGET="$IMAGES_DIR/$BASE-$attempt.img.gz"
    attempt=$((attempt + 1))
done

echo
echo "  Card      : /dev/$DISK  ($DESCRIPTION)"
echo "  Writing to: ${TARGET#"$REPO_DIR"/}"
echo
if [[ "$ASSUME_YES" -eq 0 ]]; then
    read -rp "  Back this card up? [y/N] " answer
    case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
        y|yes|o|oui) ;;
        *) echo "  Nothing done."; exit 0 ;;
    esac
fi

# --- Read it ----------------------------------------------------------------
diskutil unmountDisk "/dev/$DISK"

# /dev/rdiskN, not /dev/diskN: the raw device skips the buffer cache and is
# several times faster on a card this size.
#
# Written to a .part file and renamed only on success. A backup that stopped
# halfway must not be sitting in images/ under a name that looks finished —
# that is the file somebody restores from in a hurry, months later.
PARTIAL="$TARGET.part"
STARTED=$SECONDS
echo "  Reading... (Ctrl+T shows progress, Ctrl+C stops)"
if command -v pv >/dev/null 2>&1; then
    SIZE_BYTES="$(diskutil info "$DISK" | awk -F'[()]' '/Disk Size/ {print $2}' | awk '{print $1}')"
    sudo dd if="/dev/r$DISK" bs=4m 2>/dev/null | pv -s "${SIZE_BYTES:-0}" | gzip -c > "$PARTIAL"
else
    sudo dd if="/dev/r$DISK" bs=4m | gzip -c > "$PARTIAL"
fi
mv "$PARTIAL" "$TARGET"

ELAPSED=$((SECONDS - STARTED))
echo
echo "  Done in $((ELAPSED / 60)) min $((ELAPSED % 60)) s: $(du -h "$TARGET" | cut -f1)"
echo "  $TARGET"
echo
echo "  To put it back on a card (this one DESTROYS the card's contents):"
echo "    diskutil unmountDisk /dev/$DISK"
echo "    gunzip -c \"$TARGET\" | sudo dd of=/dev/r$DISK bs=4m"
