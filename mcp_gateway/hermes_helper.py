# -*- coding: utf-8 -*-
"""MCPHub 与 Hermes Desktop 自动挂载与配置辅助工具.

自动检测当前系统的 Python 绝对路径及 MCPHub 项目路径，
生成针对 Hermes Desktop 的无歧义配置片段，并支持一键检测和安全注册。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# =========================================================================
# 【功能块：路径解析与配置片段生成器】
# - 输入：无
# - 功能：动态获取本机的 python.exe 真实绝对路径、mcp_gateway 包路径
#         与 gateway.json 路径，生成零占位符、可直接使用的 YAML 与 JSON 配置
# - 输出：dict: 包含各种格式的配置片段与检测到的路径信息
# =========================================================================
def generate_hermes_snippets() -> dict[str, str]:
    python_exe = sys.executable
    project_root = Path(__file__).resolve().parent.parent
    gateway_pkg = (project_root / "mcp_gateway").resolve()
    gateway_cfg = (project_root / "config" / "gateway.json").resolve()

    yaml_snippet = (
        f"MCPHub:\n"
        f"  command: {python_exe}\n"
        f"  args:\n"
        f"    - {gateway_pkg}\n"
        f"    - --config\n"
        f"    - {gateway_cfg}\n"
        f"  env:\n"
        f"    PYTHONUTF8: \"1\"\n"
        f"  enabled: true\n"
    )

    json_snippet = json.dumps({
        "MCPHub": {
            "command": str(python_exe),
            "args": [
                str(gateway_pkg),
                "--config",
                str(gateway_cfg)
            ],
            "env": {
                "PYTHONUTF8": "1"
            },
            "enabled": True
        }
    }, indent=2, ensure_ascii=False)

    return {
        "python_exe": str(python_exe),
        "gateway_pkg": str(gateway_pkg),
        "gateway_cfg": str(gateway_cfg),
        "yaml": yaml_snippet,
        "json": json_snippet,
    }


# =========================================================================
# 【功能块：Hermes Desktop 用户配置文件路径探测器】
# - 输入：无
# - 功能：在当前用户目录下探测 Hermes 常用配置文件存放路径
# - 输出：Path | None: 若检测到存在的配置文件则返回路径，否则返回 None
# =========================================================================
def detect_hermes_config_file() -> Path | None:
    user_home = Path.home()
    app_data = Path(os.environ.get("APPDATA", user_home / "AppData" / "Roaming"))
    candidates = [
        user_home / ".hermes" / "config.yaml",
        user_home / ".hermes" / "config.json",
        app_data / "Hermes" / "config.yaml",
        app_data / "Hermes" / "config.json",
        app_data / "hermes-desktop" / "config.yaml",
        app_data / "hermes-desktop" / "config.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


# =========================================================================
# 【功能块：命令行交互展示与一键安装】
# - 输入：命令行参数（--show 或 --auto）
# - 功能：输出当前电脑实测真实路径的 Hermes 配置代码，或尝试安全自动挂载
# - 输出：控制台友好输出
# =========================================================================
def main() -> None:
    snippets = generate_hermes_snippets()

    print("=" * 70)
    print("      MCPHub × Hermes Desktop 专属无歧义配置生成器")
    print("=" * 70)
    print("\n[系统实测环境检测]")
    print(f"  * Python 解释器绝对路径 : {snippets['python_exe']}")
    print(f"  * MCP 服务程序入口路径  : {snippets['gateway_pkg']}")
    print(f"  * 网关主配置文件路径    : {snippets['gateway_cfg']}")

    detected = detect_hermes_config_file()
    if detected:
        print(f"\n[✓ 检测到本地 Hermes 配置文件] : {detected}")
    else:
        print("\n[提示] 未在默认路径检测到 Hermes 配置文件（可能尚未安装或使用自定义路径）。")

    print("\n" + "-" * 70)
    print("【方式 A：Hermes 配置文件片段 (config.yaml，最推荐，零歧义复制)】")
    print("-" * 70)
    print(snippets["yaml"])

    print("-" * 70)
    print("【方式 B：Hermes 桌面端表单逐项填写参数（切勿合在一行！）】")
    print("-" * 70)
    print(f"  Type      : stdio")
    print(f"  Command   : {snippets['python_exe']}")
    print(f"  Arguments（逐行添加 3 个独立参数）：")
    print(f"    1) {snippets['gateway_pkg']}")
    print(f"    2) --config")
    print(f"    3) {snippets['gateway_cfg']}")
    print(f"  Environment : PYTHONUTF8 = 1")
    print("=" * 70)


if __name__ == "__main__":
    main()
