#!/usr/bin/env bash
# No arguments: full-screen Textual interface. Other arguments go to app.py.
set -euo pipefail

HL_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$HL_SCRIPT_DIR"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONNOUSERSITE=1

if [[ ! -f "$HL_SCRIPT_DIR/app.py" ]]; then
    printf '%s\n' '缺少 app.py。请完整解压本地运行包，不要单独复制启动脚本。' >&2
    exit 1
fi
if [[ ! -x "$HL_SCRIPT_DIR/.venv/bin/python" ]]; then
    printf '%s\n' '首次运行：正在准备本目录的 Python 环境。'
    bash "$HL_SCRIPT_DIR/install_macos_linux.sh"
fi
if ! "$HL_SCRIPT_DIR/.venv/bin/python" -c 'import requests, boto3, lz4.frame, textual, rich' >/dev/null 2>&1; then
    printf '%s\n' '运行依赖不完整。请先执行 bash install_macos_linux.sh，再重新启动。' >&2
    exit 1
fi
if [[ $# -eq 0 ]]; then
    printf '%s\n' '正在打开终端界面，建议将窗口调整至至少 100 列 × 30 行。'
    exec "$HL_SCRIPT_DIR/.venv/bin/python" "$HL_SCRIPT_DIR/app.py" tui
fi
exec "$HL_SCRIPT_DIR/.venv/bin/python" "$HL_SCRIPT_DIR/app.py" "$@"

