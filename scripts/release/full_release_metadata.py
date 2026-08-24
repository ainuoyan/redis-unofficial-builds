#!/usr/bin/env python3
"""Create or validate one immutable full-platform Redis Release metadata set."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
PORTABLE_ROOT = SCRIPT_ROOT / "experimental"
sys.path.insert(0, str(PORTABLE_ROOT))

import portable_contract  # noqa: E402
import validate_portable_asset as portable_validator  # noqa: E402
import release_metadata  # noqa: E402
import validate_release_asset as linux_validator  # noqa: E402


WORKFLOW_PATH = ".github/workflows/build-linux.yml"
PLATFORMS: tuple[dict[str, str], ...] = (
    {"variant": "linux-glibc2.28", "os": "linux", "arch": "x64", "extension": "tar.gz", "runtime": "glibc", "baseline": "2.28", "service": "systemd", "validator": "linux"},
    {"variant": "linux-glibc2.28", "os": "linux", "arch": "arm64", "extension": "tar.gz", "runtime": "glibc", "baseline": "2.28", "service": "systemd", "validator": "linux"},
    {"variant": "linux-glibc2.17-legacy", "os": "linux", "arch": "x64", "extension": "tar.gz", "runtime": "glibc", "baseline": "2.17", "service": "systemd", "validator": "linux"},
    {"variant": "linux-glibc2.17-legacy", "os": "linux", "arch": "arm64", "extension": "tar.gz", "runtime": "glibc", "baseline": "2.17", "service": "systemd", "validator": "linux"},
    {"variant": "linux-musl1.2", "os": "linux", "arch": "x64", "extension": "tar.gz", "runtime": "musl", "baseline": "1.2", "service": "openrc", "validator": "portable"},
    {"variant": "linux-musl1.2", "os": "linux", "arch": "arm64", "extension": "tar.gz", "runtime": "musl", "baseline": "1.2", "service": "openrc", "validator": "portable"},
    {"variant": "macos15", "os": "macos", "arch": "x64", "extension": "tar.gz", "runtime": "darwin", "baseline": "15.0", "service": "launchd", "validator": "portable"},
    {"variant": "macos15", "os": "macos", "arch": "arm64", "extension": "tar.gz", "runtime": "darwin", "baseline": "15.0", "service": "launchd", "validator": "portable"},
    {"variant": "windows-msys2", "os": "windows", "arch": "x64", "extension": "zip", "runtime": "msys2", "baseline": "rolling", "service": "windows-scm", "validator": "portable"},
)


class FullMetadataError(release_metadata.MetadataError):
    """Raised when the unified Release set is incomplete or inconsistent."""


def archive_name(version: str, platform: dict[str, str]) -> str:
    return (
        f"Redis-{version}-{platform['variant']}-{platform['arch']}."
        f"{platform['extension']}"
    )


def expected_package_names(version: str) -> set[str]:
    result: set[str] = set()
    for platform in PLATFORMS:
        archive = archive_name(version, platform)
        result.update({archive, f"{archive}.sha256"})
    return result


def expected_release_names(version: str) -> set[str]:
    return expected_package_names(version) | {
        release_metadata.CHECKSUMS_NAME,
        release_metadata.MANIFEST_NAME,
        release_metadata.sbom_name(version),
    }


def _observe_common(
    observed: str | None, value: str, description: str
) -> str:
    if observed is not None and observed != value:
        raise FullMetadataError(f"packages use different {description}")
    return value


def _validate_linux(
    archive: Path,
    checksum: Path,
    *,
    platform: dict[str, str],
    version: str,
    source_sha256: str,
    packaging_revision: str | None,
    packaging_root: Path,
) -> tuple[dict[str, str], str, str]:
    linux_validator.validate_checksum(archive, checksum)
    values, _, build_info = linux_validator.read_package_info(
        archive, package_status="release"
    )
    linux_validator.validate_elf_binaries(
        archive,
        arch=platform["arch"],
        version=version,
        min_glibc=platform["baseline"],
        declared_max_glibc=values["MAX_GLIBC_SYMBOL"],
    )
    linux_validator.validate_metadata(
        values,
        version=version,
        variant=platform["variant"],
        arch=platform["arch"],
        source_sha256=source_sha256,
        min_glibc=platform["baseline"],
    )
    revision, hashes_commit = linux_validator.validate_build_info(
        build_info,
        version=version,
        variant=platform["variant"],
        arch=platform["arch"],
        source_sha256=source_sha256,
        patchset_sha256=values["PATCHSET_SHA256"],
        packaging_revision=packaging_revision,
    )
    linux_validator.validate_packaging_bindings(
        archive,
        packaging_root,
        values["PATCHSET_SHA256"],
        Path(WORKFLOW_PATH),
    )
    return values, revision, hashes_commit


def _validate_portable(
    archive: Path,
    checksum: Path,
    *,
    platform: dict[str, str],
    version: str,
    source_sha256: str,
    packaging_revision: str,
    packaging_root: Path,
    hashes_commit: str,
) -> tuple[dict[str, str], str, str]:
    command = [
        sys.executable,
        str(PORTABLE_ROOT / "validate_portable_asset.py"),
        "--archive", str(archive),
        "--checksum", str(checksum),
        "--packaging-root", str(packaging_root),
        "--redis-version", version,
        "--source-sha256", source_sha256,
        "--hashes-commit", hashes_commit,
        "--packaging-revision", packaging_revision,
        "--variant", platform["variant"],
        "--arch", platform["arch"],
        "--package-status", "release",
        "--build-workflow", WORKFLOW_PATH,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise FullMetadataError(
            f"portable archive validation failed for {archive.name}: {detail}"
        )
    if platform["extension"] == "zip":
        files, _ = portable_validator.read_zip(archive)
    else:
        files, _ = portable_validator.read_tar(archive)
    values = portable_validator.parse_metadata(files["redis/PACKAGE-INFO"])
    build_info = files["redis/BUILD-INFO"]
    revision = portable_validator.build_info_value(build_info, "Packaging revision")
    observed_hashes = portable_validator.build_info_value(
        build_info, "Redis hashes snapshot"
    )
    return values, revision, observed_hashes


def inspect_archives(
    asset_dir: Path,
    *,
    version: str,
    source_sha256: str,
    packaging_revision: str | None,
    packaging_root: Path,
    expected_hashes_commit: str | None = None,
) -> tuple[list[dict[str, Any]], str, str]:
    artifacts: list[dict[str, Any]] = []
    observed_revision: str | None = None
    observed_hashes: str | None = expected_hashes_commit
    for platform in PLATFORMS:
        name = archive_name(version, platform)
        archive = asset_dir / name
        checksum = asset_dir / f"{name}.sha256"
        if platform["validator"] == "linux":
            values, revision, hashes_commit = _validate_linux(
                archive,
                checksum,
                platform=platform,
                version=version,
                source_sha256=source_sha256,
                packaging_revision=packaging_revision,
                packaging_root=packaging_root,
            )
        else:
            if packaging_revision is None:
                raise FullMetadataError(
                    "full-platform validation requires the packaging revision"
                )
            if observed_hashes is None:
                raise FullMetadataError(
                    "portable validation requires the redis-hashes commit"
                )
            values, revision, hashes_commit = _validate_portable(
                archive,
                checksum,
                platform=platform,
                version=version,
                source_sha256=source_sha256,
                packaging_revision=packaging_revision,
                packaging_root=packaging_root,
                hashes_commit=observed_hashes,
            )
        observed_revision = _observe_common(
            observed_revision, revision, "packaging revisions"
        )
        observed_hashes = _observe_common(
            observed_hashes, hashes_commit, "Redis hashes snapshots"
        )
        artifacts.append(
            {
                "name": name,
                "checksum_file": f"{name}.sha256",
                "sha256": release_metadata.sha256_file(archive),
                "size": archive.stat().st_size,
                "variant": platform["variant"],
                "os": platform["os"],
                "arch": platform["arch"],
                "runtime": platform["runtime"],
                "runtime_baseline": platform["baseline"],
                "service_backend": platform["service"],
                "patchset_sha256": values["PATCHSET_SHA256"],
            }
        )
    if observed_revision is None or observed_hashes is None:
        raise FullMetadataError("no release packages were found")
    return artifacts, observed_revision, observed_hashes


def build_manifest(
    *,
    version: str,
    source_sha256: str,
    repository: str,
    revision: str,
    hashes_commit: str,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    parsed = linux_validator.parse_version(version)
    return {
        "schema": 2,
        "package_id": "redis-unofficial-builds",
        "release_tag": release_metadata.release_tag(version),
        "redis_version": version,
        "redis_series": f"{parsed[0]}.{parsed[1]}",
        "source": {
            "url": f"https://download.redis.io/releases/redis-{version}.tar.gz",
            "sha256": source_sha256,
            "hashes": {
                "repository": "redis/redis-hashes",
                "path": "README",
                "commit": hashes_commit,
            },
        },
        "build": {
            "repository": repository,
            "revision": revision,
            "workflow": WORKFLOW_PATH,
            "profile": "core",
        },
        "artifacts": artifacts,
        "metadata": {
            "checksums": release_metadata.CHECKSUMS_NAME,
            "sbom": release_metadata.sbom_name(version),
            "sbom_format": "SPDX-2.3",
            "sbom_scope": "release-package-level",
        },
    }


def create_metadata(args: argparse.Namespace) -> None:
    if release_metadata.regular_names(args.asset_dir) != expected_package_names(
        args.redis_version
    ):
        raise FullMetadataError(
            "asset directory does not contain exactly nine package pairs"
        )
    artifacts, revision, hashes_commit = inspect_archives(
        args.asset_dir,
        version=args.redis_version,
        source_sha256=args.source_sha256,
        packaging_revision=args.packaging_revision,
        packaging_root=args.packaging_root,
        expected_hashes_commit=args.hashes_commit,
    )
    manifest = build_manifest(
        version=args.redis_version,
        source_sha256=args.source_sha256,
        repository=args.repository,
        revision=revision,
        hashes_commit=hashes_commit,
        artifacts=artifacts,
    )
    release_metadata.write_json(
        args.asset_dir / release_metadata.MANIFEST_NAME, manifest
    )
    release_metadata.write_json(
        args.asset_dir / release_metadata.sbom_name(args.redis_version),
        release_metadata.build_spdx(
            manifest=manifest, repository=args.repository, created=args.created
        ),
    )
    checksummed = expected_release_names(args.redis_version) - {
        release_metadata.CHECKSUMS_NAME
    }
    (args.asset_dir / release_metadata.CHECKSUMS_NAME).write_text(
        "".join(
            f"{release_metadata.sha256_file(args.asset_dir / name)}  {name}\n"
            for name in sorted(checksummed)
        ),
        encoding="ascii",
    )
    if release_metadata.regular_names(args.asset_dir) != expected_release_names(
        args.redis_version
    ):
        raise FullMetadataError("generated full Release metadata is incomplete")


def validate_metadata_set(args: argparse.Namespace) -> None:
    expected_names = expected_release_names(args.redis_version)
    if release_metadata.regular_names(args.asset_dir) != expected_names:
        raise FullMetadataError("Release does not contain the exact full asset set")
    manifest = release_metadata.strict_json(
        args.asset_dir / release_metadata.MANIFEST_NAME
    )
    if not isinstance(manifest, dict) or manifest.get("schema") != 2:
        raise FullMetadataError("full Release manifest must use schema 2")
    build = manifest.get("build")
    if not isinstance(build, dict):
        raise FullMetadataError("full Release manifest build must be an object")
    revision = build.get("revision")
    if not isinstance(revision, str) or not release_metadata.REVISION_RE.fullmatch(
        revision
    ):
        raise FullMetadataError("full Release manifest has an invalid revision")
    if revision != args.packaging_revision:
        raise FullMetadataError("full Release manifest revision does not match")
    source = manifest.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("hashes"), dict):
        raise FullMetadataError("full Release manifest source is malformed")
    hashes_commit = source["hashes"].get("commit")
    if hashes_commit != args.hashes_commit:
        raise FullMetadataError("full Release manifest hashes commit does not match")
    artifacts, observed_revision, observed_hashes = inspect_archives(
        args.asset_dir,
        version=args.redis_version,
        source_sha256=args.source_sha256,
        packaging_revision=revision,
        packaging_root=args.packaging_root,
        expected_hashes_commit=hashes_commit,
    )
    expected_manifest = build_manifest(
        version=args.redis_version,
        source_sha256=args.source_sha256,
        repository=args.repository,
        revision=observed_revision,
        hashes_commit=observed_hashes,
        artifacts=artifacts,
    )
    if manifest != expected_manifest:
        raise FullMetadataError("full Release manifest does not match the packages")
    spdx_path = args.asset_dir / release_metadata.sbom_name(args.redis_version)
    spdx = release_metadata.strict_json(spdx_path)
    if not isinstance(spdx, dict) or not isinstance(spdx.get("creationInfo"), dict):
        raise FullMetadataError("full Release SPDX document is malformed")
    created = spdx["creationInfo"].get("created")
    if not isinstance(created, str):
        raise FullMetadataError("full Release SPDX document lacks creation time")
    if spdx != release_metadata.build_spdx(
        manifest=manifest, repository=args.repository, created=created
    ):
        raise FullMetadataError("full Release SPDX document does not match")
    expected_checksums = "".join(
        f"{release_metadata.sha256_file(args.asset_dir / name)}  {name}\n"
        for name in sorted(expected_names - {release_metadata.CHECKSUMS_NAME})
    )
    checksum_path = args.asset_dir / release_metadata.CHECKSUMS_NAME
    if checksum_path.stat().st_size > 64 * 1024:
        raise FullMetadataError("SHA256SUMS is unexpectedly large")
    if checksum_path.read_text(encoding="ascii") != expected_checksums:
        raise FullMetadataError("SHA256SUMS is not canonical or does not match")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "validate"):
        command = subparsers.add_parser(name)
        command.add_argument("--asset-dir", type=Path, required=True)
        command.add_argument("--redis-version", required=True)
        command.add_argument("--source-sha256", required=True)
        command.add_argument("--hashes-commit", required=True)
        command.add_argument("--repository", required=True)
        command.add_argument("--packaging-revision", required=True)
        command.add_argument("--packaging-root", type=Path, required=True)
        if name == "create":
            command.add_argument("--created", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        release_metadata.validate_common_values(
            args.redis_version,
            args.source_sha256,
            "full-platform",
            "2.17",
            args.repository,
            args.packaging_revision,
        )
        if not release_metadata.REVISION_RE.fullmatch(args.hashes_commit):
            raise FullMetadataError("invalid Redis hashes commit")
        if args.command == "create":
            create_metadata(args)
            print(f"Created full Release metadata for Redis {args.redis_version}")
        else:
            validate_metadata_set(args)
            print(f"Validated full Release metadata for Redis {args.redis_version}")
        return 0
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        linux_validator.ValidationError,
        portable_contract.ContractError,
        release_metadata.MetadataError,
    ) as exc:
        print(f"full Release metadata error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
