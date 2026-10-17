@echo off
setlocal
chcp 65001 >nul 2>&1
title MCPHub 智能服务管理控制台
cd /d "%~dp0"

:: 尝试优化控制台窗口尺寸（宽度 88 列，高度 30 行，提供舒适居中的视觉阅读空间）
mode con: cols=88 lines=30 >nul 2>&1

:: 检查 Python 运行环境
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo   ======================================================================
    echo   【错误】未检测到合规的 Python 运行环境（要求 Python 3.10 或更高版本）！
    echo   请先安装 Python 3.10+，并在安装时务必勾选 "Add Python to PATH"。
    echo   ======================================================================
    echo.
    pause
    exit /b 1
)

:: 启动交互式控制台
python -m mcp_gateway.console
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo   ======================================================================
    echo   【提示】控制台已退出（状态码：%ERRORLEVEL%）。
    echo   ======================================================================
    echo.
    pause
)
endlocal
