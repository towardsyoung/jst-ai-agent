"""验证交付包可独立定位运行文件，且不携带企业配置或个人环境。"""
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_skill", ROOT/"scripts/build_skill.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PackageTests(unittest.TestCase):
    def test_package_excludes_company_data_and_runtime_can_start_after_relocation(self):
        with TemporaryDirectory(prefix="jst delivery ") as folder:
            package = builder.build(Path(folder)/"skill.zip")
            with ZipFile(package) as archive:
                names = archive.namelist()
                self.assertTrue(all(name.startswith("jst-ai-agent/") for name in names))
                self.assertFalse(any(part in name for name in names for part in
                                     ("companies/", "cases/", "reports/", ".venv/", "Cookies")))
                for name in names:
                    data = archive.read(name)
                    self.assertNotIn(b"/Users/", data)
                archive.extractall(folder)
            skill = Path(folder)/"jst-ai-agent"
            runtime = skill/"runtime"
            (runtime/".venv/bin").mkdir(parents=True)
            python = runtime/".venv/bin/python"
            python.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n')
            python.chmod(0o755)
            for script in (runtime/"query.sh", skill/"scripts/query.sh"):
                script.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if k != "JST_AI_HOME"}
            result = subprocess.run([str(skill/"scripts/query.sh"), "--help"], cwd=folder,
                                    env=env, capture_output=True, text=True, check=True)
            self.assertIn("--chrome-profile", result.stdout)
            self.assertIn("replenishment-plan", result.stdout)

    def test_setup_uses_packaged_runtime_and_does_not_read_browser(self):
        with TemporaryDirectory(prefix="jst setup ") as folder:
            package = builder.build(Path(folder)/"skill.zip")
            with ZipFile(package) as archive: archive.extractall(folder)
            skill = Path(folder)/"jst-ai-agent"
            runtime = (skill/"runtime").resolve()
            python = Path(folder)/"fake-python"
            log = Path(folder)/"setup.log"
            python.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$JST_TEST_LOG"
if [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  chmod +x "$3/bin/python"
fi
''')
            python.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if k != "JST_AI_HOME"}
            env.update(JST_PYTHON=str(python), JST_TEST_LOG=str(log))
            subprocess.run(["sh", str(skill/"scripts/setup.sh")], cwd=folder, env=env,
                           capture_output=True, text=True, check=True)
            lines = log.read_text().splitlines()
            self.assertEqual(lines, ["-m venv "+str(runtime/".venv"),
                                     "-m pip install -r "+str(runtime/"requirements.txt")])
            self.assertTrue(os.access(runtime/"query.sh", os.X_OK))
            self.assertTrue(os.access(skill/"scripts/query.sh", os.X_OK))


if __name__ == "__main__":
    unittest.main()
