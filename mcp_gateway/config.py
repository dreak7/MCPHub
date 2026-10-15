"""Configuration and lightweight .env loading for the gateway."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines without overwriting a real environment value."""
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid .env line {number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            raise ConfigError(f"Invalid .env line {number}: empty key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc.msg} (line {exc.lineno})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root must be an object: {path}")
    return data


def load_config(path: str | Path) -> dict[str, Any]:
    gateway_path = Path(path).expanduser().resolve()
    gateway = _read_json(gateway_path)
    root = gateway_path.parent
    load_dotenv(root / ".env")

    merged: dict[str, Any] = {"gateway_path": gateway_path, **gateway, "capabilities": {}}
    files = gateway.get("provider_config_files", [])
    if not isinstance(files, list) or not files:
        raise ConfigError("gateway.provider_config_files must be a non-empty array")
    for relative_path in files:
        if not isinstance(relative_path, str):
            raise ConfigError("provider_config_files entries must be strings")
        source = _read_json(root / relative_path)
        capabilities = source.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise ConfigError(f"capabilities must be an object: {relative_path}")
        for name, definition in capabilities.items():
            if name in merged["capabilities"]:
                raise ConfigError(f"Duplicate capability {name!r}")
            merged["capabilities"][name] = definition
    for required in ("web_search", "web_extract"):
        if required not in merged["capabilities"]:
            raise ConfigError(f"No {required} capability configured")
    return merged


def resolve_project_path(config: dict[str, Any], setting: str, default: str) -> Path:
    value = config.get(setting, default)
    if not isinstance(value, str):
        raise ConfigError(f"{setting} must be a string")
    return (config["gateway_path"].parent / value).resolve()
