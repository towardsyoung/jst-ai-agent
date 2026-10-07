#!/bin/sh
set -eu
query_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$query_root/.venv/bin/python" ]; then
  echo '本地查询环境尚未安装，请按 README.md 的安装步骤创建 .venv。' >&2
  exit 1
fi
exec "$query_root/.venv/bin/python" "$query_root/jushuitan.py" "$@"
