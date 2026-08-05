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
cd "$project_dir"
exec /usr/bin/python3 iphone-notifications.py "$@"
