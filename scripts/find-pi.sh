#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens
#
# Find the Raspberry Pi from your laptop on installation day.
#
# Context: the laptop is often on a guest WiFi (behind a captive portal) while
# the Pi sits on a wired wall socket. The two are not necessarily on the same
# network, hence the cascade of attempts.
#
# Runs on macOS: it uses dns-sd, ipconfig and the BSD flavour of ping. On Linux
# the equivalents are avahi-browse, ip and `ping -W`.
#
# Usage:
#   ./scripts/find-pi.sh                # look for the Pi on this network
#   ./scripts/find-pi.sh mybox          # try mybox.local first: the hostname
#                                       # chosen in Raspberry Pi Imager
#   ./scripts/find-pi.sh 192.168.1.42   # test an IP read off the TV screen
#
# SSH_USER sets the account shown in the suggested ssh command. Neither the
# hostname nor the account is assumed: both are whatever was chosen in Imager.
#
# The IP shown on the TV by the diagnostic screen remains the most reliable source:
# this script checks that it is reachable, or finds it again when the TV is
# out of reach at that moment.

set -uo pipefail

SSH_USER="${SSH_USER:-<user>}"
TARGET="${1:-}"
HOSTNAME_MDNS=""
# A name rather than an address: look it up over mDNS instead of probing it.
if [[ -n "$TARGET" && ! "$TARGET" =~ ^[0-9.]+$ ]]; then
  HOSTNAME_MDNS="${TARGET%.local}.local"
  TARGET=""
fi

ok()   { printf '  \033[32m[OK]\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31m[KO]\033[0m   %s\n' "$1"; }
info() { printf '  ->     %s\n' "$1"; }

# Test whether the host answers on the SSH port (22) or the API port (8000).
probe_host() {
  local host="$1" found=0
  if nc -z -w 2 "$host" 22 2>/dev/null; then
    ok "$host: SSH port (22) open"
    found=1
  fi
  if nc -z -w 2 "$host" 8000 2>/dev/null; then
    ok "$host: Maman TV Lite API (8000) open"
    found=1
  fi
  return $((1 - found))
}

echo "=== Looking for the Raspberry Pi ==="
echo

# --- Case 1: a specific IP was given --------------------------------------
if [[ -n "$TARGET" ]]; then
  echo "Testing $TARGET:"
  if ping -c1 -t2 "$TARGET" >/dev/null 2>&1; then
    ok "Answers ping"
  else
    bad "No ping reply (may be normal if ICMP is filtered)"
  fi
  if probe_host "$TARGET"; then
    echo
    echo "  Connect with:  ssh $SSH_USER@$TARGET"
    exit 0
  fi
  bad "No reachable service on $TARGET"
  echo
  info "Your computer and the Pi are probably on two separate networks."
  info "See the fallback section at the bottom of this script."
  exit 1
fi

# --- Case 2: mDNS resolution of the name given -----------------------------
if [[ -n "$HOSTNAME_MDNS" ]]; then
  echo "1) mDNS resolution ($HOSTNAME_MDNS):"
  MDNS_IP=$(ping -c1 -t2 "$HOSTNAME_MDNS" 2>/dev/null \
    | sed -n 's/.*(\([0-9.]*\)).*/\1/p' | head -1)
  if [[ -n "$MDNS_IP" ]]; then
    ok "Found: $MDNS_IP"
    probe_host "$MDNS_IP"
    echo
    echo "  Connect with:  ssh $SSH_USER@$HOSTNAME_MDNS"
    exit 0
  fi
  bad "No mDNS answer"
  echo
fi

# --- Case 3: Bonjour SSH advertisements -----------------------------------
echo "2) Bonjour advertisements on the network:"
BONJOUR=$( { dns-sd -B _ssh._tcp local. & sleep 3; kill %1; } 2>/dev/null \
  | awk '/_ssh._tcp/ {print $NF}' | sort -u | grep -iv '^instance$' || true)
if [[ -n "$BONJOUR" ]]; then
  ok "SSH hosts advertised:"
  echo "$BONJOUR" | sed 's/^/         /'
else
  bad "No advertisement (common on a guest WiFi that isolates clients)"
fi
echo

# --- Case 4: scan of the local subnet -------------------------------------
IFACE=$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')
LOCAL_IP=$(ipconfig getifaddr "${IFACE:-en0}" 2>/dev/null)
if [[ -n "$LOCAL_IP" ]]; then
  SUBNET="${LOCAL_IP%.*}"
  # mktemp rather than a name built from the process id: /tmp is shared, and
  # a pre-created file there would be read back as if it were our results.
  SCAN_RESULTS="$(mktemp)"
  echo "3) Scanning ${SUBNET}.0/24 (this machine's network, via $IFACE):"
  for i in $(seq 1 254); do
    ( nc -z -w 1 "${SUBNET}.$i" 22 2>/dev/null && echo "${SUBNET}.$i" ) &
  done | sort -u > "$SCAN_RESULTS"
  wait
  if [[ -s "$SCAN_RESULTS" ]]; then
    ok "Hosts with SSH open:"
    sed 's/^/         /' "$SCAN_RESULTS"
    echo
    info "Test each one:  ./scripts/find-pi.sh <ip>"
  else
    bad "No SSH host found on this machine's network"
  fi
  rm -f "$SCAN_RESULTS"
else
  bad "Cannot determine this machine's network"
fi

cat <<EOF

-----------------------------------------------------------
IF NOTHING IS FOUND

This is the expected outcome when the guest WiFi (captive
portal) is isolated from the wired network: your computer
then cannot reach the Pi, even though the Pi works fine.

Two fallbacks, in order:

1. Read the IP straight off the TV (the Pi's HDMI input),
   then run again:  ./scripts/find-pi.sh <ip>
   If that fails despite a valid IP, the two networks really
   are partitioned.

2. Connect your computer to the Pi directly with an Ethernet
   cable (a USB Ethernet adapter if needed). Both take a
   169.254.x.x address automatically and mDNS works:
       ssh $SSH_USER@<hostname>.local
   This is the only guaranteed path - pack the cable.
-----------------------------------------------------------
EOF
