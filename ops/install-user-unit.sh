#!/usr/bin/env bash
set -euo pipefail

mode="${1:---dry-run}"
if [[ "$#" -gt 1 || ( "$mode" != "--dry-run" && "$mode" != "--install" ) ]]; then
    echo "Usage: $0 [--dry-run|--install]" >&2
    exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_dir="${script_dir}/systemd"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
units=(
    "mirofish-daily.service"
    "mirofish-daily.timer"
)

for unit in "${units[@]}"; do
    if [[ ! -f "${source_dir}/${unit}" ]]; then
        echo "Missing unit source: ${source_dir}/${unit}" >&2
        exit 1
    fi
done

if [[ "$mode" == "--dry-run" ]]; then
    echo "DRY RUN: would create ${unit_dir}"
    for unit in "${units[@]}"; do
        echo "DRY RUN: would install ${source_dir}/${unit} -> ${unit_dir}/${unit}"
    done
    echo "DRY RUN: would run /usr/bin/systemctl --user daemon-reload"
    exit 0
fi

/usr/bin/install -d -m 0755 "${unit_dir}"
for unit in "${units[@]}"; do
    /usr/bin/install -m 0644 "${source_dir}/${unit}" "${unit_dir}/${unit}"
done
/usr/bin/systemctl --user daemon-reload
echo "Installed ${units[*]} into ${unit_dir}; timer not enabled."

