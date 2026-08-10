#!/bin/sh

set -eu

if [ "$#" -gt 0 ]; then
  echo "用法: install.sh" >&2
  echo "把 Onevoke 命令装到 ~/.local/bin, 规则装到 ~/.agents." >&2
  exit 2
fi

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# 入口 ONEVOKE-AGENTS.md 装的是用户可改的默认取值, 缺失时种一份, 已存在就绝不静默覆盖:
# 与模板有差异时只在 tty 里先给说明, 再循环问 1 覆盖 / 2 看 diff / 3 不覆盖, 只有明确回 1
# 才覆盖, 无 tty, EOF 和一直答不出来都按保留处理 (见下面 entry_action).
# 存在却读不到 (悬空软链, 目录, 权限不足) 说明这台机器没有可加载的入口, 是要人工处理的坏
# 现场: 装任何东西之前先停, 免得留下半套状态又谎报安装成功.
entry_rule='ONEVOKE-AGENTS.md'
entry_path="$HOME/.agents/$entry_rule"
entry_template="$project_dir/rules/$entry_rule"
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
    printf '%s\n' "错误: $entry_path 存在但读不出来."
    printf '%s\n' '      它是悬空软链, 目录, 或者当前用户没有读权限.'
    printf '%s\n' '      本次什么都没装. 修好或删掉这个路径, 再重跑 install.sh.'
    printf '%s\n' "      模板: $entry_template"
  } >&2
  exit 1
fi

# seed 种新的, same 内容已一致, replace 用户同意覆盖, keep 保留用户的.
entry_action="$entry_state"
entry_scan=0
entry_prompted=0

# 差异按需展示: 入口通常几十行, 一上来就刷屏反而盖掉前面的说明, 想看的人选 2 再看.
show_entry_diff() {
  # 先接住 diff 的退出码再输出: 直接管到着色器会把状态换成 sed 的.
  diff_status=0
  diff_text=$(diff -u "$entry_path" "$entry_template") || diff_status=$?
  if [ "$diff_status" -gt 1 ]; then
    printf '%s\n' '警告: 两个文件比不出差异, 请自行对照.' >&2
    return 0
  fi

  printf '%s\n' '' >&2
  if [ -n "${NO_COLOR:-}" ]; then
    printf '%s\n' "$diff_text" >&2
    return 0
  fi

  # diff -u 本身不着色. 文件头只认前两行的行号, 不能按 ^---/^+++ 认: 规则文件里
  # 一条 `---` 分隔线被删掉后, 内容行就是 `----`, 按前缀会被误判成文件头. 头部先
  # 着色, 在行首插入转义序列, 后面的 /^-/ 和 /^+/ 就不会再吃掉这两行.
  esc=$(printf '\033')
  printf '%s\n' "$diff_text" | sed \
    -e "1s/.*/${esc}[1m&${esc}[0m/" \
    -e "2s/.*/${esc}[1m&${esc}[0m/" \
    -e "/^@@/s/.*/${esc}[36m&${esc}[0m/" \
    -e "/^-/s/.*/${esc}[31m&${esc}[0m/" \
    -e "/^+/s/.*/${esc}[32m&${esc}[0m/" >&2
  return 0
}

# 入口已存在时不闷头跳过: 先说清这是什么文件, 再让用户决定覆盖, 看差异还是保留. 无 tty
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
        printf '%s\n' "$entry_path 是拆分前的旧入口."
        printf '%s\n' '通用条款已经拆到 ~/.agents/BASE-RULES.md, 入口现在只放分册索引, 优先级'
        printf '%s\n' '和默认取值. 这种情况建议覆盖, 之后再把你自己的改动加回去.'
      else
        printf '%s\n' "$entry_path 与本版模板不一致."
        printf '%s\n' '入口装的是你自己的默认取值 (分支, Reviewer, 看板任务完成), 不会被静默'
        printf '%s\n' '覆盖. 覆盖会用模板换掉整个文件, 你改过的取值要重新填一遍.'
      fi
      printf '%s\n' "模板: $entry_template"
    } >&2

    # 选 2 只看差异, 不算答复, 回来接着问; 读到 EOF 就当不覆盖, 免得在这里空转.
    entry_answered=0
    while [ "$entry_answered" -eq 0 ]; do
      {
        printf '%s\n' ''
        printf '%s\n' '  1. 直接覆盖 (你自己的改动会丢失)'
        printf '%s\n' '  2. 先查看 diff'
        printf '%s\n' '  3. 不要覆盖'
        printf '%s' '请选择 [1/2/3]: '
      } >&2
      entry_reply=''
      if read -r entry_reply; then
        case "$entry_reply" in
        1)
          entry_action='replace'
          entry_answered=1
          ;;
        2) show_entry_diff ;;
        3)
          entry_action='keep'
          entry_answered=1
          ;;
        *) printf '%s\n' '不是 1, 2 或 3, 请重新选.' >&2 ;;
        esac
      else
        printf '%s\n' '' >&2
        entry_action='keep'
        entry_answered=1
      fi
    done
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
    printf '%s\n' "提示: $entry_path 已用本版模板覆盖."
  } >&2 2>/dev/null || true
fi

# 用户刚在提示里选了保留, 差异也已经看过, 不必再重复一遍迁移说明.
if [ "$entry_action" = 'keep' ] && [ "$entry_prompted" -eq 1 ]; then
  {
    printf '%s\n' ''
    printf '%s\n' "提示: $entry_path 保持原样."
    printf '%s\n' "      模板: $entry_template"
  } >&2 2>/dev/null || true
fi

# 没提示过就得把保留的事实和原因写清楚, 否则用户以为新版取值已经生效. 拆分前的入口装的
# 是全量通用条款, 内容早已过期, 单独点名; 判据是它不引用拆出去的 BASE-RULES.md.
if [ "$entry_action" = 'keep' ] && [ "$entry_prompted" -eq 0 ]; then
  {
    printf '%s\n' ''
    if [ "$entry_scan" -eq 0 ]; then
      printf '%s\n' "提示: $entry_path 已存在, 保持原样."
      printf '%s\n' '      它装的是你自己的默认取值, 而当前没有 tty 可以询问.'
      printf '%s\n' '      在终端里重跑 install.sh 就能看到差异并当场决定.'
    elif [ "$entry_scan" -eq 1 ]; then
      printf '%s\n' "警告: $entry_path 是拆分前的旧入口."
      printf '%s\n' '      通用条款已经拆到 ~/.agents/BASE-RULES.md, 入口现在只放分册索引,'
      printf '%s\n' '      优先级和默认取值.'
      printf '%s\n' '      在终端里重跑 install.sh 就能看到差异并选择覆盖.'
    else
      printf '%s\n' "警告: $entry_path 保持原样, 但读不出内容做比较."
      printf '%s\n' '      请自行对照下面的模板.'
    fi
    printf '%s\n' "      模板: $entry_template"
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
    printf '%s\n' '警告: ~/.agents 里还留着早期 Onevoke 版本的规则文件:'
    for stale in $stale_rules; do
      if [ -e "$HOME/.agents/$stale" ] || [ -L "$HOME/.agents/$stale" ]; then
        printf '%s\n' "  $HOME/.agents/$stale"
      fi
    done
    printf '%s\n' '它们已经改名或合并进现在的规则, 不会再被更新.'
    printf '%s\n' '先把 CLAUDE.md 的导入行或 ~/.codex/AGENTS.md 软链改指新文件名.'
    printf '%s\n' '然后自行检查并删除它们; 本安装器从不删除文件.'
  } >&2 2>/dev/null || true
fi
