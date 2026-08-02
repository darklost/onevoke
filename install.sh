#!/bin/sh

set -eu

if [ "$#" -gt 0 ]; then
  echo "Usage: install.sh" >&2
  echo "Installs solo-mode commands to ~/.local/bin and rules to ~/.agents." >&2
  exit 2
fi

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

mkdir -p "$HOME/.local/bin" "$HOME/.agents"
install -m 0755 "$project_dir/bin/kanban" "$HOME/.local/bin/kanban"
install -m 0755 "$project_dir/bin/codex-review.sh" "$HOME/.local/bin/codex-review.sh"
install -m 0755 "$project_dir/bin/merge-worktree-memory.py" \
  "$HOME/.local/bin/merge-worktree-memory.py"

# 三份规则都由本仓库拥有, 直接覆盖. 用户自己的 ~/.agents/AGENTS.md 不是安装目标,
# 也不得被本脚本读写.
install -m 0644 "$project_dir/rules/SOLO-AGENTS.md" "$HOME/.agents/SOLO-AGENTS.md"
install -m 0644 "$project_dir/rules/KANBAN-RULES.md" "$HOME/.agents/KANBAN-RULES.md"
install -m 0644 "$project_dir/rules/CODEX-REVIEW-RULES.md" \
  "$HOME/.agents/CODEX-REVIEW-RULES.md"

printf '%s\n' 'solo-mode installed'
