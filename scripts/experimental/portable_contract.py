#!/usr/bin/env python3
"""Shared contract for nonpublishing cross-platform Redis packages."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path


VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_OID_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_VALUE_RE = re.compile(r"^[ -~]{1,512}$")
SAFE_MEMBER_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")
PACKAGE_PREFIX = "Redis"

COMMON_PATCHSET_PATHS = (
    ".github/workflows/build-experimental.yml",
    "scripts/experimental/build-portable-posix.sh",
    "scripts/experimental/create_portable_package.py",
    "scripts/experimental/portable_contract.py",
    "scripts/experimental/prepare_windows_source.py",
    "scripts/experimental/validate_portable_asset.py",
    "THIRD_PARTY_NOTICES.md",
)

BACKENDS = {
    "linux-musl1.2": {
        "os": "linux",
        "archs": {"x64", "arm64"},
        "extension": "tar.gz",
        "runtime": "musl",
        "runtime_baseline": "1.2",
        "service_backend": "openrc",
        "install_prefix": "/usr/local/redis",
        "asset_root": "packaging/musl",
        "assets": {
            "scripts/common.sh": 0o755,
            "scripts/install.sh": 0o755,
            "scripts/update.sh": 0o755,
            "scripts/uninstall.sh": 0o755,
            "openrc/redis": 0o755,
        },
    },
    "macos12": {
        "os": "macos",
        "archs": {"x64", "arm64"},
        "extension": "tar.gz",
        "runtime": "darwin",
        "runtime_baseline": "12.0",
        "service_backend": "launchd",
        "install_prefix": "/usr/local/redis",
        "asset_root": "packaging/macos",
        "assets": {
            "scripts/common.sh": 0o755,
            "scripts/install.sh": 0o755,
            "scripts/update.sh": 0o755,
            "scripts/uninstall.sh": 0o755,
            "launchd/io.github.ainuoyan.redis-unofficial.plist": 0o644,
        },
    },
    "windows-msys2": {
        "os": "windows",
        "archs": {"x64"},
        "extension": "zip",
        "runtime": "msys2",
        "runtime_baseline": "rolling",
        "service_backend": "windows-scm",
        "install_prefix": r"C:\Program Files\Redis-Unofficial",
        "asset_root": "packaging/windows",
        "assets": {
            "scripts/Common-Redis.ps1": 0o644,
            "scripts/Install-Redis.ps1": 0o644,
            "scripts/Update-Redis.ps1": 0o644,
            "scripts/Uninstall-Redis.ps1": 0o644,
        },
    },
}

GENERATED_REGULAR_MEMBERS = {
    "redis/conf/redis.conf": 0o644,
    "redis/conf/sentinel.conf": 0o644,
    "redis/PACKAGE-INFO": 0o644,
    "redis/BUILD-INFO": 0o644,
    "redis/LICENSE.txt": 0o644,
    "redis/README.txt": 0o644,
    "redis/THIRD_PARTY_NOTICES.md": 0o644,
    "redis/UPSTREAM-DEPENDENCY-NOTICES.txt": 0o644,
}


class ContractError(RuntimeError):
    """Raised when an experimental package violates its checked-in contract."""


def backend_for(variant: str) -> dict[str, object]:
    try:
        return BACKENDS[variant]
    except KeyError as exc:
        raise ContractError(f"unsupported experimental package variant: {variant}") from exc


def validate_identity(version: str, variant: str, arch: str) -> dict[str, object]:
    if VERSION_RE.fullmatch(version) is None:
        raise ContractError(f"invalid canonical Redis version: {version}")
    backend = backend_for(variant)
    if arch not in backend["archs"]:
        raise ContractError(f"unsupported architecture {arch} for {variant}")
    return backend


def archive_name(version: str, variant: str, arch: str) -> str:
    backend = validate_identity(version, variant, arch)
    return f"{PACKAGE_PREFIX}-{version}-{variant}-{arch}.{backend['extension']}"


def backend_assets(variant: str) -> dict[str, int]:
    backend = backend_for(variant)
    return dict(backend["assets"])


def patchset_paths(root: Path, variant: str) -> list[Path]:
    backend = backend_for(variant)
    relative_paths = [Path(value) for value in COMMON_PATCHSET_PATHS]
    asset_root = Path(str(backend["asset_root"]))
    relative_paths.extend(asset_root / value for value in backend_assets(variant))
    if variant == "windows-msys2":
        service_root = root / asset_root / "service/RedisService"
        if not service_root.is_dir() or service_root.is_symlink():
            raise ContractError("Windows service-wrapper source directory is missing or unsafe")
        for current_root, directory_names, file_names in os.walk(
            service_root, followlinks=False
        ):
            current = Path(current_root)
            for directory_name in directory_names:
                directory_path = current / directory_name
                if directory_path.is_symlink():
                    raise ContractError("Windows service-wrapper source contains a symlink")
            for file_name in file_names:
                relative_paths.append((current / file_name).relative_to(root))
    return sorted(set(relative_paths), key=lambda value: os.fsencode(value.as_posix()))


def require_regular_file(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ContractError(f"unsafe repository path: {relative}")
    current = root
    for component in relative.parts[:-1]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise ContractError(f"missing repository path: {relative}") from exc
        if not stat.S_ISDIR(mode) or current.is_symlink():
            raise ContractError(f"unsafe repository parent: {relative}")
    path = root / relative
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ContractError(f"missing repository file: {relative}") from exc
    if not stat.S_ISREG(mode):
        raise ContractError(f"repository source is not a regular file: {relative}")
    return path


def packaging_patchset_sha256(root: Path, variant: str) -> str:
    records = []
    for relative in patchset_paths(root, variant):
        source = require_regular_file(root, relative)
        name = relative.as_posix()
        if any(character in name for character in ("\\", "\n", "\r")):
            raise ContractError("repository path cannot be represented canonically")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        records.append(f"{digest}  {name}\n".encode("utf-8"))
    return hashlib.sha256(b"".join(records)).hexdigest()


def validate_single_line(name: str, value: str) -> str:
    if SAFE_VALUE_RE.fullmatch(value) is None or any(
        character in value for character in ("\r", "\n", "\t")
    ):
        raise ContractError(f"invalid single-line value for {name}")
    return value
