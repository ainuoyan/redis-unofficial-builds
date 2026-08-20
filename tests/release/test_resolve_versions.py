#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "resolve_versions", ROOT / "scripts/release/resolve_versions.py"
)
assert SPEC is not None and SPEC.loader is not None
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


class ResolveVersionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.release_config = {
            "schema": 1,
            "upstream": {
                "allow_prerelease": False,
                "source_url_template": (
                    "https://download.redis.io/releases/redis-{version}.tar.gz"
                ),
            },
            "policy": {
                "controller_mode": "plan_only",
                "new_series": "candidate_then_pull_request",
                "new_series_floor": "8.0",
                "stop_after_eol": True,
            },
            "series": [
                {
                    "series": "7.4",
                    "release_type": "extended",
                    "eol": "2029-12-01",
                },
                {
                    "series": "8.0",
                    "release_type": "standard",
                    "eol": "2026-12-01",
                },
            ],
        }
        self.platform_config = {
            "schema": 1,
            "package_name_prefix": "Redis-Rzon",
            "platforms": [
                {
                    "id": "linux-glibc2.28-x64",
                    "variant": "linux-glibc2.28",
                    "os": "linux",
                    "arch": "x64",
                    "archive_extension": "tar.gz",
                    "status": "implemented",
                    "controller_enabled": True,
                    "build_workflow": "build-linux.yml",
                },
                {
                    "id": "linux-glibc2.28-arm64",
                    "variant": "linux-glibc2.28",
                    "os": "linux",
                    "arch": "arm64",
                    "archive_extension": "tar.gz",
                    "status": "implemented",
                    "controller_enabled": True,
                    "build_workflow": "build-linux.yml",
                },
                {
                    "id": "windows-msys2-x64",
                    "variant": "windows-msys2",
                    "os": "windows",
                    "arch": "x64",
                    "archive_extension": "zip",
                    "status": "designed",
                    "controller_enabled": False,
                    "build_workflow": None,
                },
            ],
        }
        self.hash_text = "\n".join(
            [
                "hash redis-7.4.10.tar.gz sha256 " + "1" * 64
                + " http://download.redis.io/releases/redis-7.4.10.tar.gz",
                "hash redis-7.4.11.tar.gz sha256 " + "2" * 64
                + " http://download.redis.io/releases/redis-7.4.11.tar.gz",
                "hash redis-8.0.6.tar.gz sha256 " + "3" * 64
                + " http://download.redis.io/releases/redis-8.0.6.tar.gz",
                "hash redis-8.2-rc1.tar.gz sha256 " + "4" * 64
                + " http://download.redis.io/releases/redis-8.2-rc1.tar.gz",
                "hash redis-8.2.1.tar.gz sha256 " + "5" * 64
                + " http://download.redis.io/releases/redis-8.2.1.tar.gz",
            ]
        )

    def parse_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README"
            path.write_text(self.hash_text, encoding="utf-8")
            return resolver.parse_hashes(path)

    def test_resolves_latest_missing_assets_and_new_series(self) -> None:
        resolver.validate_release_config(self.release_config)
        resolver.validate_platform_config(self.platform_config)
        hashes = self.parse_hashes()
        releases = resolver.index_releases(
            [
                {
                    "tag_name": "7.4.11",
                    "assets": [
                        {
                            "name": (
                                "Redis-Rzon-7.4.11-linux-glibc2.28-x64.tar.gz"
                            )
                        },
                        {
                            "name": (
                                "Redis-Rzon-7.4.11-linux-glibc2.28-x64.tar.gz.sha256"
                            )
                        },
                    ],
                }
            ]
        )

        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            hashes,
            releases,
            dt.date(2026, 8, 20),
        )

        self.assertEqual(
            [item["version"] for item in plan["release_plans"]],
            ["7.4.11", "8.0.6"],
        )
        self.assertEqual(plan["release_plans"][0]["action"], "plan_complete_release")
        self.assertEqual(plan["release_plans"][1]["action"], "plan_new_release")
        self.assertEqual(len(plan["build_matrix"]["include"]), 3)
        self.assertEqual(
            [item["platform_id"] for item in plan["build_matrix"]["include"]],
            [
                "linux-glibc2.28-arm64",
                "linux-glibc2.28-x64",
                "linux-glibc2.28-arm64",
            ],
        )
        self.assertEqual(
            plan["new_series_candidates"],
            [
                {
                    "series": "8.2",
                    "latest_version": "8.2.1",
                    "source_sha256": "5" * 64,
                    "action": "candidate_then_pull_request",
                }
            ],
        )
        self.assertEqual(len(plan["disabled_platforms"]), 1)
        self.assertTrue(plan["has_planned_builds"])

    def test_eol_series_is_skipped(self) -> None:
        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            self.parse_hashes(),
            {},
            dt.date(2027, 1, 1),
            requested_series={"8.0"},
        )
        self.assertEqual(plan["release_plans"][0]["action"], "skip_eol")
        self.assertEqual(plan["build_matrix"], {"include": []})
        self.assertFalse(plan["has_planned_builds"])

    def test_exact_version_uses_official_hash(self) -> None:
        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            self.parse_hashes(),
            {},
            dt.date(2026, 8, 20),
            requested_version="7.4.10",
        )
        self.assertEqual(len(plan["release_plans"]), 1)
        self.assertEqual(plan["release_plans"][0]["version"], "7.4.10")
        self.assertEqual(plan["release_plans"][0]["source_sha256"], "1" * 64)

    def test_rejects_untracked_version(self) -> None:
        with self.assertRaisesRegex(resolver.PlanError, "not in a tracked series"):
            resolver.resolve(
                self.release_config,
                self.platform_config,
                self.parse_hashes(),
                {},
                dt.date(2026, 8, 20),
                requested_version="8.2.1",
            )

    def test_conflicting_hash_entries_are_rejected(self) -> None:
        text = "\n".join(
            [
                "hash redis-7.4.11.tar.gz sha256 " + "a" * 64 + " https://one",
                "hash redis-7.4.11.tar.gz sha256 " + "b" * 64 + " https://two",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(resolver.PlanError, "Conflicting"):
                resolver.parse_hashes(path)


if __name__ == "__main__":
    unittest.main()
