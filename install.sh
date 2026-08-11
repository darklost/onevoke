#!/bin/sh

set -eu

onevoke_locale=${ONEVOKE_LANG:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}
case "$onevoke_locale" in
  zh*|ZH*) onevoke_zh=1 ;;
  *) onevoke_zh=0 ;;
esac

if [ "$#" -gt 0 ]; then
  if [ "$onevoke_zh" -eq 1 ]; then
    echo "用法: install.sh" >&2
    echo "把 Onevoke 命令装到 ~/.local/bin, 规则装到 ~/.agents." >&2
  else
    echo "usage: install.sh" >&2
    echo "Install Onevoke commands to ~/.local/bin and rules to ~/.agents." >&2
  fi
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
    if [ "$onevoke_zh" -eq 1 ]; then
      printf '%s\n' "错误: 安装目标是目录: $target" >&2
    else
      printf '%s\n' "error: installation target is a directory: $target" >&2
    fi
    exit 1
  fi
done
for rule in "$project_dir"/rules/*.md; do
  [ -f "$rule" ] || continue
  target="$agents_dir/$(basename "$rule")"
  if [ -d "$target" ]; then
    if [ "$onevoke_zh" -eq 1 ]; then
      printf '%s\n' "错误: 安装目标是目录: $target" >&2
    else
      printf '%s\n' "error: installation target is a directory: $target" >&2
    fi
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

# 工具包文件安装已完成. welcome (含可选 MemSearch 安装) 失败不得回滚或
# 把本脚本变成失败退出; MemSearch 出错时 welcome 内会提示用户自行安装.
if ! "$bin_dir/onevoke" welcome; then
  if [ "$onevoke_zh" -eq 1 ]; then
    printf '%s\n' \
      '警告: Onevoke 文件已安装, 但 welcome 未完成; 请修复提示问题后重新运行 onevoke welcome.' \
      '说明: MemSearch 为可选项, 其安装失败不影响本工具包; 可稍后自行安装或再跑 welcome.' \
      >&2
  else
    printf '%s\n' \
      'warning: Onevoke files were installed, but welcome did not complete; fix the reported issue and rerun onevoke welcome.' \
      'note: MemSearch is optional; installation failure does not affect this toolkit and can be retried later.' \
      >&2
  fi
fi
# 文件安装成功时始终以 0 结束, 不因 welcome/MemSearch 阻断.
exit 0
