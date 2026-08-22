from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCHER_PATH = (
    ROOT / "packaging/linux/patches/apply_upstream_test_fixes.py"
)

SPEC = importlib.util.spec_from_file_location(
    "apply_upstream_test_fixes", PATCHER_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load {PATCHER_PATH}")
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)


class UpstreamTestFixTests(unittest.TestCase):
    def _source_tree(self, root: Path, content: str) -> Path:
        test_dir = root / "tests/unit"
        test_dir.mkdir(parents=True)
        target = test_dir / "maxmemory.tcl"
        target.write_text(f"prefix\n{content}\nsuffix\n", encoding="utf-8")
        return target

    def test_redis_80_fix_is_exact_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = self._source_tree(root, PATCHER.UNPATCHED_MAXMEMORY_BLOCK)

            status = PATCHER.apply_upstream_test_fixes("8.0.6", root)
            self.assertEqual(status, f"applied:{PATCHER.UPSTREAM_FIX_COMMIT}")
            self.assertNotIn(
                PATCHER.UNPATCHED_MAXMEMORY_BLOCK,
                target.read_text(encoding="utf-8"),
            )
            self.assertIn(
                PATCHER.PATCHED_MAXMEMORY_BLOCK,
                target.read_text(encoding="utf-8"),
            )

            status = PATCHER.apply_upstream_test_fixes("8.0.6", root)
            self.assertEqual(status, f"present:{PATCHER.UPSTREAM_FIX_COMMIT}")

    def test_other_series_are_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = self._source_tree(root, PATCHER.UNPATCHED_MAXMEMORY_BLOCK)
            original = target.read_bytes()

            status = PATCHER.apply_upstream_test_fixes("8.2.9", root)

            self.assertEqual(status, f"not-required:{PATCHER.UPSTREAM_FIX_COMMIT}")
            self.assertEqual(target.read_bytes(), original)

    def test_redis_80_unknown_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root, "unexpected upstream contents")

            with self.assertRaisesRegex(PATCHER.FixError, "expected upstream block"):
                PATCHER.apply_upstream_test_fixes("8.0.6", root)

    def test_redis_80_symlink_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.tcl"
            outside.write_text(PATCHER.UNPATCHED_MAXMEMORY_BLOCK, encoding="utf-8")
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
            (outside / "maxmemory.tcl").write_text(
                PATCHER.UNPATCHED_MAXMEMORY_BLOCK, encoding="utf-8"
            )
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "unit").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(PATCHER.FixError, "must not be symlinks"):
                PATCHER.apply_upstream_test_fixes("8.0.6", root)


if __name__ == "__main__":
    unittest.main()
