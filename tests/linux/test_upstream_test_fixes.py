from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PATCHER_PATH = ROOT / "packaging/linux/patches/apply_upstream_test_fixes.py"

SPEC = importlib.util.spec_from_file_location(
    "apply_upstream_test_fixes", PATCHER_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load {PATCHER_PATH}")
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)


def _result(returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, "", "")


class UpstreamTestFixTests(unittest.TestCase):
    def _source_tree(self, root: Path) -> tuple[Path, Path]:
        test_dir = root / "tests/unit"
        test_dir.mkdir(parents=True)
        targets = tuple(root / target for target in PATCHER.PATCH_TARGETS)
        for target in targets:
            target.write_text("official Redis test fixture\n", encoding="utf-8")
        return targets

    def test_redis_80_patch_is_applied_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root)
            with mock.patch.object(
                PATCHER,
                "_run_git_apply",
                side_effect=[_result(0), _result(0), _result(0)],
            ) as git_apply:
                status = PATCHER.apply_upstream_test_fixes("8.0.6", root)

            self.assertEqual(status, f"applied:{PATCHER.UPSTREAM_FIX_COMMIT}")
            self.assertEqual(
                git_apply.call_args_list,
                [
                    mock.call(root.resolve(), "--check"),
                    mock.call(root.resolve()),
                    mock.call(root.resolve(), "--reverse", "--check"),
                ],
            )

    def test_redis_80_already_patched_state_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root)
            with mock.patch.object(
                PATCHER,
                "_run_git_apply",
                side_effect=[_result(1), _result(0)],
            ):
                status = PATCHER.apply_upstream_test_fixes("8.0.6", root)

            self.assertEqual(status, f"present:{PATCHER.UPSTREAM_FIX_COMMIT}")

    def test_redis_80_unknown_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root)
            with mock.patch.object(
                PATCHER,
                "_run_git_apply",
                side_effect=[_result(1), _result(1)],
            ):
                with self.assertRaisesRegex(PATCHER.FixError, "reviewed upstream"):
                    PATCHER.apply_upstream_test_fixes("8.0.6", root)

    def test_redis_80_application_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root)
            with mock.patch.object(
                PATCHER,
                "_run_git_apply",
                side_effect=[_result(0), _result(1)],
            ):
                with self.assertRaisesRegex(PATCHER.FixError, "application failed"):
                    PATCHER.apply_upstream_test_fixes("8.0.6", root)

    def test_redis_80_verification_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root)
            with mock.patch.object(
                PATCHER,
                "_run_git_apply",
                side_effect=[_result(0), _result(0), _result(1)],
            ):
                with self.assertRaisesRegex(PATCHER.FixError, "verification failed"):
                    PATCHER.apply_upstream_test_fixes("8.0.6", root)

    def test_other_series_are_not_inspected_or_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._source_tree(root)
            original = tuple(target.read_bytes() for target in targets)
            with mock.patch.object(PATCHER, "_run_git_apply") as git_apply:
                status = PATCHER.apply_upstream_test_fixes("8.2.9", root)

            self.assertEqual(status, f"not-required:{PATCHER.UPSTREAM_FIX_COMMIT}")
            self.assertEqual(tuple(target.read_bytes() for target in targets), original)
            git_apply.assert_not_called()

    def test_noncanonical_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(PATCHER.FixError, "canonical"):
            PATCHER.apply_upstream_test_fixes("8.0", Path("unused"))

    def test_redis_80_symlink_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.tcl"
            outside.write_text("outside\n", encoding="utf-8")
            target_dir = root / "tests/unit"
            target_dir.mkdir(parents=True)
            (target_dir / "maxmemory.tcl").symlink_to(outside)

            with self.assertRaisesRegex(PATCHER.FixError, "regular non-symlink"):
                PATCHER.apply_upstream_test_fixes("8.0.6", root)

    def test_redis_80_symlink_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            outside.mkdir()
            for target in PATCHER.PATCH_TARGETS:
                (outside / target.name).write_text("outside\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "unit").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(PATCHER.FixError, "must not be symlinks"):
                PATCHER.apply_upstream_test_fixes("8.0.6", root)

    def test_redis_80_hard_link_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._source_tree(root)
            (root / "maxmemory-copy.tcl").hardlink_to(targets[0])

            with self.assertRaisesRegex(PATCHER.FixError, "multiple hard links"):
                PATCHER.apply_upstream_test_fixes("8.0.6", root)

    def test_vendored_patch_targets_only_reviewed_test_files(self) -> None:
        patch_text = PATCHER.PATCH_FILE.read_text(encoding="utf-8")
        headers = [
            line
            for line in patch_text.splitlines()
            if line.startswith("diff --git ")
        ]
        self.assertEqual(
            headers,
            [
                "diff --git a/tests/unit/maxmemory.tcl b/tests/unit/maxmemory.tcl",
                "diff --git a/tests/unit/memefficiency.tcl b/tests/unit/memefficiency.tcl",
            ],
        )
        self.assertNotIn("../", patch_text)
        self.assertNotIn("--- /", patch_text)
        self.assertNotIn("+++ /", patch_text)
        self.assertIn("set batch_size 10000", patch_text)
        self.assertIn("set batch_size 1000", patch_text)
        self.assertIn("if {($j + 1) % 500 == 0}", patch_text)


if __name__ == "__main__":
    unittest.main()
