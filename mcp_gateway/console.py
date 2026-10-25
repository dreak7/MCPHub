"""Interactive management console for MCPHub service."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

# =========================================================================
# 【功能块：配置标准输入输出流编码】
# - 输入：sys.stdin, sys.stdout, sys.stderr
# - 功能：将标准输入、标准输出、标准错误流重新配置为 UTF-8 编码并替换非法字节，
#         避免在 Windows 系统下打印中文字符或符号时触发 Unicode 编码崩溃。
# - 输出：无（原地重配标准流）
# =========================================================================
def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# =========================================================================
# 【功能块：获取项目根目录与关键资源路径】
# - 输入：当前文件绝对路径
# - 功能：基于当前模块位置计算出项目根目录、配置目录、日志目录等绝对路径
# - 输出：dict[str, Path]: 各核心目录与文件路径字典
# =========================================================================
def _get_project_paths() -> dict[str, Path]:
    package_dir = Path(__file__).resolve().parent
    root_dir = package_dir.parent
    return {
        "root": root_dir,
        "config_dir": root_dir / "config",
        "env_file": root_dir / "config" / ".env",
        "env_example": root_dir / "config" / ".env.example",
        "log_file": root_dir / "logs" / "gateway.log",
        "gui_script": package_dir / "gui_add_provider.py",
    }


# =========================================================================
# 【功能块：清空终端屏幕】
# - 输入：无
# - 功能：调用操作系统原生清屏指令（Windows 下为 cls，其他系统为 clear）
# - 输出：无
# =========================================================================
def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


# =========================================================================
# 【功能块：等待用户按回车继续】
# - 输入：标准输入中的回车换行
# - 功能：暂停执行以供用户阅读输出结果，直到用户按下回车键
# - 输出：无
# =========================================================================
def press_enter_to_continue() -> None:
    print("\n按回车键返回主菜单...", end="", flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


# =========================================================================
# 【功能块：展示 Hermes Desktop 挂载配置】
# - 输入：paths (dict[str, Path]): 项目关键路径字典
# - 功能：自动探测当前 Python 与网关配置的真实绝对路径，生成零歧义的 YAML 与 JSON 配置
# - 输出：无（向终端打印配置代码与说明）
# =========================================================================
def show_hermes_config(paths: dict[str, Path]) -> None:
    clear_screen()
    try:
        from .hermes_helper import generate_hermes_snippets
        snippets = generate_hermes_snippets()
        print("=" * 70)
        print("     Hermes Desktop 挂载配置（已自动探测本机真实绝对路径）")
        print("=" * 70)
        print("\n--- 方式一：YAML 格式（推荐，直接填入 Hermes 的 config.yaml） ---")
        print(snippets.get("yaml", ""))
        print("\n--- 方式二：JSON 格式（填入 Hermes 的 mcp_servers.json） ---")
        print(snippets.get("json", ""))
        print("=" * 70)
        print("提示：可直接高亮并复制上方配置粘贴至 Hermes Desktop 配置中。")
    except Exception as exc:
        print(f"生成配置失败: {exc}")
    press_enter_to_continue()


# =========================================================================
# 【功能块：启动可视化引擎与能力配置向导 (GUI)】
# - 输入：paths (dict[str, Path]): 项目关键路径字典
# - 功能：使用当前 Python 解释器在独立后台进程中唤起 Tkinter 可视化窗口
# - 输出：无（启动子进程）
# =========================================================================
def launch_gui(paths: dict[str, Path]) -> None:
    clear_screen()
    print("正在启动可视化配置向导 (GUI)，请稍候...")
    gui_script = paths["gui_script"]
    if not gui_script.exists():
        print(f"【错误】未找到 GUI 脚本文件：{gui_script}")
        press_enter_to_continue()
        return
    try:
        subprocess.Popen([sys.executable, str(gui_script)])
        print("GUI 窗口已成功唤起！您可以直接在弹出的图形窗口中管理搜索引擎。")
    except Exception as exc:
        print(f"唤起 GUI 失败: {exc}")
    press_enter_to_continue()


# =========================================================================
# 【功能块：快捷配置与编辑 API 密钥 (.env)】
# - 输入：paths (dict[str, Path]): 项目关键路径字典
# - 功能：检查 config/.env 是否存在，若不存在自动根据 .env.example 初始化，并用系统记事本打开
# - 输出：无（打开文本编辑器）
# =========================================================================
def edit_env_keys(paths: dict[str, Path]) -> None:
    clear_screen()
    print("正在检查并打开 API 密钥配置文件...")
    env_file = paths["env_file"]
    env_example = paths["env_example"]

    if not env_file.exists():
        if env_example.exists():
            shutil.copy(env_example, env_file)
            print("已自动从 .env.example 初始化生成 config/.env 文件。")
        else:
            env_file.write_text("# MCPHub API Keys\n", encoding="utf-8")
            print("已自动创建空白 config/.env 文件。")

    try:
        if os.name == "nt":
            os.startfile(str(env_file))
        else:
            subprocess.Popen(["xdg-open", str(env_file)])
        print("已经为您在系统文本编辑器中打开 config/.env 文件。")
        print("提示：编辑填入您的 API 密钥后，按 Ctrl + S 保存并关闭文件即可。")
    except Exception as exc:
        print(f"打开编辑器失败: {exc}，请手动编辑文件：{env_file}")
    press_enter_to_continue()


# =========================================================================
# 【功能块：运行服务健康自检与全部测试套件】
# - 输入：paths (dict[str, Path]): 项目关键路径字典
# - 功能：调用 python -m unittest 执行全部 190 项单元测试，验证所有功能与算法健康状态
# - 输出：测试控制台输出
# =========================================================================
def run_health_tests(paths: dict[str, Path]) -> None:
    clear_screen()
    print("正在执行全部单元测试套件，请稍候...")
    print("=" * 70)
    try:
        subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=str(paths["root"]),
            check=False
        )
    except Exception as exc:
        print(f"执行测试失败: {exc}")
    print("=" * 70)
    press_enter_to_continue()


# =========================================================================
# 【功能块：查看服务运行日志】
# - 输入：paths (dict[str, Path]): 项目关键路径字典
# - 功能：读取并打印 logs/gateway.log 最近 30 行运行日志
# - 输出：控制台展示日志文本
# =========================================================================
def view_service_logs(paths: dict[str, Path]) -> None:
    clear_screen()
    print("=" * 70)
    print("最近服务运行日志 (logs/gateway.log):")
    print("=" * 70)
    log_file = paths["log_file"]
    if log_file.exists():
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            if lines:
                recent_lines = lines[-30:]
                print("\n".join(recent_lines))
            else:
                print("（日志文件为空，暂无记录）")
        except Exception as exc:
            print(f"读取日志失败: {exc}")
    else:
        print("暂无日志文件（服务尚未产生任何运行日志）。")
    print("=" * 70)
    press_enter_to_continue()


# =========================================================================
# 【功能块：运行 Hermes MCP 连通性测试】
# - 输入：无
# - 功能：调用 hermes mcp test MCPHub 测试与客户端通信握手
# - 输出：命令输出与故障排查建议
# =========================================================================
def run_hermes_test() -> None:
    clear_screen()
    print("正在执行 Hermes MCP 连通性测试...")
    print("=" * 70)
    try:
        res = subprocess.run(["hermes", "mcp", "test", "MCPHub"], check=False)
        if res.returncode != 0:
            print("\n提示：若提示命令未找到，请确认已在终端安装并配置好 Hermes CLI。")
    except FileNotFoundError:
        print("未检测到 'hermes' 命令。请确认已在终端全局安装 Hermes CLI。")
    except Exception as exc:
        print(f"测试执行异常: {exc}")
    print("=" * 70)
    press_enter_to_continue()


# =========================================================================
# 【功能块：控制台主菜单循环交互与居中排版渲染】
# - 输入：用户在键盘上输入的数字选项 [0-6]
# - 功能：动态探测终端窗口尺寸并计算居中边距，呈现美观舒展的卡片式居中菜单，
#         拉开各行间距提升易读性，并根据用户输入路由分发至对应功能模块
# - 输出：无
# =========================================================================
def main() -> None:
    _configure_stdio()
    paths = _get_project_paths()

    while True:
        clear_screen()

        # 动态自适应探测终端窗口尺寸并计算水平与垂直居中边距
        term_cols, term_lines = shutil.get_terminal_size((80, 25))
        box_width = min(72, max(52, term_cols - 2)) if term_cols < 76 else 72
        left_margin = max(0, (term_cols - box_width) // 2)
        pad = " " * left_margin

        # 垂直居中呼吸感留白（当终端高度充裕时增加顶部空行）
        top_padding = 2 if term_lines >= 28 else (1 if term_lines >= 24 else 0)
        for _ in range(top_padding):
            print()

        # 绘制居中顶部标题框
        print(f"{pad}{'=' * box_width}")
        title = f"MCPHub 智能服务管理控制台 v{__version__} (Hermes Desktop 增强版)"
        title_display_width = sum(2 if ord(c) > 127 else 1 for c in title)
        title_spaces = max(0, (box_width - title_display_width) // 2)
        print(f"{pad}{' ' * title_spaces}{title}")
        print(f"{pad}{'=' * box_width}")
        print()

        # 舒展式菜单选项（每项之间适当留白拉开行距，极大提升阅读舒适度）
        print(f"{pad}  [1] 一键生成并查看 Hermes Desktop 挂载配置 (含本机真实绝对路径)")
        print()
        print(f"{pad}  [2] 启动可视化引擎与能力配置向导 (GUI 界面)")
        print()
        print(f"{pad}  [3] 快捷配置/编辑 API 密钥 (.env)")
        print()
        print(f"{pad}  [4] 运行服务健康检查与全部测试套件 (192 项)")
        print()
        print(f"{pad}  [5] 查看服务运行日志 (logs/gateway.log)")
        print()
        print(f"{pad}  [6] 运行 Hermes MCP 连通性测试 (hermes mcp test MCPHub)")
        print()
        print(f"{pad}  [0] 退出控制台")
        print()
        print(f"{pad}{'=' * box_width}")
        print()

        try:
            choice = input(f"{pad}👉 请输入选项编号 [0-6] 并按回车: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出控制台。")
            break

        if choice == "1":
            show_hermes_config(paths)
        elif choice == "2":
            launch_gui(paths)
        elif choice == "3":
            edit_env_keys(paths)
        elif choice == "4":
            run_health_tests(paths)
        elif choice == "5":
            view_service_logs(paths)
        elif choice == "6":
            run_hermes_test()
        elif choice == "0":
            clear_screen()
            print()
            print(f"{pad}感谢使用 MCPHub，服务管理控制台已退出。")
            print()
            break
        else:
            print(f"\n{pad}⚠️  无效选项编号，请按回车键重新选择...", end="", flush=True)
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                break


if __name__ == "__main__":
    main()
