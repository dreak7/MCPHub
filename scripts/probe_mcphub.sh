#!/usr/bin/env bash
# =========================================================================
# 【功能块：MCPHub 运行环境与挂载健康状态通用探活脚本】
# - 输入：$1 (可选，Hermes profile 名称，缺省为 default)；
#         $HERMES_HOME_BASE (可选环境变量，优先级最高)
# - 功能：严格维持 Hermes Skill 外部调用参数契约不变的前提下，
#         自适应当前系统真实目录与安装盘符（自动兼容 C/D 等不同盘符），
#         检测 config.yaml、mcp-stderr.log 与工具缓存状态
# - 输出：标准输出保持原始格式（含 RESULT: PASS/FAIL），退出状态码 0 或 1
# =========================================================================
# 用法: bash probe_mcphub.sh [<profile>]   缺省 default
# 判据：只看「当日最后一条 MCPHub 启动记录之后」有无路径报错；历史失败只计 WARN（不参与判定）
set -u

# =========================================================================
# 【功能块 1：解析 Hermes 配置主目录与确定 Profile】
# - 输入：命令行参数 $1（Profile 名称，缺省为 default）、环境变量 $HERMES_HOME_BASE
# - 功能：自动获取系统用户主目录，自适应不同系统与用户环境（如 C:/Users/Timber 或 C:/Users/dreak），
#         定位 config.yaml 配置文件与 mcp-stderr.log 日志文件
# - 输出：HOME_DIR, CFG, LOG, SCHEMA 变量路径定义
# =========================================================================
AUTO_USER_HOME="${USERPROFILE:-${HOME:-C:/Users/Timber}}"
AUTO_USER_HOME="${AUTO_USER_HOME//\\//}"
BASE="${HERMES_HOME_BASE:-$AUTO_USER_HOME/AppData/Local/hermes}"

PROF="${1:-default}"
if [ "$PROF" = "default" ]; then
  HOME_DIR="$BASE"
else
  HOME_DIR="$BASE/profiles/$PROF"
fi
CFG="$HOME_DIR/config.yaml"
LOG="$HOME_DIR/logs/mcp-stderr.log"
SCHEMA="$HOME_DIR/cache/mcp_schema_cache.json"
FAIL=0

echo "== MCPHub 探活: profile=$PROF ($HOME_DIR) =="
if [ ! -f "$CFG" ]; then
  echo "FAIL  config.yaml 不存在: $CFG"
  exit 1
fi

# =========================================================================
# 【功能块 2：从 config.yaml 解析 MCPHub 网关脚本路径并核验其有效性】
# - 输入：$CFG (Hermes 的 config.yaml 文件路径)
# - 功能：使用 awk 提取 MCPHub 节点下的网关主路径，自动泛化匹配任意盘符（C:/D:/E:等）
#         与包含 mcp_gateway 的路径，并校验该目标路径在文件系统中是否真实存在
# - 输出：提取的网关绝对路径 $GW，若不存在则打印 FAIL 并标记 FAIL=1
# =========================================================================
GW=$(awk '
  /^[[:space:]]*MCPHub:/ { f=1; next }
  f && /^[[:space:]]*[a-zA-Z0-9_-]+:/ && !/^[[:space:]]*(command|args|env|enabled):/ { f=0 }
  f && (/- [a-zA-Z]:/ || /mcp_gateway/) { print; exit }
' "$CFG" | sed -e 's/^[[:space:]]*- //' -e 's/^["'"'"']//' -e 's/["'"'"']$//' | tr -d '\r')

# 将 Windows 反斜杠归一化为正斜杠，确保 Bash 路径检测正常
GW="${GW//\\//}"

if [ -z "${GW:-}" ]; then
  echo "FAIL  未能从 config.yaml 解析 MCPHub 网关路径"; FAIL=1
elif [ -e "$GW" ] || [ -e "$GW.py" ] || [ -d "$GW" ]; then
  echo "PASS  gateway 路径存在: $GW"
else
  echo "FAIL  gateway 路径不存在: $GW  （历史事故：MCPHub-V1.2 旧路径）"; FAIL=1
fi

# =========================================================================
# 【功能块 3：检查当日最后一次 MCPHub 启动运行状态与路径报错】
# - 输入：$LOG (mcp-stderr.log 日志路径)
# - 功能：精准提取最后一条启动记录（starting MCP server 'MCPHub'）及后续 15 行日志，
#         只针对当前最后一次启动判断是否存在路径错误或文件缺失，历史偶发错误仅记为背景 INFO
# - 输出：终端打印启动通过 PASS 或失败 FAIL，统计历史报错次数
# =========================================================================
echo "--- MCPHub 启动状态 ---"
if [ -f "$LOG" ]; then
  LAST=$(grep -n "starting MCP server 'MCPHub'" "$LOG" 2>/dev/null | tail -1 | cut -d: -f1)
  if [ -z "${LAST:-}" ]; then
    echo "WARN  日志中无 MCPHub 启动记录，需靠 no-op 调用确认"
  else
    BLOCK=$(sed -n "${LAST},$((LAST+15))p" "$LOG")
    LASTTS=$(echo "$BLOCK" | head -1 | cut -c1-19)
    if echo "$BLOCK" | grep -Ei "No such file|找不到指定的|WinError" | grep -qi "mcphub\|mcp_gateway"; then
      echo "FAIL  最后一次启动记录（$LASTTS）后报路径错误 → 通道未起"; FAIL=1
    else
      echo "PASS  最后一次启动记录（$LASTTS）后无路径报错"
    fi
    HIST=$(grep -Ei "No such file|找不到指定的|WinError" "$LOG" 2>/dev/null | grep -ci "mcphub\|mcp_gateway" || true)
    echo "INFO  历史累计路径报错 ${HIST} 次（仅作背景，不参与判定）"
  fi
else
  echo "WARN  未找到 mcp-stderr.log: $LOG"
fi

# =========================================================================
# 【功能块 4：工具注册缓存新鲜度检查与最终裁决输出】
# - 输入：$SCHEMA (mcp_schema_cache.json 路径) 以及累积的 $FAIL 状态
# - 功能：核对 MCP 工具模式定义缓存是否为今日更新，输出最终判据结论与退出码
# - 输出：RESULT: PASS (状态码 0) 或 RESULT: FAIL (状态码 1)
# =========================================================================
if [ -f "$SCHEMA" ]; then
  MT=$(date -r "$SCHEMA" +%Y-%m-%d 2>/dev/null || echo unknown)
  if [ "$MT" = "$(date +%Y-%m-%d)" ]; then
    echo "PASS  mcp_schema_cache.json 今日已更新（$MT）"
  else
    echo "WARN  工具注册缓存最后更新 $MT（非今日）"
  fi
fi

if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: PASS（仍需一次 no-op fallback_web_search 完成端到端确认）"
else
  echo "RESULT: FAIL → 主通道不可用，回落内置 web_search 并在交付物留痕"
fi
exit "$FAIL"
