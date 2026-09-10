from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class JemallocPageSizeTests(unittest.TestCase):
    def test_build_argument_selection(self) -> None:
        for path, start, end, variants in (
            (
                "scripts/linux/build-redis.sh",
                "make_args=(BUILD_TLS=no)",
                "if [[ -x scripts/build.sh ]]; then",
                ("linux-glibc2.28", "linux-glibc2.17-legacy"),
            ),
            (
                "scripts/experimental/build-portable-posix.sh",
                "make_args=(BUILD_TLS=no)",
                'if [[ "$PACKAGE_VARIANT" == windows-msys2 ]]; then',
                ("linux-musl1.2", "macos15", "windows-msys2"),
            ),
        ):
            script = (ROOT / path).read_text(encoding="utf-8")
            selection = script.split(start, 1)[1].split(end, 1)[0]
            for variant in variants:
                for arch in ("arm64", "x64"):
                    with self.subTest(variant=variant, arch=arch):
                        result = subprocess.run(
                            [
                                "bash", "-eu", "-c",
                                'PACKAGE_VARIANT=$1; PACKAGE_ARCH=$2\n'
                                + start + selection
                                + '\nprintf "%s\\n" "${make_args[@]}"',
                                "test", variant, arch,
                            ],
                            check=True, capture_output=True, text=True,
                        )
                        expected = ["BUILD_TLS=no"]
                        if variant.startswith("linux-") and arch == "arm64":
                            expected.append("JEMALLOC_CONFIGURE_OPTS=--with-lg-page=16")
                        self.assertEqual(result.stdout.split(), expected)

    def test_all_build_and_install_calls_forward_selected_arguments(self) -> None:
        for path in (
            "scripts/linux/build-redis.sh",
            "scripts/experimental/build-portable-posix.sh",
        ):
            script = (ROOT / path).read_text(encoding="utf-8")
            calls = [line.strip() for line in script.splitlines()
                     if line.strip().startswith("make ")]
            self.assertGreaterEqual(len(calls), 2)
            for call in calls:
                with self.subTest(path=path, call=call):
                    self.assertIn('"${make_args[@]}"', call)


if __name__ == "__main__":
    unittest.main()
