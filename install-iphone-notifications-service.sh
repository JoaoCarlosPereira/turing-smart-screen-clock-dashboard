#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/install-common.sh
source "$repo_root/scripts/lib/install-common.sh"

iphone_address="${1:-80:A9:97:F2:7C:2C}"

if [[ ! "$iphone_address" =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]]; then
    printf 'Endereço Bluetooth inválido: %s\n' "$iphone_address" >&2
    exit 1
fi

for command in bluetoothctl notify-send; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf '%s não está instalado.\n' "$command" >&2
        exit 1
    fi
done

project_dir="$(turing_canonical_project_or_die "$repo_root")"
turing_install_launchers "$project_dir" >/dev/null

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit_target="$unit_dir/iphone-notifications.service"
mkdir -p "$unit_dir"
sed -E \
    -e "s|run-iphone-notifications\\.sh [[:xdigit:]:]+|run-iphone-notifications.sh $iphone_address|" \
    "$project_dir/systemd/iphone-notifications.service" >"$unit_target"
turing_assert_unit_safe "$unit_target"

systemctl --user daemon-reload
systemctl --user enable --now iphone-notifications.service
systemctl --user --no-pager status iphone-notifications.service
