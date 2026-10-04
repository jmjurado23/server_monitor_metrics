#!/usr/bin/env bash
# screen.sh on|off - turns the HDMI output on or off (used from cron at night).
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
output="$(wlr-randr | awk '/^[A-Z]/{print $1; exit}')"
case "$1" in
  on)  wlr-randr --output "$output" --on ;;
  off) wlr-randr --output "$output" --off ;;
  *)   echo "usage: $0 on|off" >&2; exit 2 ;;
esac
