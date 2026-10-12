#!/bin/sh
set -eu
query_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$query_root/.venv/Scripts/python.exe" ]; then
  # Windows（含 Git Bash / MSYS）
  exec "$query_root/.venv/Scripts/python.exe" "$query_root/jushuitan.py" "$@"
elif [ -x "$query_root/.venv/bin/python" ]; then
  # macOS / Linux
  exec "$query_root/.venv/bin/python" "$query_root/jushuitan.py" "$@"
fi
echo '本地查询环境尚未安装，请按 README.md 的安装步骤创建 .venv。' >&2
exit 1
