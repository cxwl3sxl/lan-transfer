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

echo [1/5] 创建隔离虚拟环境 .build-venv ...
if not exist ".build-venv\Scripts\python.exe" (
    python -m venv .build-venv
)
call .build-venv\Scripts\activate

echo [2/5] 安装依赖 PyInstaller / qrcode / pystray / pillow ...
python -m pip install -q --upgrade pip
python -m pip install -q pyinstaller qrcode pystray pillow

echo [3/5] 打包 LanTransfer.exe（控制台版）...
python -m PyInstaller --noconfirm --onefile --name LanTransfer --distpath pkg_console --workpath pkg_build_c --add-data "index.html;." lan_transfer.py

echo [4/5] 打包 LanTransferWindowed.exe（托盘版）...
python -m PyInstaller --noconfirm --onefile --windowed --name LanTransferWindowed --distpath pkg_windowed --workpath pkg_build_w --add-data "index.html;." lan_transfer.py

echo [5/5] 合并到 dist\ 并清理临时目录 ...
if not exist dist mkdir dist
copy /Y pkg_console\LanTransfer.exe dist\ >nul
copy /Y pkg_windowed\LanTransferWindowed.exe dist\ >nul
rmdir /s /q pkg_console pkg_windowed pkg_build_c pkg_build_w build >nul 2>nul
del /q LanTransfer.spec LanTransferWindowed.spec >nul 2>nul

echo.
echo 完成！可执行文件位于 dist\ 目录:
echo   dist\LanTransfer.exe          控制台版
echo   dist\LanTransferWindowed.exe  托盘版
echo.
pause
