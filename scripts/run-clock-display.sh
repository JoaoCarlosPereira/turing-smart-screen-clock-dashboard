#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for lib in "$script_dir/lib/project-dir.sh" "$script_dir/../lib/project-dir.sh"; do
    if [[ -f "$lib" ]]; then
        # shellcheck source=lib/project-dir.sh
        source "$lib"
        break
    fi
done
if ! declare -F turing_require_project_dir >/dev/null; then
    printf 'project-dir.sh não encontrado junto de %s\n' "$script_dir" >&2
    exit 1
fi

project_dir="$(turing_require_project_dir)"

# Wait for venv + serial (USB may appear after the graphical session).
for _ in $(seq 1 300); do
    if [[ -x "$project_dir/.venv/bin/python" ]] && compgen -G '/dev/ttyACM*' >/dev/null; then
        break
    fi
    sleep 0.5
done

if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
    printf 'Ambiente .venv ausente em %s\n' "$project_dir" >&2
    exit 1
fi
if ! compgen -G '/dev/ttyACM*' >/dev/null; then
    printf 'Porta serial /dev/ttyACM* ainda indisponível\n' >&2
    exit 1
fi

cd "$project_dir"
exec .venv/bin/python clock-display.py "$@"
