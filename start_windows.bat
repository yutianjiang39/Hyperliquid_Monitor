@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONNOUSERSITE=1"
set "HL_EXIT_CODE=1"
set "HL_INTERACTIVE=0"
if "%~1"=="" set "HL_INTERACTIVE=1"
pushd "%~dp0"
if errorlevel 1 goto :directory_error
if not exist "app.py" goto :missing_files
if exist ".venv\Scripts\python.exe" goto :ready
echo 首次运行：正在准备本目录的 Python 环境。
call "%~dp0scripts\install_windows.bat" --from-launcher
if errorlevel 1 goto :finish

:ready
".venv\Scripts\python.exe" -c "import requests, boto3, lz4.frame, textual, rich" >nul 2>nul
if errorlevel 1 goto :dependency_error
if "%HL_INTERACTIVE%"=="1" goto :interactive
".venv\Scripts\python.exe" "app.py" %*
set "HL_EXIT_CODE=%ERRORLEVEL%"
goto :finish

:interactive
echo 正在打开终端界面，建议将窗口调整至至少 100 列 x 30 行。
".venv\Scripts\python.exe" "app.py" tui
set "HL_EXIT_CODE=%ERRORLEVEL%"
goto :finish

:missing_files
echo 缺少 app.py。请完整解压本地运行包，不要单独复制启动脚本。
goto :finish
:dependency_error
echo 运行依赖不完整。请先双击 scripts\install_windows.bat 安装或修复依赖。
goto :finish
:directory_error
echo 无法进入程序目录。请把压缩包完整解压到可写文件夹。
if "%HL_INTERACTIVE%"=="1" pause
exit /b 1
:finish
popd
if "%HL_INTERACTIVE%"=="1" pause
exit /b %HL_EXIT_CODE%

