#!/usr/bin/env python3
"""只打包明确列出的通用技能与运行源文件，不包含企业规则、案例或凭证。"""
import argparse
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = ("query.sh", "requirements.txt", "jushuitan.py", "jst_browser.py", "jst_analysis.py",
                 "jst_supply.py", "jst_replenishment.py", "config/replenishment-scenario-example.json")
SKILL_FILES = ("SKILL.md", "agents/openai.yaml", "scripts/query.sh", "scripts/setup.sh",
               "scripts/query.py", "scripts/setup.py", "scripts/_runtime.py",
               "references/session-access.md", "references/company-metrics.md",
               "references/sales-stock.md", "references/supply-review.md",
               "references/replenishment-plan.md", "references/replenishment.md",
               "references/analysis-workflow.md", "references/data-blueprint.md",
               "references/capability-design.md", "references/factory-analysis.md")


def build(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for source_root, names, prefix in ((ROOT/"skills/jst-ai-agent", SKILL_FILES, "jst-ai-agent/"),
                                           (ROOT, RUNTIME_FILES, "jst-ai-agent/runtime/")):
            for name in names:
                info = ZipInfo(prefix+name)
                info.create_system = 3
                info.compress_type = ZIP_DEFLATED
                mode = 0o100755 if name.endswith(".sh") else 0o100644
                info.external_attr = mode << 16
                archive.writestr(info, (source_root/name).read_bytes())
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT/"dist/jst-ai-agent.zip"))
    args = parser.parse_args()
    print(build(args.output))
