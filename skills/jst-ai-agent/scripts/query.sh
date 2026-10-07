#!/bin/sh
set -eu
skill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
if [ -n "${JST_AI_HOME:-}" ]; then
  query_entry="$JST_AI_HOME/query.sh"
elif [ -x "$skill_root/runtime/query.sh" ]; then
  query_entry="$skill_root/runtime/query.sh"
else
  query_entry="$skill_root/../../query.sh"
fi
if [ ! -x "$query_entry" ]; then
  echo '聚水潭本地查询工具未安装，请安装配套运行工具或设置 JST_AI_HOME 指向项目目录。' >&2
  exit 1
fi
exec "$query_entry" "$@"
