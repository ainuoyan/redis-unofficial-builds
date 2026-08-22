#!/usr/bin/env python3
"""Apply narrowly scoped Redis upstream test-only fixes.

The Redis 8.0 maxmemory test can deadlock on slow TCP stacks because it sends
one million deferred commands before reading any response. Redis fixed this in
https://github.com/redis/redis/commit/5400b6ac65d59c6c11c119cfcb547ed0d74a9c8a.

This helper applies only that reviewed hunk, only to Redis 8.0.x, and fails
closed if the official source no longer matches either known state.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from pathlib import Path


UPSTREAM_FIX_COMMIT = "5400b6ac65d59c6c11c119cfcb547ed0d74a9c8a"
MAX_TEST_FILE_BYTES = 4 * 1024 * 1024
VERSION_PATTERN = re.compile(
    r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})"
)

UNPATCHED_MAXMEMORY_BLOCK = """\
                set rd_master [redis_deferring_client -1]
                for {set k 0} {$k < $cmd_count} {incr k} {
                    $rd_master setrange key:0 0 [string repeat A $payload_len]
                }
                for {set k 0} {$k < $cmd_count} {incr k} {
                    $rd_master read
                }
"""

PATCHED_MAXMEMORY_BLOCK = """\
                set rd_master [redis_deferring_client -1]
                # Send commands in batches and read responses to avoid TCP deadlock.
                # Without interleaving reads, the client's send buffer fills up when
                # the server's output buffers are full (because we're not reading),
                # causing flush to block indefinitely on slow machines.
                set batch_size 10000
                for {set k 0} {$k < $cmd_count} {incr k} {
                    $rd_master setrange key:0 0 [string repeat A $payload_len]
                    if {($k + 1) % $batch_size == 0} {
                        # Drain responses to prevent TCP buffer deadlock
                        for {set j 0} {$j < $batch_size} {incr j} {
                            $rd_master read
                        }
                    }
                }
                # Read any remaining responses
                set remaining [expr {$cmd_count % $batch_size}]
                for {set k 0} {$k < $remaining} {incr k} {
                    $rd_master read
                }
"""


class FixError(RuntimeError):
    """Raised when the source tree does not satisfy the patch contract."""


def _validate_target(source_root: Path) -> Path:
    try:
        resolved_root = source_root.resolve(strict=True)
    except OSError as exc:
        raise FixError(f"source root is unavailable: {source_root}") from exc
    if source_root.is_symlink() or not resolved_root.is_dir():
        raise FixError("source root must be a real directory")

    tests_dir = resolved_root / "tests"
    unit_dir = tests_dir / "unit"
    for directory in (tests_dir, unit_dir):
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise FixError("maxmemory.tcl parent directories are unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
            raise FixError("maxmemory.tcl parent directories must not be symlinks")

    target = unit_dir / "maxmemory.tcl"
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise FixError("maxmemory.tcl is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or target.is_symlink():
        raise FixError("maxmemory.tcl must be a regular non-symlink file")
    if metadata.st_nlink != 1:
        raise FixError("maxmemory.tcl must not have multiple hard links")
    if metadata.st_size <= 0 or metadata.st_size > MAX_TEST_FILE_BYTES:
        raise FixError("maxmemory.tcl has an invalid size")
    if metadata.st_mode & 0o022:
        raise FixError("maxmemory.tcl must not be group- or world-writable")
    return target


def _replace_atomically(target: Path, content: str, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".maxmemory.tcl.", dir=target.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, stat.S_IMODE(mode))
        os.replace(temporary_path, target)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def apply_upstream_test_fixes(redis_version: str, source_root: Path) -> str:
    match = VERSION_PATTERN.fullmatch(redis_version)
    if match is None:
        raise FixError("Redis version must be canonical major.minor.patch")
    major, minor, _ = (int(component) for component in match.groups())
    if (major, minor) != (8, 0):
        return f"not-required:{UPSTREAM_FIX_COMMIT}"

    target = _validate_target(source_root)
    try:
        original = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FixError("maxmemory.tcl must be readable UTF-8") from exc

    unpatched_count = original.count(UNPATCHED_MAXMEMORY_BLOCK)
    patched_count = original.count(PATCHED_MAXMEMORY_BLOCK)
    if unpatched_count == 0 and patched_count == 1:
        return f"present:{UPSTREAM_FIX_COMMIT}"
    if unpatched_count != 1 or patched_count != 0:
        raise FixError(
            "maxmemory.tcl does not contain exactly one expected upstream block"
        )

    patched = original.replace(
        UNPATCHED_MAXMEMORY_BLOCK, PATCHED_MAXMEMORY_BLOCK, 1
    )
    metadata = target.lstat()
    _replace_atomically(target, patched, metadata.st_mode)

    verified = target.read_text(encoding="utf-8")
    if (
        verified.count(UNPATCHED_MAXMEMORY_BLOCK) != 0
        or verified.count(PATCHED_MAXMEMORY_BLOCK) != 1
    ):
        raise FixError("maxmemory.tcl fix verification failed")
    return f"applied:{UPSTREAM_FIX_COMMIT}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-version", required=True)
    parser.add_argument("--source-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(apply_upstream_test_fixes(args.redis_version, args.source_root))
    except FixError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
