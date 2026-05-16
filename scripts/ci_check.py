#!/usr/bin/env python3
"""Run CI checks locally."""

import subprocess
import sys
from typing import NamedTuple


class Check(NamedTuple):
    name: str
    command: list[str]
    is_test: bool = False


CHECKS: list[Check] = [
    Check("Requirements sync", ["python", "scripts/sync_requirements.py", "--check"]),
    Check("TOML lint", ["python", "scripts/toml_lint.py"]),
    Check("Ruff format", ["ruff", "format", "--check", "."]),
    Check("Ruff check", ["ruff", "check", "src", "tests", "scripts"]),
    Check("mypy", ["mypy", "src"]),
    Check("pytest", ["pytest", "-v"], is_test=True),
]


def run_check(check: Check) -> bool:
    print(f"\n=== {check.name} ===")
    return subprocess.run(check.command).returncode == 0


def main() -> int:
    include_tests = "--all" in sys.argv
    failed: list[str] = []

    for check in CHECKS:
        if check.is_test and not include_tests:
            continue
        if not run_check(check):
            failed.append(check.name)

    if failed:
        print("\nFailed checks:")
        for name in failed:
            print(f"- {name}")
        return 1

    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
