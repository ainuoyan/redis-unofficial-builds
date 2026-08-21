from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _gnu_userland_available() -> bool:
    probe = subprocess.run(
        ["bash", "-c", "stat -c %a / >/dev/null 2>&1 && realpath -e / >/dev/null 2>&1"],
        check=False,
    )
    return probe.returncode == 0


# The lifecycle scripts contractually run on a GNU/Linux userland; their
# deep-validation paths use GNU-only flags (stat -c, realpath -e) that BSD
# userlands such as macOS do not support.
GNU_USERLAND_AVAILABLE = _gnu_userland_available()
requires_gnu_userland = unittest.skipUnless(
    GNU_USERLAND_AVAILABLE,
    "requires a GNU/Linux userland (stat -c, realpath -e)",
)


class PackagingContractTests(unittest.TestCase):
    def test_all_shell_scripts_parse(self) -> None:
        scripts = [ROOT / "scripts/linux/build-redis.sh"]
        scripts.extend(sorted((ROOT / "packaging/linux/scripts").glob("*.sh")))
        for script in scripts:
            with self.subTest(script=script.relative_to(ROOT)):
                subprocess.run(["bash", "-n", str(script)], check=True)

    def test_symlink_metadata_checks_support_centos_7_stat(self) -> None:
        common = (ROOT / "packaging/linux/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(common, r"\bstat\s+-c\s+[^\n]*\s-h(?:\s|$)")

    def test_build_uses_private_staging_tree(self) -> None:
        script = (ROOT / "scripts/linux/build-redis.sh").read_text(encoding="utf-8")
        self.assertNotIn('rm -rf "$INSTALL_PREFIX"', script)
        self.assertNotIn("test_port=", script)
        self.assertIn('--unixsocket "$test_socket"', script)
        self.assertIn('make -j"$(nproc)" build redis', script)
        self.assertIn("core profile", script)
        self.assertIn('package_root="$staging_root/redis"', script)
        self.assertIn('cd "$staging_root"', script)
        self.assertIn('expected_file_arch=\'x86-64\'', script)
        self.assertIn('expected_file_arch=\'ARM aarch64\'', script)
        self.assertIn('Refusing to run an upstream source build', script)
        self.assertIn('elif [[ -f COPYING ]]', script)
        self.assertIn('install -m 0644 "$upstream_license_file"', script)

    def test_build_collects_bounded_deterministic_upstream_notices(self) -> None:
        script = (ROOT / "scripts/linux/build-redis.sh").read_text(encoding="utf-8")
        self.assertIn("collect_upstream_dependency_notices", script)
        self.assertIn('find -P "$deps_root" -mindepth 1 -print0', script)
        self.assertIn("LC_ALL=C sort -z", script)
        self.assertIn("MAX_DEPENDENCY_NOTICE_FILE_BYTES", script)
        self.assertIn("MAX_DEPENDENCY_NOTICE_SOURCE_BYTES", script)
        self.assertIn("UPSTREAM-DEPENDENCY-NOTICES.txt", script)
        self.assertIn("UPSTREAM_DEPENDENCY_NOTICES_FORMAT=1", script)
        self.assertIn("REDISCONTRIBUTIONS.txt", script)
        self.assertIn("contributor_license_required", script)
        self.assertIn("UPSTREAM-CONTRIBUTOR-LICENSE.txt", script)

    def test_release_assets_are_immutable_and_build_image_is_pinned(self) -> None:
        workflow = (ROOT / ".github/workflows/build-linux.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--clobber", workflow)
        self.assertIn(
            "Existing Redis Release is incomplete or uses a legacy asset contract.",
            workflow,
        )
        self.assertIn("release == null and .data.repository.ref == null", workflow)
        self.assertGreaterEqual(
            len(
                re.findall(
                    r"rockylinux/rockylinux:8@sha256:[0-9a-f]{64}", workflow
                )
            ),
            2,
        )

    @requires_gnu_userland
    def test_format_two_package_metadata_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_root = Path(directory) / "redis"
            self._write_package_fixture(package_root, package_format="2")
            result = self._validate_package_root(package_root)
            self.assertEqual(result.returncode, 0, result.stderr)

    @requires_gnu_userland
    def test_upstream_notice_files_are_required_size_bounded_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_root = Path(directory) / "redis"
            self._write_package_fixture(package_root, package_format="2")
            (package_root / "UPSTREAM-DEPENDENCY-NOTICES.txt").unlink()
            result = self._validate_package_root(package_root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("UPSTREAM-DEPENDENCY-NOTICES.txt", result.stderr)

        with tempfile.TemporaryDirectory() as directory:
            package_root = Path(directory) / "redis"
            self._write_package_fixture(package_root, package_format="2")
            with (package_root / "UPSTREAM-DEPENDENCY-NOTICES.txt").open(
                "ab"
            ) as handle:
                handle.write(b"tampered")
            result = self._validate_package_root(package_root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("UPSTREAM-DEPENDENCY-NOTICES.txt", result.stderr)

        with tempfile.TemporaryDirectory() as directory:
            package_root = Path(directory) / "redis"
            self._write_package_fixture(package_root, package_format="2")
            (package_root / "UPSTREAM-CONTRIBUTOR-LICENSE.txt").unlink()
            result = self._validate_package_root(package_root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("UPSTREAM-CONTRIBUTOR-LICENSE.txt", result.stderr)

    @requires_gnu_userland
    def test_pre_74_package_may_record_contributor_notice_absence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_root = Path(directory) / "redis"
            self._write_package_fixture(package_root, package_format="2")
            contributor = package_root / "UPSTREAM-CONTRIBUTOR-LICENSE.txt"
            contributor.unlink()
            notices = package_root / "UPSTREAM-DEPENDENCY-NOTICES.txt"
            notice_bytes = notices.read_bytes().replace(b"7.4.11", b"7.2.16")
            notices.write_bytes(notice_bytes)
            package_info = package_root / "PACKAGE-INFO"
            metadata = package_info.read_text(encoding="utf-8")
            metadata = metadata.replace("REDIS_VERSION=7.4.11", "REDIS_VERSION=7.2.16")
            metadata = metadata.replace("REDIS_SERIES=7.4", "REDIS_SERIES=7.2")
            metadata = re.sub(
                r"UPSTREAM_CONTRIBUTOR_LICENSE_SHA256=[^\n]+",
                "UPSTREAM_CONTRIBUTOR_LICENSE_SHA256=absent",
                metadata,
            )
            metadata = re.sub(
                r"UPSTREAM_DEPENDENCY_NOTICES_SHA256=[^\n]+",
                "UPSTREAM_DEPENDENCY_NOTICES_SHA256="
                + hashlib.sha256(notice_bytes).hexdigest(),
                metadata,
            )
            package_info.write_text(metadata, encoding="utf-8")
            result = self._validate_package_root(package_root)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_or_incomplete_package_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_root = Path(directory) / "redis"
            self._write_package_fixture(package_root, package_format="1")
            result = self._validate_package_root(package_root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PACKAGE-INFO", result.stderr)

    def test_duplicate_package_metadata_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_root = Path(directory) / "redis"
            self._write_package_fixture(package_root, package_format="2")
            with (package_root / "PACKAGE-INFO").open("a", encoding="utf-8") as handle:
                handle.write("PACKAGE_ARCH=x64\n")
            result = self._validate_package_root(package_root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PACKAGE-INFO", result.stderr)

    def test_conflicting_systemd_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_root = Path(directory) / "redis"
            self._write_package_fixture(package_root, package_format="2")
            with (package_root / "systemd/redis.service").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(" User=root\n")
            result = self._validate_package_root(package_root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("redis.service", result.stderr)

    def test_build_rejects_path_like_version_before_network_or_filesystem_work(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "REDIS_VERSION": "../../unsafe",
                "REDIS_SOURCE_SHA256": "a" * 64,
                "REDIS_HASHES_COMMIT": "b" * 40,
                "EXPECTED_MACHINE_ARCH": "x86_64",
                "PACKAGE_ARCH": "x64",
                "PACKAGING_REVISION": "c" * 40,
                "GITHUB_SHA": "c" * 40,
                "GITHUB_ACTIONS": "false",
            }
        )
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/linux/build-redis.sh")],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid Redis version", result.stderr)

    @requires_gnu_userland
    def test_readiness_check_accepts_pong_and_authentication_challenge(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        for response, exit_code in (("PONG", 0), ("NOAUTH Authentication required.", 1)):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                redis_root = Path(directory) / "redis"
                (redis_root / "bin").mkdir(parents=True)
                (redis_root / "conf").mkdir()
                (redis_root / "conf/redis.conf").write_text(
                    "bind 127.0.0.1\nport 6380\n", encoding="utf-8"
                )
                cli = redis_root / "bin/redis-cli"
                cli.write_text(
                    f"#!/bin/sh\nprintf '%s\\n' '{response}'\nexit {exit_code}\n",
                    encoding="utf-8",
                )
                cli.chmod(0o755)
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; run_as_redis_user() { "$@"; }; '
                        'resolve_trusted_config_file() { realpath -e -- "$1"; }; '
                        'redis_protocol_ready "$2"',
                        "bash",
                        str(common),
                        str(redis_root),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    @requires_gnu_userland
    def test_readiness_resolves_relative_unix_socket_from_service_workdir(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        with tempfile.TemporaryDirectory() as directory:
            redis_root = Path(directory) / "redis"
            (redis_root / "bin").mkdir(parents=True)
            (redis_root / "conf").mkdir()
            (redis_root / "conf/redis.conf").write_text(
                "port 0\nunixsocket data/relative.sock\n", encoding="utf-8"
            )
            expected_socket = redis_root / "data/relative.sock"
            cli = redis_root / "bin/redis-cli"
            cli.write_text(
                "#!/bin/sh\n"
                f"[ \"$1\" = \"-s\" ] && [ \"$2\" = \"{expected_socket}\" ] "
                "|| exit 2\n"
                "printf 'PONG\\n'\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; run_as_redis_user() { "$@"; }; '
                    'resolve_trusted_config_file() { realpath -e -- "$1"; }; '
                    'redis_protocol_ready "$2"',
                    "bash",
                    str(common),
                    str(redis_root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    @requires_gnu_userland
    def test_readiness_uses_final_endpoint_from_ordered_includes(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        with tempfile.TemporaryDirectory() as directory:
            redis_root = Path(directory) / "redis"
            (redis_root / "bin").mkdir(parents=True)
            (redis_root / "conf").mkdir()
            included = redis_root / "conf/endpoint.conf"
            final_socket = redis_root / "data/final.sock"
            included.write_text(
                f"unixsocket {final_socket}\nport 0\nbind 127.0.0.2\n",
                encoding="utf-8",
            )
            (redis_root / "conf/redis.conf").write_text(
                "unixsocket /wrong/main.sock\n"
                "port 6399\n"
                "bind 127.0.0.1\n"
                f"include {included}\n",
                encoding="utf-8",
            )
            cli = redis_root / "bin/redis-cli"
            cli.write_text(
                "#!/bin/sh\n"
                f'[ "$1" = "-s" ] && [ "$2" = "{final_socket}" ] '
                "|| exit 2\n"
                "printf 'PONG\\n'\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; run_as_redis_user() { "$@"; }; '
                    'resolve_trusted_config_file() { realpath -e -- "$1"; }; '
                    'redis_protocol_ready "$2"',
                    "bash",
                    str(common),
                    str(redis_root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    @requires_gnu_userland
    def test_readiness_only_probes_the_final_included_tcp_endpoint(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        with tempfile.TemporaryDirectory() as directory:
            redis_root = Path(directory) / "redis"
            (redis_root / "bin").mkdir(parents=True)
            (redis_root / "conf").mkdir()
            attempt_log = redis_root / "attempts"
            included = redis_root / "conf/endpoint.conf"
            included.write_text(
                'unixsocket ""\nport 6401\nbind 127.0.0.2\n',
                encoding="utf-8",
            )
            (redis_root / "conf/redis.conf").write_text(
                "port 6399\n"
                "bind 127.0.0.1\n"
                f"include {included}\n",
                encoding="utf-8",
            )
            cli = redis_root / "bin/redis-cli"
            cli.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >>'{attempt_log}'\n"
                '[ "$1" = "-h" ] && [ "$2" = "127.0.0.2" ] '
                '&& [ "$3" = "-p" ] && [ "$4" = "6401" ] || exit 2\n'
                "printf 'PONG\\n'\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; run_as_redis_user() { "$@"; }; '
                    'resolve_trusted_config_file() { realpath -e -- "$1"; }; '
                    'redis_protocol_ready "$2"',
                    "bash",
                    str(common),
                    str(redis_root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                attempt_log.read_text(encoding="utf-8").splitlines(),
                ["-h 127.0.0.2 -p 6401 PING"],
            )

    @requires_gnu_userland
    def test_readiness_records_loading_response_for_diagnostics(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        with tempfile.TemporaryDirectory() as directory:
            redis_root = Path(directory) / "redis"
            (redis_root / "bin").mkdir(parents=True)
            (redis_root / "conf").mkdir()
            (redis_root / "conf/redis.conf").write_text(
                "bind 127.0.0.1\nport 6380\n", encoding="utf-8"
            )
            cli = redis_root / "bin/redis-cli"
            cli.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' "
                "'-LOADING Redis is loading the dataset in memory'\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; run_as_redis_user() { "$@"; }; '
                    'resolve_trusted_config_file() { realpath -e -- "$1"; }; '
                    "loading_seen=false; "
                    'if redis_protocol_ready "$2" loading_seen; then exit 3; fi; '
                    '[[ "$loading_seen" == true ]]',
                    "bash",
                    str(common),
                    str(redis_root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_ready_timeout_seconds_validates_the_budget(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        for value, expected in (
            (None, "30"),
            ("1", "1"),
            ("3600", "3600"),
            ("99999", "99999"),
        ):
            environment = os.environ.copy()
            if value is None:
                environment.pop("REDIS_READY_TIMEOUT", None)
            else:
                environment["REDIS_READY_TIMEOUT"] = value
            with self.subTest(value=value):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; ready_timeout_seconds',
                        "bash",
                        str(common),
                    ],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
        for value in ("0", "-5", "100000", "10.5", "abc"):
            with self.subTest(value=value):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; ready_timeout_seconds',
                        "bash",
                        str(common),
                    ],
                    env={**os.environ, "REDIS_READY_TIMEOUT": value},
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("REDIS_READY_TIMEOUT", result.stderr)

    def test_readiness_budget_is_wall_clock_seconds(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        with tempfile.TemporaryDirectory() as directory:
            tool_dir = Path(directory)
            (tool_dir / "timeout").write_text(
                "#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8"
            )
            (tool_dir / "systemctl").write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                "  is-active) exit 0 ;;\n"
                "  show) printf '123\\n'; exit 0 ;;\n"
                "esac\n"
                "exit 1\n",
                encoding="utf-8",
            )
            (tool_dir / "readlink").write_text(
                "#!/bin/sh\nprintf '/usr/local/redis/bin/redis-server\\n'\n",
                encoding="utf-8",
            )
            for tool in ("timeout", "systemctl", "readlink"):
                (tool_dir / tool).chmod(0o755)
            started = time.monotonic()
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; PATH="$2:$PATH"; '
                    "redis_protocol_ready() { sleep 2; return 1; }; "
                    "wait_for_service",
                    "bash",
                    str(common),
                    directory,
                ],
                env={**os.environ, "REDIS_READY_TIMEOUT": "2"},
                check=False,
                capture_output=True,
                text=True,
            )
            elapsed = time.monotonic() - started
            self.assertNotEqual(result.returncode, 0)
            self.assertLess(elapsed, 4.0, result.stderr)
            self.assertIn("PING", result.stderr)

    def test_privileged_entrypoints_pin_environment_before_sourcing(self) -> None:
        for name in ("install.sh", "update.sh", "uninstall.sh"):
            with self.subTest(script=name):
                script = (
                    ROOT / "packaging/linux/scripts" / name
                ).read_text(encoding="utf-8")
                self.assertTrue(script.startswith("#!/bin/bash -p\n"))
                self.assertLess(script.index("PATH=/usr/sbin:/usr/bin:/sbin:/bin"),
                                script.index('source "$SCRIPT_DIR/common.sh"'))
                self.assertLess(script.index("bootstrap_validate_path_chain"),
                                script.index('source "$SCRIPT_DIR/common.sh"'))
                self.assertIn("unset CDPATH ENV BASH_ENV", script)
                self.assertIn("bootstrap_validate_no_extended_acl", script)
                if name != "uninstall.sh":
                    self.assertLess(
                        script.index('validate_package_root_security "$PACKAGE_ROOT"'),
                        script.index('validate_package_root "$PACKAGE_ROOT"'),
                    )

    def test_ui_language_is_selected_before_machine_locale_is_pinned(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        common_text = common.read_text(encoding="utf-8")
        self.assertLess(
            common_text.index('readonly REDIS_UI_LANGUAGE="$(detect_ui_language)"'),
            common_text.index("LC_ALL=C"),
        )
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; printf "%s:%s:%s\\n" '
                '"$REDIS_UI_LANGUAGE" "$LC_ALL" "$LANG"',
                "bash",
                str(common),
            ],
            env={**os.environ, "REDIS_INSTALL_LANG": "zh_CN"},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "zh:C:C")

    def test_no_service_messages_describe_the_full_layout_and_manual_start(self) -> None:
        common = (ROOT / "packaging/linux/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        install = (ROOT / "packaging/linux/scripts/install.sh").read_text(
            encoding="utf-8"
        )
        update = (ROOT / "packaging/linux/scripts/update.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "complete package layout without registering or requiring systemd",
            common,
        )
        self.assertIn("安装完整包布局但不注册或要求 systemd", common)
        self.assertIn("Install the complete package layout", install)
        self.assertIn("安装完整包布局", install)
        self.assertNotIn("binary-only installation", common)
        self.assertNotIn("仅安装程序", common + install)
        self.assertIn("the updater did not start Redis", update)
        self.assertIn("更新器不会启动 Redis", update)
        self.assertNotIn("restart any running Redis process", update)

    @requires_gnu_userland
    def test_user_controlled_package_tree_is_rejected_before_help(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_root = Path(directory) / "redis"
            scripts = package_root / "scripts"
            scripts.mkdir(parents=True)
            for name in ("install.sh", "common.sh"):
                source = ROOT / "packaging/linux/scripts" / name
                destination = scripts / name
                destination.write_bytes(source.read_bytes())
                destination.chmod(0o755)
            package_root.chmod(0o777)
            result = subprocess.run(
                [str(scripts / "install.sh"), "--help"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("root-controlled", result.stderr)

    def test_state_and_update_transaction_are_fail_closed(self) -> None:
        common = (ROOT / "packaging/linux/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        update = (ROOT / "packaging/linux/scripts/update.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('== "0:0:600:1"', common)
        self.assertIn("++seen[$1] != 1", common)
        self.assertIn('mktemp "$REDIS_INSTALL_PREFIX/.redis-package-state.tmp.XXXXXX"', common)
        self.assertIn("trap 'rollback_update $?' EXIT", update)
        self.assertIn("trap 'rollback_update 130' INT", update)
        self.assertIn("for restore_item in bin conf scripts systemd", update)
        self.assertIn("--allow-downgrade", update)
        self.assertIn(
            'redis_version_is_at_least "$new_version" "$old_version"', update
        )
        transaction = update[update.index("trap 'rollback_update $?' ERR") :]
        self.assertLess(
            transaction.index("if ! assert_no_live_install_redis_server"),
            transaction.index('staged_bin_dir="$(mktemp'),
        )
        self.assertLess(
            transaction.index("rollback_needed=true"),
            transaction.index('staged_bin_dir="$(mktemp'),
        )
        self.assertLess(
            transaction.index("rollback_needed=true"),
            transaction.index('systemctl stop "$REDIS_SERVICE_NAME"'),
        )

        common_path = ROOT / "packaging/linux/scripts/common.sh"
        for actual, required, expected_success in (
            ("7.4.11", "7.4.11", True),
            ("7.4.12", "7.4.11", True),
            ("8.0.0", "7.4.99", True),
            ("7.4.10", "7.4.11", False),
            ("7.2.99", "7.4.0", False),
            ("1000000.0.0", "7.4.0", False),
        ):
            with self.subTest(actual=actual, required=required):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; redis_version_is_at_least "$2" "$3"',
                        "bash",
                        str(common_path),
                        actual,
                        required,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, expected_success)

        for actual, required, expected_success in (
            ("2.28", "2.17", True),
            ("2.17", "2.28", False),
            ("1000000.0", "2.28", False),
            ("2.1000000", "2.28", False),
        ):
            with self.subTest(glibc_actual=actual, glibc_required=required):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; version_is_at_least "$2" "$3"',
                        "bash",
                        str(common_path),
                        actual,
                        required,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, expected_success)

    def test_retained_state_reinstall_cannot_bypass_downgrade_policy(self) -> None:
        install = (ROOT / "packaging/linux/scripts/install.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--allow-downgrade", install)
        self.assertIn('retained_version="$(state_value REDIS_VERSION)"', install)
        self.assertIn(
            'redis_version_is_at_least "$package_version" "$retained_version"',
            install,
        )
        self.assertIn(
            'rm -f "$REDIS_INSTALL_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt"',
            install,
        )

    def test_lifecycle_lock_is_root_only_and_not_symlink_following(self) -> None:
        common = (ROOT / "packaging/linux/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        lock_function = common[
            common.index("acquire_lifecycle_lock()") :
            common.index("metadata_value()")
        ]
        self.assertIn("umask 077", common)
        self.assertLess(
            lock_function.index(
                'assert_root_owned_regular_file "$REDIS_LIFECYCLE_LOCK"'
            ),
            lock_function.index('exec 9>"$REDIS_LIFECYCLE_LOCK"'),
        )
        self.assertIn('chmod 0600 "$REDIS_LIFECYCLE_LOCK"', lock_function)

    def test_lifecycle_rollbacks_cover_files_accounts_and_service_mutations(self) -> None:
        install = (ROOT / "packaging/linux/scripts/install.sh").read_text(
            encoding="utf-8"
        )
        update = (ROOT / "packaging/linux/scripts/update.sh").read_text(
            encoding="utf-8"
        )
        uninstall = (ROOT / "packaging/linux/scripts/uninstall.sh").read_text(
            encoding="utf-8"
        )
        for script, rollback_name in (
            (install, "rollback_install"),
            (update, "rollback_update"),
        ):
            with self.subTest(rollback=rollback_name):
                self.assertIn("for restore_item in bin conf scripts systemd", script)
                self.assertIn("THIRD_PARTY_NOTICES.md", script)
                self.assertIn("UPSTREAM-CONTRIBUTOR-LICENSE.txt", script)
                self.assertIn("UPSTREAM-DEPENDENCY-NOTICES.txt", script)
                self.assertIn("service_start_attempted", script)
                self.assertIn("service_enablement_mutated", script)
                self.assertIn("unit_override_existed", script)
                self.assertIn('userdel "$REDIS_USER"', script)
                self.assertIn('groupdel "$REDIS_GROUP"', script)
                self.assertIn(f"trap '{rollback_name} $?' EXIT", script)
        self.assertLess(
            uninstall.index("validate_effective_service_contract"),
            uninstall.index('systemctl stop "$REDIS_SERVICE_NAME"'),
        )

    def test_upstream_notices_are_tracked_by_lifecycle_state_and_uninstall(self) -> None:
        common = (ROOT / "packaging/linux/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("UPSTREAM_CONTRIBUTOR_LICENSE_SHA256", common)
        self.assertIn("UPSTREAM_DEPENDENCY_NOTICES_SHA256", common)
        self.assertIn("STATE_FORMAT=3", common)
        self.assertIn("REDIS_HOME", common)
        self.assertIn("REDIS_SHELL", common)
        self.assertIn('case "$state_format" in', common)
        for name in ("install.sh", "update.sh", "uninstall.sh"):
            script = (ROOT / "packaging/linux/scripts" / name).read_text(
                encoding="utf-8"
            )
            with self.subTest(script=name):
                self.assertIn("UPSTREAM-CONTRIBUTOR-LICENSE.txt", script)
                self.assertIn("UPSTREAM-DEPENDENCY-NOTICES.txt", script)

    def test_state_v3_account_provenance_is_exact_and_legacy_state_is_conservative(self) -> None:
        common = (ROOT / "packaging/linux/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        loader = common[
            common.index("load_account_ownership_from_state()") :
            common.index("validate_redis_account_security()")
        ]
        self.assertIn('if [[ "$state_format" == 3 ]]', loader)
        self.assertIn("redis_account_matches_recorded_identity", loader)
        self.assertLess(
            loader.index('if [[ "$state_format" == 3 ]]'),
            loader.index("ACCOUNT_CREATED_USER=true"),
        )
        self.assertLess(
            loader.index('if [[ "$state_format" == 3 ]]'),
            loader.index("ACCOUNT_CREATED_GROUP=true"),
        )

    def test_force_replacement_enables_disabled_foreign_unit_and_rolls_back(self) -> None:
        for name in ("install.sh", "update.sh"):
            script = (ROOT / "packaging/linux/scripts" / name).read_text(
                encoding="utf-8"
            )
            with self.subTest(script=name):
                self.assertIn("service_was_disabled=false", script)
                self.assertIn(
                    'disabled|static|indirect|generated|transient) '
                    'service_was_disabled=true',
                    script,
                )
                self.assertIn(
                    '"$service_was_foreign" == true && "$service_was_disabled" == true',
                    script,
                )
                self.assertIn("service_enablement_mutated=true", script)
                self.assertIn('systemctl disable "$REDIS_SERVICE_NAME"', script)

    def test_package_binaries_are_inspected_without_root_privileges(self) -> None:
        common = (ROOT / "packaging/linux/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        update = (ROOT / "packaging/linux/scripts/update.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--reuid 65534 --regid 65534", common)
        self.assertIn("--no-new-privs", common)
        self.assertIn("run_unprivileged", common)
        self.assertIn("--reuid 65534 --regid 65534", common)
        self.assertIn("--bounding-set=-all", common)
        self.assertIn("--inh-caps=-all", common)
        self.assertIn("--ambient-caps=-all", common)
        self.assertIn("env -i", common)
        self.assertIn("HOME=/nonexistent", common)
        self.assertIn("HOME=/usr/local/redis/data", common)
        self.assertNotIn('uid="${SUDO_UID', common)
        self.assertNotIn('redis_version_from_binary "$REDIS_INSTALL_PREFIX', update)
        self.assertIn("assert_no_extended_acl", common)

    @unittest.skipUnless(os.geteuid() == 0, "setpriv UID transition requires root")
    def test_unprivileged_preflight_drops_secrets_and_preserves_arguments(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        setpriv_probe = subprocess.run(
            ["setpriv", "--reuid", "65534", "--regid", "65534", "--clear-groups", "--", "true"],
            check=False,
            capture_output=True,
            text=True,
        )
        if setpriv_probe.returncode != 0:
            self.skipTest("sandbox does not map UID/GID 65534")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            probe = root / "probe"
            probe.write_text(
                "#!/bin/sh\n"
                "printf '%s|%s|%s|%s|%s\\n' "
                '"${SECRET-unset}" "$1" "$2" "$(id -u)" "$HOME"\n',
                encoding="utf-8",
            )
            probe.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; run_unprivileged "$2" alpha "two words"',
                    "bash",
                    str(common),
                    str(probe),
                ],
                env={
                    **os.environ,
                    "SECRET": "must-not-cross-boundary",
                    "SUDO_UID": "1234",
                    "SUDO_GID": "1234",
                },
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(), "unset|alpha|two words|65534|/nonexistent"
            )

    def test_existing_service_account_must_be_nonlogin_and_unprivileged(self) -> None:
        common_path = ROOT / "packaging/linux/scripts/common.sh"
        common = common_path.read_text(
            encoding="utf-8"
        )
        validator = common[
            common.index("validate_redis_account_security()") :
            common.index("ensure_redis_account()")
        ]
        self.assertIn('getent passwd "$REDIS_USER"', validator)
        self.assertIn('/usr/sbin/nologin', validator)
        self.assertIn('/bin/false', validator)
        self.assertIn('id -G "$REDIS_USER"', validator)
        self.assertIn('"$account_gid" == "$redis_gid"', validator)
        self.assertIn('"$account_home" =~ ^/', validator)

        scenarios = (
            ("redis:x:123:123::/nonexistent:/usr/sbin/nologin", "123", True),
            ("redis:x:123:123::/home/redis:/bin/bash", "123", False),
            ("redis:x:123:123::/nonexistent:/usr/sbin/nologin", "123 27", False),
            ("redis:x:123:456::/nonexistent:/usr/sbin/nologin", "123", False),
            ("redis:x:123:123::relative/home:/usr/sbin/nologin", "123", False),
            ("redis:x:123:123::/home/../redis:/usr/sbin/nologin", "123", False),
        )
        for passwd_entry, memberships, expected_success in scenarios:
            with self.subTest(passwd=passwd_entry, memberships=memberships):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        """
                        mock_passwd="$2"
                        mock_memberships="$3"
                        source "$1"
                        getent() {
                          case "$1" in
                            passwd) printf '%s\n' "$mock_passwd" ;;
                            group) printf 'redis:x:123:\n' ;;
                            *) return 1 ;;
                          esac
                        }
                        id() {
                          [[ "$1" == "-G" ]] || return 1
                          printf '%s\n' "$mock_memberships"
                        }
                        validate_redis_account_security
                        """,
                        "bash",
                        str(common_path),
                        passwd_entry,
                        memberships,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, expected_success)

        ownership_loader = common[
            common.index("load_account_ownership_from_state()") :
            common.index("validate_redis_account_security()")
        ]
        self.assertIn('current_uid" == "$recorded_uid', ownership_loader)
        self.assertIn('current_gid" == "$recorded_gid', ownership_loader)
        self.assertIn("validate_redis_account_security", ownership_loader)

    def test_purge_account_identity_check_rejects_all_identity_drift(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        scenarios = (
            ("redis:x:123:456::/nonexistent:/usr/sbin/nologin", "456", True),
            ("redis:x:124:456::/nonexistent:/usr/sbin/nologin", "456", False),
            ("redis:x:123:457::/nonexistent:/usr/sbin/nologin", "457", False),
            ("redis:x:123:456::/changed:/usr/sbin/nologin", "456", False),
            ("redis:x:123:456::/home/redis:/bin/bash", "456", False),
            ("redis:x:123:456::/nonexistent:/usr/sbin/nologin", "456 27", False),
        )
        for passwd_entry, memberships, expected_success in scenarios:
            with self.subTest(passwd=passwd_entry, groups=memberships):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        """
                        mock_passwd="$2"
                        mock_memberships="$3"
                        source "$1"
                        getent() {
                          [[ "$1" == passwd && "$2" == redis ]] || return 1
                          printf '%s\n' "$mock_passwd"
                        }
                        id() {
                          [[ "$1" == -G && "$2" == redis ]] || return 1
                          printf '%s\n' "$mock_memberships"
                        }
                        redis_account_matches_recorded_identity \
                          123 456 /nonexistent /usr/sbin/nologin
                        """,
                        "bash",
                        str(common),
                        passwd_entry,
                        memberships,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, expected_success)

        uninstall = (ROOT / "packaging/linux/scripts/uninstall.sh").read_text(
            encoding="utf-8"
        )
        identity_check = uninstall.index("redis_account_matches_recorded_identity")
        self.assertLess(identity_check, uninstall.index('userdel "$REDIS_USER"'))
        self.assertIn('[[ "$user_deleted" == true ]]', uninstall)
        self.assertIn('[[ "$state_format" != 3 ]]', uninstall)

    def test_new_package_files_do_not_preserve_staging_xattrs(self) -> None:
        for name in ("install.sh", "update.sh"):
            with self.subTest(script=name):
                script = (
                    ROOT / "packaging/linux/scripts" / name
                ).read_text(encoding="utf-8")
                self.assertIn("--no-preserve=context,xattr", script)

        # Backups and rollback copies intentionally retain the original host
        # metadata; only files copied from the newly extracted package strip
        # staging-tree SELinux labels and other extended attributes.
        install = (ROOT / "packaging/linux/scripts/install.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'cp -a "$REDIS_INSTALL_PREFIX/$backup_item" "$install_backup_dir/"',
            install,
        )

    @requires_gnu_userland
    def test_config_trust_rejects_symlinks_in_original_path_components(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        common_text = common.read_text(encoding="utf-8")
        resolver = common_text[
            common_text.index("resolve_trusted_config_file()") :
            common_text.index("validate_redis_config_trust()")
        ]
        self.assertLess(
            resolver.index('assert_no_symlink_path_components "$candidate"'),
            resolver.index('realpath -e -- "$candidate"'),
        )
        self.assertIn('[[ ! -L "$current" ]]', common_text)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_directory = root / "real"
            real_directory.mkdir()
            safe_file = real_directory / "redis.conf"
            safe_file.write_text("port 0\n", encoding="utf-8")
            linked_directory = root / "linked-directory"
            linked_directory.symlink_to(real_directory, target_is_directory=True)
            linked_file = root / "linked-file"
            linked_file.symlink_to(safe_file)

            for candidate, expected_success in (
                (safe_file, True),
                (linked_directory / "redis.conf", False),
                (linked_file, False),
                (root / "real/../real/redis.conf", False),
            ):
                with self.subTest(candidate=candidate):
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            'source "$1"; assert_no_symlink_path_components "$2"',
                            "bash",
                            str(common),
                            str(candidate),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode == 0, expected_success)

    def test_service_fragment_lookup_distinguishes_absence_from_errors(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        with tempfile.TemporaryDirectory() as directory:
            systemctl = Path(directory) / "systemctl"
            systemctl.write_text(
                """#!/bin/bash
case "$*" in
  "show --property=LoadState --value redis.service")
    printf '%s\\n' "${MOCK_LOAD_STATE:-}"
    exit "${MOCK_LOAD_STATUS:-0}"
    ;;
  "show --property=FragmentPath --value redis.service")
    printf '%s\\n' "${MOCK_FRAGMENT_PATH:-}"
    exit 0
    ;;
  "list-unit-files --no-legend --no-pager redis.service")
    printf '%s\\n' "${MOCK_LISTED_UNITS:-}"
    exit "${MOCK_LIST_STATUS:-0}"
    ;;
esac
exit 99
""",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            scenarios = (
                (
                    {"MOCK_LOAD_STATE": "not-found", "MOCK_LOAD_STATUS": "1"},
                    0,
                    "",
                    False,
                ),
                (
                    {
                        "MOCK_LOAD_STATE": "loaded",
                        "MOCK_FRAGMENT_PATH": "/etc/systemd/system/redis.service",
                    },
                    0,
                    "/etc/systemd/system/redis.service",
                    False,
                ),
                ({"MOCK_LOAD_STATE": "masked"}, 1, "", True),
                ({"MOCK_LOAD_STATE": "error"}, 1, "", True),
                (
                    {
                        "MOCK_LOAD_STATE": "error",
                        "MOCK_LOAD_STATUS": "1",
                        "MOCK_LIST_STATUS": "1",
                    },
                    1,
                    "",
                    True,
                ),
            )
            for additions, expected_status, expected_output, expected_error in scenarios:
                with self.subTest(load_state=additions["MOCK_LOAD_STATE"]):
                    environment = os.environ.copy()
                    environment.update(additions)
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            'source "$1"; PATH="$2:$PATH"; service_fragment_path',
                            "bash",
                            str(common),
                            directory,
                        ],
                        env=environment,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    self.assertEqual(result.stdout.strip(), expected_output)
                    self.assertEqual(
                        "[redis-package] ERROR:" in result.stderr,
                        expected_error,
                        result.stderr,
                    )

    def test_lifecycle_entrypoints_preflight_their_direct_dependencies(self) -> None:
        install = (ROOT / "packaging/linux/scripts/install.sh").read_text(
            encoding="utf-8"
        )
        update = (ROOT / "packaging/linux/scripts/update.sh").read_text(
            encoding="utf-8"
        )
        uninstall = (ROOT / "packaging/linux/scripts/uninstall.sh").read_text(
            encoding="utf-8"
        )
        self.assertRegex(install, r"require_commands[^\n]*\bgetconf\b")
        self.assertRegex(update, r"require_commands[^\n]*\bgetconf\b")
        self.assertRegex(uninstall, r"require_commands[^\n]*\bsed\b")
        for name, script in (
            ("install.sh", install),
            ("update.sh", update),
            ("uninstall.sh", uninstall),
        ):
            with self.subTest(script=name):
                self.assertRegex(script, r"require_commands[^\n]*\bwc\b")
                self.assertLess(
                    script.index("require_commands"),
                    script.index("acquire_lifecycle_lock"),
                )

    def test_platform_docs_match_filesystem_mode_validation(self) -> None:
        common = (ROOT / "packaging/linux/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        directory_check = common[
            common.index("assert_root_owned_directory()") :
            common.index("assert_root_owned_regular_file()")
        ]
        regular_file_check = common[
            common.index("assert_root_owned_regular_file()") :
            common.index("validate_package_root_security()")
        ]
        self.assertIn("mode_value & 0022", directory_check)
        self.assertNotIn("mode_value & 07000", directory_check)
        self.assertIn("mode_value & 07000", regular_file_check)

        english = (ROOT / "docs/PLATFORM-DESIGN.md").read_text(encoding="utf-8")
        chinese = (ROOT / "docs/PLATFORM-DESIGN.zh-CN.md").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            english,
            r"Regular files may not have\s+setuid, setgid, or sticky mode bits",
        )
        self.assertRegex(
            english,
            r"Directories are constrained by ownership\s+and writability",
        )
        self.assertRegex(
            chinese,
            r"普通文件不得带 setuid、setgid 或\s+sticky 特殊权限位",
        )
        self.assertIn("目录按所有者和可写性约束", chinese)

    def test_effective_systemd_contract_rejects_drop_in_execution_changes(self) -> None:
        common_path = ROOT / "packaging/linux/scripts/common.sh"
        common = common_path.read_text(
            encoding="utf-8"
        )
        self.assertIn('effective_unit="$(LC_ALL=C systemctl cat', common)
        self.assertIn("effective_unit_text_matches_contract", common)
        self.assertIn("--property=DropInPaths", common)
        self.assertIn("RootDirectory", common)
        self.assertIn("EnvironmentFile", common)
        self.assertIn("LoadCredential", common)
        self.assertIn("StandardOutput", common)
        self.assertIn("OpenFile", common)
        self.assertIn("--property=RootDirectory", common)
        self.assertIn("--property=BindPaths", common)
        self.assertIn("--property=EnvironmentFiles", common)
        self.assertIn("--property=UMask", common)
        self.assertIn("--property=KillMode", common)
        self.assertIn("--property=KillSignal", common)
        self.assertIn("--property=SendSIGKILL", common)
        self.assertIn("--property=RemainAfterExit", common)

        expected_exec_start = (
            "ExecStart=/usr/local/redis/bin/redis-server "
            "/usr/local/redis/conf/redis.conf --daemonize no"
        )
        safe_unit = "\n".join(
            (
                "[Service]",
                expected_exec_start,
                "Type=simple",
                "KillSignal=SIGTERM",
                "KillMode=control-group",
                "SendSIGKILL=yes",
                "RemainAfterExit=no",
                "PrivateTmp=true",
                "ProtectSystem=full",
            )
        )
        unsafe_units = (
            safe_unit + "\nExecStart=\nExecStart=+/usr/local/redis/bin/redis-server",
            safe_unit + "\nRootDirectory=/tmp/attacker-root",
            safe_unit + "\nEnvironment=LD_PRELOAD=/tmp/attacker.so",
            safe_unit + "\nStandardOutput=truncate:/etc/passwd",
            safe_unit + "\nOpenFile=/etc/shadow:shadow:read-only",
            safe_unit + "\nKillMode=process",
        )
        for unit_text, expected_success in (
            (safe_unit, True),
            *((unit_text, False) for unit_text in unsafe_units),
        ):
            with self.subTest(unit_text=unit_text.rsplit("\n", 1)[-1]):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; effective_unit_text_matches_contract "$2"',
                        "bash",
                        str(common_path),
                        unit_text,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, expected_success)

    def test_recursive_mutations_reject_nested_mounts(self) -> None:
        common = (ROOT / "packaging/linux/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        for name in ("install.sh", "update.sh", "uninstall.sh"):
            script = (ROOT / "packaging/linux/scripts" / name).read_text(
                encoding="utf-8"
            )
            with self.subTest(script=name):
                self.assertIn("validate_destructive_targets", script)
        self.assertIn("/proc/self/mountinfo", common)
        self.assertIn('"$path"|"$path"/*', common)
        self.assertIn("validate_service_override_path", common)
        uninstall = (ROOT / "packaging/linux/scripts/uninstall.sh").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            uninstall.index('"$REDIS_INSTALL_PREFIX/data"'),
            uninstall.index('systemctl stop "$REDIS_SERVICE_NAME"'),
        )

    @unittest.skipUnless(
        Path("/proc/self/exe").exists(), "sandbox does not expose /proc process executables"
    )
    def test_live_install_process_guard_matches_current_and_deleted_executable(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        sleep_binary = shutil.which("sleep")
        self.assertIsNotNone(sleep_binary)
        with tempfile.TemporaryDirectory() as directory:
            install_root = Path(directory) / "redis"
            binary_dir = install_root / "bin"
            binary_dir.mkdir(parents=True)
            server = binary_dir / "redis-server"
            shutil.copy2(sleep_binary, server)
            process = subprocess.Popen([str(server), "30"])
            try:
                for deleted in (False, True):
                    if deleted:
                        server.unlink()
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            'source "$1"; live_install_redis_server_pids "$2"',
                            "bash",
                            str(common),
                            str(install_root),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(str(process.pid), result.stdout.splitlines())
            finally:
                process.terminate()
                process.wait(timeout=5)

        for name in ("install.sh", "update.sh", "uninstall.sh"):
            script = (ROOT / "packaging/linux/scripts" / name).read_text(
                encoding="utf-8"
            )
            with self.subTest(script=name):
                self.assertIn("assert_no_live_install_redis_server", script)

    @requires_gnu_userland
    def test_missing_service_fragment_only_cleans_project_enablement_links(self) -> None:
        common = ROOT / "packaging/linux/scripts/common.sh"
        uninstall = (ROOT / "packaging/linux/scripts/uninstall.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("remove_stale_managed_service_enablement_links", uninstall)
        self.assertIn("service_enablement_link_targets_unit", common.read_text(
            encoding="utf-8"
        ))

        with tempfile.TemporaryDirectory() as directory:
            systemd_root = Path(directory) / "systemd"
            wants = systemd_root / "multi-user.target.wants"
            wants.mkdir(parents=True)
            managed_unit = systemd_root / "redis.service"
            managed_link = wants / "managed.service"
            managed_link.symlink_to("../redis.service")
            third_party_link = wants / "third-party.service"
            third_party_link.symlink_to("/usr/lib/systemd/system/redis.service")

            for link, expected_unit, expected_success in (
                (managed_link, managed_unit, True),
                (third_party_link, managed_unit, False),
            ):
                with self.subTest(link=link.name):
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            'source "$1"; '
                            'service_enablement_link_targets_unit "$2" "$3"',
                            "bash",
                            str(common),
                            str(link),
                            str(expected_unit),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode == 0, expected_success)

    def test_release_workflow_validates_existing_and_new_draft_assets(self) -> None:
        workflow = (ROOT / ".github/workflows/build-linux.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("gh release download", workflow)
        self.assertGreaterEqual(workflow.count("release_metadata.py validate"), 3)
        self.assertIn("before-create.json", workflow)
        self.assertIn("gh api --method PATCH", workflow)
        self.assertIn('"repos/${GITHUB_REPOSITORY}/releases/${draft_id}"', workflow)
        self.assertIn("-F draft=false", workflow)
        self.assertIn("-f make_latest=false", workflow)
        self.assertIn("publish-response.json", workflow)
        self.assertIn("draft-asset-records.json", workflow)
        self.assertIn("pre-publish-asset-records.json", workflow)
        self.assertIn("published-asset-records.json", workflow)
        self.assertIn("published-readback-assets", workflow)
        self.assertNotIn("gh release edit", workflow)

    def test_public_install_instructions_do_not_preserve_sudo_environment(self) -> None:
        for name in ("README.md", "README.zh-CN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("sudo -E", text)
            self.assertIn("sudo mktemp -d /var/tmp/redis-unofficial-builds.XXXXXX", text)
            self.assertIn("umask 022; exec tar", text)
            self.assertIn("X.Y.Z", text)
            self.assertIn("Immutable Releases", text)

        generated_readme = (ROOT / "scripts/linux/build-redis.sh").read_text(
            encoding="utf-8"
        )
        workflow = (ROOT / ".github/workflows/build-linux.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("umask 022; exec tar", generated_readme)
        self.assertGreaterEqual(workflow.count("umask 022; exec tar"), 3)

    @staticmethod
    def _validate_package_root(package_root: Path) -> subprocess.CompletedProcess[str]:
        common = ROOT / "packaging/linux/scripts/common.sh"
        return subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; validate_package_root "$2"',
                "bash",
                str(common),
                str(package_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _write_package_fixture(package_root: Path, package_format: str) -> None:
        for relative in ("bin", "conf", "systemd"):
            (package_root / relative).mkdir(parents=True, exist_ok=True)
        for binary in ("redis-server", "redis-cli"):
            path = package_root / "bin" / binary
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        (package_root / "conf/redis.conf").write_text("port 6379\n", encoding="utf-8")
        (package_root / "systemd/redis.service").write_text(
            "\n".join(
                [
                    "# Managed by redis-unofficial-builds",
                    "[Service]",
                    "User=redis",
                    "Group=redis",
                    "WorkingDirectory=/usr/local/redis",
                    "ExecStart=/usr/local/redis/bin/redis-server "
                    "/usr/local/redis/conf/redis.conf --daemonize no",
                    "Type=simple",
                    "KillSignal=SIGTERM",
                    "KillMode=control-group",
                    "SendSIGKILL=yes",
                    "RemainAfterExit=no",
                    "NoNewPrivileges=true",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (package_root / "THIRD_PARTY_NOTICES.md").write_text(
            "# Third-party notices\n", encoding="utf-8"
        )
        contributor_license = b"fixture contributor license\n"
        dependency_body = b"fixture dependency license\n"
        dependency_notices = (
            b"UPSTREAM_DEPENDENCY_NOTICES_FORMAT=1\n"
            b"REDIS_VERSION=7.4.11\n"
            b"SOURCE_SUBTREE=deps\n\n"
            + f"===== BEGIN deps/example/LICENSE ({len(dependency_body)} bytes) =====\n".encode()
            + dependency_body
            + b"\n===== END deps/example/LICENSE =====\n"
        )
        (package_root / "UPSTREAM-CONTRIBUTOR-LICENSE.txt").write_bytes(
            contributor_license
        )
        (package_root / "UPSTREAM-DEPENDENCY-NOTICES.txt").write_bytes(
            dependency_notices
        )
        machine = platform.machine().lower()
        package_arch = "arm64" if machine in {"aarch64", "arm64"} else "x64"
        (package_root / "PACKAGE-INFO").write_text(
            "\n".join(
                [
                    f"PACKAGE_FORMAT={package_format}",
                    "PACKAGE_ID=redis-unofficial-builds",
                    "REDIS_VERSION=7.4.11",
                    "REDIS_SERIES=7.4",
                    "BUILD_PROFILE=core",
                    "PACKAGE_VARIANT=linux-glibc2.28",
                    f"PACKAGE_ARCH={package_arch}",
                    "OS=linux",
                    "LIBC=glibc",
                    "MIN_GLIBC=2.28",
                    "MAX_GLIBC_SYMBOL=2.28",
                    "SERVICE_BACKEND=systemd",
                    "INSTALL_PREFIX=/usr/local/redis",
                    f"UPSTREAM_SOURCE_SHA256={'a' * 64}",
                    "UPSTREAM_CONTRIBUTOR_LICENSE_SHA256="
                    f"{hashlib.sha256(contributor_license).hexdigest()}",
                    "UPSTREAM_DEPENDENCY_NOTICES_SHA256="
                    f"{hashlib.sha256(dependency_notices).hexdigest()}",
                    f"PATCHSET_SHA256={'b' * 64}",
                    "",
                ]
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
