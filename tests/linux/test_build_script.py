from __future__ import annotations

import os
import platform
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "scripts/linux/build-redis.sh"


class BuildScriptTests(unittest.TestCase):
    def _environment(self, **overrides: str) -> dict[str, str]:
        machine = platform.machine()
        package_arch = "arm64" if machine == "aarch64" else "x64"
        environment = os.environ.copy()
        environment.update(
            {
                "REDIS_VERSION": "7.4.11",
                "REDIS_SOURCE_SHA256": "a" * 64,
                "REDIS_HASHES_COMMIT": "c" * 40,
                "EXPECTED_MACHINE_ARCH": machine,
                "PACKAGE_ARCH": package_arch,
                "BUILD_IMAGE": "example.invalid/build@sha256:" + "b" * 64,
                "PACKAGING_REVISION": "d" * 40,
                "GITHUB_SHA": "d" * 40,
                "GITHUB_ACTIONS": "false",
            }
        )
        environment.update(overrides)
        return environment

    def _run_validation(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(BUILD_SCRIPT)],
            env=self._environment(**overrides),
            check=False,
            capture_output=True,
            text=True,
        )

    def test_rejects_multiline_build_metadata_before_building(self) -> None:
        result = self._run_validation(BUILD_IMAGE="trusted\nForged field: value")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid single-line BUILD-INFO value", result.stderr)
        self.assertNotIn("download.redis.io", result.stderr)

    def test_rejects_noncanonical_source_date_epoch(self) -> None:
        for value in ("01", "1", "1700000000"):
            with self.subTest(value=value):
                result = self._run_validation(SOURCE_DATE_EPOCH=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid SOURCE_DATE_EPOCH", result.stderr)

    def test_rejects_oversized_glibc_numeric_components(self) -> None:
        result = self._run_validation(GLIBC_BASELINE="1000000.28")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid glibc baseline", result.stderr)

    def test_build_and_test_are_explicitly_unprivileged_and_tls_free(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("if (( EUID == 0 )); then", script)
        self.assertIn('make -j"$(nproc)" build redis BUILD_TLS=no', script)
        self.assertIn('make -j"$(nproc)" BUILD_TLS=no', script)
        self.assertIn("./runtest --clients 1 --timeout 1200", script)
        self.assertIn("Redis test runner must be a regular executable file", script)
        self.assertIn(
            'bash "$PROJECT_ROOT/scripts/run-test-with-one-retry.sh"', script
        )
        self.assertIn(
            'python3.11 "$UPSTREAM_TEST_FIX_HELPER"',
            script,
        )
        self.assertLess(
            script.index('make -j"$(nproc)" BUILD_TLS=no'),
            script.index('python3.11 "$UPSTREAM_TEST_FIX_HELPER"'),
        )
        self.assertLess(
            script.index('python3.11 "$UPSTREAM_TEST_FIX_HELPER"'),
            script.index('test_command=(./runtest --clients 1 --timeout 1200)'),
        )
        self.assertIn(
            'echo "Redis upstream test fix: $redis_test_fix_status"', script
        )
        self.assertIn('make PREFIX="$package_root" BUILD_TLS=no install', script)
        self.assertNotIn("--daemonize yes", script)
        self.assertIn("smoke_pid=$!", script)
        self.assertIn('wait "$smoke_pid"', script)

    def test_archive_is_normalized_and_outputs_are_not_overwritten(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('--mtime="@${SOURCE_DATE_EPOCH}"', script)
        self.assertIn("export SOURCE_DATE_EPOCH", script)
        self.assertIn("--pax-option=delete=atime,delete=ctime", script)
        self.assertIn('| gzip -n >"$temporary_package_path"', script)
        self.assertIn('[[ -e "$output_path" || -L "$output_path" ]]', script)
        self.assertIn('install -m 0644 "$temporary_checksum_path"', script)
        self.assertIn('install -m 0644 "$temporary_package_path"', script)

    def test_archive_ordering_is_compatible_with_gnu_tar_126(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--sort=name", script)
        self.assertIn("find redis -print0", script)
        self.assertIn("| LC_ALL=C sort -z", script)
        self.assertIn("--null", script)
        self.assertIn("--no-recursion", script)
        self.assertIn("-T -", script)

    def test_download_retries_are_compatible_with_rocky_linux_8_curl(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--retry 3 --retry-connrefused", script)
        self.assertNotIn("--retry-all-errors", script)

    def test_generated_readme_does_not_overstate_compatibility(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "validate the target distribution and kernel before production use",
            script,
        )
        self.assertIn("生产使用前仍须在目标发行版和内核上验证", script)
        self.assertIn('umask 022; exec tar --no-same-owner --no-same-permissions', script)
        self.assertIn('${PACKAGE_NAME}.tar.gz "\\$stage"', script)
        self.assertNotIn("-xzf PACKAGE.tar.gz", script)
        self.assertIn(
            "install supports --no-service, update supports it only with --adopt",
            script,
        )
        self.assertIn("uninstall supports either mode", script)
        self.assertIn("install 支持 --no-service", script)
        self.assertIn(
            "No-service mode installs the complete package layout without "
            "registering or requiring systemd",
            script,
        )
        self.assertIn(
            "stop every Redis process from /usr/local/redis manually before "
            "updating or uninstalling it",
            script,
        )
        self.assertIn("无服务模式会安装完整包布局，但不注册或要求 systemd", script)
        self.assertIn("更新或卸载前必须手工停止所有来自 /usr/local/redis", script)
        self.assertNotIn(
            "systemd is required by the included install, update, and uninstall scripts",
            script,
        )


if __name__ == "__main__":
    unittest.main()
