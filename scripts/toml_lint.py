#!/usr/bin/env python3
"""Robust TOML lint for local/CI checks.

Policy:
1) Parse all TOML files with stdlib ``tomllib`` (hard fail on syntax errors).
2) If ``taplo`` is available and healthy, run ``taplo check`` for stricter checks.
3) If ``taplo`` is unavailable or crashes in this environment, keep syntax lint as
   a reliable fallback so CI remains deterministic.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "htmlcov",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def iter_toml_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.toml"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def run_taplo_check(root: Path) -> tuple[bool, str]:
    taplo = shutil.which("taplo")
    if taplo is None:
        return False, "taplo not found"

    result = subprocess.run(
        [taplo, "check"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, "taplo check passed"

    stderr = (result.stderr or "").lower()
    stdout = (result.stdout or "").lower()
    combined = f"{stdout}\n{stderr}"

    # Some environments hit a taplo internal panic; prefer stable fallback.
    if "panic" in combined or "thread 'main' panicked" in combined:
        return False, "taplo panicked; falling back to syntax lint"

    # Non-panic taplo failures should fail the lint because they indicate real issues.
    print("taplo check failed:")
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    raise SystemExit(1)


def main() -> int:
    files = iter_toml_files(ROOT)
    failures: list[tuple[Path, str]] = []

    for path in files:
        try:
            with path.open("rb") as f:
                tomllib.load(f)
        except Exception as e:
            failures.append((path, str(e)))

    if failures:
        print("TOML syntax lint failed:")
        for path, err in failures:
            print(f"  - {path.relative_to(ROOT)}: {err}")
        return 1

    taplo_strict, msg = run_taplo_check(ROOT)
    if taplo_strict:
        print(f"TOML lint passed ({len(files)} files checked, strict=taplo)")
    else:
        print(f"TOML lint passed ({len(files)} files checked, strict=syntax)")
        print(f"Note: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
