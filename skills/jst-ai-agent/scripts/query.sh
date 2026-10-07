#!/bin/sh
set -eu
query_entry='/Users/naodoo/Documents/ChatGPT/此壹/query.sh'
if [ ! -x "$query_entry" ]; then
  echo '聚水潭本地查询入口不存在，请恢复项目路径和查询环境。' >&2
  exit 1
fi
exec "$query_entry" "$@"
