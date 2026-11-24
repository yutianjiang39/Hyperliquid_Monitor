#!/usr/bin/env bash
# macOS / Linux launcher.
#   bash start.sh             full-screen Textual interface (installs on first run)
#   bash start.sh --install   create or repair .venv and dependencies, then exit
#   bash start.sh <args>      pass arguments to app.py
set -euo pipefail

HL_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$HL_SCRIPT_DIR"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONNOUSERSITE=1
HL_VENV_PYTHON="$HL_SCRIPT_DIR/.venv/bin/python"

if [[ ! -f "$HL_SCRIPT_DIR/app.py" || ! -f "$HL_SCRIPT_DIR/requirements.txt" ]]; then
    printf '%s\n' '缺少 app.py 或 requirements.txt。请完整解压本地运行包，不要单独复制启动脚本。' >&2
    exit 1
fi

# Create an isolated environment in this folder; use an existing Python only.
hl_install() {
    if [[ ! -x "$HL_VENV_PYTHON" ]]; then
        local selected='' candidate
        if [[ -n "${HL_PYTHON_BIN:-}" ]]; then
            if "$HL_PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
                selected="$HL_PYTHON_BIN"
            else
                printf '%s\n' 'HL_PYTHON_BIN 必须指向已安装的 Python 3.10 或更高版本。' >&2
                exit 1
            fi
        else
            for candidate in python3.12 python3.11 python3.10 python3 python; do
                if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
                    selected="$(command -v "$candidate")"
                    break
                fi
            done
        fi
        if [[ -z "$selected" ]]; then
            printf '%s\n' '未找到 Python 3.10 或更高版本。建议先安装 Python 3.11/3.12，然后重试。' >&2
            printf '%s\n' '官方下载：https://www.python.org/downloads/' >&2
            printf '%s\n' '也可指定已有解释器：HL_PYTHON_BIN="/完整路径/python3" bash start.sh --install' >&2
            exit 1
        fi
        printf '%s\n' '正在创建本目录专用的 .venv 环境……'
        if ! "$selected" -m venv "$HL_SCRIPT_DIR/.venv"; then
            printf '%s\n' '虚拟环境创建失败。请确认此 Python 包含 venv 和 ensurepip；部分 Linux 发行版需另装 python3-venv。' >&2
            exit 1
        fi
    fi
    if ! "$HL_VENV_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) and sys.prefix != sys.base_prefix else 1)'; then
        printf '%s\n' '.venv 不是可用的 Python 3.10+ 虚拟环境。请先将该文件夹改名，再重新安装。' >&2
        exit 1
    fi
    printf '%s\n' '正在安装运行依赖（首次安装需要联网）……'
    if ! "$HL_VENV_PYTHON" -m pip install --disable-pip-version-check -r "$HL_SCRIPT_DIR/requirements.txt"; then
        printf '%s\n' '依赖安装失败。请查看上方错误，检查网络后运行 bash start.sh --install。' >&2
        exit 1
    fi
    if ! "$HL_VENV_PYTHON" -c 'import requests, boto3, lz4.frame, textual, rich, sqlite3; print("依赖检查通过。")'; then
        printf '%s\n' '依赖导入检查失败，请保留上方错误并运行 bash start.sh --install。' >&2
        exit 1
    fi
}

if [[ "${1:-}" == "--install" ]]; then
    hl_install
    printf '%s\n' '安装完成。运行 bash start.sh 启动全屏终端界面。'
    exit 0
fi
if [[ ! -x "$HL_VENV_PYTHON" ]]; then
    printf '%s\n' '首次运行：正在准备本目录的 Python 环境。'
    hl_install
fi
if ! "$HL_VENV_PYTHON" -c 'import requests, boto3, lz4.frame, textual, rich' >/dev/null 2>&1; then
    printf '%s\n' '运行依赖不完整。请先执行 bash start.sh --install，再重新启动。' >&2
    exit 1
fi
if [[ $# -eq 0 ]]; then
    printf '%s\n' '正在打开终端界面，建议将窗口调整至至少 100 列 × 30 行。'
    exec "$HL_VENV_PYTHON" "$HL_SCRIPT_DIR/app.py" tui
fi
exec "$HL_VENV_PYTHON" "$HL_SCRIPT_DIR/app.py" "$@"
