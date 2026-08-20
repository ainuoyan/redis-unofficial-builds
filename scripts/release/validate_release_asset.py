#!/usr/bin/env python3
"""Validate an immutable Linux release asset without extracting or executing it."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import tarfile
from pathlib import Path


VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$"
)
GLIBC_RE = re.compile(r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$")
VARIANT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_RE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9._-]+)\n?$")
METADATA_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=([^\x00-\x1f\x7f]*)$")
EXPECTED_METADATA_KEYS = {
    "PACKAGE_FORMAT",
    "PACKAGE_ID",
    "REDIS_VERSION",
    "REDIS_SERIES",
    "BUILD_PROFILE",
    "PACKAGE_VARIANT",
    "PACKAGE_ARCH",
    "OS",
    "LIBC",
    "MIN_GLIBC",
    "MAX_GLIBC_SYMBOL",
    "SERVICE_BACKEND",
    "INSTALL_PREFIX",
    "UPSTREAM_SOURCE_SHA256",
    "UPSTREAM_CONTRIBUTOR_LICENSE_SHA256",
    "UPSTREAM_DEPENDENCY_NOTICES_SHA256",
    "PATCHSET_SHA256",
}
REQUIRED_REGULAR_MEMBERS = {
    "redis/bin/redis-server",
    "redis/bin/redis-cli",
    "redis/bin/redis-benchmark",
    "redis/conf/redis.conf",
    "redis/conf/sentinel.conf",
    "redis/scripts/common.sh",
    "redis/scripts/install.sh",
    "redis/scripts/update.sh",
    "redis/scripts/uninstall.sh",
    "redis/systemd/redis.service",
    "redis/systemd/redis-hardening.conf.example",
    "redis/PACKAGE-INFO",
    "redis/BUILD-INFO",
    "redis/LICENSE.txt",
    "redis/README.txt",
    "redis/THIRD_PARTY_NOTICES.md",
    "redis/UPSTREAM-DEPENDENCY-NOTICES.txt",
}
OPTIONAL_REGULAR_MEMBERS = {"redis/UPSTREAM-CONTRIBUTOR-LICENSE.txt"}
ALLOWED_REGULAR_MEMBERS = REQUIRED_REGULAR_MEMBERS | OPTIONAL_REGULAR_MEMBERS
REQUIRED_EXECUTABLE_MEMBERS = {
    "redis/bin/redis-server",
    "redis/bin/redis-cli",
    "redis/bin/redis-benchmark",
    "redis/scripts/common.sh",
    "redis/scripts/install.sh",
    "redis/scripts/update.sh",
    "redis/scripts/uninstall.sh",
}
ALLOWED_SYMLINKS = {
    "redis/bin/redis-check-aof": "redis-server",
    "redis/bin/redis-check-rdb": "redis-server",
    "redis/bin/redis-sentinel": "redis-server",
}
ALLOWED_DIRECTORIES = {
    "redis",
    "redis/bin",
    "redis/conf",
    "redis/scripts",
    "redis/systemd",
}
PACKAGING_BINDINGS = {
    "redis/scripts/common.sh": "packaging/linux/scripts/common.sh",
    "redis/scripts/install.sh": "packaging/linux/scripts/install.sh",
    "redis/scripts/update.sh": "packaging/linux/scripts/update.sh",
    "redis/scripts/uninstall.sh": "packaging/linux/scripts/uninstall.sh",
    "redis/systemd/redis.service": "packaging/linux/systemd/redis.service",
    "redis/systemd/redis-hardening.conf.example": (
        "packaging/linux/systemd/redis-hardening.conf.example"
    ),
    "redis/THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
}
PATCHSET_FIXED_PATHS = (
    ".github/workflows/build-linux.yml",
    "scripts/linux/build-redis.sh",
    "THIRD_PARTY_NOTICES.md",
)
MAX_ARCHIVE_MEMBERS = 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_DECLARED_FILE_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_INFO_BYTES = 64 * 1024
MAX_BUILD_INFO_BYTES = 256 * 1024
MAX_CONTRIBUTOR_LICENSE_BYTES = 1024 * 1024
MAX_DEPENDENCY_NOTICE_FILE_BYTES = 1024 * 1024
MAX_DEPENDENCY_NOTICE_FILES = 256
MAX_DEPENDENCY_NOTICES_BYTES = 10 * 1024 * 1024
DEPENDENCY_NOTICE_BEGIN_RE = re.compile(
    rb"===== BEGIN (deps/[A-Za-z0-9._/@:+-]{1,507}) \(([1-9][0-9]*) bytes\) =====\n"
)
DEPENDENCY_NOTICE_BASENAME_RE = re.compile(
    rb"^(?:license|licence|copying|notice|copyright|readme)(?:[._-].*)?$",
    re.IGNORECASE,
)


class ValidationError(RuntimeError):
    """Raised when an asset violates the immutable package contract."""


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise ValidationError(f"invalid canonical Redis version: {value}")
    return tuple(int(part) for part in match.groups())


def parse_glibc(value: str) -> tuple[int, int]:
    match = GLIBC_RE.fullmatch(value)
    if not match:
        raise ValidationError(f"invalid glibc version: {value}")
    return tuple(int(part) for part in match.groups())


def validate_upstream_notice_payloads(
    dependency_notices: bytes,
    contributor_license: bytes | None,
    *,
    version: str,
    dependency_notices_sha256: str,
    contributor_license_sha256: str,
) -> None:
    version_parts = parse_version(version)
    if not SHA256_RE.fullmatch(dependency_notices_sha256):
        raise ValidationError("dependency notices SHA-256 metadata is invalid")
    if hashlib.sha256(dependency_notices).hexdigest() != dependency_notices_sha256:
        raise ValidationError("dependency notices do not match PACKAGE-INFO")
    if not dependency_notices or len(dependency_notices) > MAX_DEPENDENCY_NOTICES_BYTES:
        raise ValidationError("dependency notices violate the package size limit")

    expected_header = (
        "UPSTREAM_DEPENDENCY_NOTICES_FORMAT=1\n"
        f"REDIS_VERSION={version}\n"
        "SOURCE_SUBTREE=deps\n\n"
    ).encode("ascii")
    if not dependency_notices.startswith(expected_header):
        raise ValidationError("dependency notices have an invalid header")

    cursor = len(expected_header)
    previous_path: bytes | None = None
    notice_count = 0
    while cursor < len(dependency_notices):
        match = DEPENDENCY_NOTICE_BEGIN_RE.match(dependency_notices, cursor)
        if match is None:
            raise ValidationError("dependency notices have invalid framing")
        path, size_text = match.groups()
        path_parts = path.split(b"/")
        if (
            len(path_parts) < 2
            or path_parts[0] != b"deps"
            or any(part in {b"", b".", b".."} for part in path_parts)
        ):
            raise ValidationError("dependency notices contain a noncanonical path")
        basename = path.rsplit(b"/", 1)[-1]
        if DEPENDENCY_NOTICE_BASENAME_RE.fullmatch(basename) is None:
            raise ValidationError("dependency notices contain an unexpected source file")
        if previous_path is not None and path <= previous_path:
            raise ValidationError("dependency notice paths are not unique and sorted")
        if len(size_text) > 7:
            raise ValidationError("a dependency notice has an invalid declared size")
        size = int(size_text)
        if size > MAX_DEPENDENCY_NOTICE_FILE_BYTES:
            raise ValidationError("a dependency notice exceeds the source-file limit")
        body_start = match.end()
        body_end = body_start + size
        if body_end > len(dependency_notices):
            raise ValidationError("a dependency notice is truncated")
        body = dependency_notices[body_start:body_end]
        if not body or b"\x00" in body:
            raise ValidationError("a dependency notice is empty or not plain text")
        end_marker = b"\n===== END " + path + b" =====\n"
        if dependency_notices[body_end : body_end + len(end_marker)] != end_marker:
            raise ValidationError("dependency notices have a mismatched end marker")
        cursor = body_end + len(end_marker)
        previous_path = path
        notice_count += 1
        if notice_count > MAX_DEPENDENCY_NOTICE_FILES:
            raise ValidationError("dependency notices contain too many source files")
    if notice_count == 0:
        raise ValidationError("dependency notices do not contain any source files")

    contributor_required = version_parts >= (7, 4, 0)
    if contributor_license is None:
        if contributor_required:
            raise ValidationError(
                "Redis 7.4 or newer is missing its contributor license notice"
            )
        if contributor_license_sha256 != "absent":
            raise ValidationError("contributor license metadata does not record absence")
        return
    if not contributor_license or len(contributor_license) > MAX_CONTRIBUTOR_LICENSE_BYTES:
        raise ValidationError("contributor license violates the package size limit")
    if b"\x00" in contributor_license:
        raise ValidationError("contributor license is not plain text")
    if not SHA256_RE.fullmatch(contributor_license_sha256):
        raise ValidationError("contributor license SHA-256 metadata is invalid")
    if hashlib.sha256(contributor_license).hexdigest() != contributor_license_sha256:
        raise ValidationError("contributor license does not match PACKAGE-INFO")


def expected_archive_name(version: str, variant: str, arch: str) -> str:
    return f"Redis-{version}-{variant}-{arch}.tar.gz"


def validate_checksum(archive_path: Path, checksum_path: Path) -> None:
    if not archive_path.is_file() or archive_path.is_symlink():
        raise ValidationError(f"archive is not a regular file: {archive_path}")
    if not checksum_path.is_file() or checksum_path.is_symlink():
        raise ValidationError(f"checksum is not a regular file: {checksum_path}")
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValidationError("archive exceeds the compressed size limit")
    if checksum_path.stat().st_size > 256:
        raise ValidationError("checksum file is unexpectedly large")
    checksum_text = checksum_path.read_text(encoding="ascii")
    match = CHECKSUM_RE.fullmatch(checksum_text)
    if not match or match.group(2) != archive_path.name:
        raise ValidationError("checksum file must contain one canonical archive record")

    digest = hashlib.sha256()
    with archive_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != match.group(1):
        raise ValidationError("archive SHA-256 does not match its checksum file")


def validate_member_name(name: str, *, is_directory: bool) -> str:
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
    ):
        raise ValidationError(f"unsafe archive member name: {name!r}")
    candidate = name[:-1] if is_directory and name.endswith("/") else name
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(f"unsafe archive member name: {name!r}")
    if parts[0] != "redis":
        raise ValidationError(f"archive member is outside redis/: {name!r}")
    canonical = "/".join(parts)
    if name not in {canonical, f"{canonical}/"}:
        raise ValidationError(f"noncanonical archive member name: {name!r}")
    return canonical


def read_package_info(
    archive_path: Path,
) -> tuple[dict[str, str], dict[str, tarfile.TarInfo], str]:
    members: dict[str, tarfile.TarInfo] = {}
    declared_bytes = 0
    package_info_bytes: bytes | None = None
    build_info_bytes: bytes | None = None
    dependency_notices_bytes: bytes | None = None
    contributor_license_bytes: bytes | None = None

    with tarfile.open(archive_path, mode="r:gz") as archive:
        if archive.pax_headers:
            raise ValidationError("archive contains unsupported global PAX headers")
        for index, member in enumerate(archive, start=1):
            if index > MAX_ARCHIVE_MEMBERS:
                raise ValidationError("archive contains too many members")
            if member.pax_headers:
                raise ValidationError(
                    f"archive member contains unsupported PAX headers: {member.name!r}"
                )
            if member.offset_data != member.offset + tarfile.BLOCKSIZE:
                raise ValidationError(
                    f"archive member uses an unsupported extension header: {member.name!r}"
                )
            if member.issparse() or getattr(member, "sparse", None):
                raise ValidationError(
                    f"archive member uses an unsupported sparse encoding: {member.name!r}"
                )
            canonical_name = validate_member_name(
                member.name, is_directory=member.isdir()
            )
            if canonical_name in members:
                raise ValidationError(f"duplicate archive member: {canonical_name}")
            if member.uid != 0 or member.gid != 0:
                raise ValidationError(
                    f"archive member is not owned by UID/GID 0: {canonical_name}"
                )
            if (
                member.uname
                or member.gname
                or member.mtime != 0
                or member.devmajor != 0
                or member.devminor != 0
            ):
                raise ValidationError(
                    f"archive member has nonreproducible header metadata: {canonical_name}"
                )

            if member.isdir():
                if (
                    canonical_name not in ALLOWED_DIRECTORIES
                    or member.mode & 0o7777 != 0o755
                    or member.size != 0
                ):
                    raise ValidationError(
                        f"unexpected directory or mode in archive: {canonical_name}"
                    )
            elif member.isfile():
                if member.size < 0:
                    raise ValidationError(
                        f"archive member has a negative size: {canonical_name}"
                    )
                if canonical_name not in ALLOWED_REGULAR_MEMBERS:
                    raise ValidationError(
                        f"unexpected regular file in archive: {canonical_name}"
                    )
                expected_mode = (
                    0o755 if canonical_name in REQUIRED_EXECUTABLE_MEMBERS else 0o644
                )
                if member.mode & 0o7777 != expected_mode:
                    raise ValidationError(
                        f"unexpected regular-file mode in archive: {canonical_name}"
                    )
                declared_bytes += member.size
                if declared_bytes > MAX_DECLARED_FILE_BYTES:
                    raise ValidationError("archive declares too much uncompressed file data")
            elif member.issym():
                if (
                    ALLOWED_SYMLINKS.get(canonical_name) != member.linkname
                    or member.mode & 0o7777 != 0o777
                    or member.size != 0
                ):
                    raise ValidationError(f"unsafe archive symlink: {canonical_name}")
            else:
                raise ValidationError(
                    f"unsupported archive member type: {canonical_name}"
                )
            members[canonical_name] = member

            if canonical_name == "redis/PACKAGE-INFO":
                if not member.isfile() or member.size > MAX_PACKAGE_INFO_BYTES:
                    raise ValidationError("PACKAGE-INFO is not a small regular file")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValidationError("unable to read PACKAGE-INFO")
                package_info_bytes = extracted.read(MAX_PACKAGE_INFO_BYTES + 1)
            elif canonical_name == "redis/BUILD-INFO":
                if not member.isfile() or member.size > MAX_BUILD_INFO_BYTES:
                    raise ValidationError("BUILD-INFO is not a small regular file")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValidationError("unable to read BUILD-INFO")
                build_info_bytes = extracted.read(MAX_BUILD_INFO_BYTES + 1)
            elif canonical_name == "redis/UPSTREAM-DEPENDENCY-NOTICES.txt":
                if not member.isfile() or member.size > MAX_DEPENDENCY_NOTICES_BYTES:
                    raise ValidationError(
                        "dependency notices are not a size-bounded regular file"
                    )
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValidationError("unable to read dependency notices")
                dependency_notices_bytes = extracted.read(
                    MAX_DEPENDENCY_NOTICES_BYTES + 1
                )
            elif canonical_name == "redis/UPSTREAM-CONTRIBUTOR-LICENSE.txt":
                if not member.isfile() or member.size > MAX_CONTRIBUTOR_LICENSE_BYTES:
                    raise ValidationError(
                        "contributor license is not a size-bounded regular file"
                    )
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValidationError("unable to read contributor license")
                contributor_license_bytes = extracted.read(
                    MAX_CONTRIBUTOR_LICENSE_BYTES + 1
                )

    if package_info_bytes is None:
        raise ValidationError("archive does not contain PACKAGE-INFO")
    if build_info_bytes is None:
        raise ValidationError("archive does not contain BUILD-INFO")
    if dependency_notices_bytes is None:
        raise ValidationError("archive does not contain dependency notices")
    missing = sorted(REQUIRED_REGULAR_MEMBERS - members.keys())
    if missing:
        raise ValidationError(f"archive is missing required members: {', '.join(missing)}")
    for name in REQUIRED_REGULAR_MEMBERS:
        if not members[name].isfile():
            raise ValidationError(f"required member is not a regular file: {name}")
    for name in REQUIRED_EXECUTABLE_MEMBERS:
        if members[name].mode & 0o111 == 0:
            raise ValidationError(f"required member is not executable: {name}")
    missing_symlinks = sorted(ALLOWED_SYMLINKS.keys() - members.keys())
    if missing_symlinks:
        raise ValidationError(
            f"archive is missing required symlinks: {', '.join(missing_symlinks)}"
        )

    try:
        package_info_text = package_info_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("PACKAGE-INFO is not UTF-8") from exc
    values: dict[str, str] = {}
    for line in package_info_text.splitlines():
        match = METADATA_LINE_RE.fullmatch(line)
        if not match or match.group(1) in values:
            raise ValidationError("PACKAGE-INFO has an invalid or duplicate record")
        values[match.group(1)] = match.group(2)
    if set(values) != EXPECTED_METADATA_KEYS:
        raise ValidationError("PACKAGE-INFO has unknown or missing keys")
    validate_upstream_notice_payloads(
        dependency_notices_bytes,
        contributor_license_bytes,
        version=values["REDIS_VERSION"],
        dependency_notices_sha256=values["UPSTREAM_DEPENDENCY_NOTICES_SHA256"],
        contributor_license_sha256=values["UPSTREAM_CONTRIBUTOR_LICENSE_SHA256"],
    )
    try:
        build_info_text = build_info_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("BUILD-INFO is not UTF-8") from exc
    return values, members, build_info_text


def build_info_value(text: str, key: str) -> str:
    prefix = f"{key}: "
    values = [line[len(prefix) :] for line in text.splitlines() if line.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise ValidationError(f"BUILD-INFO does not contain one {key} record")
    return values[0]


def validate_build_info(
    text: str,
    *,
    version: str,
    variant: str,
    arch: str,
    source_sha256: str,
    patchset_sha256: str,
    packaging_revision: str | None,
) -> tuple[str, str]:
    expected = {
        "Redis version": version,
        "Package variant": variant,
        "Package architecture": arch,
        "Redis source SHA256": source_sha256,
        "Packaging patch-set SHA256": patchset_sha256,
    }
    for key, value in expected.items():
        if build_info_value(text, key) != value:
            raise ValidationError(f"BUILD-INFO {key} does not match")
    revision = build_info_value(text, "Packaging revision")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValidationError("BUILD-INFO packaging revision is not a commit SHA")
    if packaging_revision is not None and revision != packaging_revision:
        raise ValidationError("BUILD-INFO packaging revision does not match")
    hashes_commit = build_info_value(text, "Redis hashes snapshot")
    if not re.fullmatch(r"[0-9a-f]{40}", hashes_commit):
        raise ValidationError("BUILD-INFO Redis hashes snapshot is not a commit SHA")
    return revision, hashes_commit


def require_regular_source(path: Path, display_name: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValidationError(f"unable to inspect packaging source: {display_name}") from exc
    if not stat.S_ISREG(mode):
        raise ValidationError(f"packaging source is not a regular file: {display_name}")


def require_real_parent_directories(root: Path, relative_path: Path) -> None:
    current = root
    for component in relative_path.parts[:-1]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise ValidationError(
                f"packaging source parent is missing: {relative_path.as_posix()}"
            ) from exc
        if not stat.S_ISDIR(mode) or current.is_symlink():
            raise ValidationError(
                f"packaging source parent is not a real directory: {current}"
            )


def packaging_patchset_sha256(packaging_root: Path) -> str:
    """Reproduce build-redis.sh's sorted sha256sum-of-sha256sum patch-set hash."""

    try:
        root_mode = packaging_root.lstat().st_mode
    except OSError as exc:
        raise ValidationError("unable to inspect the packaging root") from exc
    if not stat.S_ISDIR(root_mode) or packaging_root.is_symlink():
        raise ValidationError("packaging root is not a real directory")

    relative_paths = [Path(value) for value in PATCHSET_FIXED_PATHS]
    packaging_directory = packaging_root / "packaging/linux"
    require_real_parent_directories(
        packaging_root, Path("packaging/linux/.patchset-placeholder")
    )
    try:
        packaging_mode = packaging_directory.lstat().st_mode
    except OSError as exc:
        raise ValidationError("packaging/linux is missing") from exc
    if not stat.S_ISDIR(packaging_mode) or packaging_directory.is_symlink():
        raise ValidationError("packaging/linux is not a real directory")

    def fail_walk(error: OSError) -> None:
        raise ValidationError("unable to traverse packaging/linux") from error

    for current_root, directory_names, file_names in os.walk(
        packaging_directory, onerror=fail_walk, followlinks=False
    ):
        current = Path(current_root)
        for directory_name in directory_names:
            directory_path = current / directory_name
            mode = directory_path.lstat().st_mode
            if not stat.S_ISDIR(mode) or directory_path.is_symlink():
                raise ValidationError(
                    "packaging/linux contains a symlink or special directory"
                )
        for file_name in file_names:
            file_path = current / file_name
            relative_path = file_path.relative_to(packaging_root)
            require_regular_source(file_path, relative_path.as_posix())
            relative_paths.append(relative_path)

    records: list[bytes] = []
    encoded_paths: list[tuple[bytes, Path]] = []
    for relative_path in relative_paths:
        display_name = relative_path.as_posix()
        if any(character in display_name for character in ("\\", "\n", "\r")):
            raise ValidationError("packaging source path requires sha256sum escaping")
        source_path = packaging_root / relative_path
        require_real_parent_directories(packaging_root, relative_path)
        require_regular_source(source_path, display_name)
        encoded_paths.append((os.fsencode(display_name), source_path))

    for encoded_name, source_path in sorted(encoded_paths, key=lambda item: item[0]):
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest().encode("ascii")
        records.append(digest + b"  " + encoded_name + b"\n")
    return hashlib.sha256(b"".join(records)).hexdigest()


def validate_packaging_bindings(
    archive_path: Path, packaging_root: Path, declared_patchset_sha256: str
) -> str:
    actual_patchset_sha256 = packaging_patchset_sha256(packaging_root)
    if actual_patchset_sha256 != declared_patchset_sha256:
        raise ValidationError(
            "PACKAGE-INFO patch-set SHA256 differs from the reviewed packaging tree"
        )
    with tarfile.open(archive_path, mode="r:gz") as archive:
        archive_members = {
            validate_member_name(member.name, is_directory=member.isdir()): member
            for member in archive
        }
        for archive_name, repository_name in PACKAGING_BINDINGS.items():
            repository_path = packaging_root / repository_name
            require_regular_source(repository_path, repository_name)
            member = archive_members.get(archive_name)
            if member is None or not member.isfile():
                raise ValidationError(f"archive is missing packaging file: {archive_name}")
            if member.size != repository_path.stat().st_size:
                raise ValidationError(
                    f"archive packaging file differs from the reviewed source: {archive_name}"
                )
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValidationError(f"unable to read packaging file: {archive_name}")
            matches = True
            with extracted, repository_path.open("rb") as repository_file:
                while True:
                    archive_chunk = extracted.read(1024 * 1024)
                    repository_chunk = repository_file.read(1024 * 1024)
                    if archive_chunk != repository_chunk:
                        matches = False
                        break
                    if not archive_chunk:
                        break
            if not matches:
                raise ValidationError(
                    f"archive packaging file differs from the reviewed source: {archive_name}"
                )
    return actual_patchset_sha256


def validate_metadata(
    values: dict[str, str],
    *,
    version: str,
    variant: str,
    arch: str,
    source_sha256: str,
    min_glibc: str,
) -> None:
    version_parts = parse_version(version)
    expected = {
        "PACKAGE_FORMAT": "2",
        "PACKAGE_ID": "redis-unofficial-builds",
        "REDIS_VERSION": version,
        "REDIS_SERIES": f"{version_parts[0]}.{version_parts[1]}",
        "BUILD_PROFILE": "core",
        "PACKAGE_VARIANT": variant,
        "PACKAGE_ARCH": arch,
        "OS": "linux",
        "LIBC": "glibc",
        "MIN_GLIBC": min_glibc,
        "SERVICE_BACKEND": "systemd",
        "INSTALL_PREFIX": "/usr/local/redis",
        "UPSTREAM_SOURCE_SHA256": source_sha256,
    }
    for key, expected_value in expected.items():
        if values.get(key) != expected_value:
            raise ValidationError(
                f"PACKAGE-INFO {key} does not match the release contract"
            )
    if not SHA256_RE.fullmatch(values["PATCHSET_SHA256"]):
        raise ValidationError("PACKAGE-INFO PATCHSET_SHA256 is invalid")
    if not SHA256_RE.fullmatch(values["UPSTREAM_DEPENDENCY_NOTICES_SHA256"]):
        raise ValidationError(
            "PACKAGE-INFO UPSTREAM_DEPENDENCY_NOTICES_SHA256 is invalid"
        )
    contributor_sha256 = values["UPSTREAM_CONTRIBUTOR_LICENSE_SHA256"]
    if contributor_sha256 != "absent" and not SHA256_RE.fullmatch(
        contributor_sha256
    ):
        raise ValidationError(
            "PACKAGE-INFO UPSTREAM_CONTRIBUTOR_LICENSE_SHA256 is invalid"
        )
    maximum_glibc = parse_glibc(values["MAX_GLIBC_SYMBOL"])
    if maximum_glibc > parse_glibc(min_glibc):
        raise ValidationError("package exceeds its declared glibc baseline")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--checksum", type=Path, required=True)
    parser.add_argument("--redis-version", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--min-glibc", required=True)
    parser.add_argument("--packaging-root", type=Path)
    parser.add_argument("--packaging-revision")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        parse_version(args.redis_version)
        if not VARIANT_RE.fullmatch(args.variant):
            raise ValidationError(f"invalid package variant: {args.variant}")
        if not SHA256_RE.fullmatch(args.source_sha256):
            raise ValidationError("invalid source SHA-256")
        parse_glibc(args.min_glibc)
        if args.packaging_revision is not None and not re.fullmatch(
            r"[0-9a-f]{40}", args.packaging_revision
        ):
            raise ValidationError("invalid packaging revision")
        expected_name = expected_archive_name(
            args.redis_version, args.variant, args.arch
        )
        if args.archive.name != expected_name:
            raise ValidationError(
                f"unexpected archive name: {args.archive.name}; expected {expected_name}"
            )
        if args.checksum.name != f"{expected_name}.sha256":
            raise ValidationError("unexpected checksum filename")
        validate_checksum(args.archive, args.checksum)
        values, _, build_info = read_package_info(args.archive)
        validate_metadata(
            values,
            version=args.redis_version,
            variant=args.variant,
            arch=args.arch,
            source_sha256=args.source_sha256,
            min_glibc=args.min_glibc,
        )
        validate_build_info(
            build_info,
            version=args.redis_version,
            variant=args.variant,
            arch=args.arch,
            source_sha256=args.source_sha256,
            patchset_sha256=values["PATCHSET_SHA256"],
            packaging_revision=args.packaging_revision,
        )
        if args.packaging_root is not None:
            validate_packaging_bindings(
                args.archive, args.packaging_root, values["PATCHSET_SHA256"]
            )
        print(f"Validated immutable release asset: {args.archive.name}")
        return 0
    except (OSError, UnicodeError, EOFError, tarfile.TarError, ValidationError) as exc:
        print(f"release asset validation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
