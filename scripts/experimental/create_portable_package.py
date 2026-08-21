#!/usr/bin/env python3
"""Create a deterministic, nonpublishing cross-platform Redis package."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from portable_contract import (
    ContractError,
    archive_name,
    backend_assets,
    packaging_patchset_sha256,
    require_regular_file,
    validate_identity,
    validate_single_line,
)


MAX_BINARY_BYTES = 128 * 1024 * 1024
MAX_NOTICE_FILES = 256
MAX_NOTICE_FILE_BYTES = 1024 * 1024
MAX_NOTICE_SOURCE_BYTES = 8 * 1024 * 1024
MAX_NOTICE_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_CONTRIBUTOR_LICENSE_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 8 * 1024 * 1024
NOTICE_NAME_RE = re.compile(
    r"^(?:license|licence|copying|notice|copyright|readme)(?:[._-].*)?$",
    re.IGNORECASE,
)
WINDOWS_DLL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,126}\.dll$", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--binary-dir", type=Path, required=True)
    parser.add_argument("--service-wrapper", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--packaging-root", type=Path, required=True)
    parser.add_argument("--redis-version", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--hashes-commit", required=True)
    parser.add_argument("--packaging-revision", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--build-environment", required=True)
    parser.add_argument("--compiler", required=True)
    return parser.parse_args()


def require_real_directory(path: Path, description: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ContractError(f"unable to inspect {description}: {path}") from exc
    if not stat.S_ISDIR(mode) or path.is_symlink():
        raise ContractError(f"{description} must be a real directory: {path}")
    return path


def copy_regular(source: Path, destination: Path, mode: int) -> None:
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise ContractError(f"unable to inspect package input: {source}") from exc
    if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
        raise ContractError(f"package input is not a regular file: {source}")
    if metadata.st_size <= 0 or metadata.st_size > MAX_BINARY_BYTES:
        raise ContractError(f"package input violates the size limit: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def collect_dependency_notices(source_root: Path, version: str) -> bytes:
    deps_root = require_real_directory(source_root / "deps", "Redis deps directory")
    selected: list[tuple[str, Path, int]] = []
    total = 0
    for current_root, directory_names, file_names in os.walk(
        deps_root, followlinks=False
    ):
        current = Path(current_root)
        for directory_name in directory_names:
            if (current / directory_name).is_symlink():
                raise ContractError("Redis deps contains a symlinked directory")
        for file_name in file_names:
            if NOTICE_NAME_RE.fullmatch(file_name) is None:
                continue
            candidate = current / file_name
            relative = candidate.relative_to(source_root).as_posix()
            if len(relative) > 512 or re.fullmatch(r"deps/[A-Za-z0-9._/@:+-]+", relative) is None:
                raise ContractError(f"unsafe dependency notice path: {relative}")
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode) or candidate.is_symlink():
                raise ContractError(f"dependency notice is not a regular file: {relative}")
            if metadata.st_size <= 0 or metadata.st_size > MAX_NOTICE_FILE_BYTES:
                raise ContractError(f"dependency notice violates the size limit: {relative}")
            total += metadata.st_size
            if total > MAX_NOTICE_SOURCE_BYTES:
                raise ContractError("dependency notice sources exceed the size limit")
            selected.append((relative, candidate, metadata.st_size))
            if len(selected) > MAX_NOTICE_FILES:
                raise ContractError("too many dependency notice sources")
    selected.sort(key=lambda item: item[0].encode("utf-8"))
    if not selected:
        raise ContractError("no dependency notices were found in the Redis source")
    output = bytearray(
        (
            "UPSTREAM_DEPENDENCY_NOTICES_FORMAT=1\n"
            f"REDIS_VERSION={version}\n"
            "SOURCE_SUBTREE=deps\n\n"
        ).encode("ascii")
    )
    for relative, candidate, size in selected:
        body = candidate.read_bytes()
        if len(body) != size or b"\x00" in body:
            raise ContractError(f"dependency notice is not stable plain text: {relative}")
        output.extend(f"===== BEGIN {relative} ({size} bytes) =====\n".encode("ascii"))
        output.extend(body)
        output.extend(f"\n===== END {relative} =====\n".encode("ascii"))
    if len(output) > MAX_NOTICE_OUTPUT_BYTES:
        raise ContractError("generated dependency notices exceed the size limit")
    return bytes(output)


def source_license(source_root: Path) -> Path:
    for name in ("LICENSE.txt", "COPYING"):
        candidate = source_root / name
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise ContractError("Redis source does not contain LICENSE.txt or COPYING")


def sanitize_config(source: Path) -> bytes:
    metadata = source.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or source.is_symlink()
        or metadata.st_size <= 0
        or metadata.st_size > MAX_CONFIG_BYTES
    ):
        raise ContractError(f"Redis configuration violates the size/type contract: {source}")
    text = source.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        if re.match(r"^[ \t]*loadmodule[ \t]", line, re.IGNORECASE):
            lines.append(f"# Disabled by the experimental core profile: {line}")
        else:
            lines.append(line)
    return ("\n".join(lines) + "\n").encode("utf-8")


def copy_binaries(
    binary_dir: Path,
    package_root: Path,
    os_name: str,
    service_wrapper: Path | None,
) -> None:
    suffix = ".exe" if os_name == "windows" else ""
    for name in ("redis-server", "redis-cli", "redis-benchmark"):
        copy_regular(binary_dir / f"{name}{suffix}", package_root / "bin" / f"{name}{suffix}", 0o755)
    for alias in ("redis-check-aof", "redis-check-rdb", "redis-sentinel"):
        copy_regular(
            binary_dir / f"redis-server{suffix}",
            package_root / "bin" / f"{alias}{suffix}",
            0o755,
        )
    if os_name != "windows":
        if service_wrapper is not None:
            raise ContractError("service wrapper is valid only for Windows packages")
        return
    dlls = sorted(binary_dir.glob("*.dll"), key=lambda value: value.name.lower())
    if not dlls or not any(value.name.lower() == "msys-2.0.dll" for value in dlls):
        raise ContractError("MSYS2 package is missing msys-2.0.dll")
    for dll in dlls:
        if WINDOWS_DLL_RE.fullmatch(dll.name) is None:
            raise ContractError(f"unsafe Windows runtime DLL name: {dll.name}")
        copy_regular(dll, package_root / "bin" / dll.name, 0o755)
    if service_wrapper is None:
        raise ContractError("Windows package requires the reviewed service wrapper")
    copy_regular(service_wrapper, package_root / "bin/RedisService.exe", 0o755)
    copy_regular(
        binary_dir / "MSYS2-RUNTIME-NOTICES.txt",
        package_root / "MSYS2-RUNTIME-NOTICES.txt",
        0o644,
    )


def write_metadata(
    package_root: Path,
    *,
    args: argparse.Namespace,
    backend: dict[str, object],
    notices: bytes,
    contributor_digest: str,
    patchset_digest: str,
) -> None:
    version_parts = args.redis_version.split(".")
    package_info = {
        "PACKAGE_FORMAT": "3",
        "PACKAGE_STATUS": "experimental",
        "PACKAGE_ID": "redis-unofficial-builds",
        "REDIS_VERSION": args.redis_version,
        "REDIS_SERIES": ".".join(version_parts[:2]),
        "BUILD_PROFILE": "core",
        "PACKAGE_VARIANT": args.variant,
        "PACKAGE_ARCH": args.arch,
        "OS": str(backend["os"]),
        "RUNTIME": str(backend["runtime"]),
        "RUNTIME_BASELINE": str(backend["runtime_baseline"]),
        "SERVICE_BACKEND": str(backend["service_backend"]),
        "INSTALL_PREFIX": str(backend["install_prefix"]),
        "UPSTREAM_SOURCE_SHA256": args.source_sha256,
        "UPSTREAM_CONTRIBUTOR_LICENSE_SHA256": contributor_digest,
        "UPSTREAM_DEPENDENCY_NOTICES_SHA256": hashlib.sha256(notices).hexdigest(),
        "PATCHSET_SHA256": patchset_digest,
    }
    package_root.joinpath("PACKAGE-INFO").write_text(
        "".join(f"{key}={value}\n" for key, value in package_info.items()),
        encoding="utf-8",
    )
    package_root.joinpath("PACKAGE-INFO").chmod(0o644)
    build_info = (
        f"Redis version: {args.redis_version}\n"
        f"Package variant: {args.variant}\n"
        f"Package architecture: {args.arch}\n"
        "Package status: experimental; GitHub Release publication is disabled\n"
        f"Build environment: {validate_single_line('build environment', args.build_environment)}\n"
        f"Compiler: {validate_single_line('compiler', args.compiler)}\n"
        f"Redis source SHA256: {args.source_sha256}\n"
        f"Redis hashes snapshot: {args.hashes_commit}\n"
        f"Packaging patch-set SHA256: {patchset_digest}\n"
        f"Packaging revision: {args.packaging_revision}\n"
    )
    package_root.joinpath("BUILD-INFO").write_text(build_info, encoding="utf-8")
    package_root.joinpath("BUILD-INFO").chmod(0o644)


def package_readme(args: argparse.Namespace, backend: dict[str, object]) -> str:
    lifecycle = {
        "linux-musl1.2": """Host prerequisites: bash, OpenRC, getent, util-linux (flock, findmnt,
setpriv), tar, and standard POSIX account/file utilities. The default service
listens only on /usr/local/redis/data/redis.sock.

Install:   sudo ./scripts/install.sh
Update:    sudo ./scripts/update.sh   (run from the newly extracted package)
Uninstall: sudo /usr/local/redis/scripts/uninstall.sh
Purge:     sudo /usr/local/redis/scripts/uninstall.sh --purge

主机前提：bash、OpenRC、getent、util-linux（flock、findmnt、setpriv）、tar
及标准 POSIX 账号/文件工具。默认服务只监听
/usr/local/redis/data/redis.sock。""",
        "macos12": """Host prerequisites: macOS 12 or newer and an Administrator account. The
default service listens only on /usr/local/redis/data/redis.sock.

Install:   sudo ./scripts/install.sh
Update:    sudo ./scripts/update.sh   (run from the newly extracted package)
Uninstall: sudo /usr/local/redis/scripts/uninstall.sh
Purge:     sudo /usr/local/redis/scripts/uninstall.sh --purge

主机前提：macOS 12 或更高版本及管理员账号。默认服务只监听
/usr/local/redis/data/redis.sock。""",
        "windows-msys2": r"""Host prerequisites: x64 Windows and an elevated Windows PowerShell 5.1
or newer session. The fixed service endpoint is 127.0.0.1:6379.

Install:   .\scripts\Install-Redis.ps1
Update:    .\scripts\Update-Redis.ps1   (run from the newly extracted package)
Uninstall: & 'C:\Program Files\Redis-Rzon\scripts\Uninstall-Redis.ps1'
Purge:     & 'C:\Program Files\Redis-Rzon\scripts\Uninstall-Redis.ps1' -Purge

主机前提：x64 Windows，以及以管理员身份运行的 Windows PowerShell 5.1 或
更高版本。固定服务端点为 127.0.0.1:6379。""",
    }[args.variant]
    return f"""Redis {args.redis_version} experimental unofficial package
Redis {args.redis_version} 实验性非官方安装包

Variant: {args.variant}
Architecture: {args.arch}
Runtime: {backend['runtime']} {backend['runtime_baseline']}
Service backend: {backend['service_backend']}
Install prefix: {backend['install_prefix']}

Before extraction, verify the adjacent .sha256 record against this archive.
解压前请先使用相邻的 .sha256 记录校验压缩包。

{lifecycle}

This artifact is produced only by the manual experimental workflow. It is not
eligible for GitHub Release publication or production-support claims until its
native lifecycle and compatibility acceptance gates have passed.

此产物仅由手工实验构建工作流生成。在原生平台生命周期与兼容性验收通过前，不得上传到
GitHub Release，也不得宣称可用于生产环境。

Review scripts/ and the platform service template before installation. Preserve
conf/ and data/ independently before every update or removal operation.
安装前请审查 scripts/ 与平台服务模板；每次更新或卸载前应另行备份 conf/ 和 data/。
"""


def normalized_entries(package_root: Path) -> list[tuple[str, Path, bool]]:
    entries: list[tuple[str, Path, bool]] = [("redis", package_root, True)]
    for current_root, directory_names, file_names in os.walk(
        package_root, followlinks=False
    ):
        current = Path(current_root)
        directory_names.sort()
        file_names.sort()
        for directory_name in directory_names:
            path = current / directory_name
            if path.is_symlink():
                raise ContractError("package staging tree contains a symlink")
            relative = path.relative_to(package_root).as_posix()
            entries.append((f"redis/{relative}", path, True))
        for file_name in file_names:
            path = current / file_name
            if path.is_symlink() or not path.is_file():
                raise ContractError("package staging tree contains a special file")
            relative = path.relative_to(package_root).as_posix()
            entries.append((f"redis/{relative}", path, False))
    return sorted(entries, key=lambda item: item[0].encode("utf-8"))


def write_tar_gz(package_root: Path, output: Path) -> None:
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, source, is_directory in normalized_entries(package_root):
                    info = tarfile.TarInfo(name)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    if is_directory:
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        archive.addfile(info)
                    else:
                        info.mode = source.stat().st_mode & 0o777
                        info.size = source.stat().st_size
                        with source.open("rb") as handle:
                            archive.addfile(info, handle)


def write_zip(package_root: Path, output: Path) -> None:
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, source, is_directory in normalized_entries(package_root):
            member_name = f"{name}/" if is_directory else name
            info = zipfile.ZipInfo(member_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if is_directory else source.stat().st_mode & 0o777
            info.external_attr = ((stat.S_IFDIR if is_directory else stat.S_IFREG) | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, b"" if is_directory else source.read_bytes())


def copy_exclusive(source: Path, destination: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o644)
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            descriptor = -1
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        destination.unlink(missing_ok=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        backend = validate_identity(args.redis_version, args.variant, args.arch)
        if re.fullmatch(r"[0-9a-f]{64}", args.source_sha256) is None:
            raise ContractError("invalid Redis source SHA-256")
        if re.fullmatch(r"[0-9a-f]{40}", args.hashes_commit) is None:
            raise ContractError("invalid redis-hashes commit")
        if re.fullmatch(r"[0-9a-f]{40}", args.packaging_revision) is None:
            raise ContractError("invalid packaging revision")
        source_root = require_real_directory(args.source_root.resolve(), "Redis source root")
        binary_dir = require_real_directory(args.binary_dir.resolve(), "binary directory")
        packaging_root = require_real_directory(args.packaging_root.resolve(), "packaging root")
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        name = archive_name(args.redis_version, args.variant, args.arch)
        output = output_dir / name
        checksum = output_dir / f"{name}.sha256"
        if output.exists() or output.is_symlink() or checksum.exists() or checksum.is_symlink():
            raise ContractError("refusing to overwrite an existing package output")

        with tempfile.TemporaryDirectory(prefix="redis-portable-package-") as directory:
            package_root = Path(directory) / "redis"
            package_root.mkdir(mode=0o755)
            copy_binaries(binary_dir, package_root, str(backend["os"]), args.service_wrapper)
            package_root.joinpath("conf").mkdir(mode=0o755)
            package_root.joinpath("conf/redis.conf").write_bytes(
                sanitize_config(source_root / "redis.conf")
            )
            package_root.joinpath("conf/sentinel.conf").write_bytes(
                sanitize_config(source_root / "sentinel.conf")
            )
            package_root.joinpath("conf/redis.conf").chmod(0o644)
            package_root.joinpath("conf/sentinel.conf").chmod(0o644)
            copy_regular(source_license(source_root), package_root / "LICENSE.txt", 0o644)
            copy_regular(
                packaging_root / "THIRD_PARTY_NOTICES.md",
                package_root / "THIRD_PARTY_NOTICES.md",
                0o644,
            )

            notices = collect_dependency_notices(source_root, args.redis_version)
            package_root.joinpath("UPSTREAM-DEPENDENCY-NOTICES.txt").write_bytes(notices)
            package_root.joinpath("UPSTREAM-DEPENDENCY-NOTICES.txt").chmod(0o644)
            contributor = source_root / "REDISCONTRIBUTIONS.txt"
            contributor_required = tuple(map(int, args.redis_version.split("."))) >= (7, 4, 0)
            contributor_digest = "absent"
            if contributor.exists() or contributor.is_symlink():
                if contributor.is_symlink() or not contributor.is_file():
                    raise ContractError("REDISCONTRIBUTIONS.txt is not a regular file")
                body = contributor.read_bytes()
                if not body or len(body) > MAX_CONTRIBUTOR_LICENSE_BYTES or b"\x00" in body:
                    raise ContractError("REDISCONTRIBUTIONS.txt violates the package contract")
                package_root.joinpath("UPSTREAM-CONTRIBUTOR-LICENSE.txt").write_bytes(body)
                package_root.joinpath("UPSTREAM-CONTRIBUTOR-LICENSE.txt").chmod(0o644)
                contributor_digest = hashlib.sha256(body).hexdigest()
            elif contributor_required:
                raise ContractError("Redis 7.4 or newer requires REDISCONTRIBUTIONS.txt")

            asset_root = Path(str(backend["asset_root"]))
            for relative, mode in backend_assets(args.variant).items():
                source = require_regular_file(packaging_root, asset_root / relative)
                copy_regular(source, package_root / relative, mode)
            package_root.joinpath("README.txt").write_text(
                package_readme(args, backend), encoding="utf-8"
            )
            package_root.joinpath("README.txt").chmod(0o644)
            patchset_digest = packaging_patchset_sha256(packaging_root, args.variant)
            write_metadata(
                package_root,
                args=args,
                backend=backend,
                notices=notices,
                contributor_digest=contributor_digest,
                patchset_digest=patchset_digest,
            )

            temporary_output = Path(directory) / name
            if backend["extension"] == "zip":
                write_zip(package_root, temporary_output)
            else:
                write_tar_gz(package_root, temporary_output)
            digest = hashlib.sha256(temporary_output.read_bytes()).hexdigest()
            temporary_checksum = Path(directory) / f"{name}.sha256"
            temporary_checksum.write_text(f"{digest}  {name}\n", encoding="ascii")
            created_outputs: list[Path] = []
            try:
                copy_exclusive(temporary_output, output)
                created_outputs.append(output)
                copy_exclusive(temporary_checksum, checksum)
                created_outputs.append(checksum)
            except BaseException:
                for created_output in created_outputs:
                    created_output.unlink(missing_ok=True)
                raise
        print(f"Created experimental package: {output}")
        return 0
    except (ContractError, OSError, UnicodeError, ValueError, tarfile.TarError) as exc:
        print(f"experimental package error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
