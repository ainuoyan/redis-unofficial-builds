#!/usr/bin/env python3
"""Apply reviewed Redis test-only fixes.

Redis fixed deferred-client deadlocks in the maxmemory and memory-efficiency
tests in https://github.com/redis/redis/commit/
5400b6ac65d59c6c11c119cfcb547ed0d74a9c8a. This helper applies the vendored
test-only patch to Redis 8.0.x.

Redis 8.10.1 also uses a 70-100 ms expiration window in a hash-field active
expiration test. That window repeatedly expired before its immediate HEXISTS
assertion on GitHub-hosted macOS runners. The second reviewed patch widens only
that test window and its wait bound. Redis 8.2.9 and 8.8.2 latency-monitor
tests likewise need bounded scheduling headroom on hosted macOS runners; their
lower bounds and cross-command consistency checks remain unchanged. Their
replica-flush defrag tests also scale the fragmentation allowance with jemalloc's
page size, retaining the original 2 MB at 4 KB and all lifecycle assertions.
Every applicable patch fails closed for unknown source states.
"""

from __future__ import annotations

import argparse
import re
import stat
import subprocess
from pathlib import Path


UPSTREAM_FIX_COMMIT = "5400b6ac65d59c6c11c119cfcb547ed0d74a9c8a"
UPSTREAM_PATCH_FILE = Path(__file__).with_name(
    "redis-8.0-test-tcp-deadlock.patch"
)
UPSTREAM_PATCH_TARGETS = (
    Path("tests/unit/maxmemory.tcl"),
    Path("tests/unit/memefficiency.tcl"),
)
REDIS_829_FIX_ID = "redis-8.2.9-latency-and-defrag-test-stability"
REDIS_829_PATCH_FILE = Path(__file__).with_name(
    "redis-8.2.9-test-stability.patch"
)
REDIS_829_PATCH_TARGETS = (
    Path("tests/unit/latency-monitor.tcl"),
    Path("tests/unit/memefficiency.tcl"),
)
REDIS_882_FIX_ID = "redis-8.8.2-latency-and-defrag-test-stability"
REDIS_882_PATCH_FILE = Path(__file__).with_name(
    "redis-8.8.2-test-stability.patch"
)
REDIS_882_PATCH_TARGETS = (
    Path("tests/unit/latency-monitor.tcl"),
    Path("tests/unit/memefficiency.tcl"),
)
REDIS_810_FIX_ID = "redis-8.10.1-hfe-test-timeout-stability"
REDIS_810_PATCH_FILE = Path(__file__).with_name(
    "redis-8.10.1-hfe-test-timeout.patch"
)
REDIS_810_PATCH_TARGETS = (Path("tests/unit/type/hash-field-expire.tcl"),)
MAX_TEST_FILE_BYTES = 4 * 1024 * 1024
MAX_PATCH_FILE_BYTES = 1024 * 1024
VERSION_PATTERN = re.compile(
    r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})"
)


class FixError(RuntimeError):
    """Raised when the source tree does not satisfy the patch contract."""


def _validate_regular_file(path: Path, description: str, maximum_size: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FixError(f"{description} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise FixError(f"{description} must be a regular non-symlink file")
    if metadata.st_nlink != 1:
        raise FixError(f"{description} must not have multiple hard links")
    if metadata.st_size <= 0 or metadata.st_size > maximum_size:
        raise FixError(f"{description} has an invalid size")
    if metadata.st_mode & 0o022:
        raise FixError(f"{description} must not be group- or world-writable")


def _validate_source_root(
    source_root: Path, patch_targets: tuple[Path, ...]
) -> Path:
    try:
        resolved_root = source_root.resolve(strict=True)
    except OSError as exc:
        raise FixError(f"source root is unavailable: {source_root}") from exc
    if source_root.is_symlink() or not resolved_root.is_dir():
        raise FixError("source root must be a real directory")

    checked_directories: set[Path] = set()
    for relative_target in patch_targets:
        target = resolved_root / relative_target
        for directory in (target.parent.parent, target.parent):
            if directory in checked_directories:
                continue
            checked_directories.add(directory)
            try:
                metadata = directory.lstat()
            except OSError as exc:
                raise FixError("test parent directories are unavailable") from exc
            if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
                raise FixError("test parent directories must not be symlinks")
        _validate_regular_file(target, relative_target.as_posix(), MAX_TEST_FILE_BYTES)
    return resolved_root


def _validate_patch_file(patch_file: Path) -> None:
    parent = patch_file.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise FixError("reviewed test patch parent is unavailable") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or parent.is_symlink():
        raise FixError("reviewed test patch parent must be a real directory")
    _validate_regular_file(patch_file, "reviewed test patch", MAX_PATCH_FILE_BYTES)


def _run_git_apply(
    source_root: Path, patch_file: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [
                "git",
                "apply",
                "--no-index",
                *arguments,
                str(patch_file),
            ],
            cwd=source_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FixError("unable to execute git apply for the reviewed test patch") from exc


def _apply_reviewed_patch(
    source_root: Path,
    patch_file: Path,
    patch_targets: tuple[Path, ...],
    fix_id: str,
) -> str:
    resolved_root = _validate_source_root(source_root, patch_targets)
    _validate_patch_file(patch_file)

    forward_check = _run_git_apply(resolved_root, patch_file, "--check")
    if forward_check.returncode == 0:
        application = _run_git_apply(resolved_root, patch_file)
        if application.returncode != 0:
            raise FixError("reviewed test patch application failed")
        reverse_check = _run_git_apply(
            resolved_root, patch_file, "--reverse", "--check"
        )
        if reverse_check.returncode != 0:
            raise FixError("reviewed test patch verification failed")
        return f"applied:{fix_id}"

    reverse_check = _run_git_apply(
        resolved_root, patch_file, "--reverse", "--check"
    )
    if reverse_check.returncode == 0:
        return f"present:{fix_id}"
    raise FixError("Redis tests do not match the reviewed patch states")


def apply_upstream_test_fixes(redis_version: str, source_root: Path) -> str:
    match = VERSION_PATTERN.fullmatch(redis_version)
    if match is None:
        raise FixError("Redis version must be canonical major.minor.patch")
    major, minor, patch = (int(component) for component in match.groups())
    if (major, minor) == (8, 0):
        return _apply_reviewed_patch(
            source_root,
            UPSTREAM_PATCH_FILE,
            UPSTREAM_PATCH_TARGETS,
            UPSTREAM_FIX_COMMIT,
        )
    if (major, minor, patch) == (8, 2, 9):
        return _apply_reviewed_patch(
            source_root,
            REDIS_829_PATCH_FILE,
            REDIS_829_PATCH_TARGETS,
            REDIS_829_FIX_ID,
        )
    if (major, minor, patch) == (8, 8, 2):
        return _apply_reviewed_patch(
            source_root,
            REDIS_882_PATCH_FILE,
            REDIS_882_PATCH_TARGETS,
            REDIS_882_FIX_ID,
        )
    if (major, minor, patch) == (8, 10, 1):
        return _apply_reviewed_patch(
            source_root,
            REDIS_810_PATCH_FILE,
            REDIS_810_PATCH_TARGETS,
            REDIS_810_FIX_ID,
        )
    return f"not-required:{UPSTREAM_FIX_COMMIT}"


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
