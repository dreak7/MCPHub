"""Direct entry point for launching the gateway without PYTHONPATH or working directory dependencies."""

import sys
from pathlib import Path

# =========================================================================
# 【功能块：环境路径自适应注入】
# - 输入：当前脚本所在文件绝对路径
# - 功能：自动将项目根目录加入 Python 模块搜索路径（sys.path），
#         确保无论从哪个工作目录启动都能正确导入 mcp_gateway 模块。
# - 输出：无（更新 sys.path）
# =========================================================================
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# =========================================================================
# 【功能块：启动 MCP Gateway 服务器】
# - 输入：通过命令行传入的参数（如 --config）
# - 功能：调用 mcp_gateway.server 的 main 函数，启动标准输入输出 JSON-RPC 服务。
# - 输出：退出码
# =========================================================================
from mcp_gateway.server import main

if __name__ == "__main__":
    main()
