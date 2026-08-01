#!/bin/sh

set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

mkdir -p "$HOME/.local/bin" "$HOME/.agents"
install -m 0644 "$project_dir/KANBAN-RULES.md" "$HOME/.agents/KANBAN-RULES.md"
install -m 0755 "$project_dir/bin/kanban" "$HOME/.local/bin/kanban"

printf '%s\n' 'kanban installed'
