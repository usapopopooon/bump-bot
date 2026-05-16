#!/usr/bin/env python3
"""pyproject.toml から requirements.txt を生成するスクリプト。

Usage:
    python scripts/sync_requirements.py          # 生成のみ
    python scripts/sync_requirements.py --check  # 差分チェック (CI 用)
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

HEADER = """# Production dependencies for Heroku deployment
# Generated from pyproject.toml [project.dependencies]
# Do not edit manually - run: python scripts/sync_requirements.py
"""


def generate_requirements() -> str:
    """pyproject.toml から requirements.txt の内容を生成する。"""
    project_root = Path(__file__).parent.parent
    pyproject_path = project_root / "pyproject.toml"

    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    deps = project.get("dependencies", [])

    if not isinstance(deps, list):
        deps = []

    lines = [HEADER]
    for dep in deps:
        lines.append(dep)
    lines.append("")  # 末尾の改行

    return "\n".join(lines)


def main() -> int:
    """メイン関数。"""
    check_mode = "--check" in sys.argv

    project_root = Path(__file__).parent.parent
    requirements_path = project_root / "requirements.txt"

    generated = generate_requirements()

    if check_mode:
        # 差分チェックモード
        if not requirements_path.exists():
            print("ERROR: requirements.txt does not exist")
            print("Run: python scripts/sync_requirements.py")
            return 1

        current = requirements_path.read_text()
        if current != generated:
            print("ERROR: requirements.txt is out of sync with pyproject.toml")
            print("Run: python scripts/sync_requirements.py")
            return 1

        print("OK: requirements.txt is in sync")
        return 0

    # 生成モード
    requirements_path.write_text(generated)
    print(f"Generated: {requirements_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
