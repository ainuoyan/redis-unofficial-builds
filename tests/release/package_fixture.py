from __future__ import annotations

import hashlib
import io
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
                "MAX_GLIBC_SYMBOL=2.28",
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
            add_regular(archive, executable, b"fixture\n", mode, pax_headers)
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
