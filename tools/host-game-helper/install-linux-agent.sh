#!/usr/bin/env bash
# Installs the Turing host agent as a systemd --user service on THIS Ubuntu/Linux
# machine (the remote host being reported — not the Mini-PC running the screen).
#
# Self-contained on purpose: this folder is meant to be copied to a different
# machine (see README.md), so it does not reuse the Mini-PC repo's own
# scripts/lib/install-common.sh — that helper assumes the Mini-PC's project
# layout, which does not exist here.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

for cmd in busctl loginctl dbus-monitor python3 systemctl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        printf '%s não está instalado — necessário para o host agent.\n' "$cmd" >&2
        exit 1
    fi
done

dest="${XDG_DATA_HOME:-$HOME/.local/share}/turing-host-agent"
mkdir -p "$dest/bin"
install -m 0755 "$script_dir/linux_reporter.py" "$dest/bin/linux_reporter.py"
install -m 0644 "$script_dir/common.py" "$dest/bin/common.py"

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
install -m 0644 "$script_dir/host-agent.service" "$unit_dir/host-agent.service"

systemctl --user daemon-reload
systemctl --user enable --now host-agent.service
systemctl --user --no-pager status host-agent.service
