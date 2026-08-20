@echo off
chcp 936 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   微信 AI 分身 - 环境安装脚本
echo ============================================
echo.

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD where python3 >nul 2>nul && set "PYCMD=python3"

if not defined PYCMD (
    echo [错误] 未检测到 Python。
    echo 请到 https://www.python.org/downloads/ 下载并安装 Python 3.10 或更高版本。
    echo 安装时请务必勾选 "Add Python to PATH"。
    goto :end
)

for /f "tokens=2" %%v in ('!PYCMD! --version 2^>^&1') do set "PYVER=%%v"
echo 检测到 Python: !PYVER!

!PYCMD! -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto :verfail

echo Python 版本满足要求(^>= 3.10)。
echo.

REM 升级 pip(可选,失败不影响后续)
!PYCMD! -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple >nul 2>nul

echo 正在安装依赖,依次尝试:清华镜像 -^> 阿里云镜像 -^> 官方源
echo.

set "INSTALL_OK=0"
for %%m in ("https://pypi.tuna.tsinghua.edu.cn/simple" "https://mirrors.aliyun.com/pypi/simple/" "https://pypi.org/simple") do (
    if "!INSTALL_OK!"=="0" (
        echo   尝试: %%m
        !PYCMD! -m pip install -r "%~dp0requirements.txt" -i %%m
        if not errorlevel 1 set "INSTALL_OK=1"
    )
)

if "!INSTALL_OK!"=="0" (
    echo.
    echo [错误] 所有镜像源安装均失败。请检查网络/代理,或手动执行:
    echo   !PYCMD! -m pip install -r requirements.txt
    goto :end
)

echo.
echo [成功] 依赖安装完成!
echo 现在请在本项目目录下运行:
echo   python bot.py
echo   streamlit run web_ui.py
goto :end

:verfail
echo.
echo [错误] Python 版本过低(!PYVER!)。
echo 本项目需要 Python 3.10 及以上版本,建议安装最新稳定版:
echo   https://www.python.org/downloads/
goto :end

:end
echo.
pause
