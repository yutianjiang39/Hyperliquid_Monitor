@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONNOUSERSITE=1"
rem Only use existing Python installations; do not trigger manager auto-install.
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYLAUNCHER_ALWAYS_INSTALL="
set "HL_EXIT_CODE=1"
set "HL_NO_PAUSE="
if /I "%~1"=="--from-launcher" set "HL_NO_PAUSE=1"
pushd "%~dp0"
if errorlevel 1 goto :directory_error

if not exist "requirements.txt" goto :missing_files
if exist ".venv\Scripts\python.exe" goto :install_dependencies

py -3.12 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 goto :python312
py -3.11 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 goto :python311
py -3.10 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 goto :python310
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 goto :python3
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 goto :python_path
echo 未找到 Python 3.10 或更高版本。建议先安装 Python 3.11/3.12，然后重试。
echo 官方下载：https://www.python.org/downloads/
echo 安装后重新打开终端，再运行本脚本。
goto :finish

:python312
set "HL_PYTHON_EXE=py"
set "HL_PYTHON_ARG=-3.12"
goto :create_environment
:python311
set "HL_PYTHON_EXE=py"
set "HL_PYTHON_ARG=-3.11"
goto :create_environment
:python310
set "HL_PYTHON_EXE=py"
set "HL_PYTHON_ARG=-3.10"
goto :create_environment
:python3
set "HL_PYTHON_EXE=py"
set "HL_PYTHON_ARG=-3"
goto :create_environment
:python_path
set "HL_PYTHON_EXE=python"
set "HL_PYTHON_ARG="

:create_environment
echo 正在创建本目录专用的 .venv 环境……
"%HL_PYTHON_EXE%" %HL_PYTHON_ARG% -m venv ".venv"
if errorlevel 1 goto :environment_error

:install_dependencies
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) and sys.prefix != sys.base_prefix else 1)"
if errorlevel 1 goto :environment_error
echo 正在安装运行依赖，首次安装需要联网……
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "requirements.txt"
if errorlevel 1 goto :dependency_error
".venv\Scripts\python.exe" -c "import requests, boto3, lz4.frame, textual, rich, sqlite3; print('依赖检查通过。')"
if errorlevel 1 goto :dependency_error
echo 安装完成。双击 start_windows.bat 打开终端界面。
set "HL_EXIT_CODE=0"
goto :finish

:missing_files
echo 缺少 requirements.txt。请完整解压本地运行包后再安装。
goto :finish
:environment_error
echo .venv 创建或版本检查失败。请确认 Python 包含 venv 和 ensurepip。
echo 如果 .venv 来自其他电脑或旧版本，请先将该文件夹改名，再重新安装。
goto :finish
:dependency_error
echo 依赖安装或导入失败。请查看上方错误，检查网络后重新运行本脚本。
goto :finish
:directory_error
echo 无法进入程序目录。请把压缩包完整解压到可写文件夹。
if not defined HL_NO_PAUSE pause
exit /b 1
:finish
popd
if not defined HL_NO_PAUSE pause
exit /b %HL_EXIT_CODE%

