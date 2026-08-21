from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.release.package_fixture import (
    HASHES_COMMIT,
    REVISION,
    SOURCE_SHA256,
    VARIANT,
    VERSION,
    write_package,
)


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/release"))
import release_metadata  # noqa: E402


class ReleaseMetadataTests(unittest.TestCase):
    def args(self, directory: Path, command: str) -> argparse.Namespace:
        values = {
            "command": command,
            "asset_dir": directory,
            "redis_version": VERSION,
            "source_sha256": SOURCE_SHA256,
            "variant": VARIANT,
            "min_glibc": "2.28",
            "repository": "example/redis-unofficial-builds",
            "packaging_revision": REVISION,
            "packaging_root": None,
        }
        if command == "create":
            values["created"] = "2026-08-20T12:00:00Z"
        return argparse.Namespace(**values)

    def create_set(self, directory: Path) -> None:
        write_package(directory, "x64")
        write_package(directory, "arm64")
        release_metadata.create_metadata(self.args(directory, "create"))

    def test_create_and_validate_current_release_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_dir = Path(directory)
            self.create_set(asset_dir)
            release_metadata.validate_metadata_set(self.args(asset_dir, "validate"))
            self.assertEqual(
                release_metadata.regular_names(asset_dir),
                release_metadata.expected_release_names(VERSION, VARIANT),
            )
            self.assertEqual(
                release_metadata.expected_release_names(VERSION, VARIANT),
                {
                    f"Redis-{VERSION}-{VARIANT}-x64.tar.gz",
                    f"Redis-{VERSION}-{VARIANT}-x64.tar.gz.sha256",
                    f"Redis-{VERSION}-{VARIANT}-arm64.tar.gz",
                    f"Redis-{VERSION}-{VARIANT}-arm64.tar.gz.sha256",
                    "SHA256SUMS",
                    "manifest.json",
                    f"redis-unofficial-builds-{VERSION}.spdx.json",
                },
            )
            sbom = json.loads(
                (asset_dir / release_metadata.sbom_name(VERSION)).read_text()
            )
            self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
            self.assertEqual(len(sbom["packages"]), 3)
            self.assertIn(
                "GENERATED_FROM",
                {item["relationshipType"] for item in sbom["relationships"]},
            )
            manifest = json.loads(
                (asset_dir / release_metadata.MANIFEST_NAME).read_text()
            )
            self.assertEqual(
                manifest["metadata"]["sbom_scope"], "release-package-level"
            )
            self.assertEqual(
                manifest["build"]["patchset_sha256"],
                manifest["artifacts"][0]["patchset_sha256"],
            )
            self.assertEqual(
                manifest["source"]["hashes"],
                {
                    "repository": "redis/redis-hashes",
                    "path": "README",
                    "commit": HASHES_COMMIT,
                },
            )

    def test_architectures_must_use_the_same_hashes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_dir = Path(directory)
            write_package(asset_dir, "x64")
            write_package(asset_dir, "arm64", hashes_commit="e" * 40)
            with self.assertRaisesRegex(
                release_metadata.MetadataError, "different Redis hashes snapshots"
            ):
                release_metadata.create_metadata(self.args(asset_dir, "create"))

    def test_manifest_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_dir = Path(directory)
            self.create_set(asset_dir)
            manifest_path = asset_dir / release_metadata.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text())
            manifest["source"]["sha256"] = "0" * 64
            release_metadata.write_json(manifest_path, manifest)
            with self.assertRaisesRegex(release_metadata.MetadataError, "manifest"):
                release_metadata.validate_metadata_set(self.args(asset_dir, "validate"))

    def test_malformed_manifest_build_type_is_rejected_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_dir = Path(directory)
            self.create_set(asset_dir)
            manifest_path = asset_dir / release_metadata.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text())
            manifest["build"] = []
            release_metadata.write_json(manifest_path, manifest)
            with self.assertRaisesRegex(release_metadata.MetadataError, "build"):
                release_metadata.validate_metadata_set(self.args(asset_dir, "validate"))

    def test_malformed_spdx_creation_info_type_is_rejected_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_dir = Path(directory)
            self.create_set(asset_dir)
            spdx_path = asset_dir / release_metadata.sbom_name(VERSION)
            spdx = json.loads(spdx_path.read_text())
            spdx["creationInfo"] = []
            release_metadata.write_json(spdx_path, spdx)
            with self.assertRaisesRegex(release_metadata.MetadataError, "creationInfo"):
                release_metadata.validate_metadata_set(self.args(asset_dir, "validate"))

    def test_extra_release_asset_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_dir = Path(directory)
            self.create_set(asset_dir)
            (asset_dir / "unexpected.txt").write_text("unexpected\n")
            with self.assertRaisesRegex(release_metadata.MetadataError, "exact"):
                release_metadata.validate_metadata_set(self.args(asset_dir, "validate"))

    def test_forged_archive_with_matching_checksum_is_a_clean_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_dir = Path(directory)
            self.create_set(asset_dir)
            archive = asset_dir / f"Redis-{VERSION}-{VARIANT}-x64.tar.gz"
            forged = gzip.compress(b"garbage-not-a-tar" * 100)
            archive.write_bytes(forged)
            (asset_dir / f"{archive.name}.sha256").write_text(
                f"{hashlib.sha256(forged).hexdigest()}  {archive.name}\n",
                encoding="ascii",
            )
            argv = [
                "release_metadata.py",
                "validate",
                "--asset-dir",
                str(asset_dir),
                "--redis-version",
                VERSION,
                "--source-sha256",
                SOURCE_SHA256,
                "--variant",
                VARIANT,
                "--min-glibc",
                "2.28",
                "--repository",
                "example/redis-unofficial-builds",
                "--packaging-revision",
                REVISION,
            ]
            stderr = io.StringIO()
            with (
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(release_metadata.main(), 2)
            self.assertIn("release metadata error:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
