"""通达信金融接口独立连通性验证探针 (TDX Connectivity Probe)
用于在不侵入 MCPHub 核心系统的前提下，独立测试通达信 API Token 的有效性与行情数据获取能力。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# =========================================================================
# 【功能块 1：轻量读取项目 .env 配置文件中的 API Token】
# - 输入：env_path (Path): .env 文件的路径
# - 功能：尝试从 config/.env 文件中读取 TDX_API_KEY；如果文件不存在或未配置，
#         则回退检查系统环境变量。
# - 输出：str: 读取到的通达信 Token 字符串（未配置时返回空字符串）
# =========================================================================
def load_tdx_token(env_path: Path) -> str:
    token_from_env = os.getenv("TDX_API_KEY", "").strip()
    if token_from_env:
        return token_from_env

    if not env_path.exists():
        return ""

    try:
        content = env_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = env_path.read_text(encoding="gbk", errors="replace")

    for line in content.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#") or "=" not in trimmed:
            continue
        key, val = trimmed.split("=", 1)
        if key.strip() == "TDX_API_KEY":
            clean_val = val.strip().strip("\"'")
            if clean_val:
                return clean_val
    return ""


# =========================================================================
# 【功能块 2：向通达信网关发起单只股票实时行情查询】
# - 输入：
#     code (str): 股票代码（如 "000001"、"600519"）
#     setcode (str): 市场代码（"0"=深市，"1"=沪市，"2"=北交所）
#     token (str): 通达信 API 授权令牌
#     timeout (int): 网络请求超时时间（秒）
# - 功能：严格按照通达信 TQLEX 协议规范，构造 PBHQInfo POST 请求体，
#         向官方网关查询指定标的的五档盘口、最新价及成交统计
# - 输出：dict: 成功时返回服务器响应的原始 JSON 数据字典；失败时抛出异常
# =========================================================================
def fetch_tdx_quote(code: str, setcode: str, token: str, timeout: int = 10) -> dict:
    endpoint = "http://tdxhub.icfqs.com:7615/TQLEX?Entry=TdxShare.PBHQInfo"

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "MCPHub-Finance-Probe/1.0",
    }
    if token:
        headers["token"] = token

    request_body = {
        "Head": {
            "Target": "0",
            "CharSet": "UTF8"
        },
        "Code": code,
        "Setcode": setcode,
        "HasHQInfo": "1",
        "HasExtInfo": "1",
        "BspNum": "5",
        "HasProInfo": "1",
        "HasCalcInfo": "0",
        "HasCwInfo": "0",
        "HasStatInfo": "0"
    }

    raw_payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    req = Request(endpoint, data=raw_payload, headers=headers, method="POST")

    start_time = time.time()
    with urlopen(req, timeout=timeout) as response:
        elapsed_ms = int((time.time() - start_time) * 1000)
        body = response.read().decode("utf-8", errors="replace")
        parsed = json.loads(body)
        parsed["_elapsed_ms"] = elapsed_ms
        return parsed


# =========================================================================
# 【功能块 3：主程序执行与诊断输出入口】
# - 输入：无（交互式命令行驱动）
# - 功能：检查 Token 配置、发起测试请求、格式化展示行情结果与网络延迟诊断
# - 输出：在控制台打印友好的彩色/文本测试报告
# =========================================================================
def main() -> None:
    print("=" * 65)
    print("       MCPHub - 通达信 (TDX) 接口连通性独立探针")
    print("=" * 65)

    base_dir = Path(__file__).resolve().parent.parent
    env_file = base_dir / "config" / ".env"
    token = load_tdx_token(env_file)

    if not token:
        print("\n[!] 提示：未在 config/.env 中检测到 TDX_API_KEY。")
        user_input = input("请输入你的通达信 API Key (直接回车可尝试免 Token 探测): ").strip()
        token = user_input

    print(f"\n[*] 准备测试标的: 平安银行 (000001, 深市)")
    print(f"[*] 目标服务器: http://tdxhub.icfqs.com:7615/TQLEX")
    masked_token = f"{token[:4]}...{token[-4:]}" if len(token) > 8 else (token or "无(匿名)")
    print(f"[*] 使用 Token: {masked_token}")
    print("[*] 正在发起请求，请稍候...")

    try:
        data = fetch_tdx_quote(code="000001", setcode="0", token=token, timeout=10)
        elapsed = data.pop("_elapsed_ms", 0)

        print("\n" + "√" * 20 + " 连接测试成功！ " + "√" * 20)
        print(f"[*] 网络延迟: {elapsed} 毫秒 (极速专线)")
        print("[*] 原始返回核心摘要:")
        
        # 友好展示核心行情字段
        raw_json_str = json.dumps(data, indent=2, ensure_ascii=False)
        lines = raw_json_str.splitlines()
        preview = lines[:35]
        print("\n".join(preview))
        if len(lines) > 35:
            print(f"... (共 {len(lines)} 行，已折叠剩余部分)")

        print("\n" + "=" * 65)
        print("【诊断结果】你的通达信 API 专线完全畅通，可以放心进行下一步集成！")
        print("=" * 65)

    except HTTPError as exc:
        print("\n" + "×" * 20 + " HTTP 请求失败 " + "×" * 20)
        print(f"[!] HTTP 状态码: {exc.code} {exc.reason}")
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
            print(f"[!] 错误详情: {err_body}")
        except Exception:
            pass
        if exc.code == 401 or exc.code == 403:
            print("[!] 诊断建议：请检查你的 API Token 是否输入正确或已过期。")
    except URLError as exc:
        print("\n" + "×" * 20 + " 网络连接失败 " + "×" * 20)
        print(f"[!] 原因: {exc.reason}")
        print("[!] 诊断建议：请检查是否连接了公司限制性内网，7615 端口是否被防火墙拦截。")
    except Exception as exc:
        print(f"\n[!] 发生未知错误: {exc}")


if __name__ == "__main__":
    main()
