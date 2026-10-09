#!/usr/bin/env python3
"""跨平台查询入口；不依赖当前工作目录或 shell。"""
import subprocess
import sys
import os
from _runtime import runtime_root, venv_python


def main():
    try:
        root = runtime_root()
        python = venv_python(root)
        if not python.is_file():
            raise RuntimeError("请先用 Python 3.12+ 运行 scripts/setup.py 初始化查询环境。")
        env = dict(os.environ, PYTHONUTF8="1")
        return subprocess.run([str(python), str(root/"jushuitan.py"), *sys.argv[1:]], env=env).returncode
    except (RuntimeError, OSError) as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else "无法启动查询环境，请重新运行 setup.py。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
