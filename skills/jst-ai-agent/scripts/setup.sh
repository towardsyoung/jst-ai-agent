#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
python_bin=${JST_PYTHON:-python3}
exec "$python_bin" "$script_dir/setup.py" "$@"
