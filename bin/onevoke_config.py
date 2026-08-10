#!/usr/bin/env python3

"""Onevoke global configuration shared by its command-line tools."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
EXECUTION_AGENTS = ("codex", "claude", "grok")
REVIEW_AGENTS = ("codex", "grok")
REVIEW_ROLES = ("PM", "CSA", "Hacker", "QA")
LAUNCHERS = ("tmux", "foreground")


class ConfigError(Exception):
    """Raised when the Onevoke configuration is unreadable or invalid."""


def config_path() -> Path:
    override = os.environ.get("ONEVOKE_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "onevoke" / "config.json"


def default_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "welcome_complete": False,
        "kanban_agent": "codex",
        "launcher": "tmux",
        "reviewers": {role: "codex" for role in REVIEW_ROLES},
        "memsearch": {"enabled": False},
    }


def _validate_choice(value: object, choices: tuple[str, ...], name: str) -> str:
    if not isinstance(value, str) or value not in choices:
        expected = ", ".join(choices)
        raise ConfigError(f"{name} 必须是以下取值之一: {expected}")
    return value


def validate_config(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是 JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(
            f"不支持的 schema_version: {raw.get('schema_version')!r}; "
            f"当前只支持 {SCHEMA_VERSION}"
        )

    welcome_complete = raw.get("welcome_complete")
    if not isinstance(welcome_complete, bool):
        raise ConfigError("welcome_complete 必须是 boolean")
    kanban_agent = _validate_choice(raw.get("kanban_agent"), EXECUTION_AGENTS, "kanban_agent")
    launcher = _validate_choice(raw.get("launcher"), LAUNCHERS, "launcher")

    reviewers = raw.get("reviewers")
    if not isinstance(reviewers, dict):
        raise ConfigError("reviewers 必须是 JSON object")
    validated_reviewers = {
        role: _validate_choice(reviewers.get(role), REVIEW_AGENTS, f"reviewers.{role}")
        for role in REVIEW_ROLES
    }

    memsearch = raw.get("memsearch")
    if not isinstance(memsearch, dict) or not isinstance(memsearch.get("enabled"), bool):
        raise ConfigError("memsearch.enabled 必须是 boolean")

    return {
        "schema_version": SCHEMA_VERSION,
        "welcome_complete": welcome_complete,
        "kanban_agent": kanban_agent,
        "launcher": launcher,
        "reviewers": validated_reviewers,
        "memsearch": {"enabled": memsearch["enabled"]},
    }


def load_config(*, missing_ok: bool = True) -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        if missing_ok:
            return default_config()
        raise ConfigError(f"配置不存在: {path}")
    if not path.is_file():
        raise ConfigError(f"配置不是普通文件: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"读取配置失败: {path}: {error}") from error
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
