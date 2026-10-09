#!/bin/sh
set -eu
skill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
if [ -n "${JST_AI_HOME:-}" ]; then
  query_entry="$JST_AI_HOME/query.sh"
elif [ -f "$skill_root/runtime/query.sh" ]; then
  query_entry="$skill_root/runtime/query.sh"
else
  query_entry="$skill_root/../../query.sh"
fi
if [ ! -f "$query_entry" ]; then
  echo '聚水潭运行入口不存在，请安装完整技能包或设置 JST_AI_HOME。' >&2
  exit 1
fi
exec sh "$query_entry" "$@"
