@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8+ 并勾选 "Add to PATH"。
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/2] 检查依赖 qrcode ...
python -m pip install -q -r requirements.txt

echo [2/2] 启动服务 ...
python lan_transfer.py %*

pause
