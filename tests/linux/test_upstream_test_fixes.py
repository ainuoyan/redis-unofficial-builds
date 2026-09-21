from __future__ import annotations

import importlib.util
import shutil
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
    def _source_tree(
        self,
        root: Path,
        patch_targets: tuple[Path, ...] = PATCHER.UPSTREAM_PATCH_TARGETS,
    ) -> tuple[Path, ...]:
        targets = tuple(root / target for target in patch_targets)
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
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
                    mock.call(
                        root.resolve(), PATCHER.UPSTREAM_PATCH_FILE, "--check"
                    ),
                    mock.call(root.resolve(), PATCHER.UPSTREAM_PATCH_FILE),
                    mock.call(
                        root.resolve(),
                        PATCHER.UPSTREAM_PATCH_FILE,
                        "--reverse",
                        "--check",
                    ),
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
                with self.assertRaisesRegex(PATCHER.FixError, "reviewed patch"):
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

    def test_redis_810_patch_is_applied_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root, PATCHER.REDIS_810_PATCH_TARGETS)
            with mock.patch.object(
                PATCHER,
                "_run_git_apply",
                side_effect=[_result(0), _result(0), _result(0)],
            ) as git_apply:
                status = PATCHER.apply_upstream_test_fixes("8.10.1", root)

            self.assertEqual(status, f"applied:{PATCHER.REDIS_810_FIX_ID}")
            self.assertEqual(
                git_apply.call_args_list,
                [
                    mock.call(
                        root.resolve(), PATCHER.REDIS_810_PATCH_FILE, "--check"
                    ),
                    mock.call(root.resolve(), PATCHER.REDIS_810_PATCH_FILE),
                    mock.call(
                        root.resolve(),
                        PATCHER.REDIS_810_PATCH_FILE,
                        "--reverse",
                        "--check",
                    ),
                ],
            )

    def test_redis_829_patch_is_applied_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root, PATCHER.REDIS_829_PATCH_TARGETS)
            with mock.patch.object(
                PATCHER,
                "_run_git_apply",
                side_effect=[_result(0), _result(0), _result(0)],
            ) as git_apply:
                status = PATCHER.apply_upstream_test_fixes("8.2.9", root)

            self.assertEqual(status, f"applied:{PATCHER.REDIS_829_FIX_ID}")
            self.assertEqual(
                git_apply.call_args_list,
                [
                    mock.call(
                        root.resolve(), PATCHER.REDIS_829_PATCH_FILE, "--check"
                    ),
                    mock.call(root.resolve(), PATCHER.REDIS_829_PATCH_FILE),
                    mock.call(
                        root.resolve(),
                        PATCHER.REDIS_829_PATCH_FILE,
                        "--reverse",
                        "--check",
                    ),
                ],
            )

    def test_new_patch_releases_use_reviewed_source_states(self) -> None:
        cases = (
            (
                "8.2.10",
                PATCHER.REDIS_829_PATCH_FILE,
                PATCHER.REDIS_829_PATCH_TARGETS,
                PATCHER.REDIS_8210_FIX_ID,
            ),
            (
                "8.4.7",
                PATCHER.REDIS_847_PATCH_FILE,
                PATCHER.REDIS_847_PATCH_TARGETS,
                PATCHER.REDIS_847_FIX_ID,
            ),
            (
                "8.6.7",
                PATCHER.REDIS_867_PATCH_FILE,
                PATCHER.REDIS_867_PATCH_TARGETS,
                PATCHER.REDIS_867_FIX_ID,
            ),
            (
                "8.8.3",
                PATCHER.REDIS_883_PATCH_FILE,
                PATCHER.REDIS_883_PATCH_TARGETS,
                PATCHER.REDIS_883_FIX_ID,
            ),
            (
                "8.10.2",
                PATCHER.REDIS_810_PATCH_FILE,
                PATCHER.REDIS_810_PATCH_TARGETS,
                PATCHER.REDIS_8102_FIX_ID,
            ),
        )
        for version, patch_file, patch_targets, fix_id in cases:
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                self._source_tree(root, patch_targets)
                with mock.patch.object(
                    PATCHER,
                    "_run_git_apply",
                    side_effect=[_result(0), _result(0), _result(0)],
                ) as git_apply:
                    status = PATCHER.apply_upstream_test_fixes(version, root)

                self.assertEqual(status, f"applied:{fix_id}")
                self.assertEqual(
                    git_apply.call_args_list[0],
                    mock.call(root.resolve(), patch_file, "--check"),
                )

    def test_redis_829_unknown_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root, PATCHER.REDIS_829_PATCH_TARGETS)
            with mock.patch.object(
                PATCHER,
                "_run_git_apply",
                side_effect=[_result(1), _result(1)],
            ):
                with self.assertRaisesRegex(PATCHER.FixError, "reviewed patch"):
                    PATCHER.apply_upstream_test_fixes("8.2.9", root)

    def test_redis_882_patch_is_applied_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root, PATCHER.REDIS_882_PATCH_TARGETS)
            with mock.patch.object(
                PATCHER,
                "_run_git_apply",
                side_effect=[_result(0), _result(0), _result(0)],
            ) as git_apply:
                status = PATCHER.apply_upstream_test_fixes("8.8.2", root)

            self.assertEqual(status, f"applied:{PATCHER.REDIS_882_FIX_ID}")
            self.assertEqual(
                git_apply.call_args_list,
                [
                    mock.call(
                        root.resolve(), PATCHER.REDIS_882_PATCH_FILE, "--check"
                    ),
                    mock.call(root.resolve(), PATCHER.REDIS_882_PATCH_FILE),
                    mock.call(
                        root.resolve(),
                        PATCHER.REDIS_882_PATCH_FILE,
                        "--reverse",
                        "--check",
                    ),
                ],
            )

    def test_other_88_patch_releases_are_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._source_tree(root, PATCHER.REDIS_882_PATCH_TARGETS)
            original = tuple(target.read_bytes() for target in targets)
            with mock.patch.object(PATCHER, "_run_git_apply") as git_apply:
                status = PATCHER.apply_upstream_test_fixes("8.8.4", root)

            self.assertEqual(status, f"not-required:{PATCHER.UPSTREAM_FIX_COMMIT}")
            self.assertEqual(tuple(target.read_bytes() for target in targets), original)
            git_apply.assert_not_called()

    def test_other_82_patch_releases_are_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._source_tree(root, PATCHER.REDIS_829_PATCH_TARGETS)
            original = tuple(target.read_bytes() for target in targets)
            with mock.patch.object(PATCHER, "_run_git_apply") as git_apply:
                status = PATCHER.apply_upstream_test_fixes("8.2.11", root)

            self.assertEqual(status, f"not-required:{PATCHER.UPSTREAM_FIX_COMMIT}")
            self.assertEqual(tuple(target.read_bytes() for target in targets), original)
            git_apply.assert_not_called()

    def test_redis_810_unknown_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root, PATCHER.REDIS_810_PATCH_TARGETS)
            with mock.patch.object(
                PATCHER,
                "_run_git_apply",
                side_effect=[_result(1), _result(1)],
            ):
                with self.assertRaisesRegex(PATCHER.FixError, "reviewed patch"):
                    PATCHER.apply_upstream_test_fixes("8.10.1", root)

    def test_other_810_patch_releases_are_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._source_tree(root, PATCHER.REDIS_810_PATCH_TARGETS)
            original = tuple(target.read_bytes() for target in targets)
            with mock.patch.object(PATCHER, "_run_git_apply") as git_apply:
                status = PATCHER.apply_upstream_test_fixes("8.10.3", root)

            self.assertEqual(status, f"not-required:{PATCHER.UPSTREAM_FIX_COMMIT}")
            self.assertEqual(tuple(target.read_bytes() for target in targets), original)
            git_apply.assert_not_called()

    def test_other_series_are_not_inspected_or_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._source_tree(root)
            original = tuple(target.read_bytes() for target in targets)
            with mock.patch.object(PATCHER, "_run_git_apply") as git_apply:
                status = PATCHER.apply_upstream_test_fixes("7.4.11", root)

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
            for target in PATCHER.UPSTREAM_PATCH_TARGETS:
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
        patch_text = PATCHER.UPSTREAM_PATCH_FILE.read_text(encoding="utf-8")
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

    def test_redis_810_patch_only_changes_the_reviewed_tests(self) -> None:
        patch_text = PATCHER.REDIS_810_PATCH_FILE.read_text(encoding="utf-8")
        headers = [
            line
            for line in patch_text.splitlines()
            if line.startswith("diff --git ")
        ]
        self.assertEqual(
            headers,
            [
                "diff --git a/tests/unit/type/hash-field-expire.tcl "
                "b/tests/unit/type/hash-field-expire.tcl",
                "diff --git a/tests/unit/memefficiency.tcl b/tests/unit/memefficiency.tcl",
            ],
        )
        self.assertNotIn("../", patch_text)
        self.assertNotIn("--- /", patch_text)
        self.assertNotIn("+++ /", patch_text)
        self.assertIn("5000 + int(rand() * 1000)", patch_text)
        self.assertIn("r hpexpire same$h 6000", patch_text)
        self.assertIn("r hpexpire mix$h 6000", patch_text)
        self.assertIn("wait_for_condition 500 20", patch_text)

    def test_redis_829_patch_only_changes_reviewed_tests(self) -> None:
        patch_text = PATCHER.REDIS_829_PATCH_FILE.read_text(encoding="utf-8")
        headers = [
            line
            for line in patch_text.splitlines()
            if line.startswith("diff --git ")
        ]
        self.assertEqual(
            headers,
            [
                "diff --git a/tests/unit/latency-monitor.tcl "
                "b/tests/unit/latency-monitor.tcl",
                "diff --git a/tests/unit/memefficiency.tcl "
                "b/tests/unit/memefficiency.tcl",
            ],
        )
        self.assertNotIn("../", patch_text)
        self.assertNotIn("--- /", patch_text)
        self.assertNotIn("+++ /", patch_text)
        self.assertIn("set min 250", patch_text)
        self.assertIn("set max 950", patch_text)
        self.assertIn("$max >= 450 & $max <= 1150", patch_text)

    def test_redis_882_patch_only_changes_reviewed_tests(self) -> None:
        patch_text = PATCHER.REDIS_882_PATCH_FILE.read_text(encoding="utf-8")
        headers = [
            line
            for line in patch_text.splitlines()
            if line.startswith("diff --git ")
        ]
        self.assertEqual(
            headers,
            [
                "diff --git a/tests/unit/latency-monitor.tcl "
                "b/tests/unit/latency-monitor.tcl",
                "diff --git a/tests/unit/memefficiency.tcl "
                "b/tests/unit/memefficiency.tcl",
            ],
        )
        self.assertNotIn("../", patch_text)
        self.assertNotIn("--- /", patch_text)
        self.assertNotIn("+++ /", patch_text)
        self.assertIn("set min 250", patch_text)
        self.assertIn("set max 950", patch_text)
        self.assertIn("$max >= 450 & $max <= 1150", patch_text)

    def test_redis_847_patch_adds_only_reviewed_latency_and_defrag_fixes(self) -> None:
        patch_text = PATCHER.REDIS_847_PATCH_FILE.read_text(encoding="utf-8")
        self.assertEqual(
            [
                line
                for line in patch_text.splitlines()
                if line.startswith("diff --git ")
            ],
            [
                "diff --git a/tests/unit/latency-monitor.tcl "
                "b/tests/unit/latency-monitor.tcl",
                "diff --git a/tests/unit/memefficiency.tcl "
                "b/tests/unit/memefficiency.tcl",
            ],
        )
        self.assertNotIn("../", patch_text)
        self.assertIn("set max 950", patch_text)
        self.assertIn("$max >= 450 & $max <= 1150", patch_text)
        self.assertIn("512 * [$replica debug mallctl arenas.page]", patch_text)

    def test_redis_883_patch_backports_upstream_hotkeys_fix(self) -> None:
        patch_text = PATCHER.REDIS_883_PATCH_FILE.read_text(encoding="utf-8")
        self.assertEqual(
            [
                line
                for line in patch_text.splitlines()
                if line.startswith("diff --git ")
            ],
            [
                "diff --git a/tests/unit/hotkeys.tcl b/tests/unit/hotkeys.tcl",
                "diff --git a/tests/unit/latency-monitor.tcl "
                "b/tests/unit/latency-monitor.tcl",
                "diff --git a/tests/unit/memefficiency.tcl "
                "b/tests/unit/memefficiency.tcl",
            ],
        )
        self.assertNotIn("../", patch_text)
        self.assertIn("foreach sample_ratio {1 100 500}", patch_text)
        self.assertIn("foreach sample_ratio {1 100 500 1000}", patch_text)
        self.assertIn("set max 950", patch_text)
        self.assertIn("512 * [$replica debug mallctl arenas.page]", patch_text)

    def _redis_882_source(self, root: Path) -> tuple[Path, Path]:
        targets = self._source_tree(root, PATCHER.REDIS_882_PATCH_TARGETS)
        targets[0].write_text(
            "        set min 250\n"
            "        set max 450\n"
            "        foreach event $res {\n"
            "\n"
            "            if {!$::no_latency} {\n"
            "                assert {$max >= 450 & $max <= 650}\n"
            "                assert {$time == $last_time}\n",
            encoding="utf-8",
        )
        targets[1].write_text(
            "                $replica config set active-defrag-cycle-max 75\n"
            "                $replica config set active-defrag-ignore-bytes 2mb\n"
            "\n"
            "                # add a mass of string keys\n"
            "                set count 0\n"
            "                for {set j 0} {$j < 500000} {incr j} {\n"
            "\n        }\n"
            "    } {} {defrag external:skip tsan:skip debug_defrag:skip cluster}\n"
            "\n"
            '    start_cluster 1 0 {tags {"defrag external:skip tsan:skip '
            'debug_defrag:skip cluster needs:debug"} overrides {appendonly yes '
            'auto-aof-rewrite-percentage 0 save "" loglevel notice}} {\n',
            encoding="utf-8",
        )
        return targets

    def _redis_829_source(self, root: Path) -> tuple[Path, Path]:
        # The relevant bodies match 8.8.2, but 8.2.9 has older test tags.
        targets = self._redis_882_source(root)
        text = targets[1].read_text(encoding="utf-8")
        targets[1].write_text(
            text.replace("debug_defrag:skip ", "").replace(
                "cluster needs:debug", "cluster"
            ),
            encoding="utf-8",
        )
        return targets

    def _redis_867_source(self, root: Path) -> tuple[Path, Path]:
        targets = self._redis_882_source(root)
        targets[0].write_text(
            "        set min 250\n"
            "        set max 450\n"
            "        foreach event $res {\n"
            "            lassign $event time latency\n"
            "            if {!$::no_latency} {\n"
            "                assert {$latency >= $min && $latency <= $max}\n"
            "            }\n"
            "            incr min 100\n"
            "            incr max 100\n"
            "            set last_time $time\n"
            "        }\n"
            "\n"
            "        foreach event $res {\n"
            "            lassign $event eventname time latency max\n"
            "            assert {$eventname eq \"command\"}\n"
            "            if {!$::no_latency} {\n"
            "                assert {$max >= 450 & $max <= 650}\n"
            "                assert {$time == $last_time}\n"
            "            }\n",
            encoding="utf-8",
        )
        return targets

    def test_redis_829_real_patch_is_applied_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._redis_829_source(root)
            self.assertEqual(
                PATCHER.apply_upstream_test_fixes("8.2.9", root),
                f"applied:{PATCHER.REDIS_829_FIX_ID}",
            )
            patched = tuple(target.read_bytes() for target in targets)
            self.assertIn(b"set max 950", patched[0])
            self.assertIn(b"512 * [$replica debug mallctl arenas.page]", patched[1])
            self.assertIn(b"tsan:skip cluster needs:debug}", patched[1])
            self.assertEqual(
                PATCHER.apply_upstream_test_fixes("8.2.9", root),
                f"present:{PATCHER.REDIS_829_FIX_ID}",
            )
            self.assertEqual(tuple(target.read_bytes() for target in targets), patched)

    def test_redis_829_unknown_defrag_source_leaves_both_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._redis_829_source(root)
            targets[1].write_text("unreviewed defrag test\n", encoding="utf-8")
            original = tuple(target.read_bytes() for target in targets)
            with self.assertRaisesRegex(PATCHER.FixError, "reviewed patch"):
                PATCHER.apply_upstream_test_fixes("8.2.9", root)
            self.assertEqual(tuple(target.read_bytes() for target in targets), original)

    def test_defrag_patches_use_the_same_tested_page_size_allowance(self) -> None:
        additions = []
        for patch_file in (
            PATCHER.REDIS_829_PATCH_FILE,
            PATCHER.REDIS_846_PATCH_FILE,
            PATCHER.REDIS_866_PATCH_FILE,
            PATCHER.REDIS_867_PATCH_FILE,
            PATCHER.REDIS_882_PATCH_FILE,
            PATCHER.REDIS_810_PATCH_FILE,
        ):
            defrag = patch_file.read_text(encoding="utf-8").split(
                "diff --git a/tests/unit/memefficiency.tcl", 1
            )[1]
            threshold = defrag.split("@@", 2)[2].split("\n@@", 1)[0]
            additions.append([line for line in threshold.splitlines() if line.startswith("+")])
        self.assertTrue(additions[0])
        for addition in additions[1:]:
            self.assertEqual(additions[0], addition)

    def test_redis_866_patch_is_test_only_and_idempotent(self) -> None:
        patch_text = PATCHER.REDIS_866_PATCH_FILE.read_text(encoding="utf-8")
        self.assertEqual(
            [line for line in patch_text.splitlines() if line.startswith("diff --git ")],
            ["diff --git a/tests/unit/memefficiency.tcl b/tests/unit/memefficiency.tcl"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._redis_882_source(root)
            original_latency = targets[0].read_bytes()
            self.assertEqual(
                PATCHER.apply_upstream_test_fixes("8.6.6", root),
                f"applied:{PATCHER.REDIS_866_FIX_ID}",
            )
            patched = tuple(target.read_bytes() for target in targets)
            self.assertEqual(patched[0], original_latency)
            self.assertIn(b"512 * [$replica debug mallctl arenas.page]", patched[1])
            self.assertEqual(
                PATCHER.apply_upstream_test_fixes("8.6.6", root),
                f"present:{PATCHER.REDIS_866_FIX_ID}",
            )
            self.assertEqual(tuple(target.read_bytes() for target in targets), patched)

    def test_redis_866_unknown_source_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._source_tree(root, PATCHER.REDIS_866_PATCH_TARGETS)
            original = targets[0].read_bytes()
            with self.assertRaisesRegex(PATCHER.FixError, "reviewed patch"):
                PATCHER.apply_upstream_test_fixes("8.6.6", root)
            self.assertEqual(targets[0].read_bytes(), original)

    def test_redis_867_patch_adds_only_reviewed_latency_and_defrag_fixes(self) -> None:
        patch_text = PATCHER.REDIS_867_PATCH_FILE.read_text(encoding="utf-8")
        self.assertEqual(
            [
                line
                for line in patch_text.splitlines()
                if line.startswith("diff --git ")
            ],
            [
                "diff --git a/tests/unit/latency-monitor.tcl "
                "b/tests/unit/latency-monitor.tcl",
                "diff --git a/tests/unit/memefficiency.tcl "
                "b/tests/unit/memefficiency.tcl",
            ],
        )
        self.assertNotIn("../", patch_text)
        self.assertIn("set max 950", patch_text)
        self.assertIn("$max >= 450 & $max <= 1150", patch_text)
        self.assertIn(
            "$time >= $last_time && $time <= [clock seconds]", patch_text
        )
        self.assertIn(
            "512 * [$replica debug mallctl arenas.page]", patch_text
        )

    def test_redis_867_real_patch_is_applied_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._redis_867_source(root)
            self.assertEqual(
                PATCHER.apply_upstream_test_fixes("8.6.7", root),
                f"applied:{PATCHER.REDIS_867_FIX_ID}",
            )
            patched = tuple(target.read_bytes() for target in targets)
            self.assertIn(b"set max 950", patched[0])
            self.assertIn(
                b"$time >= $last_time && $time <= [clock seconds]",
                patched[0],
            )
            self.assertIn(
                b"512 * [$replica debug mallctl arenas.page]", patched[1]
            )
            self.assertEqual(
                PATCHER.apply_upstream_test_fixes("8.6.7", root),
                f"present:{PATCHER.REDIS_867_FIX_ID}",
            )
            self.assertEqual(tuple(target.read_bytes() for target in targets), patched)

    def test_redis_867_unknown_source_leaves_both_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._redis_867_source(root)
            targets[0].write_text("unreviewed latency test\n", encoding="utf-8")
            original = tuple(target.read_bytes() for target in targets)
            with self.assertRaisesRegex(PATCHER.FixError, "reviewed patch"):
                PATCHER.apply_upstream_test_fixes("8.6.7", root)
            self.assertEqual(tuple(target.read_bytes() for target in targets), original)

    def test_other_86_patch_releases_are_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(PATCHER, "_run_git_apply") as git_apply:
                for version in ("8.6.5", "8.6.8"):
                    self.assertEqual(
                        PATCHER.apply_upstream_test_fixes(version, Path(temp_dir)),
                        f"not-required:{PATCHER.UPSTREAM_FIX_COMMIT}",
                    )
                git_apply.assert_not_called()

    def test_redis_846_patch_is_test_only_and_idempotent(self) -> None:
        patch_text = PATCHER.REDIS_846_PATCH_FILE.read_text(encoding="utf-8")
        self.assertEqual(
            [line for line in patch_text.splitlines() if line.startswith("diff --git ")],
            ["diff --git a/tests/unit/memefficiency.tcl b/tests/unit/memefficiency.tcl"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._redis_882_source(root)
            targets[1].write_text(
                targets[1].read_text(encoding="utf-8").replace("cluster needs:debug", "cluster"),
                encoding="utf-8",
            )
            original_latency = targets[0].read_bytes()
            self.assertEqual(
                PATCHER.apply_upstream_test_fixes("8.4.6", root),
                f"applied:{PATCHER.REDIS_846_FIX_ID}",
            )
            patched = tuple(target.read_bytes() for target in targets)
            self.assertEqual(patched[0], original_latency)
            self.assertIn(b"512 * [$replica debug mallctl arenas.page]", patched[1])
            self.assertIn(b"debug_defrag:skip cluster needs:debug}", patched[1])
            self.assertEqual(
                PATCHER.apply_upstream_test_fixes("8.4.6", root),
                f"present:{PATCHER.REDIS_846_FIX_ID}",
            )
            self.assertEqual(tuple(target.read_bytes() for target in targets), patched)

    def test_redis_846_unknown_source_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._source_tree(root, PATCHER.REDIS_846_PATCH_TARGETS)
            original = targets[0].read_bytes()
            with self.assertRaisesRegex(PATCHER.FixError, "reviewed patch"):
                PATCHER.apply_upstream_test_fixes("8.4.6", root)
            self.assertEqual(targets[0].read_bytes(), original)

    def test_other_84_patch_releases_are_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with mock.patch.object(PATCHER, "_run_git_apply") as git_apply:
                for version in ("8.4.5", "8.4.8"):
                    self.assertEqual(
                        PATCHER.apply_upstream_test_fixes(version, root),
                        f"not-required:{PATCHER.UPSTREAM_FIX_COMMIT}",
                    )
                git_apply.assert_not_called()

    def test_redis_882_real_patch_is_applied_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._redis_882_source(root)
            self.assertEqual(
                PATCHER.apply_upstream_test_fixes("8.8.2", root),
                f"applied:{PATCHER.REDIS_882_FIX_ID}",
            )
            patched = tuple(target.read_bytes() for target in targets)
            self.assertIn(b"set max 950", patched[0])
            self.assertIn(b"512 * [$replica debug mallctl arenas.page]", patched[1])
            self.assertIn(b"debug_defrag:skip cluster needs:debug}", patched[1])
            self.assertEqual(
                PATCHER.apply_upstream_test_fixes("8.8.2", root),
                f"present:{PATCHER.REDIS_882_FIX_ID}",
            )
            self.assertEqual(tuple(target.read_bytes() for target in targets), patched)

    def test_redis_882_unknown_defrag_source_leaves_both_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            targets = self._redis_882_source(root)
            targets[1].write_text("unreviewed defrag test\n", encoding="utf-8")
            original = tuple(target.read_bytes() for target in targets)
            with self.assertRaisesRegex(PATCHER.FixError, "reviewed patch"):
                PATCHER.apply_upstream_test_fixes("8.8.2", root)
            self.assertEqual(tuple(target.read_bytes() for target in targets), original)

    @unittest.skipUnless(shutil.which("tclsh"), "Tcl is required for page-size checks")
    def test_redis_882_defrag_threshold_uses_allocator_pages(self) -> None:
        patch_text = PATCHER.REDIS_882_PATCH_FILE.read_text(encoding="utf-8")
        defrag_patch = patch_text.split("diff --git a/tests/unit/memefficiency.tcl", 1)[1]
        threshold_hunk = defrag_patch.split("@@", 2)[2].split("\n@@", 1)[0]
        added_lines = [
            line[1:] for line in threshold_hunk.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        for allocator, page_size, expected in (
            ("jemalloc-5.3.0", 4096, 2097152),
            ("jemalloc-5.3.0", 8192, 4194304),
            ("jemalloc-5.3.0", 16384, 8388608),
            ("jemalloc-5.3.0", 65536, 33554432),
            ("libc", 65536, 2097152),
        ):
            with self.subTest(allocator=allocator, page_size=page_size):
                harness = f"set allocator {allocator}\nset page_size {page_size}\n" + """
set replica replica
set threshold 2097152
proc s {name} { return $::allocator }
proc replica {args} {
    if {$args eq "debug mallctl arenas.page"} {
        if {![string match {*jemalloc*} $::allocator]} { error "unexpected mallctl" }
        return $::page_size
    }
    if {[lrange $args 0 2] ne "config set active-defrag-ignore-bytes"} {
        error "unexpected replica command"
    }
    set ::threshold [lindex $args 3]
}
if {[catch {
""" + "\n".join(added_lines) + """
} failure]} { puts stderr $failure; exit 1 }
puts $threshold
"""
                result = subprocess.run(
                    [shutil.which("tclsh")], input=harness, text=True,
                    capture_output=True, check=True, timeout=10,
                )
                self.assertEqual(result.stdout.strip(), str(expected))


if __name__ == "__main__":
    unittest.main()
