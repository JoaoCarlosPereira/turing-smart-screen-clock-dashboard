#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/install-common.sh
source "$repo_root/scripts/lib/install-common.sh"

project_dir="$(turing_canonical_project_or_die "$repo_root")"
if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
    printf 'Ambiente .venv ausente em %s. Consulte RELOGIO.md antes de instalar o serviço.\n' "$project_dir" >&2
    exit 1
fi

turing_install_launchers "$project_dir" >/dev/null

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit_target="$unit_dir/turing-clock.service"
mkdir -p "$unit_dir"
install -m 0644 "$project_dir/systemd/turing-clock.service" "$unit_target"
turing_assert_unit_safe "$unit_target"

systemctl --user stop turing-clock.service >/dev/null 2>&1 || true
systemctl --user daemon-reload
systemctl --user enable --now turing-clock.service
systemctl --user --no-pager status turing-clock.service
