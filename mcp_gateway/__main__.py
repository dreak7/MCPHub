import sys
from pathlib import Path

# =========================================================================
# 【功能块：环境路径自适应注入】
# - 输入：当前文件（__main__.py）绝对路径
# - 功能：将包含 mcp_gateway 的父目录自动加入 sys.path，
#         确保无需依赖外部工作目录或 PYTHONPATH 即可正常导入包内模块。
# - 输出：无（更新 sys.path）
# =========================================================================
PACKAGE_DIR = Path(__file__).resolve().parent
PARENT_DIR = PACKAGE_DIR.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

# =========================================================================
# 【功能块：兼容相对导入与绝对导入】
# - 输入：当前执行环境的 __package__ 状态
# - 功能：当作为目录（python path/to/mcp_gateway）或文件直接运行（__package__ 为空）时，
#         采用绝对导入；当通过 -m 模块模式运行且包含包名时，采用相对导入。
# - 输出：main 函数入口
# =========================================================================
if __package__ in (None, ""):
    from mcp_gateway.server import main
else:
    from .server import main


if __name__ == "__main__":
    main()
