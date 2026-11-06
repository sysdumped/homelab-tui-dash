#!/bin/sh
# Accende/spegne il monitor sotto cage. Uso: screen.sh on|off
# crontab dell'utente dash:
#   0 1 * * *  /home/dash/homelab-dash/screen.sh off
#   0 7 * * *  /home/dash/homelab-dash/screen.sh on
export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0
OUT=$(wlr-randr | awk 'NR==1{print $1}')
exec wlr-randr --output "$OUT" --"$1"
