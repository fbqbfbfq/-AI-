@echo off
chcp 936 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   微信 AI 分身 - 启动脚本
echo ============================================
echo.

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD where python3 >nul 2>nul && set "PYCMD=python3"
if not defined PYCMD (
    echo [错误] 未检测到 Python,请先双击 setup.bat 安装环境。
    goto :end
)

if not exist "bot_ilink.py" (
    echo [错误] 未找到 bot_ilink.py,请在本项目目录下运行本脚本。
    goto :end
)

echo 正在启动管理网页(web_ui)...
start "微信AI分身-管理网页" cmd /k "!PYCMD! -m streamlit run web_ui.py"

echo 正在启动微信机器人(bot_ilink)...
start "微信AI分身-机器人" cmd /k "!PYCMD! bot_ilink.py"

echo.
echo 两个窗口已分别启动:
echo   - 管理网页窗口: 浏览器访问 http://localhost:8501
echo   - 机器人窗口:   用手机微信扫弹出的二维码并确认登录
echo.
echo 关闭对应窗口即可停止对应程序;本窗口可以关闭。
goto :end

:end
echo.
pause
