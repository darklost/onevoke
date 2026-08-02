#!/bin/sh

set -eu

if [ "$#" -gt 0 ]; then
  echo "Usage: install.sh" >&2
  echo "Installs Onevoke commands to ~/.local/bin and rules to ~/.agents." >&2
  exit 2
fi

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

mkdir -p "$HOME/.local/bin" "$HOME/.agents"
install -m 0755 "$project_dir/bin/kanban" "$HOME/.local/bin/kanban"
install -m 0755 "$project_dir/bin/codex-review.sh" "$HOME/.local/bin/codex-review.sh"
install -m 0755 "$project_dir/bin/merge-worktree-memory.py" \
  "$HOME/.local/bin/merge-worktree-memory.py"

# rules/ 下全部规则都由本仓库拥有, 直接覆盖. 用户自己的 ~/.agents/AGENTS.md 不在
# rules/ 内, 不是安装目标, 也不得被本脚本读写.
for rule in "$project_dir"/rules/*.md; do
  install -m 0644 "$rule" "$HOME/.agents/$(basename "$rule")"
done

printf '%s\n' 'Onevoke installed'
