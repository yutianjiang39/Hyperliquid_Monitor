#!/usr/bin/env bash
# Create an isolated environment in this folder; use an existing Python only.
set -euo pipefail

HL_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$HL_SCRIPT_DIR"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONNOUSERSITE=1

if [[ ! -f "$HL_SCRIPT_DIR/requirements.txt" ]]; then
    printf '%s\n' '缺少 requirements.txt。请完整解压本地运行包后再安装。' >&2
    exit 1
fi

if [[ ! -x "$HL_SCRIPT_DIR/.venv/bin/python" ]]; then
    HL_SELECTED_PYTHON=''
    if [[ -n "${HL_PYTHON_BIN:-}" ]]; then
        if "$HL_PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            HL_SELECTED_PYTHON="$HL_PYTHON_BIN"
        else
            printf '%s\n' 'HL_PYTHON_BIN 必须指向已安装的 Python 3.10 或更高版本。' >&2
            exit 1
        fi
    else
        for HL_CANDIDATE in python3.12 python3.11 python3.10 python3 python; do
            if command -v "$HL_CANDIDATE" >/dev/null 2>&1 && "$HL_CANDIDATE" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
                HL_SELECTED_PYTHON="$(command -v "$HL_CANDIDATE")"
                break
            fi
        done
    fi
    if [[ -z "$HL_SELECTED_PYTHON" ]]; then
        printf '%s\n' '未找到 Python 3.10 或更高版本。建议先安装 Python 3.11/3.12，然后重试。' >&2
        printf '%s\n' '官方下载：https://www.python.org/downloads/' >&2
        printf '%s\n' '也可指定已有解释器：HL_PYTHON_BIN="/完整路径/python3" bash scripts/install_macos_linux.sh' >&2
        exit 1
    fi
    printf '%s\n' '正在创建本目录专用的 .venv 环境……'
    if ! "$HL_SELECTED_PYTHON" -m venv "$HL_SCRIPT_DIR/.venv"; then
        printf '%s\n' '虚拟环境创建失败。请确认此 Python 包含 venv 和 ensurepip；部分 Linux 发行版需另装 python3-venv。' >&2
        exit 1
    fi
fi

HL_VENV_PYTHON="$HL_SCRIPT_DIR/.venv/bin/python"
if ! "$HL_VENV_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) and sys.prefix != sys.base_prefix else 1)'; then
    printf '%s\n' '.venv 不是可用的 Python 3.10+ 虚拟环境。请先将该文件夹改名，再重新安装。' >&2
    exit 1
fi

printf '%s\n' '正在安装运行依赖（首次安装需要联网）……'
if ! "$HL_VENV_PYTHON" -m pip install --disable-pip-version-check -r "$HL_SCRIPT_DIR/requirements.txt"; then
    printf '%s\n' '依赖安装失败。请查看上方错误，检查网络后重新运行安装脚本。' >&2
    exit 1
fi
if ! "$HL_VENV_PYTHON" -c 'import requests, boto3, lz4.frame, textual, rich, sqlite3; print("依赖检查通过。")'; then
    printf '%s\n' '依赖导入检查失败，请保留上方错误并重新运行安装脚本。' >&2
    exit 1
fi
printf '%s\n' '安装完成。运行 bash start_macos_linux.sh 启动全屏终端界面。'

