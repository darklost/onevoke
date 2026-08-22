#!/usr/bin/env python3

"""Onevoke global configuration shared by its command-line tools."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
EXECUTION_AGENTS = ("codex", "claude", "grok")
REVIEW_AGENTS = ("codex", "claude", "grok")
REVIEW_ROLES = ("PM", "CSA", "Hacker", "QA")
REVIEW_STAGE_MODES = ("auto", "skip", "required")
LAUNCHERS = ("tmux", "tmux-session", "foreground")
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
    if not arguments:
        return
    value = arguments[1] if arguments[0] == "--lang" and len(arguments) > 1 else None
    if arguments[0].startswith("--lang="):
        value = arguments[0].partition("=")[2]
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


def config_path() -> Path:
    override = os.environ.get("ONEVOKE_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "onevoke" / "config.json"


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
        "launcher": "tmux",
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
        os.chmod(temporary, 0o600)
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
