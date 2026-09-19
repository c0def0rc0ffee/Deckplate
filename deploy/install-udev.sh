#!/usr/bin/env bash
# <summary>
# One time Linux setup for Deckplate. Run with sudo:
#
#   sudo ./deploy/install-udev.sh
#
# Idempotent: safe to re-run. Installs the udev rule that lets your user open
# the deck's hidraw node, reloads udev, and asks you to replug the deck. This
# is the only step that needs root; everything else runs as you.
# </summary>
# <remarks>
# Without the rule the deck still enumerates and still shows up in lsusb, but
# opening it fails as a permission error that reads like a missing or broken
# device. If the daemon reports that no deck is present while one is plugged
# in, come here before suspecting the hardware.
# The reload alone does not fix a deck that is already plugged in: udev applies
# permissions when a device appears, so the replug at the end is part of the
# job and not politeness.
# The rule file itself is deploy/40-deckplate.rules and is the only thing
# written outside the project tree.
# </remarks>
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "error: run me with sudo: sudo ./deploy/install-udev.sh" >&2
  exit 1
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rule_src="$root/deploy/40-deckplate.rules"
rule_dst=/etc/udev/rules.d/40-deckplate.rules

if [[ -f "$rule_dst" ]] && cmp -s "$rule_src" "$rule_dst"; then
  echo "==> udev rule already installed and up to date"
else
  install -m 0644 "$rule_src" "$rule_dst"
  echo "==> installed $rule_dst"
fi

echo "==> reloading udev"
udevadm control --reload-rules
udevadm trigger

echo
echo "Done. Unplug and replug the deck once, then as your normal user:"
echo "    python3 -m deckplate devices"
