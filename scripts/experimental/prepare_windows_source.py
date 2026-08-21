#!/usr/bin/env python3
"""Apply the minimal reviewed source adjustment required by the MSYS2 build."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def remove_optional_module_tests_target(makefile: Path) -> int:
    text = makefile.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"^(all:.*?)(?:[ \t]+module_tests)[ \t]*$",
        r"\1",
        text,
        flags=re.MULTILINE,
    )
    if count > 1:
        raise ValueError("refusing an ambiguous Makefile with multiple module_tests targets")
    if count == 1:
        makefile.write_text(updated, encoding="utf-8")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    makefile = source_root / "src/Makefile"
    if not source_root.is_dir() or source_root.is_symlink():
        parser.error("source root must be a real directory")
    if not makefile.is_file() or makefile.is_symlink():
        parser.error("Redis src/Makefile must be a regular file")
    try:
        remove_optional_module_tests_target(makefile)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
