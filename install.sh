#!/bin/sh

set -eu

if [ "$#" -gt 0 ]; then
  echo "Usage: install.sh" >&2
  echo "Installs Onevoke commands to ~/.local/bin and rules to ~/.agents." >&2
  exit 2
fi

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# 入口 ONEVOKE-AGENTS.md 装的是用户可改的默认取值, 缺失时种一份, 已存在就绝不静默覆盖:
# 与模板有差异时只在 tty 里问一次, 用户回 1 才覆盖, 其余情况一律保留 (见下面 entry_action).
# 存在却读不到 (悬空软链, 目录, 权限不足) 说明这台机器没有可加载的入口, 是要人工处理的坏
# 现场: 装任何东西之前先停, 免得留下半套状态又谎报安装成功.
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

entry_template="$project_dir/rules/$entry_rule"
# seed 种新的, same 内容已一致, replace 用户同意覆盖, keep 保留用户的.
entry_action="$entry_state"
entry_scan=0
entry_prompted=0

# 入口已存在时不闷头跳过: 先摆出它与本版模板的差异, 再让用户决定覆盖还是保留. 无 tty
# 时 (CI, 管道, hook) 不能卡住安装, 一律保留, 由末尾的提示交代.
if [ "$entry_state" = 'kept' ]; then
  # 入口已确认是可读普通文件, grep 只该返回 0 或 1; 真出现别的状态就照实说读不出来,
  # 不能拿它当"拆分前入口"的证据.
  grep -q 'BASE-RULES.md' "$entry_path" || entry_scan=$?

  if cmp -s "$entry_path" "$entry_template"; then
    entry_action='same'
  elif [ -t 0 ] && [ -t 2 ]; then
    entry_prompted=1
    {
      printf '%s\n' ''
      if [ "$entry_scan" -eq 1 ]; then
        printf '%s\n' "$entry_path looks like a pre-split entry file."
        printf '%s\n' 'The shared rules moved to ~/.agents/BASE-RULES.md; the entry now only holds'
        printf '%s\n' 'the rule index, the priority chain and the default settings. Overwriting is'
        printf '%s\n' 'recommended here; re-apply your own edits afterwards.'
      else
        printf '%s\n' "$entry_path differs from this version's template."
        printf '%s\n' 'It carries your own default settings, so it is never overwritten silently.'
      fi
      printf '%s\n' ''
    } >&2

    entry_diff=0
    diff -u "$entry_path" "$entry_template" >&2 || entry_diff=$?
    if [ "$entry_diff" -gt 1 ]; then
      printf '%s\n' 'Warning: could not diff the two files; compare them yourself.' >&2
    fi

    {
      printf '%s\n' ''
      printf '%s\n' '  1. Overwrite with the template (your own edits are lost)'
      printf '%s\n' '  2. Keep your current file'
      printf '%s' 'Choose [1/2]: '
    } >&2
    entry_reply=''
    read -r entry_reply || entry_reply=''
    case "$entry_reply" in
    1) entry_action='replace' ;;
    2) entry_action='keep' ;;
    *)
      entry_action='keep'
      printf '%s\n' 'Not 1 or 2; keeping your current file.' >&2
      ;;
    esac
  else
    entry_action='keep'
  fi
fi

mkdir -p "$HOME/.local/bin" "$HOME/.agents"
install -m 0755 "$project_dir/bin/kanban" "$HOME/.local/bin/kanban"
install -m 0755 "$project_dir/bin/codex-review.sh" "$HOME/.local/bin/codex-review.sh"
install -m 0755 "$project_dir/bin/grok-review.sh" "$HOME/.local/bin/grok-review.sh"
install -m 0755 "$project_dir/bin/merge-worktree-memory.py" \
  "$HOME/.local/bin/merge-worktree-memory.py"

# 分册都由本仓库拥有, 直接覆盖. 入口按上面定下的 entry_action 处理. 用户自己的
# ~/.agents/AGENTS.md 不在 rules/ 内, 不是安装目标, 也不得被本脚本读写.
for rule in "$project_dir"/rules/*.md; do
  rule_name=$(basename "$rule")
  if [ "$rule_name" = "$entry_rule" ] &&
    [ "$entry_action" != 'seed' ] && [ "$entry_action" != 'replace' ]; then
    continue
  fi
  install -m 0644 "$rule" "$HOME/.agents/$rule_name"
done

printf '%s\n' 'Onevoke installed'

if [ "$entry_action" = 'replace' ]; then
  {
    printf '%s\n' ''
    printf '%s\n' "Note: $entry_path was overwritten with this version's template."
  } >&2 2>/dev/null || true
fi

# 用户刚在提示里选了保留, 差异也已经看过, 不必再重复一遍迁移说明.
if [ "$entry_action" = 'keep' ] && [ "$entry_prompted" -eq 1 ]; then
  {
    printf '%s\n' ''
    printf '%s\n' "Note: $entry_path left as it is."
    printf '%s\n' "      Template: $entry_template"
  } >&2 2>/dev/null || true
fi

# 没提示过就得把保留的事实和原因写清楚, 否则用户以为新版取值已经生效. 拆分前的入口装的
# 是全量通用条款, 内容早已过期, 单独点名; 判据是它不引用拆出去的 BASE-RULES.md.
if [ "$entry_action" = 'keep' ] && [ "$entry_prompted" -eq 0 ]; then
  {
    printf '%s\n' ''
    if [ "$entry_scan" -eq 0 ]; then
      printf '%s\n' "Note: $entry_path already exists and was left untouched."
      printf '%s\n' '      It carries your own default settings, and there is no tty to ask on.'
      printf '%s\n' '      Run install.sh from a terminal to see the diff and decide.'
    elif [ "$entry_scan" -eq 1 ]; then
      printf '%s\n' "Warning: $entry_path looks like a pre-split entry file."
      printf '%s\n' '      The shared rules moved to ~/.agents/BASE-RULES.md; the entry now only holds'
      printf '%s\n' '      the rule index, the priority chain and the default settings.'
      printf '%s\n' '      Replace it with the template below, then re-apply your own edits.'
    else
      printf '%s\n' "Warning: $entry_path was left untouched but could not be inspected."
      printf '%s\n' '      Compare it with the template below yourself.'
    fi
    printf '%s\n' "      Template: $entry_template"
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
