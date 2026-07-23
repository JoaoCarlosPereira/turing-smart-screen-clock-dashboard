#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
unit_source="$project_dir/systemd/turing-clock.service"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit_target="$unit_dir/turing-clock.service"

if [[ "$project_dir" != "/mnt/dados/dsv/turing-smart-screen-python" ]]; then
    printf 'Este serviço está configurado para /mnt/dados/dsv/turing-smart-screen-python.\n' >&2
    printf 'Ajuste WorkingDirectory, ExecStart e Documentation em %s.\n' "$unit_source" >&2
    exit 1
fi

if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
    printf 'Ambiente .venv ausente. Consulte RELOGIO.md antes de instalar o serviço.\n' >&2
    exit 1
fi

mkdir -p "$unit_dir"
systemctl --user stop turing-clock.service >/dev/null 2>&1 || true
systemctl --user disable turing-clock.service >/dev/null 2>&1 || true
# Recreate after disable — systemd may remove the previous enablement/unit link.
ln -sfn "$unit_source" "$unit_target"
systemctl --user daemon-reload
systemctl --user enable --now turing-clock.service
systemctl --user --no-pager status turing-clock.service
