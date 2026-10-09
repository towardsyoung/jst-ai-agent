"""项目源码、符号链接和独立交付包共用的运行目录定位。"""
import os
from pathlib import Path
import sys


def runtime_root():
    skill = Path(__file__).resolve().parents[1]
    if os.environ.get("JST_AI_HOME"):
        root = Path(os.environ["JST_AI_HOME"]).expanduser().resolve()
    elif (skill/"runtime/requirements.txt").is_file():
        root = skill/"runtime"
    else:
        root = skill.parent.parent
    if not (root/"jushuitan.py").is_file() or not (root/"requirements.txt").is_file():
        raise RuntimeError("运行文件不完整，请使用含 runtime 的技能包或设置 JST_AI_HOME。")
    return root


def venv_python(root):
    return root/".venv"/("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
