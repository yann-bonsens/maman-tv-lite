#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens
#
# Send the local git HEAD to the box and, if asked, install it there.
#
# Runs on the Mac. Only what is committed is sent (git archive): uncommitted
# changes and untracked files stay here. The target directory is emptied
# first, so the box never keeps a file that no longer exists in HEAD.
#
# It goes to ~/maman-tv-lite-src, where the README clones the project, and
# replaces that tree outright, .git included. This is a developer's loop, for
# a box being worked on from a Mac; a box that reaches the internet is updated
# with `git pull` and the installer instead, and should not be pushed to.
#
# The first run asks for the box's address (user@host) and saves it in
# .push-to-pi.conf at the repository root (gitignored); later runs reuse it.
# Delete that file to be asked again. There is no default: the hostname and
# the account are whatever was chosen in Raspberry Pi Imager. SSH key access
# must already work.
#
# Once sent, it offers to run the installer on the box with a terminal
# (ssh -t): the questions, the sudo prompt and the output appear here exactly
# as if typed on the Pi.
#
# Usage: scripts/push-to-pi.sh [user@host]

set -euo pipefail

DEST="maman-tv-lite-src"   # relative: the remote user's home directory

cd "$(git rev-parse --show-toplevel)"
CONF=".push-to-pi.conf"

TARGET="${1:-}"
if [ -z "$TARGET" ] && [ -f "$CONF" ]; then
    TARGET="$(cat "$CONF")"
fi

asked=0
if [ -z "$TARGET" ]; then
    read -r -p "Box address, as chosen in Raspberry Pi Imager (user@hostname.local): " TARGET
    [ -n "$TARGET" ] || { echo "No address given." >&2; exit 1; }
    asked=1
fi

# BatchMode: never fall back to a password prompt, fail with the help below.
if ! err="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" true 2>&1)"; then
    cat >&2 <<MSG
Cannot log in to $TARGET. ssh says:

    $err

If it is a typo in the address, run the script again with the right one.
If the key is the problem (Permission denied), install it on the box (once):

    ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519
    ssh-copy-id $TARGET

then run this script again. For a timeout or "could not resolve", check that
the box is on and reachable: scripts/find-pi.sh ${TARGET#*@}
MSG
    exit 1
fi

if [ "$asked" = 1 ]; then
    echo "$TARGET" > "$CONF"
    echo "Saved $TARGET in $CONF"
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Note: uncommitted changes are NOT sent, only $(git rev-parse --short HEAD)."
fi

echo "Sending $(git rev-parse --short HEAD) to $TARGET:~/$DEST ..."

git archive --format=tar HEAD |
    ssh "$TARGET" "rm -rf '$DEST' && mkdir '$DEST' && tar -x -C '$DEST'"

echo "Sent."

read -r -p "Run scripts/install.sh on the box now? [Y/n] " answer
case "$answer" in
    [nN]*) ;;
    *) exec ssh -t "$TARGET" "cd '$DEST' && ./scripts/install.sh" ;;
esac
