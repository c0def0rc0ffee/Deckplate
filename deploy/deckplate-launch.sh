#!/usr/bin/env bash
# <summary>
# Desktop click handler for Deckplate: brings up the daemon if it is not
# already running, then opens the configuration window.
# </summary>
# <remarks>
# Installed to ~/.local/bin by deploy/install-desktop.sh and named on the Exec
# line of deploy/deckplate.desktop. Idempotent: a second click while the
# daemon is up just opens the window again. The daemon is started detached in
# its own session, logs to $XDG_STATE_HOME/deckplate/run.log (~/.local/state
# by default) and keeps running after the window closes. Stop it with:
#     pkill -f 'deckplate run'
# The program is found from $DECKPLATE, then PATH, then ~/.local/bin. No
# terminal is attached, so the question about other deck software cannot be
# asked; it is left alone with a warning in the log, as under systemd.
# </remarks>
set -euo pipefail

port="${DECKPLATE_PORT:-8765}"

# <summary>Show a message where a desktop launch can be seen, and on stderr.</summary>
# <param name="1">The message.</param>
notice() {
    if command -v notify-send >/dev/null; then notify-send "Deckplate" "$1" || true; fi
    echo "deckplate-launch: $1" >&2
}

# <summary>True when something is listening on the daemon's local port.</summary>
# <remarks>A bare socket test, no tools needed. The gui command does the real
# check against the API and says so if the port belongs to something else.</remarks>
listening() {
    (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null
}

if [[ -n "${DECKPLATE:-}" ]]; then
    program="$DECKPLATE"
elif command -v deckplate >/dev/null; then
    program="$(command -v deckplate)"
elif [[ -x "$HOME/.local/bin/deckplate" ]]; then
    program="$HOME/.local/bin/deckplate"
else
    notice "the deckplate program is not installed: run deploy/install-desktop.sh"
    exit 1
fi

if ! listening; then
    state="${XDG_STATE_HOME:-$HOME/.local/state}/deckplate"
    mkdir -p "$state"
    echo "=== $(date '+%d/%m/%Y %H:%M:%S') started from the desktop ===" >> "$state/run.log"
    setsid -f "$program" run --keep-official >> "$state/run.log" 2>&1 < /dev/null
    for _ in $(seq 1 60); do
        listening && break
        sleep 0.25
    done
    if ! listening; then
        notice "the daemon did not start; see $state/run.log"
        exit 1
    fi
fi

exec "$program" gui
