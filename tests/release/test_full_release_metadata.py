from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/release"))
import full_release_metadata as metadata  # noqa: E402
import release_metadata  # noqa: E402


VERSION = "7.4.11"
SOURCE_SHA256 = "a" * 64
REVISION = "b" * 40
HASHES_COMMIT = "c" * 40


class FullReleaseMetadataTests(unittest.TestCase):
    def args(self, directory: Path, command: str) -> argparse.Namespace:
        values = {
            "command": command,
            "asset_dir": directory,
            "redis_version": VERSION,
            "release_tag": f"Redis-{VERSION}-r2",
            "source_sha256": SOURCE_SHA256,
            "hashes_commit": HASHES_COMMIT,
            "repository": "example/redis-unofficial-builds",
            "packaging_revision": REVISION,
            "packaging_root": ROOT,
        }
        if command == "create":
            values["created"] = "2026-08-23T00:00:00Z"
        return argparse.Namespace(**values)

    def test_full_contract_has_nine_package_pairs_and_three_metadata_files(self) -> None:
        packages = metadata.expected_package_names(VERSION)
        release = metadata.expected_release_names(VERSION)
        self.assertEqual(len(packages), 18)
        self.assertEqual(len(release), 21)
        self.assertIn(f"Redis-{VERSION}-windows-msys2-x64.zip", packages)
        self.assertIn(f"Redis-{VERSION}-macos15-arm64.tar.gz.sha256", packages)
        self.assertEqual(
            release - packages,
            {
                "SHA256SUMS",
                "manifest.json",
                f"redis-unofficial-builds-{VERSION}.spdx.json",
            },
        )

    def test_incomplete_package_set_is_rejected_before_metadata_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                metadata.FullMetadataError, "nine package pairs"
            ):
                metadata.create_metadata(self.args(Path(directory), "create"))

    def test_manifest_and_spdx_cover_every_platform_with_unique_ids(self) -> None:
        artifacts = []
        for platform in metadata.PLATFORMS:
            name = metadata.archive_name(VERSION, platform)
            artifacts.append(
                {
                    "name": name,
                    "checksum_file": f"{name}.sha256",
                    "sha256": "d" * 64,
                    "size": 1,
                    "variant": platform["variant"],
                    "os": platform["os"],
                    "arch": platform["arch"],
                    "runtime": platform["runtime"],
                    "runtime_baseline": platform["baseline"],
                    "service_backend": platform["service"],
                    "patchset_sha256": "e" * 64,
                }
            )
        manifest = metadata.build_manifest(
            version=VERSION,
            release_tag=f"Redis-{VERSION}-r2",
            source_sha256=SOURCE_SHA256,
            repository="example/redis-unofficial-builds",
            revision=REVISION,
            hashes_commit=HASHES_COMMIT,
            artifacts=artifacts,
        )
        self.assertEqual(manifest["schema"], 2)
        self.assertEqual(manifest["release_tag"], f"Redis-{VERSION}-r2")
        self.assertEqual(len(manifest["artifacts"]), 9)
        spdx = release_metadata.build_spdx(
            manifest=manifest,
            repository="example/redis-unofficial-builds",
            created="2026-08-23T00:00:00Z",
        )
        package_ids = [item["SPDXID"] for item in spdx["packages"]]
        self.assertEqual(len(package_ids), len(set(package_ids)))
        self.assertEqual(len(spdx["packages"]), 10)
        self.assertIn(
            f"/releases/tag/Redis-{VERSION}-r2/spdx/",
            spdx["documentNamespace"],
        )
        for package in spdx["packages"]:
            if package["SPDXID"] == "SPDXRef-Package-Redis-Upstream":
                continue
            self.assertIn(
                f"/releases/download/Redis-{VERSION}-r2/",
                package["downloadLocation"],
            )

    def test_mixed_packaging_revisions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_dir = Path(directory)
            for name in metadata.expected_package_names(VERSION):
                (asset_dir / name).write_bytes(b"fixture")
            linux_result = (
                {"PATCHSET_SHA256": "d" * 64},
                REVISION,
                HASHES_COMMIT,
            )
            portable_results = [
                (
                    {"PATCHSET_SHA256": "e" * 64},
                    ("f" * 40 if index == 0 else REVISION),
                    HASHES_COMMIT,
                )
                for index in range(5)
            ]
            with (
                mock.patch.object(metadata, "_validate_linux", return_value=linux_result),
                mock.patch.object(
                    metadata, "_validate_portable", side_effect=portable_results
                ),
                self.assertRaisesRegex(
                    metadata.FullMetadataError, "different packaging revisions"
                ),
            ):
                metadata.inspect_archives(
                    asset_dir,
                    version=VERSION,
                    source_sha256=SOURCE_SHA256,
                    packaging_revision=REVISION,
                    packaging_root=ROOT,
                    expected_hashes_commit=HASHES_COMMIT,
                )

    def test_manifest_json_has_no_experimental_status(self) -> None:
        artifacts = []
        for platform in metadata.PLATFORMS:
            name = metadata.archive_name(VERSION, platform)
            artifacts.append(
                {
                    "name": name,
                    "checksum_file": f"{name}.sha256",
                    "sha256": "d" * 64,
                    "size": 1,
                    "variant": platform["variant"],
                    "os": platform["os"],
                    "arch": platform["arch"],
                    "runtime": platform["runtime"],
                    "runtime_baseline": platform["baseline"],
                    "service_backend": platform["service"],
                    "patchset_sha256": "e" * 64,
                }
            )
        manifest = metadata.build_manifest(
            version=VERSION,
            release_tag=f"Redis-{VERSION}",
            source_sha256=SOURCE_SHA256,
            repository="example/redis-unofficial-builds",
            revision=REVISION,
            hashes_commit=HASHES_COMMIT,
            artifacts=artifacts,
        )
        self.assertNotIn("experimental", json.dumps(manifest).lower())


if __name__ == "__main__":
    unittest.main()
