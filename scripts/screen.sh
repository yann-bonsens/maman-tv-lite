#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Yann Bonsens
#
# The television's screen, drawn on request.
#
# Consoles belong to root. Every one of them on this board is root:root 0600
# and held by a login prompt, so the API — an ordinary account with
# NoNewPrivileges — cannot draw a photo or a diagnostic page itself. This
# service does it for it.
#
# It does nothing until it is asked. It blocks on a named pipe and wakes only
# when the API writes a line into it:
#
#   diagnostic   draw the state of the box and of the network on tty1
#   photos       start fbi on the list the API has just written
#   page         show the image the API has just written to page.png
#   key <c>      press one key inside the running slideshow
#   off          stop drawing: no photos, no page, an empty console
#
# What it replaced: a loop that came round once a second for the life of the
# machine, repainted every five minutes, watched a state file, counted who was
# allowed to draw and juggled consoles around every power-off. That loop alone
# cost about 2% of this single core, for ever, and most of the display faults
# of September 2026 lived in it. Nothing here runs between two requests.

set -uo pipefail

TTY_PATH="/dev/tty1"
ONCE=""

# Where the API publishes what the box is doing. Read at draw time only.
STATE_FILE="${STATE_FILE:-/run/maman-tv-lite/state.json}"

TTY_PATH="/dev/tty1"


# Standard captive-portal detection endpoint: returns an empty 204 when access
# is genuinely open. Any other answer means something is intercepting.
PROBE_URL="http://connectivitycheck.gstatic.com/generate_204"
PROBE_IP="1.1.1.1"

# Returns the public IP as seen from outside AND proves that HTTPS to
# Cloudflare works — the two facts that matter for the tunnel.
CF_TRACE_URL="https://www.cloudflare.com/cdn-cgi/trace"

# UDP port used by cloudflared for QUIC. When it is blocked the tunnel falls
# back to TCP 443 (the `protocol: http2` option in config.yml).
CF_QUIC_HOST="region1.v2.argotunnel.com"
CF_QUIC_PORT=7844

CLOUDFLARED_CONFIG="/etc/cloudflared/config.yml"

# Account shown in the ssh command. The service runs as root, so $USER cannot
# be trusted: take the first real user account (uid 1000), which is the main
# account on Raspberry Pi OS.
SSH_LOGIN="${SSH_LOGIN:-$(getent passwd 1000 2>/dev/null | cut -d: -f1)}"
SSH_LOGIN="${SSH_LOGIN:-pi}"

usage() {
  awk '
    /^#!/ || /^# SPDX/ || /^# Copyright/ { next }
    /^#/ { sub(/^# ?/, ""); print; next }
    { exit }
  ' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tty-path) TTY_PATH="$2"; shift 2 ;;
    # Draw one page and exit, for somebody at an ssh prompt who wants to see
    # what the television would show.
    --once) ONCE="${2:-diagnostic}"; shift 2 ;;
    # Prints the header block: every comment line from the first blank
    # comment after the SPDX lines up to the first line that is not a comment.
    # Derived rather than a fixed line range, which silently printed the wrong
    # thing the last two times the header changed length.
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# Short state of a service. `systemctl is-active` exits non-zero as soon as the
# service is not active, so `|| echo` cannot be used — it would concatenate
# both texts. Test the output, not the exit code.
svc_state() {
  local state
  state=$(systemctl is-active "$1" 2>/dev/null)
  case "$state" in
    active) echo "OK" ;;
    "")     echo "?" ;;
    *)      echo "$state" ;;
  esac
}

bad()  { printf '  [FAIL] %s\n' "$1"; }

# Interface carrying the default route, otherwise the first interface that is
# up.
primary_iface() {
  local iface
  iface=$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')
  [[ -n "$iface" ]] && { echo "$iface"; return; }
  ip -o link show up 2>/dev/null \
    | awk -F': ' '$2 != "lo" {print $2; exit}'
}

# Public hostname declared in the tunnel configuration. /etc/cloudflared is
# mode 700: readable by the service (root), not by a normal user.
tunnel_hostname() {
  [[ -r "$CLOUDFLARED_CONFIG" ]] || return 1
  awk '/^[[:space:]]*-?[[:space:]]*hostname:[[:space:]]*/ {print $NF; exit}' \
    "$CLOUDFLARED_CONFIG"
}

render() {
  local iface ip ip6 ip6_global gw dns carrier http_code redirect mac speed addr_src clock
  local cf_trace public_ip quic_state tunnel_host tunnel_state net_state
  local ts_ip ts_state
  iface=$(primary_iface)


  # Deliberately kept under ~22 lines: in a 32x16 font on a 1366x768 TV the
  # console is only 24 lines tall, and any overflow would scroll the screen,
  # hiding exactly the IP address we came for.
  echo "================================================"
  printf '  MAMAN TV LITE   %s\n' "$(date '+%H:%M')"
  echo "================================================"

  # No early return other than this one. Even without IPv4 the MAC address and
  # the IPv6 address MUST be shown: they are needed precisely when nothing
  # works (whitelist registration, IPv6 fallback). An earlier version returned
  # as soon as DHCP failed and hid that information.
  if [[ -z "$iface" ]]; then
    bad "No network interface at all"
    echo "  -> Check the Ethernet cable and the wall socket."
    echo "================================================"
    return
  fi

  # --- 1. Physical identity of the interface ------------------------------
  # The MAC address first: on managed networks access is often filtered by MAC
  # address, and without a keyboard this is the only way to read it out to the
  # people running the network.
  mac=$(cat "/sys/class/net/$iface/address" 2>/dev/null)
  carrier=$(cat "/sys/class/net/$iface/carrier" 2>/dev/null || echo 0)
  speed=$(cat "/sys/class/net/$iface/speed" 2>/dev/null)
  if [[ -n "$speed" ]] && [[ "$speed" =~ ^[0-9]+$ ]] && (( speed > 0 )); then
    speed="${speed}Mb/s"
  else
    speed="?"
  fi
  if [[ "$carrier" == "1" ]]; then
    printf '  %-5s  %-6s link UP    %s\n' "iface" "$iface" "$speed"
  else
    printf '  %-5s  %-6s link DOWN  <- cable / wall socket\n' "iface" "$iface"
  fi
  printf '  %-5s  %s\n' "MAC" "${mac:-unknown}"

  # --- 2. Addresses -------------------------------------------------------
  # The CIDR prefix is kept: a client on a different subnet explains a
  # connection failure that would otherwise make no sense.
  ip=$(ip -4 -o addr show "$iface" 2>/dev/null | awk '{print $4; exit}')
  if ip -4 -o addr show "$iface" 2>/dev/null | grep -q dynamic; then
    addr_src="dhcp"
  else
    addr_src="static"
  fi
  if [[ -n "$ip" ]]; then
    printf '  %-5s  %s (%s)\n' "IPv4" "$ip" "$addr_src"
  else
    printf '  %-5s  NONE - no DHCP answer\n' "IPv4"
  fi

  # Global and link-local are told apart: only a global address is usable as
  # is from another machine. A link-local one would require naming the client
  # interface (fe80::...%en0), which cannot be guessed from here.
  ip6_global=$(ip -6 -o addr show "$iface" scope global 2>/dev/null | awk '{print $4; exit}')
  if [[ -n "$ip6_global" ]]; then
    printf '  %-5s  %s\n' "IPv6" "$ip6_global"
  else
    ip6=$(ip -6 -o addr show "$iface" scope link 2>/dev/null | awk '{print $4; exit}')
    if [[ -n "$ip6" ]]; then
      printf '  %-5s  %s (link-local only)\n' "IPv6" "$ip6"
    else
      printf '  %-5s  none\n' "IPv6"
    fi
  fi

  # --- 3. Gateway ---------------------------------------------------------
  gw=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')
  if [[ -n "$gw" ]] && ping -c1 -W2 "$gw" >/dev/null 2>&1; then
    printf '  %-5s  %s (reachable)\n' "GW" "$gw"
  elif [[ -n "$gw" ]]; then
    printf '  %-5s  %s (no ping reply)\n' "GW" "$gw"
  else
    printf '  %-5s  NONE - no default route\n' "GW"
  fi

  # --- 4. DNS -------------------------------------------------------------
  # A successful lookup proves a complete UDP 53 round trip: the only really
  # bidirectional UDP test available without a third-party tool.
  dns=$(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf 2>/dev/null)
  if getent hosts connectivitycheck.gstatic.com >/dev/null 2>&1; then
    printf '  %-5s  %s (resolves)\n' "DNS" "${dns:-none}"
  else
    printf '  %-5s  %s (RESOLUTION FAILS)\n' "DNS" "${dns:-none}"
  fi

  # --- 5. Clock and raw reachability --------------------------------------
  # A Pi has no battery-backed clock: after booting on a network that blocks
  # NTP the time is wrong and EVERY TLS validation fails (the Cloudflare tunnel
  # included), with very confusing symptoms.
  if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]]; then
    clock="synced"
  else
    clock="NOT SYNCED - TLS will fail"
  fi
  if ping -c1 -W3 "$PROBE_IP" >/dev/null 2>&1; then
    net_state="ping OK"
  else
    net_state="no ping"
  fi
  printf '  %-5s  %-12s  %-5s %s\n' "Clock" "$clock" "Net" "$net_state"

  # --- 6. Captive portal --------------------------------------------------
  http_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$PROBE_URL" 2>/dev/null || echo 000)
  case "$http_code" in
    204) echo "  >>> OPEN ACCESS - no captive portal <<<" ;;
    000) echo "  >>> NO INTERNET ACCESS <<<" ;;
    *)
      redirect=$(curl -s -o /dev/null -w '%{redirect_url}' --max-time 8 "$PROBE_URL" 2>/dev/null)
      echo "  >>> CAPTIVE PORTAL (HTTP $http_code) <<<"
      [[ -n "$redirect" ]] && echo "  ${redirect:0:46}"
      ;;
  esac

  # --- 7. Cloudflare: public IP + outbound HTTPS --------------------------
  # A single call gives both: that Cloudflare is reachable over HTTPS, and the
  # public address as seen from outside.
  cf_trace=$(curl -s --max-time 8 "$CF_TRACE_URL" 2>/dev/null)
  public_ip=$(printf '%s' "$cf_trace" | awk -F= '/^ip=/{print $2; exit}')
  if [[ -n "$public_ip" ]]; then
    printf '  Cloudflare HTTPS : OK    Public IP : %s\n' "$public_ip"
  else
    echo "  Cloudflare HTTPS : UNREACHABLE"
  fi

  # --- 8. Outbound UDP (QUIC) ---------------------------------------------
  # Two sources, of very unequal value:
  #
  # 1. The protocol cloudflared actually negotiated, read from the journal.
  #    This is the ONLY reliable proof: "quic" means UDP 7844 gets through,
  #    "http2" means it is blocked and cloudflared fell back to TCP 443.
  # 2. Failing that (tunnel not configured yet), an `nc -zu` probe. Verified in
  #    testing: it reports "succeeded" even against a completely unreachable
  #    address, because UDP has no handshake and only an explicit ICMP reject
  #    makes it fail. Managed-network firewalls usually drop packets in
  #    silence, so the result is inconclusive — and the label says so.
  quic_state=$(journalctl -u cloudflared -n 300 --no-pager 2>/dev/null \
    | grep -oE 'protocol=[a-z0-9]+' | tail -1 | cut -d= -f2)
  case "$quic_state" in
    quic)  quic_state="quic in use (UDP 7844 open)" ;;
    http2) quic_state="http2 fallback (UDP blocked)" ;;
    *)
      if timeout 6 nc -zu -w 3 "$CF_QUIC_HOST" "$CF_QUIC_PORT" >/dev/null 2>&1; then
        quic_state="no ICMP reject (inconclusive)"
      else
        quic_state="REJECTED -> set protocol: http2"
      fi
      ;;
  esac
  printf '  UDP %s (QUIC)  : %s\n' "$CF_QUIC_PORT" "$quic_state"

  # --- 9. Services and Cloudflare tunnel ----------------------------------
  tunnel_state=$(svc_state cloudflared)
  tunnel_host=$(tunnel_hostname 2>/dev/null)
  if [[ -z "$tunnel_host" ]]; then
    # /etc/cloudflared is mode 700: outside the systemd service (root), a
    # normal user cannot read the configuration, hence the distinction.
    if [[ -e "$CLOUDFLARED_CONFIG" ]]; then
      tunnel_host="(unreadable - run as root)"
    else
      tunnel_host="(not configured)"
    fi
  fi
  # Tailscale: a second way in, independent of Cloudflare. An address in
  # 100.x.y.z means the Pi can be reached from anywhere, even when the local
  # network misbehaves or the tunnel is down.
  if command -v tailscale >/dev/null 2>&1; then
    ts_ip=$(tailscale ip -4 2>/dev/null | head -1)
    if [[ -n "$ts_ip" ]]; then
      ts_state="OK"
    else
      ts_state="logged out"
    fi
  else
    ts_ip=""
    ts_state="absent"
  fi

  # Two lines rather than one: on a single line, long states ("activating",
  # "logged out") ran past the 85 columns of the console and wrapped the
  # display. Separating local services from remote access also reads better
  # when looking for a way in.
  # mdns belongs here because `ssh <name>.local` depends entirely on it: when
  # avahi is stopped, that command fails for no apparent reason.
  printf '  Services   ssh %s   api %s   mdns %s\n' \
    "$(svc_state ssh)" "$(svc_state maman-api)" "$(svc_state avahi-daemon)"
  printf '  Remote     tunnel %s   tailscale %s\n' "$tunnel_state" "$ts_state"
  printf '  Public URL %s\n' "$tunnel_host"

  # The techniques in force used to be printed here from the state file this
  # script read on its own. They come from box_state_lines now, with the mode
  # and the output beside them — one source, drawn by whoever is drawing.

  # --- 11. How to connect -------------------------------------------------
  # Two paths on purpose: if SSH fails (missing key, filtered port), the API
  # is still reachable from a browser.
  echo "------------------------------------------------"
  # The two scopes are stated explicitly. mDNS (.local) is link-local by
  # design (multicast TTL=1) and a LAN address does not cross a router either:
  # verified from a phone hotspot, both fail while the Tailscale address still
  # answers. Without this note it looks like a breakdown, when it is normal
  # behaviour — and it is the likely case on site when the client sits on a
  # guest WiFi and the Pi on the wired network.
  if [[ -n "$ts_ip" ]]; then
    printf '  ANYWHERE  ssh %s@%s\n' "$SSH_LOGIN" "$ts_ip"
  fi
  if [[ -n "$ip" ]]; then
    printf '  SAME LAN  ssh %s@%s\n' "$SSH_LOGIN" "${ip%%/*}"
    printf '            http://%s:8000/docs\n' "${ip%%/*}"
  elif [[ -n "$ip6_global" ]]; then
    # Without IPv4, a global IPv6 address remains directly usable.
    printf '  SAME LAN  ssh %s@%s\n' "$SSH_LOGIN" "${ip6_global%%/*}"
  else
    printf '  SAME LAN  no usable IP\n'
  fi
  # Always shown: avahi also publishes IPv6 records, so this name can work
  # even when IPv4 has failed.
  printf '            ssh %s@%s.local\n' "$SSH_LOGIN" \
    "$(hostname | tr '[:upper:]' '[:lower:]')"
  echo "================================================"
}


# ============================================================================
# The slideshow
# ----------------------------------------------------------------------------
# fbi runs it, not the API, and for two separate reasons.
#
# The first is a permission: fbi needs a console, every console on this board
# is root:root 0600 and held by agetty, and no group grants access to it. This
# service is root and already owns that console for the diagnostic screen.
#
# The second was found the hard way. Starting a fresh fbi for each photo makes
# it retake the console every time, and measured here that BLANKS THE SCREEN
# for about eight tenths of a second before it paints — sampled every 200 ms,
# the screen went 60% black, then 100, 100, 100, 86, then back. A fade could
# hide that black but not remove it. Given the whole list, fbi never retakes
# the console: it decodes the next photo while the current one is up and swaps
# them, which is both smoother and lighter. Timed on the real board at exactly
# one photo every fifteen seconds.
#
# What that costs is real and is accepted for now: fbi owns the list, so the
# box cannot say which photo is on screen, and "next", "previous" and "set
# this one aside" do not work in this mode.
#
# The API asks for a slideshow by writing the list of photos it wants into
# SLIDESHOW_REQUEST, and stops it by removing that file.
# ============================================================================
# A single page
# ----------------------------------------------------------------------------
# The installation screen's own images, one at a time. Same idea as the
# slideshow with the list dropped to one entry and no timer: fbi shows the
# image and sits, which is exactly "stays up until the next page or the mode
# ends." Reuses the slideshow's own console (PHOTO_VT) — the two modes are
# mutually exclusive, so there is no collision, and it is one less console to
# manage.
PAGE_PATH="${RUNTIME_DIR:-/run/maman-tv-lite}/page.png"

SLIDESHOW_REQUEST="${RUNTIME_DIR:-/run/maman-tv-lite}/slideshow"
# One character the API wants pressed in the slideshow: j for the next photo,
# k for the previous. The consoles belong to root and the API does not, so it
# asks here — the same way it asks for a slideshow at all.
SLIDESHOW_RUNNING="${RUNTIME_DIR:-/run/maman-tv-lite}/slideshow-running"

# Seconds each photo is looked at.
PHOTO_SECONDS="${PHOTO_SECONDS:-15}"

# ============================================================================
# The slideshow gets a console of its own
# ----------------------------------------------------------------------------
# Not tty1, and the reason is that tty1 already belongs to agetty. Measured:
# fbi started there with setsid ends up in its own session with NO controlling
# terminal, so it is not the foreground process group and never sees a key —
# a key pushed into tty1 goes to the login prompt instead. On a console of its
# own, fbi is `Ss+` with tpgid its own pid, nothing else is attached, and keys
# reach it: `j` moves to the next photo, `k` to the previous.
#
# Eight because logind opens a login on the first NAutoVTs consoles (six by
# default) the moment anything switches to them, and reserves the sixth.
# Above that, nothing competes.
#
# It also keeps the slideshow off the diagnostic screen's console entirely,
# which is where the graphics-mode trap came from.
PHOTO_VT="${PHOTO_VT:-8}"

# fbi forks and the parent exits at once, so the pid a shell records is never
# its own — measured, the shell was handed 19457 while fbi ran as 19459.
# Nothing here holds a pid: "is one running" is asked of the process table.

# fbi puts the console into graphics mode to draw on it, and restores it when
# it is asked to stop — but NOT when it dies any other way. Measured on the
# board: KDGETMODE stayed at 1 long after fbi was gone, and from then on every
# byte written to the console went nowhere at all. The television kept the last
# photo, the diagnostic screen wrote into the void, and nothing ever recovered:
# a silent, permanent failure.
#
# So it is forced back, every time, rather than trusted. python3 because the
# kernel wants an ioctl and there is no command for it; the API already brings
# python, so nothing new is installed.
restore_text_console() {
  python3 -c 'import fcntl, sys
with open(sys.argv[1], "wb") as tty:
    fcntl.ioctl(tty, 0x4B3A, 0)          # KDSETMODE, KD_TEXT
' "$TTY_PATH" 2>/dev/null || true
}

stop_slideshow() {
  pkill -x fbi 2>/dev/null || true
  rm -f "$SLIDESHOW_RUNNING" 2>/dev/null || true
  # fbi leaves the console in graphics mode when it is killed rather than asked
  # to stop. Measured on this board: KDGETMODE stayed at 1 long after fbi was
  # gone, and from then on every byte written to the console went nowhere —
  # silently, and for ever. Forced back, never trusted.
  restore_text_console
  hide_photo_console_text
}

# Hands fbi a keystroke the API asked for.
#
# Pushed into the console fbi is reading, which is why the slideshow has one of
# its own: on tty1 the key would go to agetty's login prompt instead — measured,
# fbi there has no controlling terminal and is never the foreground group.
#
# python3 because this is an ioctl and there is no command for it; the API
# already brings python, so nothing new is installed.
press_key() {
  local key="${1:-}"
  case "$key" in
    next)     key="j" ;;
    previous) key="k" ;;
    "")       return 0 ;;
  esac
  pgrep -x fbi >/dev/null 2>&1 || return 0
  python3 -c 'import fcntl, sys, termios
with open(sys.argv[1], "wb") as tty:
    fcntl.ioctl(tty, termios.TIOCSTI, sys.argv[2].encode())
' "/dev/tty$PHOTO_VT" "${key:0:1}" 2>/dev/null || true
}

# Starts the slideshow the API asked for, and stops it when it asks for none.
# Returns 0 while a slideshow is up, so the caller leaves the console alone.
serve_slideshow() {
  if [[ ! -r "$SLIDESHOW_REQUEST" ]]; then
    echo "screen: asked for photos with no list in $SLIDESHOW_REQUEST" >&2
    return 1
  fi
  pkill -x fbi 2>/dev/null || true

  # The television is moved to the slideshow's console FIRST, while it is still
  # empty. Starting fbi and switching afterwards left tty1 on screen for the
  # couple of seconds fbi takes to come up — and tty1 carries the login prompt,
  # so what the viewer saw was a flash of console text before the first photo.
  chvt "$PHOTO_VT" 2>/dev/null || true
  hide_photo_console_text

  # -a fits each photo to the screen keeping its shape, -u shuffles, -t is the
  # turn. fbi owns the list and the clock: a fresh fbi per photo retakes the
  # console and blanks the screen for about eight tenths of a second each time.
  # setsid and a closed stdin because fbi refuses to start when it can see a
  # terminal that is not a console — which is every service and every ssh
  # session.
  setsid fbi -T "$PHOTO_VT" -d /dev/fb0 -noverbose -a -u \
      -t "$PHOTO_SECONDS" -l "$SLIDESHOW_REQUEST" \
      </dev/null >/dev/null 2>&1 &
  printf '%s\n' "running" > "$SLIDESHOW_RUNNING" 2>/dev/null || true
  return 0
}

# Shows one image and sits: no -t, no -u, no -l, so fbi never advances on its
# own. Each new "page" request kills the previous fbi (stop_slideshow, shared
# with the photo slideshow — same binary, same cleanup) and starts a fresh one
# on the new file. A page changes every 30 seconds to several minutes, so the
# ~0.8 s black flash a fresh fbi costs on retaking the console (measured for
# the slideshow, see serve_slideshow) is a non-issue at this cadence, unlike
# at 15 s.
# How long to let a dying fbi finish restoring the console before it is taken
# out with SIGKILL. Measured in tenths of a second; fbi normally goes in one
# or two.
FBI_EXIT_TRIES="${FBI_EXIT_TRIES:-20}"

# A pipe of its own, opened read-write on fd 9 and never written to, purely so
# `read -t` can wait a tenth of a second without forking /bin/sleep — that
# fork was measured at 1150 ms of CPU per minute on this board, 2% of the
# single core, which is the whole reason this service stopped polling.
#
# Emphatically NOT the request pipe: reading from that one here would consume
# a request somebody had already sent (an "off" queued while a page was being
# drawn), and it would vanish without trace.
pause_briefly() {
  if [[ -z "${PAUSE_FD_READY:-}" ]]; then
    local pipe
    pipe="$(mktemp -u)"
    mkfifo "$pipe" || return 0
    exec 9<>"$pipe"
    rm -f "$pipe"
    PAUSE_FD_READY=1
  fi
  read -r -t 0.1 -u 9 || true
}

wait_for_fbi_to_go() {
  pgrep -x fbi >/dev/null 2>&1 || return 0
  pkill -x fbi 2>/dev/null || true
  local tries=0
  while pgrep -x fbi >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [[ "$tries" -ge "$FBI_EXIT_TRIES" ]]; then
      echo "screen: fbi would not exit; killing it" >&2
      pkill -KILL -x fbi 2>/dev/null || true
      break
    fi
    pause_briefly
  done
  # Whatever it did or did not restore on its way out, the console is put
  # back into text mode here rather than trusted to have been: the failure is
  # silent and permanent, so it is forced every time.
  restore_text_console
}

serve_page() {
  if [[ ! -r "$PAGE_PATH" ]]; then
    echo "screen: asked for a page with nothing at $PAGE_PATH" >&2
    return 1
  fi
  # Waited for, not merely signalled. pkill returns as soon as the signal is
  # queued, and fbi restores the console to KD_TEXT on its way out — so the
  # old one could hand the console back AFTER the new one had taken it as
  # KD_GRAPHICS, which leaves every byte written to that console going
  # nowhere, silently and for good (see CLAUDE.md, "fbi leaves the console in
  # graphics mode"). Bounded, because a refusal to die must not wedge the
  # screen service: past the deadline it is killed outright and we carry on.
  wait_for_fbi_to_go

  chvt "$PHOTO_VT" 2>/dev/null || true
  hide_photo_console_text

  setsid fbi -T "$PHOTO_VT" -d /dev/fb0 -noverbose -a "$PAGE_PATH" \
      </dev/null >/dev/null 2>&1 &
  return 0
}



# Blanked, not merely left alone: the set wakes onto whatever the console
# shows, and that was a screenful of diagnostics for the seconds before the
# first photo. The API raises the flag before it even wakes the television, so
# the picture comes up black. Done here because /dev/tty1 is root-only and the
# API is not root; this service is.
blank_console() {
  printf '\033[2J\033[H' > "$TTY_PATH" 2>/dev/null || true
}

# fbi writes some of what it says straight to its console, around the
# framebuffer and around its own stdout: "trying fbdev: /dev/fb0" as it starts,
# "Ooops: Terminated" as it is killed. Redirecting the process captures neither
# — measured, running fbi exactly as this service does with its output in a
# file caught only the font line, and the two above still appeared on the
# television.
#
# So the console is made unable to show them: black on black, no cursor. fbi
# paints the framebuffer directly and is unaffected — measured, a photo still
# came up with 210 distinct colours — while its text is written in black onto
# black and the screen reads 100% black before and after it.
#
# TERM is set because setterm resolves the terminal type and a service has no
# TERM at all: without it, "unknown: unknown terminal type" and nothing is
# applied. Re-applied on every start, because fbi resets the console's
# attributes when it exits.
hide_photo_console_text() {
  TERM=linux setterm --foreground black --background black --cursor off \
      --clear all > "/dev/tty$PHOTO_VT" 2>/dev/null || true
}

# Enlarge the console font so the screen stays readable from an armchair.
# Lost once during the rewrite, and nothing noticed until a test started
# checking that every function this script calls is one it defines: the page
# would have been drawn in the default tiny font, with "command not found" on
# the television beside it.
set_big_font() {
  local f
  for f in Uni3-TerminusBold32x16 Lat15-TerminusBold32x16 Lat15-Terminus32x16 \
           Uni3-Terminus32x16 Lat15-TerminusBold28x14 sun12x22; do
    if setfont "/usr/share/consolefonts/$f.psf.gz" 2>/dev/null; then
      return
    fi
  done
}

# ----------------------------------------------------------------------------
# What the box itself is doing
# ----------------------------------------------------------------------------
# Three lines, read from the file the API publishes. Without a network and
# without a keyboard they answer the questions that took a whole evening to
# answer by hand in September 2026: which mode is the box in, is its output
# asleep, which techniques is it configured with and where did those come
# from, and is this the configuration made for the set standing in front of
# you.
box_state_lines() {
  python3 - "$STATE_FILE" <<'STATEPY' 2>/dev/null || echo "  BOX       state unavailable"
import json, sys
try:
    state = json.load(open(sys.argv[1]))
except Exception:
    print("  BOX       state unavailable"); raise SystemExit
config = state.get("configuration", {})
tv = config.get("television") or {}


def technique(kind):
    entry = config.get(kind) or {}
    return "%s(%s)" % (entry.get("technique", "?"), entry.get("source", "?"))


print("  BOX       mode=%-12s output=%s (%s)" % (state.get("mode", "?"), state.get("hdmi", "?"),
                                                 config.get("hdmi_sleep", "?")))
# All three techniques, not two: "back" is the one that decides what the tv
# button does while the music plays, and it was on no page anywhere.
print("  TECHNIQUE on=%s off=%s" % (technique("wake"), technique("sleep")))
print("  TECHNIQUE back=%s" % technique("release"))
identity = " ".join(str(tv.get(k, "")) for k in ("manufacturer", "product", "name")).strip()
print("  TV        %s" % (identity or "unknown (run the installation screen)"))
# Said out loud rather than left to be inferred from the sources above: a
# configuration nobody finished is not the same as one nobody started.
if not config.get("detection_complete"):
    print("  SETUP     never completed - run the installation screen")
STATEPY
}

# ----------------------------------------------------------------------------
# Drawing a page
# ----------------------------------------------------------------------------
draw_page() {
  local title="$1"
  stop_slideshow
  chvt 1 2>/dev/null || true
  restore_text_console
  set_big_font
  {
    printf '\033[2J\033[H'
    if [[ "$title" != "diagnostic" ]]; then
      printf '  MAMAN TV LITE - %s\n\n' "$(echo "$title" | tr '[:lower:]' '[:upper:]')"
    fi
    render
    box_state_lines
  } > "$TTY_PATH" 2>/dev/null
}

stop_drawing() {
  stop_slideshow
  chvt 1 2>/dev/null || true
  restore_text_console
  blank_console
}

# ----------------------------------------------------------------------------
# Waiting to be asked
# ----------------------------------------------------------------------------
FIFO="${RUNTIME_DIR:-/run/maman-tv-lite}/screen"

serve() {
  mkdir -p "$(dirname "$FIFO")"
  [[ -p "$FIFO" ]] || { rm -f "$FIFO"; mkfifo -m 0622 "$FIFO"; }
  # Opened read-write and kept open: a pipe opened read-only reports end of
  # file every time its last writer closes, which would spin this loop at full
  # speed — the very cost this service was rewritten to remove.
  exec 3<>"$FIFO"
  stop_drawing
  while IFS= read -r line <&3; do
    # shellcheck disable=SC2086  # the request is two words at most, ours
    set -- $line
    case "${1:-}" in
      diagnostic) draw_page diagnostic ;;
      photos)     serve_slideshow ;;
      page)       serve_page ;;
      key)        press_key "${2:-}" ;;
      off)        stop_drawing ;;
      "")         ;;
      *)          echo "screen: unknown request ${1}" >&2 ;;
    esac
  done
}

if [[ -n "$ONCE" ]]; then
  draw_page "$ONCE"
else
  serve
fi
