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
install -m 0755 "$project_dir/bin/grok-review.sh" "$HOME/.local/bin/grok-review.sh"
install -m 0755 "$project_dir/bin/merge-worktree-memory.py" \
  "$HOME/.local/bin/merge-worktree-memory.py"

# rules/ 下全部规则都由本仓库拥有, 直接覆盖. 用户自己的 ~/.agents/AGENTS.md 不在
# rules/ 内, 不是安装目标, 也不得被本脚本读写.
for rule in "$project_dir"/rules/*.md; do
  install -m 0644 "$rule" "$HOME/.agents/$(basename "$rule")"
done

printf '%s\n' 'Onevoke installed'

# 早期版本装过的规则文件已改名或合并. 它们不再被覆盖, 会以过期内容留在 ~/.agents/,
# Agent 仍可能读到. 只检测并提示: 删不删是用户的决定, 本脚本不动用户目录下的任何文件.
stale_rules='SOLO-AGENTS.md CODEX-REVIEW-RULES.md GROK-REVIEW-RULES.md'
stale_found=0

# -e 不认悬空软链, 补 -L 才能覆盖旧文件被软链管理又断链的情况.
for stale in $stale_rules; do
  if [ -e "$HOME/.agents/$stale" ] || [ -L "$HOME/.agents/$stale" ]; then
    stale_found=$((stale_found + 1))
  fi
done

# 安装本身已经成功, 提示写不出去不该把退出码变成失败; 尤其 2 已被用法错误占用.
if [ "$stale_found" -gt 0 ]; then
  {
    printf '%s\n' ''
    printf '%s\n' 'Warning: outdated rule files from an earlier Onevoke version remain in ~/.agents:'
    for stale in $stale_rules; do
      if [ -e "$HOME/.agents/$stale" ] || [ -L "$HOME/.agents/$stale" ]; then
        printf '%s\n' "  $HOME/.agents/$stale"
      fi
    done
    printf '%s\n' 'They were renamed or merged into the current rules and are no longer updated.'
    printf '%s\n' 'Point your CLAUDE.md import or ~/.codex/AGENTS.md symlink at the new names first.'
    printf '%s\n' 'Then review and remove them yourself; this installer never deletes files.'
  } >&2 2>/dev/null || true
fi
