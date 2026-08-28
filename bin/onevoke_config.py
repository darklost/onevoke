#!/usr/bin/env python3

"""Onevoke configuration and install-context paths shared by its tools."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from onevoke_fs import (
    UnsafePathError,
    ensure_directory_path_nofollow,
    ensure_inherited_directory_path_nofollow,
    exclusive_file_lock,
    is_reparse_point,
    open_append_file_nofollow,
    open_private_regular_file_if_exists_nofollow,
    open_regular_file_if_exists_nofollow,
    tighten_private_file_permissions,
    tighten_private_open_file_permissions,
    write_text_atomic_nofollow,
)


def configure_stdio() -> None:
    """Windows 的重定向流常沿用系统代码页；Onevoke 的 CLI 契约统一使用 UTF-8。"""
    if os.name != "nt":
        return
    # Onevoke 经常在待处理仓库内运行。关闭 Windows 对当前目录的隐式可执行
    # 文件搜索，避免仓库中的 git.exe/agent.exe 在参数校验前被执行；显式 PATH
    # 中的工具仍照常解析。
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # 测试替换的内存流或已关闭流可能不支持重新配置；调用方仍可正常工作.
            pass


configure_stdio()


SCHEMA_VERSION = 1
PROJECT_INSTALL_DIRNAME = ".onevoke"
PROJECT_GIT_EXCLUDE_PATTERN = "/.onevoke/"
InstallMode = Literal["global", "project"]
EXECUTION_AGENTS = ("codex", "claude", "grok")
REVIEW_AGENTS = ("codex", "claude", "grok")
REVIEW_ROLES = ("PM", "CSA", "Hacker", "QA")
REVIEW_STAGE_MODES = ("auto", "skip", "required")
LAUNCHERS = ("auto", "tmux", "tmux-session", "herdr", "foreground", "console")
LANGUAGES = ("cn", "en")
# model 允许空字符串, 表示用对应 CLI 自己的默认模型.
KANBAN_MODEL_DEFAULTS = {
    "codex": {"model": "gpt-5.6-sol", "large_effort": "high", "small_effort": "medium"},
    "claude": {"model": "opus", "large_effort": "high", "small_effort": "medium"},
    "grok": {"model": "", "large_effort": "xhigh", "small_effort": "high"},
}
REVIEW_MODEL_DEFAULTS = {
    "codex": {"model": "gpt-5.6-sol", "effort": "high"},
    "claude": {"model": "opus", "effort": "high"},
    "grok": {"model": "", "effort": "high"},
}
ARGPARSE_ZH = {
    "usage: ": "用法: ",
    "positional arguments": "位置参数",
    "optional arguments": "可选参数",
    "options": "选项",
    "show this help message and exit": "显示帮助并退出",
    "unrecognized arguments: %s": "无法识别的参数: %s",
    "the following arguments are required: %s": "缺少以下必需参数: %s",
    "expected one argument": "需要一个参数",
    "expected at least one argument": "至少需要一个参数",
    "ignored explicit argument %r": "不接受显式参数 %r",
    "invalid choice: %(value)r (choose from %(choices)s)": (
        "无效选择: %(value)r (可选: %(choices)s)"
    ),
    "invalid %(type)s value: %(value)r": "无效 %(type)s 值: %(value)r",
    "%(prog)s: error: %(message)s\n": "%(prog)s: 错误: %(message)s\n",
}


_cli_language_override: str | None = None
_config_language: str | None = None


def apply_language_argument(arguments: list[str]) -> None:
    global _cli_language_override
    os.environ.pop("ONEVOKE_LANG_CLI", None)
    value = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--lang" and index + 1 < len(arguments):
            value = arguments[index + 1]
            break
        if argument.startswith("--lang="):
            value = argument.partition("=")[2]
            break
        index += 1
    if value in LANGUAGES:
        _cli_language_override = value
        os.environ["ONEVOKE_LANG"] = value
        os.environ["ONEVOKE_LANG_CLI"] = "1"


def bind_config_language(config: dict[str, Any] | None) -> None:
    global _config_language
    if not config:
        _config_language = None
        return
    language = config.get("language")
    _config_language = language if language in LANGUAGES else None


def _explicit_config_language(raw: object) -> str | None:
    if not isinstance(raw, dict) or not raw.get("welcome_complete"):
        return None
    if "language" not in raw:
        return None
    language = raw.get("language")
    return language if language in LANGUAGES else None


def configured_language() -> str | None:
    """Return explicitly saved language from a valid config.json, or None."""
    try:
        path = config_path()
        if os.name == "nt":
            absolute = Path(os.path.abspath(os.fspath(path)))
            anchor = Path(absolute.anchor)
            with open_regular_file_if_exists_nofollow(
                anchor, absolute
            ) as stream:
                if stream is None:
                    return None
                raw = json.loads(stream.read().decode("utf-8"))
        else:
            if not path.is_file():
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
        validate_config(raw)
        return _explicit_config_language(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ConfigError):
        return None


def resolve_language() -> str:
    if _cli_language_override in LANGUAGES:
        return _cli_language_override
    if _config_language in LANGUAGES:
        return _config_language
    locale = _effective_locale().lower()
    if locale.startswith("en"):
        return "en"
    if locale:
        return "cn"
    return "cn"


def _effective_locale() -> str:
    return next(
        (
            os.environ[name]
            for name in ("ONEVOKE_LANG", "LC_ALL", "LC_MESSAGES", "LANG")
            if os.environ.get(name)
        ),
        "",
    )


def bind_effective_language() -> None:
    language = configured_language()
    if language is None:
        bind_config_language(None)
    else:
        bind_config_language({"language": language})


def language_is_chinese() -> bool:
    return resolve_language() == "cn"


def language_text(chinese: str, english: str) -> str:
    return chinese if language_is_chinese() else english


class LocalizedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if language_is_chinese():
            names = {
                "arguments": "参数",
                "command": "命令",
                "project": "项目",
                "slug": "slug",
                "state": "状态",
                "task": "任务",
                "title": "标题",
                "type": "类型",
            }
            if message.startswith("argument "):
                name, separator, detail = message.removeprefix("argument ").partition(":")
                message = f"参数 {names.get(name, name)}{separator}{detail}"
            required = "缺少以下必需参数: "
            if message.startswith(required):
                values = message.removeprefix(required).split(", ")
                message = required + ", ".join(names.get(value, value) for value in values)
        super().error(message)


class ConfigError(Exception):
    """Raised when the Onevoke configuration is unreadable or invalid."""


@dataclass(frozen=True)
class InstallPaths:
    """当前安装作用域的公共路径.

    ``global`` 映射到用户 HOME 下的既有布局; ``project`` 映射到 Git 主
    worktree 的 ``.onevoke/``. 源码树直接运行属于 ``global``, 即使仓库
    根同时含 ``bin/`` 与 ``rules/``.
    """

    mode: InstallMode
    config_path: Path
    rules_dir: Path
    bin_dir: Path
    share_dir: Path
    project_root: Path | None = None
    install_root: Path | None = None


def _lexical_absolute(path: Path) -> Path:
    """生成绝对路径; 不跟随符号链接或 Windows reparse point."""
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _unsafe_path_error(path: Path) -> ConfigError:
    return ConfigError(
        language_text(
            f"路径分量不得是符号链接/重解析点: {path}",
            f"path component must not be a symlink/reparse point: {path}",
        )
    )


def _ensure_windows_path_nofollow_safe(path: Path) -> Path:
    """Windows 上拒绝路径自身或任一已存在祖先中的 reparse point."""
    absolute = _lexical_absolute(path)
    parts = absolute.parts
    if not parts:
        raise ConfigError(
            language_text(f"无效安装路径: {path}", f"invalid install path: {path}")
        )
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        if not os.path.lexists(current):
            break
        if is_reparse_point(current):
            raise _unsafe_path_error(current)
    return absolute


def _reject_leaf_reparse(path: Path) -> None:
    if os.path.lexists(path) and is_reparse_point(path):
        raise _unsafe_path_error(path)


def _same_dir_name(left: str, right: str) -> bool:
    if os.name == "nt":
        return os.path.normcase(left) == os.path.normcase(right)
    return left == right


_GIT_OVERRIDE_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


def _git_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in _GIT_OVERRIDE_VARS:
        env.pop(name, None)
    return env


def _git_main_worktree(directory: Path) -> Path | None:
    target = _lexical_absolute(directory)
    if not os.path.isdir(os.fspath(target)):
        return None
    try:
        git = subprocess.run(
            ["git", "-C", str(target), "worktree", "list", "--porcelain"],
            text=True,
            capture_output=True,
            check=False,
            env=_git_subprocess_env(),
        )
    except OSError:
        return None
    if git.returncode != 0:
        return None
    for line in git.stdout.splitlines():
        if line.startswith("worktree "):
            return _lexical_absolute(Path(line.removeprefix("worktree ")))
    return None


def _global_install_paths() -> InstallPaths:
    home = Path.home()
    return InstallPaths(
        mode="global",
        config_path=home / ".config" / "onevoke" / "config.json",
        rules_dir=home / ".agents",
        bin_dir=home / ".local" / "bin",
        share_dir=home / ".local" / "share" / "onevoke",
    )


def _project_install_paths(project_root: Path) -> InstallPaths:
    install_root = project_root / PROJECT_INSTALL_DIRNAME
    return InstallPaths(
        mode="project",
        config_path=install_root / "config.json",
        rules_dir=install_root / "rules",
        bin_dir=install_root / "bin",
        share_dir=install_root / "share",
        project_root=project_root,
        install_root=install_root,
    )


def install_paths(*, entry: Path | None = None) -> InstallPaths:
    """按当前入口解析全局或项目安装路径.

    ``entry`` 默认为本模块文件. 仅当入口位于名为 ``.onevoke/bin/`` 的
    目录时进入项目模式; 源码树的 ``bin/`` 不会被误判.
    """
    source = _lexical_absolute(entry if entry is not None else Path(__file__))
    bin_dir = source.parent
    install_root = bin_dir.parent
    if not _same_dir_name(bin_dir.name, "bin") or not _same_dir_name(
        install_root.name, PROJECT_INSTALL_DIRNAME
    ):
        return _global_install_paths()
    if os.name == "nt":
        _ensure_windows_path_nofollow_safe(bin_dir)
    else:
        _reject_leaf_reparse(install_root)
        _reject_leaf_reparse(bin_dir)
    parent = install_root.parent
    main = _git_main_worktree(parent)
    if main is None:
        raise ConfigError(
            language_text(
                f"项目不是 Git 仓库: {parent}",
                f"project is not a Git repository: {parent}",
            )
        )
    paths = _project_install_paths(main)
    if os.name == "nt":
        _ensure_windows_path_nofollow_safe(paths.bin_dir)
    else:
        if paths.install_root is not None:
            _reject_leaf_reparse(paths.install_root)
        _reject_leaf_reparse(paths.bin_dir)
    return paths


def project_install_paths(project: Path) -> InstallPaths:
    """把用户给出的项目目录归一到 Git 主 worktree 下的项目安装路径."""
    candidate = _lexical_absolute(project)
    if os.name == "nt":
        _ensure_windows_path_nofollow_safe(candidate)
    else:
        _reject_leaf_reparse(candidate)
    if not candidate.is_dir():
        raise ConfigError(
            language_text(
                f"项目目录不存在: {candidate}",
                f"project directory does not exist: {candidate}",
            )
        )
    main = _git_main_worktree(candidate)
    if main is None:
        raise ConfigError(
            language_text(
                f"项目不是 Git 仓库: {candidate}",
                f"project is not a Git repository: {candidate}",
            )
        )
    if os.name == "nt":
        _ensure_windows_path_nofollow_safe(main)
    return _project_install_paths(main)


def _git_exclude_path(git_root: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "--git-path", "info/exclude"],
            text=True,
            capture_output=True,
            check=False,
            env=_git_subprocess_env(),
        )
    except OSError as error:
        raise ConfigError(
            language_text("无法定位 Git info/exclude", "Cannot locate Git info/exclude")
        ) from error
    if result.returncode != 0:
        raise ConfigError(
            language_text("无法定位 Git info/exclude", "Cannot locate Git info/exclude")
        )
    exclude = Path(result.stdout.strip())
    if not exclude.is_absolute():
        exclude = git_root / exclude
    return _lexical_absolute(exclude)


def _append_git_exclude_pattern(git_root: Path, pattern: str) -> Path:
    exclude = _git_exclude_path(git_root)
    if os.name == "nt":
        _ensure_windows_path_nofollow_safe(exclude.parent)
        _reject_leaf_reparse(exclude)
        ensure_inherited_directory_path_nofollow(exclude.parent)
        open_root = Path(exclude.anchor)
    else:
        _reject_leaf_reparse(git_root)
        current = git_root
        try:
            relative_parent = exclude.parent.relative_to(git_root)
        except ValueError as error:
            raise ConfigError(
                language_text(
                    f"Git exclude 不在仓库内: {exclude}",
                    f"git exclude is outside the repository: {exclude}",
                )
            ) from error
        for part in relative_parent.parts:
            current /= part
            _reject_leaf_reparse(current)
        _reject_leaf_reparse(exclude)
        open_root = git_root
    try:
        with open_append_file_nofollow(open_root, exclude) as file:
            with exclusive_file_lock(file):
                file.seek(0)
                existing = file.read().decode("utf-8")
                if pattern not in existing.splitlines():
                    addition = (
                        ("\n" if existing and not existing.endswith("\n") else "")
                        + pattern
                        + "\n"
                    )
                    file.write(addition.encode("utf-8"))
    except (UnsafePathError, OSError, UnicodeError) as error:
        raise ConfigError(
            language_text(
                f"更新 Git exclude 失败: {exclude}: {error}",
                f"failed to update git exclude: {exclude}: {error}",
            )
        ) from error
    return exclude


def ensure_project_git_exclude(project: Path) -> Path:
    """幂等把 ``/.onevoke/`` 写入仓库本地 Git exclude, 并保持既有权限."""
    paths = project_install_paths(project)
    if paths.project_root is None:
        raise ConfigError(
            language_text(
                "项目安装路径缺少主 worktree",
                "project install paths are missing the main worktree",
            )
        )
    return _append_git_exclude_pattern(paths.project_root, PROJECT_GIT_EXCLUDE_PATTERN)


def config_path() -> Path:
    override = os.environ.get("ONEVOKE_CONFIG")
    if override:
        return Path(override).expanduser()
    return install_paths().config_path


def default_models() -> dict[str, Any]:
    return {
        "kanban": {agent: dict(entry) for agent, entry in KANBAN_MODEL_DEFAULTS.items()},
        "review": {agent: dict(entry) for agent, entry in REVIEW_MODEL_DEFAULTS.items()},
    }


def default_review_stages() -> dict[str, str]:
    return {role: "auto" for role in REVIEW_ROLES}


def default_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "welcome_complete": False,
        "kanban_agent": "codex",
        "launcher": "console" if os.name == "nt" else "auto",
        "reviewers": {role: "codex" for role in REVIEW_ROLES},
        "review_stages": default_review_stages(),
        "models": default_models(),
        "memsearch": {"enabled": False},
        "language": "cn",
    }


def _validate_choice(value: object, choices: tuple[str, ...], name: str) -> str:
    if not isinstance(value, str) or value not in choices:
        expected = ", ".join(choices)
        raise ConfigError(
            language_text(
                f"{name} 必须是以下取值之一: {expected}",
                f"{name} must be one of: {expected}",
            )
        )
    return value


def _validate_review_stages(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ConfigError(language_text(
            "review_stages 必须是 JSON object",
            "review_stages must be a JSON object",
        ))
    stages = default_review_stages()
    unknown = set(raw) - set(REVIEW_ROLES)
    if unknown:
        raise ConfigError(language_text(
            f"review_stages 含未知角色: {', '.join(sorted(unknown))}",
            f"review_stages has unknown roles: {', '.join(sorted(unknown))}",
        ))
    for role in REVIEW_ROLES:
        if role not in raw:
            continue
        stages[role] = _validate_choice(raw[role], REVIEW_STAGE_MODES, f"review_stages.{role}")
    return stages


def _validate_models(raw: object) -> dict[str, Any]:
    """校验 models 段; 缺失的层级和字段用默认值补齐, 未知键和显式 null 一律拒绝."""
    models = default_models()
    if not isinstance(raw, dict):
        raise ConfigError(language_text("models 必须是 JSON object", "models must be a JSON object"))
    unknown = set(raw) - {"kanban", "review"}
    if unknown:
        raise ConfigError(language_text(
            f"models 含未知键: {', '.join(sorted(unknown))}",
            f"models has unknown keys: {', '.join(sorted(unknown))}",
        ))
    for section, agents in (("kanban", EXECUTION_AGENTS), ("review", REVIEW_AGENTS)):
        if section not in raw:
            continue
        provided = raw[section]
        if not isinstance(provided, dict):
            raise ConfigError(language_text(
                f"models.{section} 必须是 JSON object", f"models.{section} must be a JSON object"
            ))
        unknown = set(provided) - set(agents)
        if unknown:
            raise ConfigError(language_text(
                f"models.{section} 含未知 agent: {', '.join(sorted(unknown))}",
                f"models.{section} has unknown agents: {', '.join(sorted(unknown))}",
            ))
        for agent, entry in provided.items():
            if not isinstance(entry, dict):
                raise ConfigError(language_text(
                    f"models.{section}.{agent} 必须是 JSON object",
                    f"models.{section}.{agent} must be a JSON object",
                ))
            fields = models[section][agent]
            unknown = set(entry) - set(fields)
            if unknown:
                raise ConfigError(language_text(
                    f"models.{section}.{agent} 含未知字段: {', '.join(sorted(unknown))}",
                    f"models.{section}.{agent} has unknown fields: {', '.join(sorted(unknown))}",
                ))
            for field, value in entry.items():
                if not isinstance(value, str) or (field != "model" and not value.strip()):
                    raise ConfigError(language_text(
                        f"models.{section}.{agent}.{field} 必须是{'字符串' if field == 'model' else '非空字符串'}",
                        f"models.{section}.{agent}.{field} must be a "
                        f"{'string' if field == 'model' else 'non-empty string'}",
                    ))
                # 值会拼进命令行并经 review-model 按行输出: 换行破坏两行协议,
                # NUL 使 subprocess 参数直接抛 ValueError.
                if any(banned in value for banned in ("\n", "\r", "\x00")):
                    raise ConfigError(language_text(
                        f"models.{section}.{agent}.{field} 不得包含换行或 NUL 字符",
                        f"models.{section}.{agent}.{field} must not contain line breaks or NUL",
                    ))
                fields[field] = value
    return models


def validate_config(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(language_text("配置根节点必须是 JSON object", "config root must be a JSON object"))
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(language_text(
            f"不支持的 schema_version: {raw.get('schema_version')!r}; 当前只支持 {SCHEMA_VERSION}",
            f"unsupported schema_version: {raw.get('schema_version')!r}; only {SCHEMA_VERSION} is supported",
        ))

    welcome_complete = raw.get("welcome_complete")
    if not isinstance(welcome_complete, bool):
        raise ConfigError(language_text("welcome_complete 必须是 boolean", "welcome_complete must be a boolean"))
    kanban_agent = _validate_choice(raw.get("kanban_agent"), EXECUTION_AGENTS, "kanban_agent")
    launcher = _validate_choice(raw.get("launcher"), LAUNCHERS, "launcher")

    reviewers = raw.get("reviewers")
    if not isinstance(reviewers, dict):
        raise ConfigError(language_text("reviewers 必须是 JSON object", "reviewers must be a JSON object"))
    validated_reviewers = {
        role: _validate_choice(reviewers.get(role), REVIEW_AGENTS, f"reviewers.{role}")
        for role in REVIEW_ROLES
    }

    review_stages = (
        _validate_review_stages(raw["review_stages"])
        if "review_stages" in raw
        else default_review_stages()
    )

    models = _validate_models(raw["models"]) if "models" in raw else default_models()

    memsearch = raw.get("memsearch")
    if not isinstance(memsearch, dict) or not isinstance(memsearch.get("enabled"), bool):
        raise ConfigError(language_text("memsearch.enabled 必须是 boolean", "memsearch.enabled must be a boolean"))

    language = _validate_choice(raw.get("language", "cn"), LANGUAGES, "language")

    return {
        "schema_version": SCHEMA_VERSION,
        "welcome_complete": welcome_complete,
        "kanban_agent": kanban_agent,
        "launcher": launcher,
        "reviewers": validated_reviewers,
        "review_stages": review_stages,
        "models": models,
        "memsearch": {"enabled": memsearch["enabled"]},
        "language": language,
    }


def load_config(*, missing_ok: bool = True) -> dict[str, Any]:
    path = config_path()
    if os.name == "nt":
        absolute = Path(os.path.abspath(os.fspath(path)))
        anchor = Path(absolute.anchor)
        try:
            with open_private_regular_file_if_exists_nofollow(
                anchor, absolute
            ) as stream:
                if stream is None:
                    if missing_ok:
                        return default_config()
                    raise ConfigError(language_text(
                        f"配置不存在: {path}",
                        f"config does not exist: {path}",
                    ))
                raw = json.loads(stream.read().decode("utf-8"))
                validated = validate_config(raw)
                try:
                    # 内容通过 schema 后再收紧读取所用的同一句柄;
                    # 无效配置不会产生 ACL 迁移副作用.
                    tighten_private_open_file_permissions(stream, absolute)
                except OSError as error:
                    raise ConfigError(language_text(
                        f"收紧配置文件权限失败: {path}: {error}",
                        f"failed to tighten config file permissions: {path}: {error}",
                    )) from error
                return validated
        except ConfigError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ConfigError(language_text(
                f"读取配置失败: {path}: {error}",
                f"failed to read config: {path}: {error}",
            )) from error

    if not path.exists():
        if missing_ok:
            return default_config()
        raise ConfigError(language_text(f"配置不存在: {path}", f"config does not exist: {path}"))
    if not path.is_file():
        raise ConfigError(language_text(f"配置不是普通文件: {path}", f"config is not a regular file: {path}"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(language_text(f"读取配置失败: {path}: {error}", f"failed to read config: {path}: {error}")) from error
    return validate_config(raw)


def effective_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return runtime values; unfinished welcome selections are not active."""
    loaded = load_config() if config is None else validate_config(config)
    if loaded["welcome_complete"]:
        return loaded
    return default_config()


def save_config(config: dict[str, Any]) -> Path:
    validated = validate_config(config)
    path = config_path()
    if os.name == "nt":
        absolute = Path(os.path.abspath(os.fspath(path)))
        anchor = Path(absolute.anchor)
        ensure_directory_path_nofollow(absolute.parent)
        payload = json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
        write_text_atomic_nofollow(anchor, absolute, payload, replace=True)
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(validated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        tighten_private_file_permissions(temporary)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def main(argv: list[str]) -> int:
    """查询入口, 目前只供 onevoke-review.sh 读取 review 模型配置."""
    apply_language_argument(argv)
    bind_effective_language()
    import argparse

    argparse._ = lambda message: language_text(ARGPARSE_ZH.get(message, message), message)
    parser = LocalizedArgumentParser(prog="onevoke_config.py")
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser(
        "review-model",
        help=language_text("输出两行: <model> 与 <effort>", "print two lines: <model> and <effort>"),
    )
    review.add_argument("agent", choices=REVIEW_AGENTS)
    stages = commands.add_parser(
        "review-stages",
        help=language_text(
            "输出四行: PM/CSA/Hacker/QA 的 auto|skip|required",
            "print four lines: auto|skip|required for PM/CSA/Hacker/QA",
        ),
    )
    commands.add_parser(
        "configured-language",
        help=language_text(
            "输出配置中的 language (cn|en)",
            "print configured language (cn|en)",
        ),
    )
    args = parser.parse_args(argv)
    if args.command == "review-model":
        entry = effective_config()["models"]["review"][args.agent]
        print(entry["model"])
        print(entry["effort"])
        return 0
    if args.command == "configured-language":
        language = configured_language()
        if language:
            print(language)
        return 0
    stages = effective_config()["review_stages"]
    for role in REVIEW_ROLES:
        print(stages[role])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ConfigError as error:
        print(error, file=sys.stderr)
        sys.exit(1)
