"""Configuration and lightweight .env loading for the gateway."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


# =========================================================================
# 【功能块：轻量级 .env 环境变量加载器】
# - 输入：path (Path): .env 文件的绝对路径
# - 功能：安全读取 KEY=VALUE 配置行（自动兼容 UTF-8 与本地 GBK 编码），
#         仅在环境变量中未定义该 KEY 时进行默认注入，不覆盖现有真实环境变量
# - 输出：无（原地注入 os.environ）
# =========================================================================
def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(encoding="gbk", errors="replace")
    for number, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
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


# =========================================================================
# 【功能块：网关自适应配置加载与能力合并】
# - 输入：path (str | Path): gateway.json 配置文件的路径
# - 功能：读取主网关配置并加载 .env 环境变量；支持通过通配符（如 providers/*.json）
#         或指定路径自适应扫描并合并所有提供商配置文件中的 capabilities 节点；
#         支持动态能力插拔，解除必须包含指定能力的硬编码限制。
# - 输出：merged (dict[str, Any]): 包含网关基础配置与全部已合并能力的配置字典
# =========================================================================
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
        # 支持通配符扫描（例如 providers/*.json）或直接指定单个文件
        if any(char in relative_path for char in "*?[]"):
            matched_paths = sorted(root.glob(relative_path))
        else:
            matched_paths = [root / relative_path]
        if not matched_paths:
            raise ConfigError(f"No configuration files matched: {relative_path}")
        for config_file in matched_paths:
            source = _read_json(config_file)
            capabilities = source.get("capabilities", {})
            if not isinstance(capabilities, dict):
                raise ConfigError(f"capabilities must be an object: {config_file.name}")
            for name, definition in capabilities.items():
                if name in merged["capabilities"]:
                    raise ConfigError(f"Duplicate capability {name!r}")
                merged["capabilities"][name] = definition
    if not merged["capabilities"]:
        raise ConfigError("No capabilities configured in provider config files")
    return merged


def resolve_project_path(config: dict[str, Any], setting: str, default: str) -> Path:
    value = config.get(setting, default)
    if not isinstance(value, str):
        raise ConfigError(f"{setting} must be a string")
    return (config["gateway_path"].parent / value).resolve()
