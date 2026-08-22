from __future__ import annotations

import copy
import datetime as dt
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/release"))
import resolve_versions  # noqa: E402
import validate_publish_policy  # noqa: E402


class PublishPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release_config = json.loads(
            (ROOT / "config/release-lines.json").read_text(encoding="utf-8")
        )
        cls.platform_config = json.loads(
            (ROOT / "config/platforms.json").read_text(encoding="utf-8")
        )

    def test_real_repository_configs_and_workflows_are_valid(self) -> None:
        resolve_versions.validate_release_config(self.release_config)
        resolve_versions.validate_platform_config(
            self.platform_config, ROOT / ".github/workflows"
        )

    def test_real_config_tracks_all_current_supported_release_lines(self) -> None:
        actual = {
            entry["series"]: (entry["release_type"], entry["eol"])
            for entry in self.release_config["series"]
        }
        self.assertEqual(
            actual,
            {
                "6.2": ("extended", "2027-04-01"),
                "7.2": ("extended", "2029-12-01"),
                "7.4": ("extended", "2029-12-01"),
                "8.0": ("standard", "2026-12-01"),
                "8.2": ("extended", "2030-09-01"),
                "8.4": ("standard", None),
                "8.6": ("standard", None),
                "8.8": ("standard", None),
                "8.10": ("standard", None),
            },
        )
        self.assertEqual(self.release_config["policy"]["new_series_floor"], "8.10")
        self.assertEqual(
            {
                platform["id"]
                for platform in self.platform_config["platforms"]
                if platform["controller_enabled"]
            },
            {"linux-glibc2.28-x64", "linux-glibc2.28-arm64"},
        )

    def test_tracked_non_eol_version_is_publishable(self) -> None:
        entry = validate_publish_policy.validate_publish_policy(
            self.release_config, "7.4.11", dt.date(2026, 8, 20)
        )
        self.assertEqual(entry["series"], "7.4")

    def test_untracked_series_is_rejected(self) -> None:
        with self.assertRaisesRegex(validate_publish_policy.PolicyError, "tracked"):
            validate_publish_policy.validate_publish_policy(
                self.release_config, "9.0.0", dt.date(2026, 8, 20)
            )

    def test_publication_stops_after_eol(self) -> None:
        validate_publish_policy.validate_publish_policy(
            self.release_config, "8.0.6", dt.date(2026, 12, 1)
        )
        with self.assertRaisesRegex(validate_publish_policy.PolicyError, "EOL"):
            validate_publish_policy.validate_publish_policy(
                self.release_config, "8.0.6", dt.date(2026, 12, 2)
            )

    def test_linux_backend_contract_drift_is_rejected(self) -> None:
        config = copy.deepcopy(self.platform_config)
        config["platforms"][0]["variant"] = "linux-other"
        config["platforms"][0]["id"] = "linux-other-x64"
        with self.assertRaisesRegex(resolve_versions.PlanError, "build-linux"):
            resolve_versions.validate_platform_config(config)

    def test_missing_enabled_workflow_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(resolve_versions.PlanError, "missing workflow"):
                resolve_versions.validate_platform_config(
                    self.platform_config, Path(directory)
                )

    def test_publish_policy_requires_a_date_not_datetime(self) -> None:
        with self.assertRaisesRegex(validate_publish_policy.PolicyError, "date"):
            validate_publish_policy.validate_publish_policy(
                self.release_config,
                "7.4.11",
                dt.datetime(2026, 8, 20, 12, 0),
            )

    def test_resolver_workflow_uses_an_immutable_hashes_snapshot(self) -> None:
        workflow = (ROOT / ".github/workflows/resolve-versions.yml").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            workflow.index("Validate reviewed controller configuration"),
            workflow.index("Download an immutable official Redis hashes snapshot"),
        )
        self.assertIn("commits/${hashes_ref}", workflow)
        self.assertIn("contents/${hashes_path}?ref=${hashes_commit}", workflow)
        self.assertIn('--hashes-commit "$HASHES_COMMIT"', workflow)
        self.assertNotIn("curl ", workflow)

    def test_external_actions_are_pinned_to_full_commit_oids(self) -> None:
        for name in ("resolve-versions.yml", "validate.yml"):
            workflow = (ROOT / ".github/workflows" / name).read_text(
                encoding="utf-8"
            )
            uses = re.findall(r"^\s*uses:\s*([^#\s]+)", workflow, re.MULTILINE)
            self.assertTrue(uses, name)
            for action in uses:
                with self.subTest(workflow=name, action=action):
                    self.assertRegex(action, r"^[^@\s]+@[0-9a-f]{40}$")

    def test_validation_workflow_checks_all_release_scripts_and_real_configs(self) -> None:
        workflow = (ROOT / ".github/workflows/validate.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python3 -m py_compile scripts/release/*.py", workflow)
        self.assertIn("packaging/linux/patches/*.py", workflow)
        self.assertIn("python3 scripts/release/validate_configs.py", workflow)


if __name__ == "__main__":
    unittest.main()
