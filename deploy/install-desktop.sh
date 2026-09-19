#!/usr/bin/env bash
# <summary>
# Install Deckplate for the logged in user with a clickable desktop and menu
# entry. No sudo: everything goes under ~/.local and the Desktop folder.
# </summary>
# <param name="1">Optional path to the deckplate program to install.</param>
# <remarks>
# Puts the program at ~/.local/bin/deckplate with the click handler
# deckplate-launch.sh beside it, the icon under ~/.local/share/icons, the
# menu entry under ~/.local/share/applications, and a copy of the entry on
# the Desktop marked trusted so Cinnamon launches it without a prompt.
# Idempotent: re-run after every release and the installed program is
# replaced. The program comes from the argument, else from the unzipped
# release folder this script sits in, else from the App mirror of a source
# tree. The udev rule is a separate step that needs sudo: install-udev.sh.
# </remarks>
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"
program="${1:-}"
if [[ -z "$program" ]]; then
    if [[ -f "$root/deckplate" ]]; then
        program="$root/deckplate"
    elif [[ -f "$root/Deckplate App/linux/deckplate" ]]; then
        program="$root/Deckplate App/linux/deckplate"
    else
        echo "error: no deckplate program found; pass its path: $0 /path/to/deckplate" >&2
        exit 1
    fi
fi
[[ -f "$program" ]] || { echo "error: $program is not a file" >&2; exit 1; }

bin="$HOME/.local/bin"
apps="$HOME/.local/share/applications"
icons="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$bin" "$apps" "$icons"

install -m 0755 "$program" "$bin/deckplate"
install -m 0755 "$here/deckplate-launch.sh" "$bin/deckplate-launch.sh"
install -m 0644 "$here/deckplate.svg" "$icons/deckplate.svg"
echo "==> program and launcher in $bin, icon in $icons"

# Absolute paths in the entry, so it works whether or not ~/.local/bin is on PATH.
sed -e "s#^Exec=.*#Exec=$bin/deckplate-launch.sh#" \
    -e "s#^Icon=.*#Icon=$icons/deckplate.svg#" \
    "$here/deckplate.desktop" > "$apps/deckplate.desktop"
chmod 0644 "$apps/deckplate.desktop"
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$apps" 2>/dev/null || true
fi
echo "==> menu entry $apps/deckplate.desktop"

desktop="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [[ -d "$desktop" ]]; then
    install -m 0755 "$apps/deckplate.desktop" "$desktop/deckplate.desktop"
    if command -v gio >/dev/null; then
        gio set "$desktop/deckplate.desktop" metadata::trusted true 2>/dev/null || true
    fi
    echo "==> desktop icon $desktop/deckplate.desktop"
fi

echo
echo "Done. Click Deckplate on the desktop or in the menu: it starts the daemon"
echo "if needed and opens the window. The daemon log is"
echo "${XDG_STATE_HOME:-$HOME/.local/state}/deckplate/run.log"
