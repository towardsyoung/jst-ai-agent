"""验证交付包可独立定位、跨平台安装，且不携带企业数据或个人环境。"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("build_skill", ROOT/"scripts/build_skill.py")


class PackageTests(unittest.TestCase):
    def test_package_excludes_private_data_and_python_entry_starts_after_relocation(self):
        with TemporaryDirectory(prefix="jst delivery ") as folder:
            package = builder.build(Path(folder)/"skill.zip")
            with ZipFile(package) as archive:
                names = archive.namelist()
                self.assertTrue(all(name.startswith("jst-ai-agent/") for name in names))
                self.assertFalse(any(part in name for name in names for part in
                                     ("companies/", "cases/", "reports/", ".venv/", "Cookies", "browser-profile")))
                for name in names:
                    self.assertNotIn(b"/Users/", archive.read(name))
                archive.extractall(folder)
            skill = Path(folder)/"jst-ai-agent"
            scripts = skill/"scripts"
            # 用当前已安装依赖的解释器启动真实打包后CLI；不安装依赖、不登录ERP。
            with patch.dict(os.environ, {"JST_AI_HOME": str(skill/"runtime")}):
                with patch.dict(sys.modules, {"_runtime": load("runtime_test", scripts/"_runtime.py")}):
                    query = load("query_test", scripts/"query.py")
                real_run = subprocess.run
                results = []
                def run(command, **kwargs):
                    result = real_run(command, cwd=folder, capture_output=True, text=True, encoding="utf-8", check=True, **kwargs)
                    results.append(result)
                    return result
                with patch.object(query, "venv_python", return_value=Path(sys.executable)), \
                     patch.object(sys, "argv", ["query", "--help"]), patch.object(query.subprocess, "run", side_effect=run):
                    self.assertEqual(query.main(), 0)
                self.assertIn("--auth", results[0].stdout)
                self.assertIn("replenishment-plan", results[0].stdout)
            # 单独验证不靠环境变量的打包定位。
            with patch.dict(os.environ, {}, clear=True):
                runtime = load("runtime_test2", scripts/"_runtime.py")
                self.assertEqual(runtime.runtime_root(), (skill/"runtime").resolve())

    def test_setup_uses_packaged_runtime_and_does_not_read_browser(self):
        with TemporaryDirectory(prefix="jst setup ") as folder:
            package = builder.build(Path(folder)/"skill.zip")
            with ZipFile(package) as archive:
                archive.extractall(folder)
            scripts = Path(folder)/"jst-ai-agent/scripts"
            runtime = (Path(folder)/"jst-ai-agent/runtime").resolve()
            with patch.dict(os.environ, {}, clear=True):
                with patch.dict(sys.modules, {"_runtime": load("runtime_setup", scripts/"_runtime.py")}):
                    setup = load("setup_test", scripts/"setup.py")
                for platform, directory in (("win32", "Scripts/python.exe"), ("linux", "bin/python"), ("darwin", "bin/python")):
                    with self.subTest(platform=platform), patch.object(sys, "platform", platform), \
                         patch.object(sys, "argv", ["setup"]), patch.object(setup.venv, "EnvBuilder") as env, \
                         patch.object(setup.subprocess, "run") as run:
                        self.assertEqual(setup.main(), 0)
                        env.return_value.create.assert_called_once_with(runtime/".venv")
                        python = str(runtime/".venv"/directory)
                        self.assertEqual(run.call_args_list[0].args[0], [python, "-m", "pip", "install", "-r", str(runtime/"requirements.txt")])
                        if platform == "darwin":
                            self.assertEqual(run.call_count, 1)
                        else:
                            self.assertEqual(run.call_args_list[1].args[0], [python, "-m", "playwright", "install", "chromium", "--no-shell"])
                        if platform != "win32":
                            self.assertTrue(os.access(runtime/"query.sh", os.X_OK))
                            self.assertTrue(os.access(scripts/"query.sh", os.X_OK))
                        self.assertFalse(any("browser-login" in c.args[0] for c in run.call_args_list))

    def test_explicit_runtime_and_missing_runtime_do_not_fall_back(self):
        script = ROOT/"skills/jst-ai-agent/scripts/_runtime.py"
        runtime = load("runtime_override", script)
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"JST_AI_HOME": folder}):
            with self.assertRaises(RuntimeError):
                runtime.runtime_root()


if __name__ == "__main__":
    unittest.main()
