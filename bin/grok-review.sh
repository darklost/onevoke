#!/usr/bin/env bash

set -euo pipefail

script_path="${BASH_SOURCE[0]}"
while [[ -L "$script_path" ]]; do
  script_dir="$(CDPATH= cd -- "$(dirname -- "$script_path")" && pwd -P)"
  link_target="$(readlink -- "$script_path")"
  [[ "$link_target" == /* ]] && script_path="$link_target" || script_path="$script_dir/$link_target"
done
readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$script_path")" && pwd -P)"
exec "$SCRIPT_DIR/onevoke-review.sh" grok "$@"
