"""Exercise the real POSIX config generators without installing a service."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = ("linux", "musl", "macos")
PORTABLE_DEFAULTS = {
    "bind": "127.0.0.1 -::1",
    "protected-mode": "yes",
    "port": "0",
    "daemonize": "no",
    "supervised": "no",
    "dir": "/usr/local/redis/data",
    "logfile": "/usr/local/redis/log/redis.log",
    "unixsocket": "/usr/local/redis/data/redis.sock",
    "unixsocketperm": "770",
}


def active_records(text: str, key: str) -> list[str]:
    pattern = rf"^[ \t]*(?:{key}|\"{key}\"|'{key}')[ \t]+(.*)$"
    return re.findall(pattern, text, re.MULTILINE | re.IGNORECASE)


class DefaultConfigTests(unittest.TestCase):
    def generate(
        self, platform: str, root: Path, text: str | None
    ) -> tuple[Path, subprocess.CompletedProcess]:
        source = root / "package [测试]" / "conf"
        source.mkdir(parents=True, exist_ok=True)
        if text is not None:
            (source / "redis.conf").write_text(text, encoding="utf-8")
        (source / "sentinel.conf").write_text("port 26379\n", encoding="utf-8")
        prefix = root / "installed"
        (prefix / "conf").mkdir(parents=True, exist_ok=True)
        destination = prefix / "conf/redis.conf"
        function = "install_default_configs" if platform == "linux" else "write_default_config"
        common = (ROOT / f"packaging/{platform}/scripts/common.sh").read_text(encoding="utf-8")
        match = re.search(rf"(?ms)^{function}\(\) \{{\n.*?^\}}$", common)
        self.assertIsNotNone(match, "Expected the actual generator function")
        # Only ownership operations are stubbed. Copying, awk and file replacement
        # really run, confined to this fixture; GNU mv -T is absent on macOS.
        script = r'''
set -euo pipefail
install() {
  local -a args=()
  while (( $# )); do
    case "$1" in
      -o|-g) shift 2 ;;
      *) args+=("$1"); shift ;;
    esac
  done
  command install "${args[@]}"
}
chown() { :; }
mv() {
  if [[ "$1" == -fT ]]; then shift; fi
  command mv "$@"
}
die() { printf '%s\n' "$*" >&2; exit 1; }
REDIS_INSTALL_PREFIX="$1"
REDIS_LOCAL_SOCKET="$1/data/redis.sock"
REDIS_GROUP=unused-test-group
'''
        script += match.group(0) + "\n"
        if platform == "linux":
            script += 'install_default_configs "$2"\n'
        else:
            script += 'write_default_config "$2/conf/redis.conf" "$1/conf/redis.conf"\n'
        result = subprocess.run(
            ["bash", "-c", script, "bash", str(prefix), str(source.parent)],
            env={**os.environ, "LC_ALL": "C"},
            capture_output=True, text=True, check=False,
        )
        if text is not None:
            self.assertEqual((source / "redis.conf").read_text(encoding="utf-8"), text)
        return destination, result

    def defaults(self, platform: str, root: Path) -> dict[str, str]:
        if platform == "linux":
            return {
                "port": "0", "unixsocket": f"{root}/installed/data/redis.sock",
                "unixsocketperm": "0770", "dir": f"{root}/installed/data",
            }
        return {**PORTABLE_DEFAULTS, "pidfile": (
            "/run/redis-unofficial.pid" if platform == "musl" else "/var/run/redis-unofficial.pid"
        )}

    def assert_defaults(self, platform: str, root: Path, text: str) -> None:
        for key, value in self.defaults(platform, root).items():
            self.assertEqual(active_records(text, key), [value], key)

    def test_defaults_replace_original_positions_without_a_trailing_override(self) -> None:
        for platform in PLATFORMS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = "# 网络设置\nport 6379\n# persistence\ndir ./\nlogfile \"\"\nsave 60 1\n"
                path, result = self.generate(platform, root, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.splitlines()[:3], ["# 网络设置", "port 0", "# persistence"])
                self.assert_defaults(platform, root, text)
                self.assertIn('save 60 1\n', text)
                self.assertEqual(path.stat().st_mode & 0o777, 0o640 if platform == "linux" else 0o644)

    def test_editing_original_port_is_not_overridden(self) -> None:
        for platform in PLATFORMS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, result = self.generate(platform, root, "port 6379\nbind 127.0.0.1\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = path.read_text(encoding="utf-8").splitlines()
                lines[0] = "port 16379"
                lines[1] = "bind 127.0.0.2"
                edited = "\n".join(lines) + "\n"
                self.assertEqual(active_records(edited, "port"), ["16379"])
                self.assertEqual(active_records(edited, "bind"), ["127.0.0.2"])

    def test_duplicates_case_tabs_and_quoted_names_are_deduplicated(self) -> None:
        for platform in PLATFORMS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lines = ["# port 9999"]
                for key in self.defaults(platform, root):
                    lines.extend((f"{key} old", f"\t{key.upper()}\tother", f'"{key}" third', f"'{key}' fourth"))
                path, result = self.generate(platform, root, "\n".join(lines))
                self.assertEqual(result.returncode, 0, result.stderr)
                text = path.read_text(encoding="utf-8")
                self.assert_defaults(platform, root, text)
                self.assertTrue(text.startswith("# port 9999\n"))

    def test_missing_defaults_empty_source_and_generation_are_deterministic(self) -> None:
        for platform in PLATFORMS:
            for source in ("", '# 用户配置\nrequirepass "secret #1"\nsave 60 1\nsave 300 10\ninclude extra.conf'):
                with self.subTest(platform=platform, source=source), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path, result = self.generate(platform, root, source)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    text = path.read_text(encoding="utf-8")
                    self.assert_defaults(platform, root, text)
                    self.assertTrue(text.startswith(source))
                    # Regenerate into a fresh destination; no install/update is run.
                    path.unlink()
                    path, result = self.generate(platform, root, text)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_existing_user_config_is_never_rewritten(self) -> None:
        for platform in PLATFORMS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "installed/conf/redis.conf"
                path.parent.mkdir(parents=True)
                original = 'port 16379\nrequirepass "keep me"\ninclude custom.conf\n'
                path.write_text(original, encoding="utf-8")
                _, result = self.generate(platform, root, "port 6379\n")
                if platform == "linux":
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_portable_missing_source_leaves_no_partial_config_or_temporary(self) -> None:
        for platform in ("musl", "macos"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                path, result = self.generate(platform, Path(directory), None)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(path.exists())
                self.assertEqual(list(path.parent.iterdir()), [])

    def test_portable_generators_refuse_existing_and_dangling_destination_links(self) -> None:
        for platform in ("musl", "macos"):
            for dangling in (False, True):
                with self.subTest(platform=platform, dangling=dangling), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path = root / "installed/conf/redis.conf"
                    path.parent.mkdir(parents=True)
                    target = root / "untouched.conf"
                    if not dangling:
                        target.write_text("port 16379\n", encoding="utf-8")
                    path.symlink_to(target)
                    _, result = self.generate(platform, root, "port 6379\n")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(path.readlink(), target)
                    if dangling:
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(target.read_text(encoding="utf-8"), "port 16379\n")
                    self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
