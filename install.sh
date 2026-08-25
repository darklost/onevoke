#!/bin/sh

set -eu

_install_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

onevoke_lang=
onevoke_lang_set=0
project_arg=
project_set=0
show_help=0
parse_error=0
missing_lang=0
missing_project=0
duplicate_project=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --lang)
      if [ "$#" -lt 2 ]; then
        missing_lang=1
        parse_error=1
        break
      fi
      onevoke_lang_set=1
      onevoke_lang=$2
      shift 2
      ;;
    --lang=*)
      onevoke_lang_set=1
      onevoke_lang=${1#--lang=}
      shift
      ;;
    --project)
      if [ "$project_set" -eq 1 ]; then
        duplicate_project=1
        parse_error=1
        break
      fi
      if [ "$#" -lt 2 ]; then
        missing_project=1
        parse_error=1
        break
      fi
      case "$2" in
        --lang|--lang=*|--project|--project=*|-h|--help)
          missing_project=1
          parse_error=1
          break
          ;;
      esac
      project_set=1
      project_arg=$2
      shift 2
      ;;
    --project=*)
      if [ "$project_set" -eq 1 ]; then
        duplicate_project=1
        parse_error=1
        break
      fi
      project_set=1
      project_arg=${1#--project=}
      shift
      ;;
    -h|--help)
      show_help=1
      shift
      ;;
    *)
      parse_error=1
      break
      ;;
  esac
done

if [ "$project_set" -eq 1 ] && [ -z "$project_arg" ]; then
  missing_project=1
  parse_error=1
fi

onevoke_locale=
case "$onevoke_lang" in
  cn) onevoke_locale=cn ;;
  en) onevoke_locale=en ;;
esac
# 项目安装禁止探测全局配置. 无参数全局安装保持既有 configured-language 回退.
if [ "$project_set" -eq 0 ]; then
  if [ "$onevoke_lang_set" -eq 0 ] || [ -z "$onevoke_locale" ]; then
    if command -v python3 >/dev/null 2>&1; then
      _cfg_lang=$(python3 "$_install_root/bin/onevoke_config.py" configured-language 2>/dev/null || true)
      case "$_cfg_lang" in
        cn|en) onevoke_locale=$_cfg_lang ;;
      esac
    fi
  fi
fi
if [ -z "$onevoke_locale" ]; then
  onevoke_locale=${ONEVOKE_LANG:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}
fi
case "$(printf '%s' "$onevoke_locale" | tr '[:upper:]' '[:lower:]')" in
  en*) onevoke_zh=0 ;;
  *) onevoke_zh=1 ;;
esac

usage() {
  if [ "$onevoke_zh" -eq 1 ]; then
    echo "用法: install.sh [--lang {cn,en}] [--project <目录>]"
    echo "把 Onevoke 命令装到 ~/.local/bin, 规则装到 ~/.agents."
    echo "指定 --project 时只装到该 Git 项目主 worktree 的 .onevoke/, 不写全局路径, 也不运行 welcome."
  else
    echo "usage: install.sh [--lang {cn,en}] [--project <directory>]"
    echo "Install Onevoke commands to ~/.local/bin and rules to ~/.agents."
    echo "With --project, install only into that Git project's main worktree .onevoke/, skip global paths, and do not run welcome."
  fi
}

fail_usage() {
  usage >&2
  if [ "$#" -gt 0 ]; then
    printf '%s\n' "$1" >&2
  fi
  exit 2
}

if [ "$missing_lang" -eq 1 ] || {
  [ "$onevoke_lang_set" -eq 1 ] && [ "$onevoke_lang" != "cn" ] && [ "$onevoke_lang" != "en" ]
}; then
  if [ "$onevoke_zh" -eq 1 ]; then
    fail_usage "错误: --lang 只接受 cn 或 en"
  else
    fail_usage "error: --lang must be cn or en"
  fi
fi
if [ "$missing_project" -eq 1 ]; then
  if [ "$onevoke_zh" -eq 1 ]; then
    fail_usage "错误: --project 需要目录"
  else
    fail_usage "error: --project requires a directory"
  fi
fi
if [ "$duplicate_project" -eq 1 ]; then
  if [ "$onevoke_zh" -eq 1 ]; then
    fail_usage "错误: --project 只能指定一次"
  else
    fail_usage "error: --project may be given only once"
  fi
fi
if [ "$show_help" -eq 1 ] && [ "$parse_error" -eq 0 ]; then
  usage
  exit 0
fi
if [ "$parse_error" -eq 1 ]; then
  usage >&2
  exit 2
fi

reject_if_directory() {
  target=$1
  if [ -d "$target" ]; then
    if [ "$onevoke_zh" -eq 1 ]; then
      printf '%s\n' "错误: 安装目标是目录: $target" >&2
    else
      printf '%s\n' "error: installation target is a directory: $target" >&2
    fi
    exit 1
  fi
}

reject_if_symlink() {
  target=$1
  if [ -L "$target" ]; then
    if [ "$onevoke_zh" -eq 1 ]; then
      printf '%s\n' "错误: 安装目标是符号链接: $target" >&2
    else
      printf '%s\n' "error: installation target is a symlink: $target" >&2
    fi
    exit 1
  fi
}

reject_payload_targets() {
  dest_bin=$1
  dest_agents=$2
  dest_share=$3
  reject_symlinks=${4:-0}
  for command in "$source_dir"/bin/*; do
    [ -f "$command" ] || continue
    if [ "$reject_symlinks" -eq 1 ]; then
      reject_if_symlink "$dest_bin/$(basename "$command")"
    fi
    reject_if_directory "$dest_bin/$(basename "$command")"
  done
  for rule in "$source_dir"/rules/*.md; do
    [ -f "$rule" ] || continue
    if [ "$reject_symlinks" -eq 1 ]; then
      reject_if_symlink "$dest_agents/$(basename "$rule")"
    fi
    reject_if_directory "$dest_agents/$(basename "$rule")"
  done
  share_src="$source_dir/share/kanban-web"
  if [ -d "$share_src" ]; then
    if [ -e "$dest_share" ]; then
      if [ "$reject_symlinks" -eq 1 ]; then
        reject_if_symlink "$dest_share"
      fi
      if [ ! -d "$dest_share" ]; then
        if [ "$onevoke_zh" -eq 1 ]; then
          printf '%s\n' "错误: 安装目标不是目录: $dest_share" >&2
        else
          printf '%s\n' "error: installation target is not a directory: $dest_share" >&2
        fi
        exit 1
      fi
    fi
    for asset in "$share_src"/*; do
      [ -f "$asset" ] || continue
      if [ "$reject_symlinks" -eq 1 ]; then
        reject_if_symlink "$dest_share/$(basename "$asset")"
      fi
      reject_if_directory "$dest_share/$(basename "$asset")"
    done
  fi
}

install_payloads() {
  dest_bin=$1
  dest_agents=$2
  dest_share=$3
  mkdir -p "$dest_bin" "$dest_agents"
  for command in "$source_dir"/bin/*; do
    [ -f "$command" ] || continue
    install -m 0755 "$command" "$dest_bin/$(basename "$command")"
  done
  for rule in "$source_dir"/rules/*.md; do
    [ -f "$rule" ] || continue
    install -m 0644 "$rule" "$dest_agents/$(basename "$rule")"
  done
  if [ -d "$source_dir/share/kanban-web" ]; then
    mkdir -p "$dest_share"
    for asset in "$source_dir"/share/kanban-web/*; do
      [ -f "$asset" ] || continue
      install -m 0644 "$asset" "$dest_share/$(basename "$asset")"
    done
  fi
  agent_rules="$dest_agents/AGENTS.md"
  entry_rules="$dest_agents/ONEVOKE-AGENTS.md"
  if [ -f "$entry_rules" ] && [ ! -e "$agent_rules" ] && [ ! -L "$agent_rules" ]; then
    ln -s "$(basename "$entry_rules")" "$agent_rules"
  fi
}

print_installed() {
  if [ "$onevoke_zh" -eq 1 ]; then
    printf '%s\n' 'Onevoke 已安装'
  else
    printf '%s\n' 'Onevoke installed'
  fi
}

helper_lang=cn
if [ "$onevoke_zh" -eq 0 ]; then
  helper_lang=en
fi

run_project_helper() {
  if ! command -v python3 >/dev/null 2>&1; then
    if [ "$onevoke_zh" -eq 1 ]; then
      printf '%s\n' "错误: 项目安装需要 python3" >&2
    else
      printf '%s\n' "error: project install requires python3" >&2
    fi
    exit 1
  fi
  ONEVOKE_LANG="$helper_lang" ONEVOKE_INSTALL_BIN="$_install_root/bin" python3 - "$@" <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["ONEVOKE_INSTALL_BIN"])
from onevoke_config import (
    ConfigError,
    ensure_project_git_exclude,
    language_text,
    project_install_paths,
)
from onevoke_fs import is_reparse_point


def fail(path: Path, kind: str) -> None:
    if kind == "symlink":
        raise ConfigError(
            language_text(
                f"安装目标是符号链接: {path}",
                f"installation target is a symlink: {path}",
            )
        )
    if kind == "not-dir":
        raise ConfigError(
            language_text(
                f"安装目标不是目录: {path}",
                f"installation target is not a directory: {path}",
            )
        )
    raise ConfigError(
        language_text(
            f"安装目标是目录: {path}",
            f"installation target is a directory: {path}",
        )
    )


def reject_existing(path: Path, *, must_be_dir: bool = False) -> None:
    if not os.path.lexists(path):
        return
    if is_reparse_point(path) or os.path.islink(path):
        fail(path, "symlink")
    if must_be_dir and not path.is_dir():
        fail(path, "not-dir")


command = sys.argv[1]
target = Path(sys.argv[2])
try:
    if command == "exclude":
        ensure_project_git_exclude(target)
        sys.exit(0)
    paths = project_install_paths(target)
    if paths.install_root is None or paths.project_root is None:
        raise ConfigError(
            language_text(
                "项目安装路径缺少主 worktree",
                "project install paths are missing the main worktree",
            )
        )
    reject_existing(paths.install_root, must_be_dir=True)
    reject_existing(paths.bin_dir, must_be_dir=True)
    reject_existing(paths.rules_dir, must_be_dir=True)
    reject_existing(paths.share_dir, must_be_dir=True)
    web_share = paths.share_dir / "kanban-web"
    reject_existing(web_share, must_be_dir=True)
except ConfigError as error:
    print(error, file=sys.stderr)
    sys.exit(1)

print(paths.project_root)
print(paths.install_root)
print(paths.bin_dir)
print(paths.rules_dir)
print(web_share)
PY
}

if [ "$project_set" -eq 1 ]; then
  prepared=$(run_project_helper resolve "$project_arg") || exit 1
  {
    IFS= read -r _main_worktree
    IFS= read -r _install_target
    IFS= read -r dest_bin
    IFS= read -r dest_agents
    IFS= read -r dest_share
  } <<EOF
$prepared
EOF
  if [ -z "${dest_bin:-}" ] || [ -z "${dest_agents:-}" ]; then
    if [ "$onevoke_zh" -eq 1 ]; then
      printf '%s\n' "错误: 无法解析项目安装路径" >&2
    else
      printf '%s\n' "error: failed to resolve project install paths" >&2
    fi
    exit 1
  fi
  reject_payload_targets "$dest_bin" "$dest_agents" "$dest_share" 1
  run_project_helper exclude "$project_arg" >/dev/null || exit 1
  install_payloads "$dest_bin" "$dest_agents" "$dest_share"
  print_installed
  printf '%s\n' "$dest_bin/onevoke" "$dest_bin/kanban"
  if [ "$onevoke_zh" -eq 1 ]; then
    printf '%s\n' \
      "项目安装完成, 未修改 PATH, 也未改动全局 Onevoke 安装." \
      "请使用以上绝对路径." \
      >&2
  else
    printf '%s\n' \
      "Project install finished; PATH and the global Onevoke install were not changed." \
      "Use the absolute command paths above." \
      >&2
  fi
  exit 0
fi

bin_dir="$HOME/.local/bin"
agents_dir="$HOME/.agents"
legacy_review_commands=
remove_legacy_reviews=0

# 同名目标若是目录, `install` 会把文件塞进目录而不是覆盖目标, 会形成看似成功的
# 坏安装. 在写入任何文件前统一拒绝.
reject_payload_targets "$bin_dir" "$agents_dir" "$HOME/.local/share/onevoke/kanban-web"
for legacy_command in codex-review.sh claude-review.sh grok-review.sh; do
  target="$bin_dir/$legacy_command"
  if [ -d "$target" ]; then
    if [ "$onevoke_zh" -eq 1 ]; then
      printf '%s\n' "错误: 旧版安装目标是目录: $target" >&2
    else
      printf '%s\n' "error: legacy installation target is a directory: $target" >&2
    fi
    exit 1
  fi
  if [ -e "$target" ] || [ -L "$target" ]; then
    legacy_review_commands="${legacy_review_commands}${legacy_review_commands:+ }$legacy_command"
  fi
done

if [ -n "$legacy_review_commands" ]; then
  if [ "$onevoke_zh" -eq 1 ]; then
    printf '%s\n' \
      "检测到已退役的 Reviewer 脚本:" \
      "  $legacy_review_commands" \
      "审核入口现已统一为 onevoke-review.sh." \
      >&2
    printf '%s' "是否删除这些旧脚本? [y/N] " >&2
  else
    printf '%s\n' \
      "Retired reviewer scripts were detected:" \
      "  $legacy_review_commands" \
      "The review entry point is now unified as onevoke-review.sh." \
      >&2
    printf '%s' "Delete these legacy scripts? [y/N] " >&2
  fi
  legacy_answer=
  if IFS= read -r legacy_answer; then
    :
  fi
  if [ ! -t 0 ]; then
    printf '\n' >&2
  fi
  case "$legacy_answer" in
    y|Y|yes|YES|Yes|是)
      remove_legacy_reviews=1
      ;;
    *)
      if [ "$onevoke_zh" -eq 1 ]; then
        printf '%s\n' "已保留旧 Reviewer 脚本." >&2
      else
        printf '%s\n' "Legacy reviewer scripts were kept." >&2
      fi
      ;;
  esac
fi

install_payloads "$bin_dir" "$agents_dir" "$HOME/.local/share/onevoke/kanban-web"

if [ "$remove_legacy_reviews" -eq 1 ]; then
  if [ ! -x "$bin_dir/onevoke-review.sh" ]; then
    if [ "$onevoke_zh" -eq 1 ]; then
      printf '%s\n' "错误: 新审核入口不可执行, 已保留旧 Reviewer 脚本: $bin_dir/onevoke-review.sh" >&2
    else
      printf '%s\n' "error: new review entry is not executable; legacy reviewer scripts were kept: $bin_dir/onevoke-review.sh" >&2
    fi
    exit 1
  fi
  for legacy_command in $legacy_review_commands; do
    rm -f "$bin_dir/$legacy_command"
  done
  if [ "$onevoke_zh" -eq 1 ]; then
    printf '%s\n' "已删除旧 Reviewer 脚本." >&2
  else
    printf '%s\n' "Legacy reviewer scripts were removed." >&2
  fi
fi

print_installed

# 工具包文件安装已完成. welcome (含可选 MemSearch 安装) 失败不得回滚或
# 把本脚本变成失败退出; MemSearch 出错时 welcome 内会提示用户自行安装.
if [ -n "$onevoke_lang" ]; then
  set -- --lang "$onevoke_lang"
fi
if ! "$bin_dir/onevoke" "$@" welcome; then
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
