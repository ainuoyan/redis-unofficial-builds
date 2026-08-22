#!/usr/bin/env python3
"""Validate a nonpublishing cross-platform Redis package without extracting it."""

from __future__ import annotations

import argparse
import hashlib
import re
import stat
import struct
import sys
import tarfile
import zipfile
from pathlib import Path

from portable_contract import (
    ContractError,
    archive_name,
    backend_for,
    backend_assets,
    packaging_patchset_sha256,
    require_regular_file,
    validate_identity,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release"))
from validate_release_asset import validate_upstream_notice_payloads  # noqa: E402


MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 1024
MAX_COMPRESSION_RATIO = 500
MAX_ELF_DYNAMIC_BYTES = 1024 * 1024
MAX_ELF_STRING_BYTES = 16 * 1024 * 1024
ELF_PT_LOAD = 1
ELF_PT_DYNAMIC = 2
ELF_PT_INTERP = 3
ELF_DT_NULL = 0
ELF_DT_NEEDED = 1
ELF_DT_STRTAB = 5
ELF_DT_STRSZ = 10
ELF_DT_VERNEED = 0x6FFFFFFE
ELF_DT_VERNEEDNUM = 0x6FFFFFFF
CHECKSUM_RE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9._-]+)\n$")
METADATA_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=([^\x00-\x1f\x7f]*)$")
WINDOWS_DLL_RE = re.compile(r"^redis/bin/[A-Za-z0-9][A-Za-z0-9._+-]{0,126}\.dll$", re.I)
WINDOWS_DLL_MAPPING_RE = re.compile(
    r"^DLL=([A-Za-z0-9][A-Za-z0-9._+-]{0,126}\.dll) PACKAGE=([A-Za-z0-9@._+-]+)$",
    re.I,
)
WINDOWS_PACKAGE_RE = re.compile(r"^PACKAGE=([A-Za-z0-9@._+-]+) ([^\x00-\x20\x7f]+)$")
EXPERIMENTAL_PACKAGE_STATUS = (
    "experimental; separate GitHub prerelease publication is allowed after acceptance"
)
METADATA_KEYS = {
    "PACKAGE_FORMAT",
    "PACKAGE_STATUS",
    "PACKAGE_ID",
    "REDIS_VERSION",
    "REDIS_SERIES",
    "BUILD_PROFILE",
    "PACKAGE_VARIANT",
    "PACKAGE_ARCH",
    "OS",
    "RUNTIME",
    "RUNTIME_BASELINE",
    "SERVICE_BACKEND",
    "INSTALL_PREFIX",
    "UPSTREAM_SOURCE_SHA256",
    "UPSTREAM_CONTRIBUTOR_LICENSE_SHA256",
    "UPSTREAM_DEPENDENCY_NOTICES_SHA256",
    "PATCHSET_SHA256",
}
COMMON_FILES = {
    "redis/conf/redis.conf": 0o644,
    "redis/conf/sentinel.conf": 0o644,
    "redis/PACKAGE-INFO": 0o644,
    "redis/BUILD-INFO": 0o644,
    "redis/LICENSE.txt": 0o644,
    "redis/README.txt": 0o644,
    "redis/THIRD_PARTY_NOTICES.md": 0o644,
    "redis/UPSTREAM-DEPENDENCY-NOTICES.txt": 0o644,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--checksum", type=Path, required=True)
    parser.add_argument("--packaging-root", type=Path, required=True)
    parser.add_argument("--redis-version", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--hashes-commit", required=True)
    parser.add_argument("--packaging-revision", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--arch", required=True)
    return parser.parse_args()


def validate_name(name: str, *, directory: bool = False) -> str:
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
    ):
        raise ContractError(f"unsafe archive member name: {name!r}")
    candidate = name[:-1] if directory and name.endswith("/") else name
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts) or parts[0] != "redis":
        raise ContractError(f"unsafe archive member name: {name!r}")
    canonical = "/".join(parts)
    if name not in {canonical, f"{canonical}/"}:
        raise ContractError(f"noncanonical archive member name: {name!r}")
    return canonical


def validate_checksum(archive: Path, checksum: Path) -> None:
    for path in (archive, checksum):
        if not path.is_file() or path.is_symlink():
            raise ContractError(f"package input is not a regular file: {path}")
    if archive.stat().st_size <= 0 or archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ContractError("archive violates the compressed-size limit")
    if checksum.stat().st_size > 256:
        raise ContractError("checksum file is unexpectedly large")
    match = CHECKSUM_RE.fullmatch(checksum.read_text(encoding="ascii"))
    if match is None or match.group(2) != archive.name:
        raise ContractError("checksum file is not a canonical single-file record")
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != match.group(1):
        raise ContractError("archive does not match its SHA-256 file")


def read_tar(archive_path: Path) -> tuple[dict[str, bytes], dict[str, int]]:
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    total = 0
    with tarfile.open(archive_path, mode="r:gz") as archive:
        if archive.pax_headers:
            raise ContractError("archive contains global PAX headers")
        for index, member in enumerate(archive, start=1):
            if index > MAX_MEMBERS:
                raise ContractError("archive contains too many members")
            if member.pax_headers or member.issparse() or getattr(member, "sparse", None):
                raise ContractError("archive uses unsupported extension metadata")
            name = validate_name(member.name, directory=member.isdir())
            if name in modes:
                raise ContractError(f"duplicate archive member: {name}")
            if member.uid != 0 or member.gid != 0 or member.uname or member.gname or member.mtime != 0:
                raise ContractError(f"nonreproducible tar metadata: {name}")
            if member.isdir():
                if member.mode & 0o7777 != 0o755 or member.size != 0:
                    raise ContractError(f"invalid archive directory: {name}")
                modes[name] = 0o755
                continue
            if not member.isfile() or member.size <= 0 or member.size > MAX_MEMBER_BYTES:
                raise ContractError(f"unsupported or oversized archive member: {name}")
            total += member.size
            if total > MAX_TOTAL_BYTES:
                raise ContractError("archive declares too much uncompressed data")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ContractError(f"unable to read archive member: {name}")
            data = extracted.read(MAX_MEMBER_BYTES + 1)
            if len(data) != member.size:
                raise ContractError(f"archive member is truncated: {name}")
            files[name] = data
            modes[name] = member.mode & 0o7777
    return files, modes


def read_zip(archive_path: Path) -> tuple[dict[str, bytes], dict[str, int]]:
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        for index, member in enumerate(archive.infolist(), start=1):
            if index > MAX_MEMBERS:
                raise ContractError("archive contains too many members")
            directory = member.is_dir()
            name = validate_name(member.filename, directory=directory)
            if name in modes:
                raise ContractError(f"duplicate archive member: {name}")
            if member.flag_bits & 0x1 or member.compress_type not in {
                zipfile.ZIP_STORED,
                zipfile.ZIP_DEFLATED,
            }:
                raise ContractError(f"unsupported ZIP member encoding: {name}")
            if member.date_time != (1980, 1, 1, 0, 0, 0):
                raise ContractError(f"nonreproducible ZIP timestamp: {name}")
            unix_mode = (member.external_attr >> 16) & 0o177777
            expected_type = stat.S_IFDIR if directory else stat.S_IFREG
            if stat.S_IFMT(unix_mode) != expected_type:
                raise ContractError(f"ZIP member is not a regular file/directory: {name}")
            mode = unix_mode & 0o7777
            if directory:
                if mode != 0o755 or member.file_size != 0:
                    raise ContractError(f"invalid ZIP directory: {name}")
                modes[name] = mode
                continue
            if member.file_size <= 0 or member.file_size > MAX_MEMBER_BYTES:
                raise ContractError(f"ZIP member violates the size limit: {name}")
            if member.compress_size == 0 or member.file_size > member.compress_size * MAX_COMPRESSION_RATIO:
                raise ContractError(f"ZIP member has an unsafe compression ratio: {name}")
            total += member.file_size
            if total > MAX_TOTAL_BYTES:
                raise ContractError("ZIP declares too much uncompressed data")
            data = archive.read(member)
            if len(data) != member.file_size:
                raise ContractError(f"ZIP member is truncated: {name}")
            files[name] = data
            modes[name] = mode
    return files, modes


def expected_files(variant: str, os_name: str) -> dict[str, int]:
    expected = dict(COMMON_FILES)
    suffix = ".exe" if os_name == "windows" else ""
    for name in (
        "redis-server",
        "redis-cli",
        "redis-benchmark",
        "redis-check-aof",
        "redis-check-rdb",
        "redis-sentinel",
    ):
        expected[f"redis/bin/{name}{suffix}"] = 0o755
    for relative, mode in backend_assets(variant).items():
        expected[f"redis/{relative}"] = mode
    if os_name == "windows":
        expected["redis/bin/RedisService.exe"] = 0o755
        expected["redis/MSYS2-RUNTIME-NOTICES.txt"] = 0o644
    return expected


def parse_metadata(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("PACKAGE-INFO is not UTF-8") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = METADATA_RE.fullmatch(line)
        if match is None or match.group(1) in values:
            raise ContractError("PACKAGE-INFO contains an invalid or duplicate record")
        values[match.group(1)] = match.group(2)
    if set(values) != METADATA_KEYS:
        raise ContractError("PACKAGE-INFO contains unknown or missing fields")
    return values


def build_info_value(data: bytes, key: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("BUILD-INFO is not UTF-8") from exc
    prefix = f"{key}: "
    values = [line[len(prefix) :] for line in text.splitlines() if line.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise ContractError(f"BUILD-INFO does not contain one {key} record")
    return values[0]


def elf_file_offset(
    load_segments: list[tuple[int, int, int]], address: int, size: int
) -> int:
    matches = []
    for file_offset, virtual_address, file_size in load_segments:
        if virtual_address <= address:
            relative = address - virtual_address
            if relative <= file_size and size <= file_size - relative:
                matches.append(file_offset + relative)
    if len(matches) != 1:
        raise ContractError("Redis ELF runtime table is not mapped by one load segment")
    return matches[0]


def validate_musl_dynamic_runtime(
    data: bytes,
    arch: str,
    load_segments: list[tuple[int, int, int]],
    dynamic_segments: list[tuple[int, int]],
) -> None:
    if len(dynamic_segments) != 1:
        raise ContractError("Redis ELF does not contain one dynamic runtime table")
    dynamic_offset, dynamic_size = dynamic_segments[0]
    if (
        dynamic_size == 0
        or dynamic_size > MAX_ELF_DYNAMIC_BYTES
        or dynamic_size % 16 != 0
    ):
        raise ContractError("Redis ELF dynamic runtime table is invalid")

    entries: list[tuple[int, int]] = []
    for offset in range(dynamic_offset, dynamic_offset + dynamic_size, 16):
        tag, value = struct.unpack_from("<qQ", data, offset)
        if tag == ELF_DT_NULL:
            break
        entries.append((tag, value))
    else:
        raise ContractError("Redis ELF dynamic runtime table is unterminated")

    def one_value(tag: int, description: str) -> int:
        values = [value for entry_tag, value in entries if entry_tag == tag]
        if len(values) != 1:
            raise ContractError(f"Redis ELF does not contain one {description}")
        return values[0]

    string_address = one_value(ELF_DT_STRTAB, "dynamic string-table address")
    string_size = one_value(ELF_DT_STRSZ, "dynamic string-table size")
    if string_size == 0 or string_size > min(len(data), MAX_ELF_STRING_BYTES):
        raise ContractError("Redis ELF dynamic string table is invalid")
    string_offset = elf_file_offset(load_segments, string_address, string_size)
    string_table = data[string_offset : string_offset + string_size]

    def dynamic_string(index: int) -> str:
        if index >= len(string_table):
            raise ContractError("Redis ELF dynamic string index is out of bounds")
        end = string_table.find(b"\x00", index)
        if end < 0:
            raise ContractError("Redis ELF dynamic string is unterminated")
        try:
            return string_table[index:end].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ContractError("Redis ELF dynamic string is not ASCII") from exc

    dependencies = [
        dynamic_string(value) for tag, value in entries if tag == ELF_DT_NEEDED
    ]
    expected_libc = {
        "x64": "libc.musl-x86_64.so.1",
        "arm64": "libc.musl-aarch64.so.1",
    }[arch]
    if expected_libc not in dependencies or any(
        dependency.rsplit("/", 1)[-1] == "libc.so.6" for dependency in dependencies
    ):
        raise ContractError("Redis ELF dynamic dependencies do not identify musl")

    version_addresses = [value for tag, value in entries if tag == ELF_DT_VERNEED]
    version_counts = [value for tag, value in entries if tag == ELF_DT_VERNEEDNUM]
    if bool(version_addresses) != bool(version_counts):
        raise ContractError("Redis ELF version requirements are incomplete")
    if not version_addresses:
        return
    if len(version_addresses) != 1 or len(version_counts) != 1:
        raise ContractError("Redis ELF contains duplicate version requirements")
    count = version_counts[0]
    if count == 0 or count > 4096:
        raise ContractError("Redis ELF version requirement count is invalid")

    address = version_addresses[0]
    for requirement_index in range(count):
        offset = elf_file_offset(load_segments, address, 16)
        version, auxiliary_count, file_index, auxiliary_offset, next_offset = (
            struct.unpack_from("<HHIII", data, offset)
        )
        if (
            version != 1
            or auxiliary_count == 0
            or auxiliary_count > 4096
            or auxiliary_offset < 16
        ):
            raise ContractError("Redis ELF version requirement record is invalid")
        dependency = dynamic_string(file_index)
        auxiliary_address = address + auxiliary_offset
        for auxiliary_index in range(auxiliary_count):
            auxiliary_file_offset = elf_file_offset(load_segments, auxiliary_address, 16)
            _, _, _, name_index, auxiliary_next = struct.unpack_from(
                "<IHHII", data, auxiliary_file_offset
            )
            requirement = dynamic_string(name_index)
            if dependency == "libc.so.6" or requirement.startswith("GLIBC_"):
                raise ContractError("Redis ELF has a glibc runtime requirement")
            if auxiliary_index + 1 < auxiliary_count:
                if auxiliary_next < 16:
                    raise ContractError("Redis ELF version auxiliary chain is invalid")
                auxiliary_address += auxiliary_next
            elif auxiliary_next != 0:
                raise ContractError("Redis ELF version auxiliary chain is unterminated")
        if requirement_index + 1 < count:
            if next_offset < 16:
                raise ContractError("Redis ELF version requirement chain is invalid")
            address += next_offset
        elif next_offset != 0:
            raise ContractError("Redis ELF version requirement chain is unterminated")


def validate_elf(data: bytes, arch: str, version: str) -> None:
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4:7] != b"\x02\x01\x01":
        raise ContractError("Redis binary is not a supported ELF64 file")
    machine = struct.unpack_from("<H", data, 18)[0]
    expected_machine = {"x64": 0x3E, "arm64": 0xB7}[arch]
    if machine != expected_machine:
        raise ContractError("Redis ELF architecture does not match the package")
    expected_interpreter = {
        "x64": b"/lib/ld-musl-x86_64.so.1\x00",
        "arm64": b"/lib/ld-musl-aarch64.so.1\x00",
    }[arch]
    program_offset = struct.unpack_from("<Q", data, 32)[0]
    program_size = struct.unpack_from("<H", data, 54)[0]
    program_count = struct.unpack_from("<H", data, 56)[0]
    if program_size != 56 or program_count == 0 or program_count > 256:
        raise ContractError("Redis ELF has an invalid program-header table")
    if program_offset > len(data) or program_count * program_size > len(data) - program_offset:
        raise ContractError("Redis ELF program-header table is truncated")
    interpreters = []
    load_segments: list[tuple[int, int, int]] = []
    dynamic_segments: list[tuple[int, int]] = []
    for index in range(program_count):
        offset = program_offset + index * program_size
        (
            program_type,
            _,
            file_offset,
            virtual_address,
            _,
            file_size,
            memory_size,
            _,
        ) = struct.unpack_from("<IIQQQQQQ", data, offset)
        if file_offset > len(data) or file_size > len(data) - file_offset:
            raise ContractError("Redis ELF program segment is out of bounds")
        if memory_size < file_size:
            raise ContractError("Redis ELF program segment has an invalid memory size")
        if program_type == ELF_PT_LOAD:
            load_segments.append((file_offset, virtual_address, file_size))
        elif program_type == ELF_PT_DYNAMIC:
            dynamic_segments.append((file_offset, file_size))
        if program_type == ELF_PT_INTERP:
            interpreters.append(data[file_offset : file_offset + file_size])
    if interpreters != [expected_interpreter]:
        raise ContractError("Redis ELF does not use the expected musl interpreter")
    validate_musl_dynamic_runtime(
        data, arch, load_segments, dynamic_segments
    )
    if b"\x00" + version.encode("ascii") + b"\x00" not in data:
        raise ContractError("Redis ELF does not contain the declared version")


def validate_macho(data: bytes, arch: str, version: str) -> None:
    if len(data) < 32 or data[:4] != b"\xcf\xfa\xed\xfe":
        raise ContractError("Redis binary is not a little-endian Mach-O 64 file")
    cpu_type = struct.unpack_from("<I", data, 4)[0]
    if cpu_type != {"x64": 0x01000007, "arm64": 0x0100000C}[arch]:
        raise ContractError("Redis Mach-O architecture does not match the package")
    command_count = struct.unpack_from("<I", data, 16)[0]
    command_bytes = struct.unpack_from("<I", data, 20)[0]
    if command_count == 0 or command_count > 4096 or command_bytes > len(data) - 32:
        raise ContractError("Redis Mach-O load-command table is invalid")
    offset = 32
    deployment_versions: list[tuple[int, int, int]] = []
    dylib_commands = {0xC, 0x18, 0x1F, 0x23, 0x80000018, 0x8000001F, 0x80000023}
    for _ in range(command_count):
        if offset > len(data) - 8:
            raise ContractError("Redis Mach-O load-command table is truncated")
        command, command_size = struct.unpack_from("<II", data, offset)
        if command_size < 8 or command_size > len(data) - offset:
            raise ContractError("Redis Mach-O load command is out of bounds")
        if command == 0x32:
            if command_size < 24 or struct.unpack_from("<I", data, offset + 8)[0] != 1:
                raise ContractError("Redis Mach-O has an invalid macOS build-version command")
            encoded = struct.unpack_from("<I", data, offset + 12)[0]
            deployment_versions.append((encoded >> 16, (encoded >> 8) & 0xFF, encoded & 0xFF))
        elif command == 0x24:
            if command_size < 16:
                raise ContractError("Redis Mach-O has an invalid minimum-version command")
            encoded = struct.unpack_from("<I", data, offset + 8)[0]
            deployment_versions.append((encoded >> 16, (encoded >> 8) & 0xFF, encoded & 0xFF))
        if command in dylib_commands:
            if command_size < 24:
                raise ContractError("Redis Mach-O has an invalid dylib command")
            name_offset = struct.unpack_from("<I", data, offset + 8)[0]
            if name_offset < 24 or name_offset >= command_size:
                raise ContractError("Redis Mach-O dylib name is out of bounds")
            raw = data[offset + name_offset : offset + command_size].split(b"\x00", 1)[0]
            try:
                dylib = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContractError("Redis Mach-O dylib name is not UTF-8") from exc
            if not (dylib.startswith("/usr/lib/") or dylib.startswith("/System/Library/")):
                raise ContractError(f"Redis Mach-O links an unapproved runtime path: {dylib}")
        offset += command_size
    if offset != 32 + command_bytes or not deployment_versions:
        raise ContractError("Redis Mach-O does not declare a deployment target")
    if max(deployment_versions) > (12, 0, 0):
        raise ContractError("Redis Mach-O requires a macOS version newer than 12.0")
    if b"\x00" + version.encode("ascii") + b"\x00" not in data:
        raise ContractError("Redis Mach-O does not contain the declared version")


def validate_pe(
    data: bytes,
    *,
    require_version: str | None = None,
    reject_cygwin_marker: bool = True,
) -> None:
    if len(data) < 0x100 or data[:2] != b"MZ":
        raise ContractError("Windows binary does not have a valid DOS header")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset > len(data) - 26 or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ContractError("Windows binary does not have a valid PE header")
    if struct.unpack_from("<H", data, pe_offset + 4)[0] != 0x8664:
        raise ContractError("Windows PE architecture is not x64")
    optional_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
    if (
        optional_size < 0x70
        or pe_offset + 24 + optional_size > len(data)
        or struct.unpack_from("<H", data, pe_offset + 24)[0] != 0x20B
    ):
        raise ContractError("Windows binary is not PE32+ x64")
    if reject_cygwin_marker and b"cygwin1.dll" in data.lower():
        raise ContractError("Windows MSYS2 package contains a Cygwin-linked binary")
    if require_version is not None:
        ascii_marker = require_version.encode("ascii")
        utf16_marker = require_version.encode("utf-16le")
        if ascii_marker not in data and utf16_marker not in data:
            raise ContractError("Windows Redis binary does not contain the declared version")


def validate_binaries(
    files: dict[str, bytes], *, os_name: str, arch: str, version: str
) -> None:
    suffix = ".exe" if os_name == "windows" else ""
    for name in ("redis-server", "redis-cli", "redis-benchmark"):
        data = files[f"redis/bin/{name}{suffix}"]
        if os_name == "linux":
            validate_elf(data, arch, version)
        elif os_name == "macos":
            validate_macho(data, arch, version)
        else:
            validate_pe(data, require_version=version)
    if os_name == "windows":
        validate_pe(files["redis/bin/RedisService.exe"])
        dll_names = [name for name in files if WINDOWS_DLL_RE.fullmatch(name)]
        if not any(name.lower() == "redis/bin/msys-2.0.dll" for name in dll_names):
            raise ContractError("Windows archive is missing msys-2.0.dll")
        for name in dll_names:
            # The MSYS2 runtime is derived from Cygwin and may contain the
            # upstream DLL name as inert data. Redis and the service wrapper
            # remain subject to the marker check above.
            validate_pe(files[name], reject_cygwin_marker=False)


def validate_assets(
    files: dict[str, bytes], packaging_root: Path, variant: str
) -> None:
    backend = backend_for(variant)
    asset_root = Path(str(backend["asset_root"]))
    for relative in backend_assets(variant):
        repository_path = require_regular_file(packaging_root, asset_root / relative)
        archive_name_value = f"redis/{relative}"
        if files.get(archive_name_value) != repository_path.read_bytes():
            raise ContractError(f"archive asset differs from reviewed source: {archive_name_value}")
    notice = require_regular_file(packaging_root, Path("THIRD_PARTY_NOTICES.md"))
    if files.get("redis/THIRD_PARTY_NOTICES.md") != notice.read_bytes():
        raise ContractError("archive third-party notices differ from reviewed source")


def validate_windows_runtime_notices(data: bytes, runtime_dlls: set[str]) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("Windows runtime notices are not UTF-8") from exc
    lines = text.splitlines()
    if not lines or lines[0] != "MSYS2_RUNTIME_NOTICES_FORMAT=1":
        raise ContractError("Windows runtime notices have an invalid header")
    mappings: dict[str, str] = {}
    packages: set[str] = set()
    for line in lines[1:]:
        mapping = WINDOWS_DLL_MAPPING_RE.fullmatch(line)
        if mapping is not None:
            dll_name = mapping.group(1).lower()
            if dll_name in mappings:
                raise ContractError("Windows runtime notices contain a duplicate DLL mapping")
            mappings[dll_name] = mapping.group(2)
            continue
        package = WINDOWS_PACKAGE_RE.fullmatch(line)
        if package is not None:
            if package.group(1) in packages:
                raise ContractError("Windows runtime notices contain a duplicate package record")
            packages.add(package.group(1))
    expected_dlls = {Path(name).name.lower() for name in runtime_dlls}
    if set(mappings) != expected_dlls:
        raise ContractError("Windows runtime notices do not map every packaged DLL exactly once")
    if not set(mappings.values()).issubset(packages) or "msys2-runtime" not in packages:
        raise ContractError("Windows runtime notices lack an owning package record")
    for package in packages:
        marker = f"===== BEGIN /usr/share/licenses/{package}/".encode("utf-8")
        if marker in data:
            continue
        if package == "msys2-runtime" and all(
            f"===== BEGIN /usr/share/doc/Cygwin/{name} (".encode("utf-8") in data
            for name in ("COPYING", "CYGWIN_LICENSE")
        ):
            continue
        raise ContractError(f"Windows runtime notices lack license text for {package}")


def main() -> int:
    args = parse_args()
    try:
        backend = validate_identity(args.redis_version, args.variant, args.arch)
        expected_name = archive_name(args.redis_version, args.variant, args.arch)
        if args.archive.name != expected_name or args.checksum.name != f"{expected_name}.sha256":
            raise ContractError("archive or checksum filename violates the package contract")
        if re.fullmatch(r"[0-9a-f]{64}", args.source_sha256) is None:
            raise ContractError("invalid source SHA-256")
        if re.fullmatch(r"[0-9a-f]{40}", args.hashes_commit) is None:
            raise ContractError("invalid redis-hashes commit")
        if re.fullmatch(r"[0-9a-f]{40}", args.packaging_revision) is None:
            raise ContractError("invalid packaging revision")
        validate_checksum(args.archive, args.checksum)
        if backend["extension"] == "zip":
            files, modes = read_zip(args.archive)
        else:
            files, modes = read_tar(args.archive)

        expected = expected_files(args.variant, str(backend["os"]))
        if "redis/UPSTREAM-CONTRIBUTOR-LICENSE.txt" in files:
            expected["redis/UPSTREAM-CONTRIBUTOR-LICENSE.txt"] = 0o644
        unexpected = set(files) - set(expected)
        if str(backend["os"]) == "windows":
            runtime_dlls = {
                name for name in unexpected if WINDOWS_DLL_RE.fullmatch(name) is not None
            }
            if not runtime_dlls:
                raise ContractError("Windows archive does not contain runtime DLLs")
            for name in runtime_dlls:
                if modes.get(name) != 0o755:
                    raise ContractError(f"archive member has an invalid mode: {name}")
            unexpected -= runtime_dlls
        missing = set(expected) - set(files)
        if missing or unexpected:
            raise ContractError(
                f"archive inventory mismatch; missing={sorted(missing)}, unexpected={sorted(unexpected)}"
            )
        for name, expected_mode in expected.items():
            if modes.get(name) != expected_mode:
                raise ContractError(f"archive member has an invalid mode: {name}")
        expected_directories = {"redis"}
        for name in expected:
            parent = Path(name).parent
            while parent.as_posix() != ".":
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        actual_directories = set(modes) - set(files)
        if actual_directories != expected_directories:
            raise ContractError(
                "archive directory inventory mismatch; "
                f"missing={sorted(expected_directories - actual_directories)}, "
                f"unexpected={sorted(actual_directories - expected_directories)}"
            )

        values = parse_metadata(files["redis/PACKAGE-INFO"])
        version_parts = args.redis_version.split(".")
        metadata_expected = {
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
        }
        for key, value in metadata_expected.items():
            if values.get(key) != value:
                raise ContractError(f"PACKAGE-INFO {key} does not match")
        patchset = packaging_patchset_sha256(args.packaging_root.resolve(), args.variant)
        if values["PATCHSET_SHA256"] != patchset:
            raise ContractError("PACKAGE-INFO patch-set hash does not match reviewed source")
        if build_info_value(files["redis/BUILD-INFO"], "Packaging patch-set SHA256") != patchset:
            raise ContractError("BUILD-INFO patch-set hash does not match")
        if build_info_value(files["redis/BUILD-INFO"], "Packaging revision") != args.packaging_revision:
            raise ContractError("BUILD-INFO packaging revision does not match")
        for label, expected_value in (
            ("Redis version", args.redis_version),
            ("Package variant", args.variant),
            ("Package architecture", args.arch),
            ("Redis source SHA256", args.source_sha256),
            ("Redis hashes snapshot", args.hashes_commit),
        ):
            if build_info_value(files["redis/BUILD-INFO"], label) != expected_value:
                raise ContractError(f"BUILD-INFO {label} does not match")
        if (
            build_info_value(files["redis/BUILD-INFO"], "Package status")
            != EXPERIMENTAL_PACKAGE_STATUS
        ):
            raise ContractError("BUILD-INFO does not preserve the publication boundary")

        contributor = files.get("redis/UPSTREAM-CONTRIBUTOR-LICENSE.txt")
        validate_upstream_notice_payloads(
            files["redis/UPSTREAM-DEPENDENCY-NOTICES.txt"],
            contributor,
            version=args.redis_version,
            dependency_notices_sha256=values["UPSTREAM_DEPENDENCY_NOTICES_SHA256"],
            contributor_license_sha256=values["UPSTREAM_CONTRIBUTOR_LICENSE_SHA256"],
        )
        validate_binaries(
            files,
            os_name=str(backend["os"]),
            arch=args.arch,
            version=args.redis_version,
        )
        validate_assets(files, args.packaging_root.resolve(), args.variant)
        if str(backend["os"]) == "windows":
            validate_windows_runtime_notices(
                files["redis/MSYS2-RUNTIME-NOTICES.txt"], runtime_dlls
            )
        for config_name in ("redis/conf/redis.conf", "redis/conf/sentinel.conf"):
            config_text = files[config_name].decode("utf-8")
            if any(
                re.match(r"^[ \t]*loadmodule[ \t]", line, re.IGNORECASE)
                for line in config_text.splitlines()
            ):
                raise ContractError(f"active loadmodule remains in {config_name}")
        readme_text = files["redis/README.txt"].decode("utf-8")
        if (
            re.search(
                r"published\s+only\s+in\s+a\s+separately\s+tagged\s+GitHub\s+prerelease",
                readme_text,
            )
            is None
            or re.search(
                r"not\s+eligible\s+for\s+the\s+numeric\s+stable\s+Release",
                readme_text,
            )
            is None
        ):
            raise ContractError("README does not state the experimental publication boundary")
        print(f"Validated experimental package: {args.archive.name}")
        return 0
    except (
        ContractError,
        OSError,
        UnicodeError,
        EOFError,
        ValueError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"experimental asset validation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
