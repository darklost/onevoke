#!/bin/sh

set -eu

if [ "$#" -gt 0 ]; then
  echo "用法: install.sh" >&2
  echo "把 Onevoke 命令装到 ~/.local/bin, 规则装到 ~/.agents." >&2
  exit 2
fi

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin_dir="$HOME/.local/bin"
agents_dir="$HOME/.agents"

# 同名目标若是目录, `install` 会把文件塞进目录而不是覆盖目标, 会形成看似成功的
# 坏安装. 在写入任何文件前统一拒绝.
for command in "$project_dir"/bin/*; do
  [ -f "$command" ] || continue
  target="$bin_dir/$(basename "$command")"
  if [ -d "$target" ]; then
    printf '%s\n' "错误: 安装目标是目录: $target" >&2
    exit 1
  fi
done
for rule in "$project_dir"/rules/*.md; do
  [ -f "$rule" ] || continue
  target="$agents_dir/$(basename "$rule")"
  if [ -d "$target" ]; then
    printf '%s\n' "错误: 安装目标是目录: $target" >&2
    exit 1
  fi
done

mkdir -p "$bin_dir" "$agents_dir"

# bin/ 和 rules/ 都由本仓库拥有, 每次安装直接覆盖. 用户自己的
# ~/.agents/AGENTS.md 不在 rules/ 中, 不是安装目标.
for command in "$project_dir"/bin/*; do
  [ -f "$command" ] || continue
  install -m 0755 "$command" "$bin_dir/$(basename "$command")"
done

for rule in "$project_dir"/rules/*.md; do
  [ -f "$rule" ] || continue
  install -m 0644 "$rule" "$agents_dir/$(basename "$rule")"
done

printf '%s\n' 'Onevoke installed'

# 用绝对路径启动, 即使 ~/.local/bin 尚未进入 PATH 也能完成诊断和引导.
if ! "$bin_dir/onevoke" welcome; then
  printf '%s\n' '警告: Onevoke 文件已安装, 但 welcome 未完成; 请修复提示问题后重新运行 onevoke welcome.' >&2
fi
