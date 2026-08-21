#!/usr/bin/env python3
"""Create or validate the immutable metadata set for one Redis release."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path
from typing import Any

import validate_release_asset as asset_validator


MANIFEST_NAME = "manifest.json"
CHECKSUMS_NAME = "SHA256SUMS"
WORKFLOW_PATH = ".github/workflows/build-linux.yml"
ARCHITECTURES = ("x64", "arm64")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CREATED_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
MAX_JSON_BYTES = 1024 * 1024


class MetadataError(RuntimeError):
    """Raised when release metadata is missing, ambiguous, or inconsistent."""


def sbom_name(version: str) -> str:
    return f"redis-unofficial-builds-{version}.spdx.json"


def sha256_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise MetadataError(f"release asset is not a regular file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strict_json(path: Path) -> Any:
    if not path.is_file() or path.is_symlink():
        raise MetadataError(f"metadata is not a regular file: {path.name}")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise MetadataError(f"metadata is unexpectedly large: {path.name}")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MetadataError(f"duplicate JSON key in {path.name}: {key}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MetadataError(f"invalid JSON metadata: {path.name}") from exc


def validate_common_values(
    version: str,
    source_sha256: str,
    variant: str,
    min_glibc: str,
    repository: str,
    packaging_revision: str | None,
) -> None:
    asset_validator.parse_version(version)
    if not asset_validator.SHA256_RE.fullmatch(source_sha256):
        raise MetadataError("invalid Redis source SHA-256")
    if not asset_validator.VARIANT_RE.fullmatch(variant):
        raise MetadataError("invalid package variant")
    asset_validator.parse_glibc(min_glibc)
    if not REPOSITORY_RE.fullmatch(repository):
        raise MetadataError("invalid GitHub repository name")
    if packaging_revision is not None and not REVISION_RE.fullmatch(
        packaging_revision
    ):
        raise MetadataError("invalid packaging revision")


def inspect_archives(
    asset_dir: Path,
    *,
    version: str,
    source_sha256: str,
    variant: str,
    min_glibc: str,
    packaging_revision: str | None,
    packaging_root: Path | None,
) -> tuple[list[dict[str, Any]], str, str]:
    artifacts: list[dict[str, Any]] = []
    observed_revision: str | None = None
    observed_patchset: str | None = None
    observed_hashes_commit: str | None = None
    for arch in ARCHITECTURES:
        archive_name = asset_validator.expected_archive_name(version, variant, arch)
        archive = asset_dir / archive_name
        checksum = asset_dir / f"{archive_name}.sha256"
        asset_validator.validate_checksum(archive, checksum)
        values, _, build_info = asset_validator.read_package_info(archive)
        asset_validator.validate_elf_binaries(
            archive,
            arch=arch,
            version=version,
            min_glibc=min_glibc,
            declared_max_glibc=values["MAX_GLIBC_SYMBOL"],
        )
        asset_validator.validate_metadata(
            values,
            version=version,
            variant=variant,
            arch=arch,
            source_sha256=source_sha256,
            min_glibc=min_glibc,
        )
        revision, hashes_commit = asset_validator.validate_build_info(
            build_info,
            version=version,
            variant=variant,
            arch=arch,
            source_sha256=source_sha256,
            patchset_sha256=values["PATCHSET_SHA256"],
            packaging_revision=packaging_revision,
        )
        if observed_revision is None:
            observed_revision = revision
        elif revision != observed_revision:
            raise MetadataError("package architectures use different revisions")
        if observed_hashes_commit is None:
            observed_hashes_commit = hashes_commit
        elif hashes_commit != observed_hashes_commit:
            raise MetadataError(
                "package architectures use different Redis hashes snapshots"
            )
        patchset = values["PATCHSET_SHA256"]
        if observed_patchset is None:
            observed_patchset = patchset
        elif patchset != observed_patchset:
            raise MetadataError("package architectures use different patch sets")
        if packaging_root is not None:
            asset_validator.validate_packaging_bindings(
                archive, packaging_root, patchset
            )
        artifacts.append(
            {
                "name": archive_name,
                "checksum_file": f"{archive_name}.sha256",
                "sha256": sha256_file(archive),
                "size": archive.stat().st_size,
                "arch": arch,
                "patchset_sha256": patchset,
            }
        )
    if observed_revision is None or observed_hashes_commit is None:
        raise MetadataError("no package architectures were found")
    return artifacts, observed_revision, observed_hashes_commit


def build_manifest(
    *,
    version: str,
    source_sha256: str,
    variant: str,
    min_glibc: str,
    repository: str,
    packaging_revision: str,
    hashes_commit: str,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    parsed = asset_validator.parse_version(version)
    return {
        "schema": 1,
        "package_id": "redis-unofficial-builds",
        "release_tag": version,
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
            "revision": packaging_revision,
            "patchset_sha256": artifacts[0]["patchset_sha256"],
            "workflow": WORKFLOW_PATH,
            "profile": "core",
            "variant": variant,
            "min_glibc": min_glibc,
            "service_backend": "systemd",
        },
        "artifacts": artifacts,
        "metadata": {
            "checksums": CHECKSUMS_NAME,
            "sbom": sbom_name(version),
            "sbom_format": "SPDX-2.3",
            "sbom_scope": "release-package-level",
        },
    }


def build_spdx(
    *,
    manifest: dict[str, Any],
    repository: str,
    created: str,
) -> dict[str, Any]:
    if not CREATED_RE.fullmatch(created):
        raise MetadataError("SPDX creation time must be canonical UTC")
    try:
        dt.datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise MetadataError("SPDX creation time is not a real UTC timestamp") from exc
    version = manifest["redis_version"]
    revision = manifest["build"]["revision"]
    packages: list[dict[str, Any]] = [
        {
            "name": "Redis",
            "SPDXID": "SPDXRef-Package-Redis-Upstream",
            "versionInfo": version,
            "downloadLocation": manifest["source"]["url"],
            "filesAnalyzed": False,
            "checksums": [
                {
                    "algorithm": "SHA256",
                    "checksumValue": manifest["source"]["sha256"],
                }
            ],
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "supplier": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:generic/redis@{version}",
                }
            ],
        }
    ]
    relationships: list[dict[str, str]] = []
    for artifact in manifest["artifacts"]:
        package_id = f"SPDXRef-Package-{artifact['arch']}"
        packages.append(
            {
                "name": artifact["name"],
                "SPDXID": package_id,
                "versionInfo": version,
                "packageFileName": artifact["name"],
                "downloadLocation": (
                    f"https://github.com/{repository}/releases/download/"
                    f"{version}/{artifact['name']}"
                ),
                "filesAnalyzed": False,
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": artifact["sha256"]}
                ],
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "supplier": "NOASSERTION",
            }
        )
        relationships.extend(
            [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": package_id,
                },
                {
                    "spdxElementId": package_id,
                    "relationshipType": "GENERATED_FROM",
                    "relatedSpdxElement": "SPDXRef-Package-Redis-Upstream",
                },
            ]
        )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"redis-unofficial-builds-{version}",
        "documentNamespace": (
            f"https://github.com/{repository}/releases/tag/{version}/spdx/{revision}"
        ),
        "creationInfo": {
            "created": created,
            "creators": ["Tool: redis-unofficial-builds/release_metadata.py"],
        },
        "packages": packages,
        "relationships": relationships,
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def expected_package_names(version: str, variant: str) -> set[str]:
    result: set[str] = set()
    for arch in ARCHITECTURES:
        archive = asset_validator.expected_archive_name(version, variant, arch)
        result.update({archive, f"{archive}.sha256"})
    return result


def expected_release_names(version: str, variant: str) -> set[str]:
    return expected_package_names(version, variant) | {
        MANIFEST_NAME,
        CHECKSUMS_NAME,
        sbom_name(version),
    }


def regular_names(directory: Path) -> set[str]:
    if not directory.is_dir() or directory.is_symlink():
        raise MetadataError(f"asset directory is unsafe: {directory}")
    names: set[str] = set()
    for entry in directory.iterdir():
        if not entry.is_file() or entry.is_symlink():
            raise MetadataError(f"unexpected non-regular release asset: {entry.name}")
        names.add(entry.name)
    return names


def create_metadata(args: argparse.Namespace) -> None:
    expected_packages = expected_package_names(args.redis_version, args.variant)
    if regular_names(args.asset_dir) != expected_packages:
        raise MetadataError("asset directory does not contain exactly two package pairs")
    artifacts, revision, hashes_commit = inspect_archives(
        args.asset_dir,
        version=args.redis_version,
        source_sha256=args.source_sha256,
        variant=args.variant,
        min_glibc=args.min_glibc,
        packaging_revision=args.packaging_revision,
        packaging_root=args.packaging_root,
    )
    manifest = build_manifest(
        version=args.redis_version,
        source_sha256=args.source_sha256,
        variant=args.variant,
        min_glibc=args.min_glibc,
        repository=args.repository,
        packaging_revision=revision,
        hashes_commit=hashes_commit,
        artifacts=artifacts,
    )
    write_json(args.asset_dir / MANIFEST_NAME, manifest)
    spdx = build_spdx(manifest=manifest, repository=args.repository, created=args.created)
    write_json(args.asset_dir / sbom_name(args.redis_version), spdx)

    checksummed = expected_release_names(args.redis_version, args.variant) - {
        CHECKSUMS_NAME
    }
    lines = [
        f"{sha256_file(args.asset_dir / name)}  {name}"
        for name in sorted(checksummed)
    ]
    (args.asset_dir / CHECKSUMS_NAME).write_text(
        "\n".join(lines) + "\n", encoding="ascii"
    )
    if regular_names(args.asset_dir) != expected_release_names(
        args.redis_version, args.variant
    ):
        raise MetadataError("generated release metadata set is incomplete")


def validate_metadata_set(args: argparse.Namespace) -> None:
    expected_names = expected_release_names(args.redis_version, args.variant)
    if regular_names(args.asset_dir) != expected_names:
        raise MetadataError("release does not contain the exact current asset set")
    manifest_path = args.asset_dir / MANIFEST_NAME
    manifest = strict_json(manifest_path)
    if not isinstance(manifest, dict):
        raise MetadataError("release manifest must be a JSON object")
    build = manifest.get("build")
    if not isinstance(build, dict):
        raise MetadataError("release manifest build must be a JSON object")
    revision = build.get("revision")
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise MetadataError("release manifest has an invalid packaging revision")
    if args.packaging_revision is not None and revision != args.packaging_revision:
        raise MetadataError("release manifest packaging revision does not match")

    artifacts, observed_revision, hashes_commit = inspect_archives(
        args.asset_dir,
        version=args.redis_version,
        source_sha256=args.source_sha256,
        variant=args.variant,
        min_glibc=args.min_glibc,
        packaging_revision=revision,
        packaging_root=args.packaging_root,
    )
    if observed_revision != revision:
        raise MetadataError("package and manifest revisions differ")
    expected_manifest = build_manifest(
        version=args.redis_version,
        source_sha256=args.source_sha256,
        variant=args.variant,
        min_glibc=args.min_glibc,
        repository=args.repository,
        packaging_revision=revision,
        hashes_commit=hashes_commit,
        artifacts=artifacts,
    )
    if manifest != expected_manifest:
        raise MetadataError("release manifest does not match the package set")

    spdx_path = args.asset_dir / sbom_name(args.redis_version)
    spdx = strict_json(spdx_path)
    if not isinstance(spdx, dict):
        raise MetadataError("SPDX SBOM must be a JSON object")
    creation_info = spdx.get("creationInfo")
    if not isinstance(creation_info, dict):
        raise MetadataError("SPDX SBOM creationInfo must be a JSON object")
    created = creation_info.get("created")
    if not isinstance(created, str):
        raise MetadataError("SPDX SBOM is missing its creation time")
    expected_spdx = build_spdx(
        manifest=manifest, repository=args.repository, created=created
    )
    if spdx != expected_spdx:
        raise MetadataError("SPDX SBOM does not match the release manifest")

    checksummed = expected_names - {CHECKSUMS_NAME}
    expected_text = "".join(
        f"{sha256_file(args.asset_dir / name)}  {name}\n"
        for name in sorted(checksummed)
    )
    checksums_path = args.asset_dir / CHECKSUMS_NAME
    if checksums_path.stat().st_size > 64 * 1024:
        raise MetadataError("SHA256SUMS is unexpectedly large")
    try:
        actual_text = checksums_path.read_text(encoding="ascii")
    except UnicodeError as exc:
        raise MetadataError("SHA256SUMS is not ASCII") from exc
    if actual_text != expected_text:
        raise MetadataError("SHA256SUMS is not canonical or does not match")


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--redis-version", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--min-glibc", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--packaging-revision")
    parser.add_argument("--packaging-root", type=Path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    add_common_arguments(create)
    create.add_argument("--created", required=True)
    validate = subparsers.add_parser("validate")
    add_common_arguments(validate)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validate_common_values(
            args.redis_version,
            args.source_sha256,
            args.variant,
            args.min_glibc,
            args.repository,
            args.packaging_revision,
        )
        if args.command == "create":
            create_metadata(args)
            print(f"Created release metadata for Redis {args.redis_version}")
        else:
            validate_metadata_set(args)
            print(f"Validated release metadata for Redis {args.redis_version}")
        return 0
    except (
        OSError,
        UnicodeError,
        EOFError,
        json.JSONDecodeError,
        tarfile.TarError,
        asset_validator.ValidationError,
        MetadataError,
    ) as exc:
        print(f"release metadata error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
