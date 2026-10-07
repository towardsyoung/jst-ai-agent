#!/bin/sh
set -eu
skill_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
if [ -n "${JST_AI_HOME:-}" ]; then
  runtime_root="$JST_AI_HOME"
elif [ -f "$skill_root/runtime/requirements.txt" ]; then
  runtime_root="$skill_root/runtime"
else
  runtime_root="$skill_root/../.."
fi
if [ ! -f "$runtime_root/requirements.txt" ] || [ ! -f "$runtime_root/jushuitan.py" ]; then
  echo '聚水潭运行文件不完整，请使用含 runtime 的技能包或指定 JST_AI_HOME。' >&2
  exit 1
fi
chmod +x "$runtime_root/query.sh" "$skill_root/scripts/query.sh"
python_bin=${JST_PYTHON:-python3}
if [ ! -x "$runtime_root/.venv/bin/python" ]; then
  "$python_bin" -m venv "$runtime_root/.venv"
fi
"$runtime_root/.venv/bin/python" -m pip install -r "$runtime_root/requirements.txt"
