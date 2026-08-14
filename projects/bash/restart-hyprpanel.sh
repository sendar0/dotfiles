#!/bin/sh
# Restart the HyprPanel Astal instance.
#
# Usage: restart-hyprpanel.sh [-f|--force]
#   default    graceful quit via `astal -i hyprpanel -q`
#   -f/--force SIGTERM (then SIGKILL) the gjs process — for when the
#              panel hangs and the graceful quit times out

set -u

INSTANCE=hyprpanel
LOG="${XDG_RUNTIME_DIR:-/tmp}/hyprpanel.log"

force=0
case "${1:-}" in
    -f|--force) force=1 ;;
    "") ;;
    *) echo "usage: $(basename "$0") [-f|--force]" >&2; exit 2 ;;
esac

running() {
    astal --list 2>/dev/null | grep -qx "$INSTANCE"
}

if running; then
    if [ "$force" -eq 1 ]; then
        # HyprPanel runs as gjs executing an ags bundle from XDG_RUNTIME_DIR
        pids=$(pgrep -f 'gjs -m .*-ags\.js')
        if [ -n "$pids" ]; then
            kill $pids 2>/dev/null
            sleep 1
            for pid in $pids; do
                kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
            done
        fi
    else
        if ! timeout 5 astal -i "$INSTANCE" -q >/dev/null 2>&1; then
            echo "graceful quit timed out — panel is likely hung, retry with --force" >&2
            exit 1
        fi
    fi

    # wait for the instance registration to clear
    i=0
    while running && [ "$i" -lt 20 ]; do
        sleep 0.25
        i=$((i + 1))
    done
    if running; then
        echo "instance '$INSTANCE' still registered — retry with --force" >&2
        exit 1
    fi
fi

setsid hyprpanel >"$LOG" 2>&1 </dev/null &
sleep 2
if running; then
    echo "hyprpanel restarted (log: $LOG)"
else
    echo "hyprpanel failed to start — see $LOG" >&2
    exit 1
fi
