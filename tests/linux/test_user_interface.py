from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKENDS = ("linux", "musl", "macos")


class UserInterfaceTests(unittest.TestCase):
    def shell(self, code: str, *args: str, **environment: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "REDIS_INSTALL_LANG": "", "REDIS_UI_LANGUAGE": ""}
        env.update(environment)
        return subprocess.run(
            ["bash", "-eu", "-c", code, "ui-test", *args],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )

    def test_default_is_english_independent_of_system_locale(self) -> None:
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                common = ROOT / "packaging" / backend / "scripts/common.sh"
                result = self.shell(
                    'source "$1"; info "English" "中文"', str(common),
                    LANG="zh_CN.UTF-8", LC_ALL="", LC_MESSAGES="zh_CN.UTF-8",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "[redis-package] English\n")

    def test_chinese_and_paths_survive_c_locale_without_format_expansion(self) -> None:
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                common = ROOT / "packaging" / backend / "scripts/common.sh"
                result = self.shell(
                    'source "$1"; die "Failed: $2" "失败：$2"',
                    str(common), '目录 %s %n "quoted"', REDIS_INSTALL_LANG="zh_CN",
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, '[redis-package] 错误: 失败：目录 %s %n "quoted"\n')

    def test_language_parser_preserves_operation_arguments_and_rejects_bad_input(self) -> None:
        for backend in BACKENDS:
            for operation in ("install", "update", "uninstall"):
                entry = ROOT / "packaging" / backend / "scripts" / f"{operation}.sh"
                parser = entry.read_text(encoding="utf-8").split("bootstrap_fail()", 1)[0]
                suffix = '\nprintf "%s\\n" "$REDIS_UI_LANGUAGE" "$@"\n'
                cases = (
                    ((), 0, "en\n"),
                    (("--purge", "--lang", "zh"), 0, "zh\n--purge\n"),
                    (("--lang", "en", "--no-start"), 0, "en\n--no-start\n"),
                    (("--lang", "zh", "--help"), 0, "zh\n--help\n"),
                    (("--lang",), 2, ""),
                    (("--lang", "invalid"), 2, ""),
                )
                for args, code, output in cases:
                    with self.subTest(backend=backend, operation=operation, args=args):
                        result = self.shell(parser + suffix, *args)
                        self.assertEqual(result.returncode, code, result.stderr)
                        self.assertEqual(result.stdout, output)

    def test_explicit_language_overrides_legacy_environment(self) -> None:
        entry = ROOT / "packaging/linux/scripts/install.sh"
        parser = entry.read_text(encoding="utf-8").split("bootstrap_fail()", 1)[0]
        result = self.shell(
            parser + '\nprintf "%s" "$REDIS_UI_LANGUAGE"',
            "--lang", "en", REDIS_INSTALL_LANG="zh_CN",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "en")


if __name__ == "__main__":
    unittest.main()
