#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/install-common.sh
source "$repo_root/scripts/lib/install-common.sh"

if ! command -v bluetoothctl >/dev/null 2>&1; then
    printf 'bluetoothctl não está instalado.\n' >&2
    exit 1
fi

project_dir="$(turing_canonical_project_or_die "$repo_root")"
turing_install_launchers "$project_dir" >/dev/null

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit_target="$unit_dir/bluetooth-autoconnect.service"
mkdir -p "$unit_dir"
install -m 0644 "$project_dir/systemd/bluetooth-autoconnect.service" "$unit_target"
turing_assert_unit_safe "$unit_target"

systemctl --user daemon-reload
systemctl --user enable --now bluetooth-autoconnect.service
systemctl --user --no-pager status bluetooth-autoconnect.service
