#!/usr/bin/env python3
from __future__ import annotations

import copy
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
                "project": "redis/redis",
                "hashes_repository": "redis/redis-hashes",
                "hashes_ref": "master",
                "hashes_path": "README",
                "hashes_url": (
                    "https://raw.githubusercontent.com/redis/redis-hashes/master/README"
                ),
                "allow_prerelease": False,
                "source_url_template": (
                    "https://download.redis.io/releases/redis-{version}.tar.gz"
                ),
            },
            "policy": {
                "patch_updates": "auto_release",
                "controller_mode": "plan_only",
                "new_series": "candidate_then_pull_request",
                "new_series_floor": "8.0",
                "stop_after_eol": True,
                "retain_existing_releases": True,
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

    def complete_assets(self, version: str) -> list[dict[str, str]]:
        names = [
            f"Redis-Rzon-{version}-linux-glibc2.28-x64.tar.gz",
            f"Redis-Rzon-{version}-linux-glibc2.28-x64.tar.gz.sha256",
            f"Redis-Rzon-{version}-linux-glibc2.28-arm64.tar.gz",
            f"Redis-Rzon-{version}-linux-glibc2.28-arm64.tar.gz.sha256",
            "SHA256SUMS",
            "manifest.json",
            f"redis-unofficial-builds-{version}.spdx.json",
        ]
        return [{"name": name} for name in names]

    def test_blocks_existing_incomplete_release_and_plans_new_release(self) -> None:
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
        self.assertEqual(
            plan["release_plans"][0]["action"],
            "blocked_incomplete_immutable_release",
        )
        self.assertTrue(plan["release_plans"][0]["blocked"])
        self.assertEqual(plan["release_plans"][1]["action"], "plan_new_release")
        self.assertEqual(len(plan["build_matrix"]["include"]), 2)
        self.assertEqual(
            [item["platform_id"] for item in plan["build_matrix"]["include"]],
            [
                "linux-glibc2.28-x64",
                "linux-glibc2.28-arm64",
            ],
        )
        self.assertEqual(
            [item["version"] for item in plan["version_matrix"]["include"]],
            ["8.0.6"],
        )
        self.assertEqual(plan["blocked_release_count"], 1)
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
                "hash redis-7.4.11.tar.gz sha256 " + "a" * 64
                + " https://download.redis.io/releases/redis-7.4.11.tar.gz",
                "hash redis-7.4.11.tar.gz sha256 " + "b" * 64
                + " http://download.redis.io/releases/redis-7.4.11.tar.gz",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(resolver.PlanError, "Conflicting"):
                resolver.parse_hashes(path)

    def test_identical_duplicate_hash_entries_are_collapsed(self) -> None:
        line = (
            "hash redis-7.4.11.tar.gz sha256 "
            + "a" * 64
            + " https://download.redis.io/releases/redis-7.4.11.tar.gz"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README"
            path.write_text(f"{line}\n{line}\n", encoding="utf-8")
            hashes = resolver.parse_hashes(path)
            self.assertEqual(hashes[(7, 4, 11)]["sha256"], "a" * 64)

    def test_hash_entry_must_name_the_same_official_source_archive(self) -> None:
        text = (
            "hash redis-7.4.11.tar.gz sha256 "
            + "a" * 64
            + " https://example.invalid/redis-7.4.11.tar.gz"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(resolver.PlanError, "unexpected source URL"):
                resolver.parse_hashes(path)

    def test_malformed_stable_sha256_entry_cannot_hide_a_new_patch(self) -> None:
        text = (
            "hash redis-7.4.12.tar.gz sha256 NOT-A-DIGEST "
            "http://download.redis.io/releases/redis-7.4.12.tar.gz"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README"
            path.write_text(f"{self.hash_text}\n{text}\n", encoding="utf-8")
            with self.assertRaisesRegex(resolver.PlanError, "Malformed stable"):
                resolver.parse_hashes(path)

    def test_historical_sha1_entry_is_validated_but_not_selected(self) -> None:
        sha1_line = (
            "hash redis-2.8.0.tar.gz sha1 "
            + "a" * 40
            + " http://download.redis.io/releases/redis-2.8.0.tar.gz"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README"
            path.write_text(f"{sha1_line}\n{self.hash_text}\n", encoding="utf-8")
            hashes = resolver.parse_hashes(path)
        self.assertNotIn((2, 8, 0), hashes)
        self.assertIn((7, 4, 11), hashes)

    def test_wrong_length_sha1_entry_is_rejected(self) -> None:
        line = (
            "hash redis-2.8.0.tar.gz sha1 "
            + "a" * 39
            + " http://download.redis.io/releases/redis-2.8.0.tar.gz"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README"
            path.write_text(f"{line}\n{self.hash_text}\n", encoding="utf-8")
            with self.assertRaisesRegex(resolver.PlanError, "Invalid sha1"):
                resolver.parse_hashes(path)

    def test_draft_stable_tag_is_reported_as_blocked(self) -> None:
        archive = "Redis-Rzon-7.4.11-linux-glibc2.28-x64.tar.gz"
        releases = resolver.index_releases(
            [
                {
                    "tag_name": "7.4.11",
                    "draft": True,
                    "assets": [
                        {"name": archive},
                        {"name": f"{archive}.sha256"},
                    ],
                }
            ]
        )
        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            self.parse_hashes(),
            releases,
            dt.date(2026, 8, 20),
            requested_series={"7.4"},
        )
        self.assertEqual(
            plan["release_plans"][0]["action"],
            "blocked_nonfinal_release_state",
        )
        self.assertEqual(plan["build_matrix"], {"include": []})

    def test_nonstable_prerelease_tag_is_ignored(self) -> None:
        releases = resolver.index_releases(
            [
                {
                    "tag_name": "8.2-rc1",
                    "prerelease": True,
                    "assets": [{"name": "candidate-only"}],
                }
            ]
        )
        self.assertEqual(releases, {})

    def test_published_release_without_assets_is_blocked_and_not_rebuilt(self) -> None:
        releases = resolver.index_releases([{"tag_name": "7.4.11", "assets": []}])
        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            self.parse_hashes(),
            releases,
            dt.date(2026, 8, 20),
            requested_series={"7.4"},
        )
        self.assertTrue(plan["release_plans"][0]["release_exists"])
        self.assertEqual(
            plan["release_plans"][0]["action"],
            "blocked_incomplete_immutable_release",
        )
        self.assertEqual(plan["build_matrix"], {"include": []})
        self.assertEqual(plan["version_matrix"], {"include": []})

    def test_exact_complete_asset_contract_is_skipped(self) -> None:
        releases = resolver.index_releases(
            [
                {
                    "tag_name": "7.4.11",
                    "assets": self.complete_assets("7.4.11"),
                }
            ]
        )
        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            self.parse_hashes(),
            releases,
            dt.date(2026, 8, 20),
            requested_series={"7.4"},
        )
        self.assertEqual(plan["release_plans"][0]["action"], "skip_complete")
        self.assertFalse(plan["release_plans"][0]["blocked"])
        self.assertEqual(plan["blocked_release_count"], 0)

    def test_release_level_metadata_is_part_of_the_asset_contract(self) -> None:
        assets = self.complete_assets("7.4.11")
        assets = [asset for asset in assets if asset["name"] != "manifest.json"]
        releases = resolver.index_releases(
            [{"tag_name": "7.4.11", "assets": assets}]
        )
        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            self.parse_hashes(),
            releases,
            dt.date(2026, 8, 20),
            requested_series={"7.4"},
        )
        item = plan["release_plans"][0]
        self.assertEqual(item["action"], "blocked_incomplete_immutable_release")
        self.assertEqual(item["missing_assets"], ["manifest.json"])

    def test_unexpected_existing_asset_is_reported_as_blocked(self) -> None:
        assets = self.complete_assets("7.4.11") + [{"name": "legacy.zip"}]
        releases = resolver.index_releases(
            [{"tag_name": "7.4.11", "assets": assets}]
        )
        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            self.parse_hashes(),
            releases,
            dt.date(2026, 8, 20),
            requested_series={"7.4"},
        )
        item = plan["release_plans"][0]
        self.assertEqual(
            item["action"], "blocked_unexpected_immutable_release_assets"
        )
        self.assertEqual(item["unexpected_assets"], ["legacy.zip"])

    def test_noncanonical_release_tag_is_rejected(self) -> None:
        for tag in ("redis-v7.4.11", "07.4.11"):
            with self.subTest(tag=tag), self.assertRaisesRegex(
                resolver.PlanError, "Noncanonical"
            ):
                resolver.index_releases([{"tag_name": tag, "assets": []}])

    def test_noncanonical_requested_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(resolver.PlanError, "Noncanonical"):
            resolver.resolve(
                self.release_config,
                self.platform_config,
                self.parse_hashes(),
                {},
                dt.date(2026, 8, 20),
                requested_version="07.4.11",
            )

    def test_malformed_release_state_is_rejected(self) -> None:
        with self.assertRaisesRegex(resolver.PlanError, "publication state"):
            resolver.index_releases(
                [{"tag_name": "7.4.11", "draft": "false", "assets": []}]
            )

    def test_unknown_release_configuration_key_is_rejected(self) -> None:
        self.release_config["unexpected"] = True
        with self.assertRaisesRegex(resolver.PlanError, "top-level keys"):
            resolver.validate_release_config(self.release_config)

    def test_unknown_platform_configuration_key_is_rejected(self) -> None:
        self.platform_config["unexpected"] = True
        with self.assertRaisesRegex(resolver.PlanError, "top-level keys"):
            resolver.validate_platform_config(self.platform_config)

    def test_checked_in_designed_platform_matrix_cannot_silently_shrink(self) -> None:
        config = resolver.load_json(ROOT / "config/platforms.json")
        resolver.validate_repository_platform_matrix(config)
        config["platforms"] = [
            platform
            for platform in config["platforms"]
            if platform["id"] != "windows-msys2-x64"
        ]
        with self.assertRaisesRegex(resolver.PlanError, "designed platform"):
            resolver.validate_repository_platform_matrix(config)

    def test_boolean_schema_and_policy_integer_are_rejected(self) -> None:
        config = copy.deepcopy(self.release_config)
        config["schema"] = True
        with self.assertRaisesRegex(resolver.PlanError, "schema 1"):
            resolver.validate_release_config(config)

        config = copy.deepcopy(self.release_config)
        config["policy"]["stop_after_eol"] = 1
        with self.assertRaisesRegex(resolver.PlanError, "stop_after_eol"):
            resolver.validate_release_config(config)

    def test_noncanonical_eol_date_is_rejected(self) -> None:
        self.release_config["series"][0]["eol"] = "20291201"
        with self.assertRaisesRegex(resolver.PlanError, "Noncanonical EOL"):
            resolver.validate_release_config(self.release_config)

    def test_duplicate_and_nonfinite_json_values_are_rejected(self) -> None:
        fixtures = (
            ('{"schema":1,"schema":1}', "Duplicate JSON key"),
            ('{"schema":NaN}', "Non-finite JSON number"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (text, error) in enumerate(fixtures):
                with self.subTest(error=error):
                    path = Path(directory) / f"input-{index}.json"
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(resolver.PlanError, error):
                        resolver.load_json(path)

    def test_hashes_snapshot_oid_is_strict_and_recorded(self) -> None:
        commit = "a" * 40
        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            self.parse_hashes(),
            {},
            dt.date(2026, 8, 20),
            requested_series={"7.4"},
            hashes_commit=commit,
        )
        self.assertEqual(plan["hashes_commit"], commit)
        for invalid in ("A" * 40, "a" * 39, "main"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                resolver.PlanError, "Git OID"
            ):
                resolver.parse_git_oid(invalid)

    def test_overlong_numeric_version_component_is_rejected(self) -> None:
        with self.assertRaisesRegex(resolver.PlanError, "Invalid stable"):
            resolver.parse_version("1234567.0.0")

    def test_duplicate_stable_release_tag_is_rejected(self) -> None:
        with self.assertRaisesRegex(resolver.PlanError, "duplicate release tag"):
            resolver.index_releases(
                [
                    {"tag_name": "7.4.11", "assets": []},
                    {"tag_name": "7.4.11", "assets": []},
                ]
            )

    def test_partial_archive_checksum_pair_is_blocked_not_rebuilt(self) -> None:
        archive = "Redis-Rzon-7.4.11-linux-glibc2.28-x64.tar.gz"
        releases = resolver.index_releases(
            [{"tag_name": "7.4.11", "assets": [{"name": archive}]}]
        )
        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            self.parse_hashes(),
            releases,
            dt.date(2026, 8, 20),
            requested_series={"7.4"},
        )
        self.assertEqual(
            plan["release_plans"][0]["action"],
            "blocked_incomplete_immutable_release",
        )
        self.assertEqual(plan["build_matrix"], {"include": []})

    def test_exact_version_and_series_filter_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(resolver.PlanError, "mutually exclusive"):
            resolver.resolve(
                self.release_config,
                self.platform_config,
                self.parse_hashes(),
                {},
                dt.date(2026, 8, 20),
                requested_series={"7.4"},
                requested_version="7.4.11",
            )

    def test_platform_id_must_match_variant_and_arch(self) -> None:
        duplicate = dict(self.platform_config["platforms"][0])
        duplicate["id"] = "linux-glibc2.28-x64-duplicate"
        self.platform_config["platforms"].append(duplicate)
        with self.assertRaisesRegex(resolver.PlanError, "variant-arch"):
            resolver.validate_platform_config(self.platform_config)

    def test_invalid_workflow_name_is_rejected(self) -> None:
        self.platform_config["platforms"][0]["build_workflow"] = "../unsafe.yml"
        with self.assertRaisesRegex(resolver.PlanError, "name a workflow"):
            resolver.validate_platform_config(self.platform_config)

    def test_unsupported_enabled_workflow_is_rejected(self) -> None:
        self.platform_config["platforms"][0]["build_workflow"] = "other.yml"
        with self.assertRaisesRegex(resolver.PlanError, "unsupported workflow"):
            resolver.validate_platform_config(self.platform_config)

    def test_package_prefix_drift_is_rejected(self) -> None:
        self.platform_config["package_name_prefix"] = "Other"
        with self.assertRaisesRegex(resolver.PlanError, "backend contract"):
            resolver.validate_platform_config(self.platform_config)

    def test_required_architecture_cannot_be_silently_disabled(self) -> None:
        self.platform_config["platforms"][1]["controller_enabled"] = False
        self.platform_config["platforms"][1]["build_workflow"] = None
        with self.assertRaisesRegex(resolver.PlanError, "not controller-enabled"):
            resolver.validate_platform_config(self.platform_config)

    def test_blocked_summary_is_explicit(self) -> None:
        releases = resolver.index_releases(
            [{"tag_name": "7.4.11", "assets": []}]
        )
        plan = resolver.resolve(
            self.release_config,
            self.platform_config,
            self.parse_hashes(),
            releases,
            dt.date(2026, 8, 20),
            requested_series={"7.4"},
        )
        summary = resolver.render_summary(plan)
        self.assertIn("Blocked immutable releases: **1**", summary)
        self.assertIn("excluded from all build matrices", summary)


if __name__ == "__main__":
    unittest.main()
