#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens
#
# Prepare photos on a Mac for the television, before copying them to the box.
#
# Three things are done here rather than on the Pi, and each has a reason:
#
#   - EVERYTHING BECOMES A BASELINE JPEG. An iPhone writes HEIC, which the
#     box cannot decode at all — its FFmpeg is too old, and the photos would
#     simply never appear. A library of holiday photos is a mixture, so the
#     conversion happens here for every format macOS can read. Baseline rather
#     than progressive, because a progressive JPEG is slower to decode and the
#     board has no time to spare.
#
#   - THE ROTATION IS BAKED IN. A camera writes the picture sideways and adds
#     an EXIF tag saying "turn this". The box draws with `fbi`, which does not
#     read that tag — the photo would appear on its side. Nothing on the box
#     rotates it either: that would mean a JPEG library and a decode of every
#     photo before it can be shown, on a board that has neither the CPU nor the
#     package. So the tag is applied here and removed.
#
#   - THE PHOTO IS SHRUNK TO THE SCREEN. A photo out of a camera is 12
#     megapixels; the television is two. Decoding the extra pixels is work the
#     box does for nothing, every time the photo comes round.
#
#   - THE COLOURS ARE CONVERTED TO sRGB. A modern iPhone writes Display P3,
#     which is a wider gamut than an ordinary television can show. The box
#     draws the pixels as they are, with no colour management at all, so a P3
#     photo comes out over-saturated. Converting here is the only place it can
#     happen. Deleting the profile without converting — which an earlier
#     version of this script did — is the worst of both: the numbers stay P3
#     and nothing is left to say so.
#
#   - THE METADATA GOES. Nothing on the television reads it, and a photo
#     library carries places and dates that need not travel. Orientation and
#     the colour profile are the only two EXIF fields that change what the
#     viewer sees, and both are applied above before being dropped.
#
# Reads whatever macOS can read — JPEG, HEIC, PNG, TIFF — and writes JPEG.
#
# Usage:
#   scripts/prepare-photos.sh <photo folder> [HD|FULL-HD|4K] [quality 0-100]
#
# The prepared photos are written to a `resized` folder inside the photo
# folder, created if it is not there. Run it again and it simply does the work
# again; `resized` itself is never read back in.
#
# The screen size is the TELEVISION's, not the photo's:
#
#   HD        1280 x 720    most sets that are not sold as Full HD
#   FULL-HD   1920 x 1080   the default
#   4K        3840 x 2160
#
# Nothing is ever stretched: a photo is made to FIT inside that rectangle,
# keeping its shape, and is never enlarged beyond its own size.
#
# Then copy the `resized` folder to the box's share, as `pictures`.

set -euo pipefail

SOURCE="${1:-}"
SCREEN="${2:-FULL-HD}"
# 82 is where a photo stops getting visibly better at the far end of a room,
# and the file is a third of the size — which the box reads off an SD card.
QUALITY="${3:-82}"

if [ -z "$SOURCE" ]; then
    sed -n '6,45p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi
[ -d "$SOURCE" ] || { echo "no such folder: $SOURCE" >&2; exit 1; }

case "$(printf '%s' "$SCREEN" | tr '[:lower:]' '[:upper:]' | tr -d ' _-')" in
    HD)               MAX_WIDTH=1280; MAX_HEIGHT=720  ;;
    FULLHD|FHD|1080P) MAX_WIDTH=1920; MAX_HEIGHT=1080 ;;
    4K|UHD|2160P)     MAX_WIDTH=3840; MAX_HEIGHT=2160 ;;
    *) echo "unknown screen size: $SCREEN (use HD, FULL-HD or 4K)" >&2; exit 2 ;;
esac

TARGET="$SOURCE/resized"

# Shipped with macOS. Without it the conversion is skipped and the photos keep
# whatever gamut they came in, which is only wrong for the wide-gamut ones.
SRGB_PROFILE="/System/Library/ColorSync/Profiles/sRGB Profile.icc"
[ -f "$SRGB_PROFILE" ] || SRGB_PROFILE=""
command -v sips >/dev/null || { echo "sips is missing: this script needs macOS." >&2; exit 1; }

mkdir -p "$TARGET"

# The EXIF orientation, read from the file's own header. `sips -g orientation`
# reports nothing useful on these files, so the tag is read directly. Only the
# four rotations are handled; the mirrored ones do not come out of a camera
# held in a human hand.
orientation_of() {
    python3 - "$1" <<'PYTHON'
import struct, sys
data = open(sys.argv[1], "rb").read(200000)
i = 2
while i < len(data) - 4:
    if data[i] != 0xFF:
        i += 1
        continue
    marker = data[i + 1]
    if marker == 0xE1 and data[i + 4:i + 8] == b"Exif":
        tiff = i + 10
        endian = "<" if data[tiff:tiff + 2] == b"II" else ">"
        offset = struct.unpack(endian + "I", data[tiff + 4:tiff + 8])[0]
        count = struct.unpack(endian + "H", data[tiff + offset:tiff + offset + 2])[0]
        for k in range(count):
            entry = tiff + offset + 2 + k * 12
            if struct.unpack(endian + "H", data[entry:entry + 2])[0] == 0x0112:
                print(struct.unpack(endian + "H", data[entry + 8:entry + 10])[0])
                raise SystemExit
        break
    if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
        i += 2
        continue
    if marker == 0xDA:
        break
    i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
print(1)
PYTHON
}

# Rewrites the JPEG without its Exif block. Pure Python so the script needs
# nothing installed: it walks the markers and copies everything but APP1.
strip_metadata() {
    python3 - "$1" <<'PYTHON'
import sys

path = sys.argv[1]
data = open(path, "rb").read()
if data[:2] != b"\xff\xd8":
    raise SystemExit                      # not a JPEG; leave it alone
out = bytearray(data[:2])
i = 2
while i < len(data) - 1:
    if data[i] != 0xFF:
        out += data[i:]
        break
    marker = data[i + 1]
    if marker == 0xDA:                    # start of scan: the rest is pixels
        out += data[i:]
        break
    if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
        out += data[i:i + 2]
        i += 2
        continue
    length = int.from_bytes(data[i + 2:i + 4], "big")
    if marker not in (0xE1, 0xED, 0xEE):  # Exif, Photoshop, Adobe
        out += data[i:i + 2 + length]
    i += 2 + length
open(path, "wb").write(bytes(out))
PYTHON
}

prepared=0
skipped=0
while IFS= read -r photo; do
    name="$(basename "$photo")"
    case "$name" in ._*) continue ;; esac          # the Mac's own companion files
    out="$TARGET/${name%.*}.jpg"
    if [ -e "$out" ]; then
        # Two files of the same name in different formats: keep both rather
        # than quietly lose one.
        out="$TARGET/${name%.*}-${name##*.}.jpg"
    fi

    # To JPEG first, whatever came in. sips carries a HEIC's rotation into the
    # JPEG's Exif, so the orientation below is read the same way for every
    # format — there is only one kind of file from here on.
    if ! sips -s format jpeg -s formatOptions "$QUALITY" "$photo" --out "$out" \
         >/dev/null 2>&1; then
        echo "  SKIPPED (macOS cannot read it): $name" >&2
        skipped=$((skipped + 1))
        continue
    fi
    turn="$(orientation_of "$out")"

    case "$turn" in
        3) sips --rotate 180 "$out" >/dev/null ;;
        6) sips --rotate 90  "$out" >/dev/null ;;   # camera held clockwise
        8) sips --rotate 270 "$out" >/dev/null ;;
        *) : ;;
    esac

    # FIT inside the screen, never stretch and never enlarge. Done in two
    # passes because sips has no "fit in a box" of its own: cap the longest
    # side first, then the height if the shape is still too tall. Each pass
    # keeps the aspect ratio, so the photo is only ever made smaller.
    read -r width height <<<"$(sips -g pixelWidth -g pixelHeight "$out" \
        | awk '/pixelWidth/{w=$2} /pixelHeight/{h=$2} END{print w, h}')"
    if [ "$width" -gt "$MAX_WIDTH" ] || [ "$height" -gt "$MAX_HEIGHT" ]; then
        sips --resampleHeightWidthMax "$MAX_WIDTH" "$out" >/dev/null
        read -r width height <<<"$(sips -g pixelWidth -g pixelHeight "$out" \
            | awk '/pixelWidth/{w=$2} /pixelHeight/{h=$2} END{print w, h}')"
        if [ "$height" -gt "$MAX_HEIGHT" ]; then
            sips --resampleHeight "$MAX_HEIGHT" "$out" >/dev/null
        fi
    fi

    # Into sRGB, if there is a profile to convert from. --matchTo converts the
    # pixels; deleting the profile would only throw away the label.
    if [ -n "$SRGB_PROFILE" ]; then
        sips --matchTo "$SRGB_PROFILE" "$out" >/dev/null 2>&1 || true
    fi

    # Re-encoded once more at the chosen quality, because the rotation and the
    # resize above make sips write the file again at its own default.
    sips -s format jpeg -s formatOptions "$QUALITY" "$out" --out "$out" \
        >/dev/null 2>&1 || true

    # Everything the television will never read — and the rotation tag above
    # all. sips turns the pixels but LEAVES THE TAG, so a photo prepared here
    # and opened by anything that honours EXIF would be turned a second time.
    # fbi ignores the tag, so the fault would have stayed invisible on the
    # television and shown up everywhere else. The Exif block goes entirely,
    # which takes the places and dates with it.
    strip_metadata "$out"
    prepared=$((prepared + 1))
    printf '  %-44s %s\n' "$name" \
        "$(sips -g pixelWidth -g pixelHeight "$out" \
           | awk '/pixelWidth/{w=$2} /pixelHeight/{h=$2} END{print w"x"h}')"
done < <(find "$SOURCE" -type f \( -iname '*.jpg' -o -iname '*.jpeg' \
             -o -iname '*.heic' -o -iname '*.heif' -o -iname '*.png' \
             -o -iname '*.tif' -o -iname '*.tiff' \) \
         ! -path "$TARGET/*" | sort)

echo
echo "$prepared photos written to $TARGET"
echo "fitted inside $MAX_WIDTH x $MAX_HEIGHT ($SCREEN), upright, sRGB,"
echo "baseline JPEG at quality $QUALITY, without metadata."
[ "$skipped" -gt 0 ] && echo "$skipped could not be read and were skipped."
echo "Copy that folder into the box's share, as 'pictures'."
