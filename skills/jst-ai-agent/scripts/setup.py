#!/usr/bin/env python3
"""安装独立 Python 环境；Windows/Linux 默认安装专用浏览器。"""
import argparse
from pathlib import Path
import subprocess
import sys
import venv
from _runtime import runtime_root, venv_python


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true", help="macOS 也安装专用浏览器")
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 12):
            raise RuntimeError("请安装 Python 3.12 或更高版本，再用该版本运行 setup.py。")
        root = runtime_root()
        if sys.platform != "win32":
            for script in (root/"query.sh", Path(__file__).resolve().with_name("query.sh")):
                if script.is_file():
                    script.chmod(script.stat().st_mode | 0o111)
        python = venv_python(root)
        if not python.is_file():
            venv.EnvBuilder(with_pip=True).create(root/".venv")
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(root/"requirements.txt")], check=True)
        if args.browser or sys.platform != "darwin":
            subprocess.run([str(python), "-m", "playwright", "install", "chromium", "--no-shell"], check=True)
        print("安装完成。Windows/Linux 请运行 scripts/query.py browser-login；macOS 可运行 session-check。")
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print("安装未完成：" + (str(exc) if isinstance(exc, RuntimeError) else
              "请检查 Python、网络和依赖安装输出，修复后重试。"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
