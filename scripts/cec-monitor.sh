#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens
# Installed by maman-tv-lite's scripts/install.sh; scripts/uninstall.sh removes what carries this line.
# Record everything on the CEC bus, for a measurement session.
#
# Off by default and enabled by hand:
#
#     sudo systemctl start maman-cec-monitor      # for this session only
#     sudo systemctl enable --now maman-cec-monitor
#     sudo systemctl disable --now maman-cec-monitor
#
# It only ever LISTENS. `cec-ctl --monitor` claims no logical address and
# transmits nothing, which is what makes it safe to run beside the box: verified
# repeatedly on the real machine, with the API driving the television throughout.
# Nothing here may ever gain a transmit option — a second talker on the bus is
# how the box loses its own logical address, and a test forbids it.
#
# Why not simply raise the API's log level instead: that records what the box
# SENT and what the adapter made of it, which is most of the story and costs
# nothing. This records what the television said as well, including frames it
# sends on its own. Use it when that is the question.
set -uo pipefail

# The adapter the API last found a television on, so the recorder listens on
# the same port of a board that has two. /dev/cec0 before the API ever saw one.
DEVICE="${MAMAN_CEC_DEVICE:-$(cat /var/lib/maman-tv-lite/cec-adapter 2>/dev/null || true)}"
DEVICE="${DEVICE:-/dev/cec0}"
# systemd's LogsDirectory= creates this and makes it writable; the fallback is
# for running the script by hand.
DIR="${LOGS_DIRECTORY:-${MAMAN_CEC_LOG_DIR:-/var/log/maman-tv-lite}}"
LOG="$DIR/cec-bus.log"

# 50 MB, plus one kept generation: 100 MB at the very most, on a card with
# 16 GB. The journal's own cap is separate (200 MB) and this file is not part of
# it — a recorder that could fill the card would be worse than no recorder.
CAP_BYTES="${MAMAN_CEC_LOG_CAP_BYTES:-52428800}"
# The size is checked every N lines rather than on a timer. A shell loop that
# forks on a timer is a permanent tax on this board — `sleep 1` alone was
# measured at 1150 ms of CPU per minute — so nothing here happens unless a frame
# actually arrived.
CHECK_EVERY_LINES="${MAMAN_CEC_LOG_CHECK_LINES:-200}"

mkdir -p "$DIR"

# `stdbuf -oL` because cec-ctl writes into a pipe, where stdio block-buffers:
# without it the last few kilobytes before a crash — the interesting ones — are
# never written.
open_log() { exec 3>>"$LOG"; }
open_log

stdbuf -oL cec-ctl -d "$DEVICE" --monitor --wall-clock 2>&1 | {
    seen=0
    while IFS= read -r line; do
        printf '%s\n' "$line" >&3
        seen=$((seen + 1))
        if [ "$seen" -ge "$CHECK_EVERY_LINES" ]; then
            seen=0
            size="$(stat -c%s "$LOG" 2>/dev/null || echo 0)"
            if [ "$size" -ge "$CAP_BYTES" ]; then
                exec 3>&-
                mv -f "$LOG" "$LOG.1"
                open_log
            fi
        fi
    done
}
