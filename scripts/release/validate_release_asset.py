#!/usr/bin/env python3
"""Validate an immutable Linux release asset without extracting or executing it."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import struct
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
ELF_BINARY_MEMBERS = (
    "redis/bin/redis-server",
    "redis/bin/redis-cli",
    "redis/bin/redis-benchmark",
)
ELF_MACHINE_BY_ARCH = {"x64": 0x3E, "arm64": 0xB7}
ELF_INTERPRETER_BY_ARCH = {
    "x64": "/lib64/ld-linux-x86-64.so.2",
    "arm64": "/lib/ld-linux-aarch64.so.1",
}
MAX_ELF_BINARY_BYTES = 128 * 1024 * 1024
ELF_HEADER = struct.Struct("<16sHHIQQQIHHHHHH")
ELF_PROGRAM_HEADER = struct.Struct("<IIQQQQQQ")
ELF_SECTION_HEADER = struct.Struct("<IIQQQQIIQQ")
ELF_DYNAMIC_ENTRY = struct.Struct("<qQ")
ELF_VERSION_NEED = struct.Struct("<HHIII")
ELF_VERSION_AUX = struct.Struct("<IHHII")
PT_LOAD = 1
PT_DYNAMIC = 2
PT_INTERP = 3
PT_GNU_STACK = 0x6474E551
PF_X = 1
PF_W = 2
DT_NULL = 0
DT_NEEDED = 1
DT_STRTAB = 5
DT_STRSZ = 10
DT_VERNEED = 0x6FFFFFFE
DT_VERNEEDNUM = 0x6FFFFFFF
SHT_NOBITS = 8
SHT_STRTAB = 3
GLIBC_SYMBOL_RE = re.compile(
    rb"^GLIBC_(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})"
    rb"(?:\.(0|[1-9][0-9]{0,5}))?$"
)
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
    return f"Redis-Rzon-{version}-{variant}-{arch}.tar.gz"


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


def bounded_slice(data: bytes, offset: int, size: int, description: str) -> bytes:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise ValidationError(f"ELF {description} is outside the binary")
    return data[offset : offset + size]


def elf_c_string(table: bytes, offset: int, description: str) -> str:
    if offset < 0 or offset >= len(table):
        raise ValidationError(f"ELF {description} has an invalid string offset")
    end = table.find(b"\x00", offset)
    if end < 0:
        raise ValidationError(f"ELF {description} is not NUL-terminated")
    raw = table[offset:end]
    if not raw or any(byte < 0x20 or byte >= 0x7F for byte in raw):
        raise ValidationError(f"ELF {description} is not canonical ASCII")
    return raw.decode("ascii")


def elf_vaddr_to_offset(
    program_headers: list[tuple[int, ...]], address: int, size: int
) -> int:
    for header in program_headers:
        p_type, _, p_offset, p_vaddr, _, p_filesz, _, _ = header
        if p_type != PT_LOAD or address < p_vaddr:
            continue
        relative = address - p_vaddr
        if relative <= p_filesz and size <= p_filesz - relative:
            return p_offset + relative
    raise ValidationError("ELF dynamic address is not backed by a loadable file range")


def parse_glibc_symbol(value: bytes) -> tuple[int, int, int] | None:
    match = GLIBC_SYMBOL_RE.fullmatch(value)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or b"0")


def format_glibc_symbol(value: tuple[int, int, int]) -> str:
    major, minor, patch = value
    if patch:
        return f"{major}.{minor}.{patch}"
    return f"{major}.{minor}"


def validate_elf_binary(
    data: bytes, *, name: str, arch: str, version: str
) -> tuple[int, int, int]:
    if data[:4] != b"\x7fELF":
        raise ValidationError(f"binary is not an ELF file: {name}")
    if len(data) < ELF_HEADER.size:
        raise ValidationError(f"binary has a truncated ELF header: {name}")
    (
        ident,
        elf_type,
        machine,
        elf_version,
        entry,
        program_offset,
        section_offset,
        _,
        header_size,
        program_entry_size,
        program_count,
        section_entry_size,
        section_count,
        section_names_index,
    ) = ELF_HEADER.unpack_from(data)
    if ident[4:7] != b"\x02\x01\x01" or ident[7] not in {0, 3} or ident[8] != 0:
        raise ValidationError(f"binary is not a supported ELF64 file: {name}")
    if elf_type not in {2, 3} or elf_version != 1 or header_size != ELF_HEADER.size:
        raise ValidationError(f"binary has an invalid ELF header: {name}")
    if machine != ELF_MACHINE_BY_ARCH[arch]:
        raise ValidationError(f"binary architecture does not match {arch}: {name}")
    if (
        program_entry_size != ELF_PROGRAM_HEADER.size
        or not 1 <= program_count <= 1024
        or program_offset < ELF_HEADER.size
        or program_offset % 8 != 0
    ):
        raise ValidationError(f"binary has an invalid ELF program-header table: {name}")
    bounded_slice(
        data,
        program_offset,
        program_entry_size * program_count,
        "program-header table",
    )
    if (
        section_entry_size != ELF_SECTION_HEADER.size
        or not 1 <= section_count < 0xFF00
        or section_names_index <= 0
        or section_names_index >= section_count
        or section_offset < ELF_HEADER.size
        or section_offset % 8 != 0
    ):
        raise ValidationError(f"binary has an invalid ELF section-header table: {name}")
    bounded_slice(
        data,
        section_offset,
        section_entry_size * section_count,
        "section-header table",
    )

    program_headers = [
        ELF_PROGRAM_HEADER.unpack_from(data, program_offset + index * program_entry_size)
        for index in range(program_count)
    ]
    load_headers = []
    dynamic_headers = []
    interpreter_headers = []
    stack_headers = []
    for header in program_headers:
        p_type, flags, offset, vaddr, _, file_size, memory_size, alignment = header
        bounded_slice(data, offset, file_size, "program segment")
        if memory_size < file_size:
            raise ValidationError(f"ELF segment is larger on disk than in memory: {name}")
        if alignment not in {0, 1}:
            if alignment & (alignment - 1) or offset % alignment != vaddr % alignment:
                raise ValidationError(f"ELF segment has invalid alignment: {name}")
        if p_type == PT_LOAD:
            if flags & PF_X and flags & PF_W:
                raise ValidationError(f"ELF has a writable executable segment: {name}")
            load_headers.append(header)
        elif p_type == PT_DYNAMIC:
            dynamic_headers.append(header)
        elif p_type == PT_INTERP:
            interpreter_headers.append(header)
        elif p_type == PT_GNU_STACK:
            stack_headers.append(header)
    executable_loads = [header for header in load_headers if header[1] & PF_X]
    if not load_headers or not executable_loads:
        raise ValidationError(f"ELF has no executable loadable segment: {name}")
    if not any(header[3] <= entry < header[3] + header[6] for header in executable_loads):
        raise ValidationError(f"ELF entry point is not executable: {name}")
    if len(interpreter_headers) != 1 or len(dynamic_headers) != 1:
        raise ValidationError(f"ELF must have one interpreter and dynamic segment: {name}")
    if len(stack_headers) != 1 or stack_headers[0][1] & PF_X:
        raise ValidationError(f"ELF does not declare a non-executable stack: {name}")

    interpreter_header = interpreter_headers[0]
    interpreter_data = bounded_slice(
        data, interpreter_header[2], interpreter_header[5], "interpreter"
    )
    if (
        not interpreter_data.endswith(b"\x00")
        or b"\x00" in interpreter_data[:-1]
        or len(interpreter_data) > 4096
    ):
        raise ValidationError(f"ELF interpreter is malformed: {name}")
    try:
        interpreter = interpreter_data[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"ELF interpreter is not ASCII: {name}") from exc
    if interpreter != ELF_INTERPRETER_BY_ARCH[arch]:
        raise ValidationError(
            f"ELF interpreter does not match glibc {arch}: {name}"
        )

    section_headers = [
        ELF_SECTION_HEADER.unpack_from(data, section_offset + index * section_entry_size)
        for index in range(section_count)
    ]
    if any(section_headers[0]):
        raise ValidationError(f"ELF null section is not empty: {name}")
    for section in section_headers[1:]:
        _, section_type, _, _, offset, size, _, _, alignment, entry_size = section
        if section_type != SHT_NOBITS:
            bounded_slice(data, offset, size, "section")
        if alignment not in {0, 1} and alignment & (alignment - 1):
            raise ValidationError(f"ELF section has invalid alignment: {name}")
        if entry_size and size % entry_size:
            raise ValidationError(f"ELF section has a partial entry: {name}")
    names_header = section_headers[section_names_index]
    if names_header[1] != SHT_STRTAB:
        raise ValidationError(f"ELF section-name table is invalid: {name}")
    section_names = bounded_slice(data, names_header[4], names_header[5], "section names")
    if not section_names or section_names[0] != 0:
        raise ValidationError(f"ELF section-name table is malformed: {name}")
    observed_section_names = {
        elf_c_string(section_names, section[0], "section name")
        for section in section_headers[1:]
    }
    required_sections = {".interp", ".dynstr", ".dynamic", ".gnu.version_r"}
    if not required_sections.issubset(observed_section_names):
        raise ValidationError(f"ELF is missing required dynamic sections: {name}")

    dynamic_header = dynamic_headers[0]
    dynamic_data = bounded_slice(
        data, dynamic_header[2], dynamic_header[5], "dynamic segment"
    )
    if not dynamic_data or len(dynamic_data) % ELF_DYNAMIC_ENTRY.size:
        raise ValidationError(f"ELF dynamic segment is malformed: {name}")
    dynamic_values: dict[int, list[int]] = {}
    terminated = False
    for offset in range(0, len(dynamic_data), ELF_DYNAMIC_ENTRY.size):
        tag, value = ELF_DYNAMIC_ENTRY.unpack_from(dynamic_data, offset)
        if tag == DT_NULL:
            terminated = True
            break
        dynamic_values.setdefault(tag, []).append(value)
    if not terminated:
        raise ValidationError(f"ELF dynamic segment is not terminated: {name}")
    for required_tag in (DT_STRTAB, DT_STRSZ, DT_VERNEED, DT_VERNEEDNUM):
        if len(dynamic_values.get(required_tag, [])) != 1:
            raise ValidationError(f"ELF dynamic metadata is incomplete: {name}")
    needed_offsets = dynamic_values.get(DT_NEEDED, [])
    if not needed_offsets:
        raise ValidationError(f"ELF has no shared-library dependencies: {name}")
    string_size = dynamic_values[DT_STRSZ][0]
    if not 1 <= string_size <= 16 * 1024 * 1024:
        raise ValidationError(f"ELF dynamic string table is unreasonably large: {name}")
    string_offset = elf_vaddr_to_offset(
        program_headers, dynamic_values[DT_STRTAB][0], string_size
    )
    dynamic_strings = bounded_slice(data, string_offset, string_size, "dynamic strings")
    if dynamic_strings[0] != 0 or dynamic_strings[-1] != 0:
        raise ValidationError(f"ELF dynamic string table is malformed: {name}")
    needed = {
        elf_c_string(dynamic_strings, offset, "needed library")
        for offset in needed_offsets
    }
    if "libc.so.6" not in needed or any(
        "/" in library or "musl" in library.lower() for library in needed
    ):
        raise ValidationError(f"ELF dependencies do not describe glibc: {name}")

    version_need_count = dynamic_values[DT_VERNEEDNUM][0]
    if not 1 <= version_need_count <= 1024:
        raise ValidationError(f"ELF version-need count is invalid: {name}")
    version_need_offset = elf_vaddr_to_offset(
        program_headers, dynamic_values[DT_VERNEED][0], ELF_VERSION_NEED.size
    )
    required_glibc: list[tuple[int, int, int]] = []
    cursor = version_need_offset
    for need_index in range(version_need_count):
        need_data = bounded_slice(data, cursor, ELF_VERSION_NEED.size, "version need")
        need_version, aux_count, file_offset, aux_delta, next_delta = (
            ELF_VERSION_NEED.unpack(need_data)
        )
        if need_version != 1 or not 1 <= aux_count <= 1024 or aux_delta == 0:
            raise ValidationError(f"ELF version-need record is invalid: {name}")
        library = elf_c_string(dynamic_strings, file_offset, "version-need library")
        if library not in needed:
            raise ValidationError(f"ELF version need references an undeclared library: {name}")
        aux_cursor = cursor + aux_delta
        for aux_index in range(aux_count):
            aux_data = bounded_slice(data, aux_cursor, ELF_VERSION_AUX.size, "version aux")
            _, _, _, aux_name_offset, aux_next = ELF_VERSION_AUX.unpack(aux_data)
            aux_name = elf_c_string(
                dynamic_strings, aux_name_offset, "required symbol version"
            ).encode("ascii")
            glibc_version = parse_glibc_symbol(aux_name)
            if glibc_version is not None:
                required_glibc.append(glibc_version)
            if aux_index + 1 < aux_count:
                if aux_next < ELF_VERSION_AUX.size:
                    raise ValidationError(f"ELF version-aux chain is invalid: {name}")
                aux_cursor += aux_next
            elif aux_next != 0:
                raise ValidationError(f"ELF version-aux chain is not terminated: {name}")
        if need_index + 1 < version_need_count:
            if next_delta < ELF_VERSION_NEED.size:
                raise ValidationError(f"ELF version-need chain is invalid: {name}")
            cursor += next_delta
        elif next_delta != 0:
            raise ValidationError(f"ELF version-need chain is not terminated: {name}")
    if not required_glibc:
        raise ValidationError(f"ELF has no verifiable GLIBC symbol requirements: {name}")

    version_marker = b"\x00" + version.encode("ascii") + b"\x00"
    if version_marker not in data:
        raise ValidationError(f"binary does not contain Redis version {version}: {name}")
    return max(required_glibc)


def validate_elf_binaries(
    archive_path: Path,
    *,
    arch: str,
    version: str,
    min_glibc: str,
    declared_max_glibc: str,
) -> None:
    """Validate the real ELF structure, ABI, version, and GLIBC requirements."""
    observed_glibc: list[tuple[int, int, int]] = []
    with tarfile.open(archive_path, mode="r:gz") as archive:
        binaries = {
            member.name: member
            for member in archive
            if member.name in ELF_BINARY_MEMBERS and member.isfile()
        }
        missing = sorted(set(ELF_BINARY_MEMBERS) - binaries.keys())
        if missing:
            raise ValidationError(
                f"archive is missing ELF binaries: {', '.join(missing)}"
            )
        for name in ELF_BINARY_MEMBERS:
            member = binaries[name]
            if member.size <= 0 or member.size > MAX_ELF_BINARY_BYTES:
                raise ValidationError(f"ELF binary violates the size limit: {name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValidationError(f"unable to read binary: {name}")
            with extracted:
                data = extracted.read(MAX_ELF_BINARY_BYTES + 1)
            if len(data) != member.size:
                raise ValidationError(f"ELF binary is truncated: {name}")
            observed_glibc.append(
                validate_elf_binary(data, name=name, arch=arch, version=version)
            )
    maximum = max(observed_glibc)
    baseline = (*parse_glibc(min_glibc), 0)
    if maximum > baseline:
        raise ValidationError(
            f"ELF binaries require GLIBC_{format_glibc_symbol(maximum)}, "
            f"newer than GLIBC_{min_glibc}"
        )
    if format_glibc_symbol(maximum) != declared_max_glibc:
        raise ValidationError(
            "PACKAGE-INFO MAX_GLIBC_SYMBOL does not match the ELF binaries"
        )


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
        validate_elf_binaries(
            args.archive,
            arch=args.arch,
            version=args.redis_version,
            min_glibc=args.min_glibc,
            declared_max_glibc=values["MAX_GLIBC_SYMBOL"],
        )
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
