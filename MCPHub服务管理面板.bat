@echo off
chcp 65001 >nul
title MCPHub 智能服务管理控制台
cls

:: 切换到当前批处理文件所在目录
cd /d "%~dp0"

:CHECK_PYTHON
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ======================================================================
    echo 【错误】未检测到合规的 Python 运行环境（要求 Python 3.10 或更高版本）！
    echo 请先安装 Python 3.10+，并在安装时务必勾选 "Add Python to PATH"。
    echo ======================================================================
    pause
    exit /b 1
)

:MENU
cls
echo ======================================================================
echo           MCPHub 智能服务管理控制台 (Hermes Desktop 增强版)
echo ======================================================================
echo.
echo   [1] 一键生成并查看 Hermes Desktop 挂载配置 (含本机真实绝对路径)
echo   [2] 启动可视化引擎与能力配置向导 (GUI 界面)
echo   [3] 快捷配置/编辑 API 密钥 (.env)
echo   [4] 运行服务健康检查与全部测试套件 (42 项)
echo   [5] 查看服务运行日志 (logs\gateway.log)
echo   [6] 运行 Hermes MCP 连通性测试 (hermes mcp test MCPHub)
echo.
echo   [0] 退出
echo ======================================================================
set /p choice="请输入选项编号 [0-6] 并按回车: "

if "%choice%"=="1" goto OP_HERMES
if "%choice%"=="2" goto OP_GUI
if "%choice%"=="3" goto OP_ENV
if "%choice%"=="4" goto OP_TEST
if "%choice%"=="5" goto OP_LOG
if "%choice%"=="6" goto OP_HERMES_TEST
if "%choice%"=="0" goto OP_EXIT

echo 无效选项，请重新输入！
timeout /t 2 >nul
goto MENU

:OP_HERMES
cls
python -m mcp_gateway.hermes_helper
echo.
echo 提示：可直接复制上方配置填入 Hermes 的 config.yaml 文件中。
echo.
pause
goto MENU

:OP_GUI
cls
echo 正在启动可视化配置向导，请稍候...
start "" python "%~dp0mcp_gateway\gui_add_provider.py"
goto MENU

:OP_ENV
cls
echo 正在检查并打开 API 密钥配置文件...
if not exist "%~dp0config\.env" (
    if exist "%~dp0config\.env.example" (
        copy "%~dp0config\.env.example" "%~dp0config\.env" >nul
        echo 已自动从 .env.example 初始化生成 config\.env
    ) else (
        echo # MCPHub API Keys > "%~dp0config\.env"
    )
)
start notepad "%~dp0config\.env"
echo.
echo 已经使用记事本打开 config\.env 文件。
echo 编辑完成后请按 Ctrl + S 保存并关闭记事本。
echo.
pause
goto MENU

:OP_TEST
cls
echo 正在执行全部单元测试套件，请稍候...
echo ======================================================================
python -m unittest discover -s tests -v
echo ======================================================================
echo.
pause
goto MENU

:OP_LOG
cls
echo ======================================================================
echo 最近服务运行日志 (logs\gateway.log):
echo ======================================================================
if exist "%~dp0logs\gateway.log" (
    python -c "from pathlib import Path; p = Path(r'%~dp0logs\gateway.log'); print(''.join(p.read_text(encoding='utf-8', errors='replace').splitlines(True)[-30:]) if p.exists() else '暂无日志记录。')"
) else (
    echo 暂无日志文件（服务尚未产生日志记录）。
)
echo.
echo ======================================================================
pause
goto MENU

:OP_HERMES_TEST
cls
echo 正在执行 Hermes MCP 连通性测试...
echo ======================================================================
hermes mcp test MCPHub
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo 提示：若提示命令未找到，请确认已在终端安装并配置好 Hermes CLI。
)
echo ======================================================================
echo.
pause
goto MENU

:OP_EXIT
exit /b 0
