#!/usr/bin/env python3
"""Apply the minimal reviewed source adjustment required by the MSYS2 build."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


DLADDR_BLOCK_START = "void dumpX86Calls(void *addr, size_t len) {"
DLADDR_BLOCK_END = "\nvoid invalidFunctionWasCalled(void) {}"
DLADDR_GUARD = "#if !defined(__CYGWIN__) && !defined(__MSYS__)\n"
DLADDR_FALLBACK = (
    "\n#else\n"
    "void dumpCodeAroundEIP(void *eip) {\n"
    "    UNUSED(eip);\n"
    "}\n"
    "#endif /* !defined(__CYGWIN__) && !defined(__MSYS__) */\n"
)


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


def guard_unsupported_dladdr_diagnostics(debug_c: Path) -> int:
    text = debug_c.read_text(encoding="utf-8")
    guarded_start = DLADDR_GUARD + DLADDR_BLOCK_START
    guarded_end = DLADDR_FALLBACK + DLADDR_BLOCK_END
    if guarded_start in text or DLADDR_FALLBACK in text:
        if text.count(guarded_start) != 1 or text.count(guarded_end) != 1:
            raise ValueError("refusing an incomplete dladdr diagnostics guard")
        return 0

    if text.count(DLADDR_BLOCK_START) != 1:
        raise ValueError("refusing an ambiguous dumpX86Calls implementation")
    if text.count(DLADDR_BLOCK_END) != 1:
        raise ValueError("refusing an ambiguous dumpCodeAroundEIP implementation")

    start = text.index(DLADDR_BLOCK_START)
    end = text.index(DLADDR_BLOCK_END, start)
    if end <= start:
        raise ValueError("refusing an invalid dladdr diagnostics layout")
    updated = text[:start] + DLADDR_GUARD + text[start:end] + DLADDR_FALLBACK + text[end:]
    debug_c.write_text(updated, encoding="utf-8")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    makefile = source_root / "src/Makefile"
    debug_c = source_root / "src/debug.c"
    if not source_root.is_dir() or source_root.is_symlink():
        parser.error("source root must be a real directory")
    if not makefile.is_file() or makefile.is_symlink():
        parser.error("Redis src/Makefile must be a regular file")
    if not debug_c.is_file() or debug_c.is_symlink():
        parser.error("Redis src/debug.c must be a regular file")
    try:
        remove_optional_module_tests_target(makefile)
        guard_unsupported_dladdr_diagnostics(debug_c)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
