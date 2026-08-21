from __future__ import annotations

import hashlib
import io
import struct
import tarfile
from pathlib import Path


VERSION = "7.4.11"
VARIANT = "linux-glibc2.28"
SOURCE_SHA256 = "a" * 64
REVISION = "b" * 40
HASHES_COMMIT = "d" * 40
PATCHSET_SHA256 = "c" * 64
CONTRIBUTOR_LICENSE = b"fixture upstream contributor license\n"
DEPENDENCY_NOTICE_BODY = b"fixture dependency license\n"
DEPENDENCY_NOTICES = (
    b"UPSTREAM_DEPENDENCY_NOTICES_FORMAT=1\n"
    + f"REDIS_VERSION={VERSION}\n".encode()
    + b"SOURCE_SUBTREE=deps\n\n"
    + f"===== BEGIN deps/example/LICENSE ({len(DEPENDENCY_NOTICE_BODY)} bytes) =====\n".encode()
    + DEPENDENCY_NOTICE_BODY
    + b"\n===== END deps/example/LICENSE =====\n"
)


DIRECTORIES = (
    "redis",
    "redis/bin",
    "redis/conf",
    "redis/scripts",
    "redis/systemd",
)
EXECUTABLES = (
    "redis/bin/redis-server",
    "redis/bin/redis-cli",
    "redis/bin/redis-benchmark",
    "redis/scripts/common.sh",
    "redis/scripts/install.sh",
    "redis/scripts/update.sh",
    "redis/scripts/uninstall.sh",
)
ELF_BINARIES = (
    "redis/bin/redis-server",
    "redis/bin/redis-cli",
    "redis/bin/redis-benchmark",
)
ELF_MACHINE = {"x64": 0x3E, "arm64": 0xB7}
ELF_INTERPRETER = {
    "x64": "/lib64/ld-linux-x86-64.so.2",
    "arm64": "/lib/ld-linux-aarch64.so.1",
}


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) // boundary * boundary


def elf_fixture(
    arch: str,
    *,
    version: str = VERSION,
    glibc: str = "2.28",
    interpreter: str | None = None,
    needed_library: str = "libc.so.6",
) -> bytes:
    """Small but structurally complete dynamically linked ELF64 fixture."""
    elf_header = struct.Struct("<16sHHIQQQIHHHHHH")
    program_header = struct.Struct("<IIQQQQQQ")
    section_header = struct.Struct("<IIQQQQIIQQ")
    dynamic_entry = struct.Struct("<qQ")
    version_need = struct.Struct("<HHIII")
    version_aux = struct.Struct("<IHHII")
    interpreter_bytes = (
        (interpreter or ELF_INTERPRETER[arch]).encode("ascii") + b"\x00"
    )
    needed_bytes = needed_library.encode("ascii")
    glibc_bytes = f"GLIBC_{glibc}".encode("ascii")
    dynamic_strings = b"\x00" + needed_bytes + b"\x00" + glibc_bytes + b"\x00"
    needed_offset = 1
    glibc_offset = 1 + len(needed_bytes) + 1

    program_offset = elf_header.size
    program_count = 4
    cursor = align(program_offset + program_header.size * program_count, 8)
    interpreter_offset = cursor
    cursor = align(cursor + len(interpreter_bytes), 8)
    strings_offset = cursor
    cursor = align(cursor + len(dynamic_strings), 8)
    versions_offset = cursor
    versions = version_need.pack(1, 1, needed_offset, version_need.size, 0)
    versions += version_aux.pack(0, 0, 2, glibc_offset, 0)
    cursor = align(cursor + len(versions), 8)
    dynamic_offset = cursor
    dynamic = b"".join(
        (
            dynamic_entry.pack(1, needed_offset),
            dynamic_entry.pack(5, strings_offset),
            dynamic_entry.pack(10, len(dynamic_strings)),
            dynamic_entry.pack(0x6FFFFFFE, versions_offset),
            dynamic_entry.pack(0x6FFFFFFF, 1),
            dynamic_entry.pack(0, 0),
        )
    )
    cursor = align(cursor + len(dynamic), 8)
    version_offset = cursor
    version_bytes = b"\x00" + version.encode("ascii") + b"\x00"
    cursor = align(cursor + len(version_bytes), 8)
    section_names_offset = cursor
    section_names = (
        b"\x00.interp\x00.dynstr\x00.gnu.version_r\x00.dynamic\x00.rodata\x00.shstrtab\x00"
    )
    cursor = align(cursor + len(section_names), 8)
    sections_offset = cursor
    section_count = 7
    file_size = sections_offset + section_header.size * section_count

    image = bytearray(file_size)
    ident = b"\x7fELF\x02\x01\x01\x00\x00" + b"\x00" * 7
    elf_header.pack_into(
        image,
        0,
        ident,
        3,
        ELF_MACHINE[arch],
        1,
        version_offset,
        program_offset,
        sections_offset,
        0,
        elf_header.size,
        program_header.size,
        program_count,
        section_header.size,
        section_count,
        6,
    )
    program_headers = (
        (1, 5, 0, 0, 0, file_size, file_size, 0x1000),
        (
            3,
            4,
            interpreter_offset,
            interpreter_offset,
            interpreter_offset,
            len(interpreter_bytes),
            len(interpreter_bytes),
            1,
        ),
        (
            2,
            6,
            dynamic_offset,
            dynamic_offset,
            dynamic_offset,
            len(dynamic),
            len(dynamic),
            8,
        ),
        (0x6474E551, 6, 0, 0, 0, 0, 0, 16),
    )
    for index, values in enumerate(program_headers):
        program_header.pack_into(
            image, program_offset + index * program_header.size, *values
        )
    image[
        interpreter_offset : interpreter_offset + len(interpreter_bytes)
    ] = interpreter_bytes
    image[strings_offset : strings_offset + len(dynamic_strings)] = dynamic_strings
    image[versions_offset : versions_offset + len(versions)] = versions
    image[dynamic_offset : dynamic_offset + len(dynamic)] = dynamic
    image[version_offset : version_offset + len(version_bytes)] = version_bytes
    image[section_names_offset : section_names_offset + len(section_names)] = section_names

    def section_name(value: bytes) -> int:
        return section_names.index(value)

    sections = (
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (
            section_name(b".interp"),
            1,
            2,
            interpreter_offset,
            interpreter_offset,
            len(interpreter_bytes),
            0,
            0,
            1,
            0,
        ),
        (
            section_name(b".dynstr"),
            3,
            2,
            strings_offset,
            strings_offset,
            len(dynamic_strings),
            0,
            0,
            1,
            0,
        ),
        (
            section_name(b".gnu.version_r"),
            0x6FFFFFFE,
            2,
            versions_offset,
            versions_offset,
            len(versions),
            2,
            1,
            8,
            0,
        ),
        (
            section_name(b".dynamic"),
            6,
            3,
            dynamic_offset,
            dynamic_offset,
            len(dynamic),
            2,
            0,
            8,
            dynamic_entry.size,
        ),
        (
            section_name(b".rodata"),
            1,
            2,
            version_offset,
            version_offset,
            len(version_bytes),
            0,
            0,
            1,
            0,
        ),
        (
            section_name(b".shstrtab"),
            3,
            0,
            0,
            section_names_offset,
            len(section_names),
            0,
            0,
            1,
            0,
        ),
    )
    for index, values in enumerate(sections):
        section_header.pack_into(
            image, sections_offset + index * section_header.size, *values
        )
    return bytes(image)


def elf_stub(arch: str) -> bytes:
    """Minimal 64-byte ELF64 little-endian header for the given package arch."""
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2  # EI_CLASS: 64-bit
    header[5] = 1  # EI_DATA: little-endian
    header[18:20] = ELF_MACHINE[arch].to_bytes(2, "little")
    return bytes(header)


DATA_FILES = (
    "redis/conf/redis.conf",
    "redis/conf/sentinel.conf",
    "redis/systemd/redis.service",
    "redis/systemd/redis-hardening.conf.example",
    "redis/LICENSE.txt",
    "redis/README.txt",
    "redis/THIRD_PARTY_NOTICES.md",
)
SYMLINKS = {
    "redis/bin/redis-check-aof": "redis-server",
    "redis/bin/redis-check-rdb": "redis-server",
    "redis/bin/redis-sentinel": "redis-server",
}


def package_info(
    arch: str,
    patchset_sha256: str = PATCHSET_SHA256,
    *,
    contributor_license_sha256: str,
    dependency_notices_sha256: str,
    max_glibc: str = "2.28",
) -> bytes:
    return (
        "\n".join(
            [
                "PACKAGE_FORMAT=2",
                "PACKAGE_ID=redis-unofficial-builds",
                f"REDIS_VERSION={VERSION}",
                "REDIS_SERIES=7.4",
                "BUILD_PROFILE=core",
                f"PACKAGE_VARIANT={VARIANT}",
                f"PACKAGE_ARCH={arch}",
                "OS=linux",
                "LIBC=glibc",
                "MIN_GLIBC=2.28",
                f"MAX_GLIBC_SYMBOL={max_glibc}",
                "SERVICE_BACKEND=systemd",
                "INSTALL_PREFIX=/usr/local/redis",
                f"UPSTREAM_SOURCE_SHA256={SOURCE_SHA256}",
                "UPSTREAM_CONTRIBUTOR_LICENSE_SHA256="
                f"{contributor_license_sha256}",
                f"UPSTREAM_DEPENDENCY_NOTICES_SHA256={dependency_notices_sha256}",
                f"PATCHSET_SHA256={patchset_sha256}",
                "",
            ]
        )
    ).encode()


def build_info(
    arch: str,
    revision: str = REVISION,
    patchset_sha256: str = PATCHSET_SHA256,
    hashes_commit: str = HASHES_COMMIT,
) -> bytes:
    return (
        "\n".join(
            [
                f"Redis version: {VERSION}",
                f"Package variant: {VARIANT}",
                f"Package architecture: {arch}",
                f"Redis source SHA256: {SOURCE_SHA256}",
                f"Redis hashes snapshot: {hashes_commit}",
                f"Packaging patch-set SHA256: {patchset_sha256}",
                f"Packaging revision: {revision}",
                "",
            ]
        )
    ).encode()


def add_regular(
    archive: tarfile.TarFile,
    name: str,
    content: bytes,
    mode: int,
    pax_headers: dict[str, str] | None = None,
) -> None:
    member = tarfile.TarInfo(name)
    member.uid = 0
    member.gid = 0
    member.mode = mode
    member.size = len(content)
    member.mtime = 0
    if pax_headers is not None:
        member.pax_headers = pax_headers
    archive.addfile(member, io.BytesIO(content))


def write_package(
    directory: Path,
    arch: str,
    *,
    revision: str = REVISION,
    patchset_sha256: str = PATCHSET_SHA256,
    build_patchset_sha256: str | None = None,
    hashes_commit: str = HASHES_COMMIT,
    contributor_license: bytes | None = CONTRIBUTOR_LICENSE,
    dependency_notices: bytes | None = DEPENDENCY_NOTICES,
    declared_contributor_license_sha256: str | None = None,
    declared_dependency_notices_sha256: str | None = None,
    extra_member: tuple[str, bytes, int] | None = None,
    server_mode: int = 0o755,
    binary_contents: dict[str, bytes] | None = None,
    max_glibc: str = "2.28",
    server_pax_headers: dict[str, str] | None = None,
    archive_format: int = tarfile.PAX_FORMAT,
) -> tuple[Path, Path]:
    name = f"Redis-{VERSION}-{VARIANT}-{arch}.tar.gz"
    archive_path = directory / name
    with tarfile.open(archive_path, "w:gz", format=archive_format) as archive:
        for directory_name in DIRECTORIES:
            member = tarfile.TarInfo(directory_name)
            member.type = tarfile.DIRTYPE
            member.uid = 0
            member.gid = 0
            member.mode = 0o755
            member.mtime = 0
            archive.addfile(member)
        for executable in EXECUTABLES:
            mode = server_mode if executable == "redis/bin/redis-server" else 0o755
            pax_headers = (
                server_pax_headers
                if executable == "redis/bin/redis-server"
                else None
            )
            if executable in ELF_BINARIES:
                content = elf_fixture(arch)
                if binary_contents is not None:
                    content = binary_contents.get(executable, content)
            else:
                content = b"fixture\n"
            add_regular(archive, executable, content, mode, pax_headers)
        for data_file in DATA_FILES:
            add_regular(archive, data_file, b"fixture\n", 0o644)
        if contributor_license is not None:
            add_regular(
                archive,
                "redis/UPSTREAM-CONTRIBUTOR-LICENSE.txt",
                contributor_license,
                0o644,
            )
        if dependency_notices is not None:
            add_regular(
                archive,
                "redis/UPSTREAM-DEPENDENCY-NOTICES.txt",
                dependency_notices,
                0o644,
            )
        contributor_license_sha256 = declared_contributor_license_sha256
        if contributor_license_sha256 is None:
            contributor_license_sha256 = (
                "absent"
                if contributor_license is None
                else hashlib.sha256(contributor_license).hexdigest()
            )
        dependency_notices_sha256 = declared_dependency_notices_sha256
        if dependency_notices_sha256 is None:
            dependency_notices_sha256 = hashlib.sha256(
                dependency_notices or b""
            ).hexdigest()
        add_regular(
            archive,
            "redis/PACKAGE-INFO",
            package_info(
                arch,
                patchset_sha256,
                contributor_license_sha256=contributor_license_sha256,
                dependency_notices_sha256=dependency_notices_sha256,
                max_glibc=max_glibc,
            ),
            0o644,
        )
        add_regular(
            archive,
            "redis/BUILD-INFO",
            build_info(
                arch,
                revision,
                build_patchset_sha256 or patchset_sha256,
                hashes_commit,
            ),
            0o644,
        )
        for link_name, target in SYMLINKS.items():
            member = tarfile.TarInfo(link_name)
            member.type = tarfile.SYMTYPE
            member.uid = 0
            member.gid = 0
            member.mode = 0o777
            member.linkname = target
            member.mtime = 0
            archive.addfile(member)
        if extra_member is not None:
            add_regular(archive, *extra_member)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = directory / f"{name}.sha256"
    checksum_path.write_text(f"{digest}  {name}\n", encoding="ascii")
    return archive_path, checksum_path
