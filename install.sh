#!/bin/sh

set -eu

if [ "$#" -gt 0 ]; then
  echo "Usage: install.sh" >&2
  echo "Installs Onevoke commands to ~/.local/bin and rules to ~/.agents." >&2
  exit 2
fi

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# 入口 ONEVOKE-AGENTS.md 装的是用户可改的默认取值, 只在缺失时种一次, 已存在就原样保留,
# 免得升级把定制冲掉. 存在却读不到 (悬空软链, 目录, 权限不足) 说明这台机器没有可加载的
# 入口, 是要人工处理的坏现场: 装任何东西之前先停, 免得留下半套状态又谎报安装成功.
entry_rule='ONEVOKE-AGENTS.md'
entry_path="$HOME/.agents/$entry_rule"
entry_state='seed'

# -e 不认悬空软链, 补 -L 才能把断链认成"已存在"; 指向普通可读文件的软链仍算正常入口.
if [ -e "$entry_path" ] || [ -L "$entry_path" ]; then
  if [ -f "$entry_path" ] && [ -r "$entry_path" ]; then
    entry_state='kept'
  else
    entry_state='unreadable'
  fi
fi

if [ "$entry_state" = 'unreadable' ]; then
  {
    printf '%s\n' "Error: $entry_path exists but cannot be read."
    printf '%s\n' '       It is a dangling symlink, a directory, or not readable by this user.'
    printf '%s\n' '       Nothing was installed. Fix or remove that path, then run install.sh again.'
    printf '%s\n' "       Template: $project_dir/rules/$entry_rule"
  } >&2
  exit 1
fi

mkdir -p "$HOME/.local/bin" "$HOME/.agents"
install -m 0755 "$project_dir/bin/kanban" "$HOME/.local/bin/kanban"
install -m 0755 "$project_dir/bin/codex-review.sh" "$HOME/.local/bin/codex-review.sh"
install -m 0755 "$project_dir/bin/grok-review.sh" "$HOME/.local/bin/grok-review.sh"
install -m 0755 "$project_dir/bin/merge-worktree-memory.py" \
  "$HOME/.local/bin/merge-worktree-memory.py"

# 分册都由本仓库拥有, 直接覆盖. 入口按上面判定的 entry_state 处理. 用户自己的
# ~/.agents/AGENTS.md 不在 rules/ 内, 不是安装目标, 也不得被本脚本读写.
for rule in "$project_dir"/rules/*.md; do
  rule_name=$(basename "$rule")
  if [ "$rule_name" = "$entry_rule" ] && [ "$entry_state" = 'kept' ]; then
    continue
  fi
  install -m 0644 "$rule" "$HOME/.agents/$rule_name"
done

printf '%s\n' 'Onevoke installed'

# 保留入口要说清楚, 否则用户以为新版取值已经生效. 拆分前的入口装的是全量通用条款,
# 内容早已过期, 单独点名; 判据是它不引用拆出去的 BASE-RULES.md.
if [ "$entry_state" = 'kept' ]; then
  # 入口已确认是可读普通文件, grep 只该返回 0 或 1; 真出现别的状态就照实说读不出来,
  # 不能拿它当"拆分前入口"的证据.
  entry_scan=0
  grep -q 'BASE-RULES.md' "$entry_path" || entry_scan=$?
  {
    printf '%s\n' ''
    if [ "$entry_scan" -eq 0 ]; then
      printf '%s\n' "Note: $entry_path already exists and was left untouched."
      printf '%s\n' '      It carries your own default settings; the installer seeds it only once.'
    elif [ "$entry_scan" -eq 1 ]; then
      printf '%s\n' "Warning: $entry_path looks like a pre-split entry file."
      printf '%s\n' '      The shared rules moved to ~/.agents/BASE-RULES.md; the entry now only holds'
      printf '%s\n' '      the rule index, the priority chain and the default settings.'
      printf '%s\n' '      Replace it with the template below, then re-apply your own edits.'
    else
      printf '%s\n' "Warning: $entry_path was left untouched but could not be inspected."
      printf '%s\n' '      Compare it with the template below yourself.'
    fi
    printf '%s\n' "      Template: $project_dir/rules/$entry_rule"
  } >&2 2>/dev/null || true
fi

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
