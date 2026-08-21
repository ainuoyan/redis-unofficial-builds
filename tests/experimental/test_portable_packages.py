from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CREATE = ROOT / "scripts/experimental/create_portable_package.py"
VALIDATE = ROOT / "scripts/experimental/validate_portable_asset.py"
BUILD_SCRIPT = ROOT / "scripts/experimental/build-portable-posix.sh"
WINDOWS_SERVICE_SOURCE = ROOT / "packaging/windows/service/RedisService/Program.cs"
WINDOWS_COMMON_SCRIPT = ROOT / "packaging/windows/scripts/Common-Redis.ps1"
VERSION = "7.4.11"
SOURCE_SHA256 = "a" * 64
HASHES_COMMIT = "b" * 40
REVISION = "c" * 40

PREPARE_WINDOWS_SPEC = importlib.util.spec_from_file_location(
    "prepare_windows_source",
    ROOT / "scripts/experimental/prepare_windows_source.py",
)
assert PREPARE_WINDOWS_SPEC is not None and PREPARE_WINDOWS_SPEC.loader is not None
prepare_windows_source = importlib.util.module_from_spec(PREPARE_WINDOWS_SPEC)
PREPARE_WINDOWS_SPEC.loader.exec_module(prepare_windows_source)


def elf_fixture(arch: str) -> bytes:
    data = bytearray(512)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", data, 18, {"x64": 0x3E, "arm64": 0xB7}[arch])
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<H", data, 54, 56)
    struct.pack_into("<H", data, 56, 1)
    interpreter = {
        "x64": b"/lib/ld-musl-x86_64.so.1\x00",
        "arm64": b"/lib/ld-musl-aarch64.so.1\x00",
    }[arch]
    struct.pack_into("<IIQQQQQQ", data, 64, 3, 4, 256, 0, 0, len(interpreter), len(interpreter), 1)
    data[256 : 256 + len(interpreter)] = interpreter
    marker = b"\x00" + VERSION.encode("ascii") + b"\x00"
    data[320 : 320 + len(marker)] = marker
    return bytes(data)


def macho_fixture(arch: str) -> bytes:
    data = bytearray(160)
    data[:4] = b"\xcf\xfa\xed\xfe"
    struct.pack_into("<I", data, 4, {"x64": 0x01000007, "arm64": 0x0100000C}[arch])
    struct.pack_into("<I", data, 16, 1)
    struct.pack_into("<I", data, 20, 24)
    struct.pack_into("<IIIIII", data, 32, 0x32, 24, 1, 12 << 16, 15 << 16, 0)
    marker = b"\x00" + VERSION.encode("ascii") + b"\x00"
    data[96 : 96 + len(marker)] = marker
    return bytes(data)


def pe_fixture(*, include_version: bool) -> bytes:
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", data, 0x84, 0x8664)
    struct.pack_into("<H", data, 0x94, 0xF0)
    struct.pack_into("<H", data, 0x98, 0x20B)
    if include_version:
        data[0x1C0 : 0x1C0 + len(VERSION)] = VERSION.encode("ascii")
    return bytes(data)


class PortablePackageTests(unittest.TestCase):
    def make_source(self, root: Path) -> Path:
        source = root / "source"
        (source / "deps/example").mkdir(parents=True)
        (source / "redis.conf").write_text(
            "bind 127.0.0.1\nloadmodule /tmp/untrusted.so\n", encoding="utf-8"
        )
        (source / "sentinel.conf").write_text("port 26379\n", encoding="utf-8")
        (source / "LICENSE.txt").write_text("Redis license fixture\n", encoding="utf-8")
        (source / "REDISCONTRIBUTIONS.txt").write_text(
            "Contributor license fixture\n", encoding="utf-8"
        )
        (source / "deps/example/LICENSE").write_text(
            "Dependency license fixture\n", encoding="utf-8"
        )
        return source

    def make_binaries(self, root: Path, variant: str, arch: str) -> tuple[Path, Path | None]:
        binaries = root / "bin"
        binaries.mkdir()
        service_wrapper = None
        if variant == "linux-musl1.2":
            body = elf_fixture(arch)
            suffix = ""
        elif variant == "macos12":
            body = macho_fixture(arch)
            suffix = ""
        else:
            body = pe_fixture(include_version=True)
            suffix = ".exe"
        for name in ("redis-server", "redis-cli", "redis-benchmark"):
            (binaries / f"{name}{suffix}").write_bytes(body)
        if variant == "windows-msys2":
            runtime = bytearray(pe_fixture(include_version=False))
            runtime[0x1C0 : 0x1C0 + len(b"cygwin1.dll")] = b"cygwin1.dll"
            (binaries / "msys-2.0.dll").write_bytes(runtime)
            (binaries / "MSYS2-RUNTIME-NOTICES.txt").write_text(
                "MSYS2_RUNTIME_NOTICES_FORMAT=1\n"
                "DLL=msys-2.0.dll PACKAGE=msys2-runtime\n"
                "PACKAGE=msys2-runtime 3.6.5-1\n"
                "===== BEGIN /usr/share/doc/Cygwin/COPYING (5 bytes) =====\n"
                "test\n"
                "===== END /usr/share/doc/Cygwin/COPYING =====\n"
                "===== BEGIN /usr/share/doc/Cygwin/CYGWIN_LICENSE (5 bytes) =====\n"
                "test\n"
                "===== END /usr/share/doc/Cygwin/CYGWIN_LICENSE =====\n",
                encoding="utf-8",
            )
            service_wrapper = root / "RedisService.exe"
            service_wrapper.write_bytes(pe_fixture(include_version=False))
        return binaries, service_wrapper

    def create_and_validate(self, root: Path, variant: str, arch: str) -> Path:
        source = self.make_source(root)
        binaries, service_wrapper = self.make_binaries(root, variant, arch)
        output = root / "output"
        command = [
            sys.executable,
            str(CREATE),
            "--source-root", str(source),
            "--binary-dir", str(binaries),
            "--output-dir", str(output),
            "--packaging-root", str(ROOT),
            "--redis-version", VERSION,
            "--source-sha256", SOURCE_SHA256,
            "--hashes-commit", HASHES_COMMIT,
            "--packaging-revision", REVISION,
            "--variant", variant,
            "--arch", arch,
            "--build-environment", "unit-test fixture",
            "--compiler", "fixture compiler",
        ]
        if service_wrapper is not None:
            command.extend(("--service-wrapper", str(service_wrapper)))
        created = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, created.stderr)
        extension = "zip" if variant == "windows-msys2" else "tar.gz"
        archive = output / f"Redis-{VERSION}-{variant}-{arch}.{extension}"
        validated = subprocess.run(
            [
                sys.executable,
                str(VALIDATE),
                "--archive", str(archive),
                "--checksum", str(archive) + ".sha256",
                "--packaging-root", str(ROOT),
                "--redis-version", VERSION,
                "--source-sha256", SOURCE_SHA256,
                "--hashes-commit", HASHES_COMMIT,
                "--packaging-revision", REVISION,
                "--variant", variant,
                "--arch", arch,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)
        return archive

    def test_all_experimental_portable_variants_are_created_and_validated(self) -> None:
        for variant, arch in (
            ("linux-musl1.2", "x64"),
            ("linux-musl1.2", "arm64"),
            ("macos12", "x64"),
            ("macos12", "arm64"),
            ("windows-msys2", "x64"),
        ):
            with self.subTest(variant=variant, arch=arch), tempfile.TemporaryDirectory() as directory:
                archive = self.create_and_validate(Path(directory), variant, arch)
                self.assertGreater(archive.stat().st_size, 0)

    def test_active_loadmodule_is_disabled_in_generated_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = self.create_and_validate(Path(directory), "linux-musl1.2", "x64")
            with tarfile.open(archive, "r:gz") as package:
                member = package.extractfile("redis/conf/redis.conf")
                self.assertIsNotNone(member)
                assert member is not None
                config = member.read().decode("utf-8")
            self.assertIn("# Disabled by the experimental core profile: loadmodule", config)
            self.assertNotRegex(config, r"(?m)^\s*loadmodule\s")

    def test_archive_generation_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            left = self.create_and_validate(Path(first), "macos12", "x64")
            right = self.create_and_validate(Path(second), "macos12", "x64")
            self.assertEqual(hashlib.sha256(left.read_bytes()).digest(), hashlib.sha256(right.read_bytes()).digest())

    def test_windows_patchset_hash_ignores_dotnet_build_outputs(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts/experimental"))
        import portable_contract

        with tempfile.TemporaryDirectory() as directory:
            packaging_root = Path(directory)
            for relative in portable_contract.patchset_paths("windows-msys2"):
                destination = packaging_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)

            baseline = portable_contract.packaging_patchset_sha256(
                packaging_root, "windows-msys2"
            )
            for relative in (
                Path("packaging/windows/service/RedisService/obj/project.assets.json"),
                Path("packaging/windows/service/RedisService/bin/Release/RedisService.dll"),
            ):
                generated = packaging_root / relative
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_text("generated build output\n", encoding="utf-8")

            self.assertEqual(
                portable_contract.packaging_patchset_sha256(
                    packaging_root, "windows-msys2"
                ),
                baseline,
            )

    def test_windows_patchset_files_are_checked_out_with_lf_endings(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts/experimental"))
        import portable_contract

        paths = portable_contract.patchset_paths("windows-msys2")
        result = subprocess.run(
            ["git", "check-attr", "-z", "eol", "--", *map(str, paths)],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        fields = result.stdout.decode("utf-8").split("\0")
        self.assertEqual(fields[-1], "")
        records = {
            fields[index]: fields[index + 2]
            for index in range(0, len(fields) - 1, 3)
        }
        self.assertEqual(records, {str(path): "lf" for path in paths})

    def test_hashes_snapshot_is_bound_to_the_validated_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = self.create_and_validate(Path(directory), "macos12", "x64")
            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATE),
                    "--archive", str(archive),
                    "--checksum", str(archive) + ".sha256",
                    "--packaging-root", str(ROOT),
                    "--redis-version", VERSION,
                    "--source-sha256", SOURCE_SHA256,
                    "--hashes-commit", "d" * 40,
                    "--packaging-revision", REVISION,
                    "--variant", "macos12",
                    "--arch", "x64",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("Redis hashes snapshot does not match", result.stderr)

    def test_windows_runtime_notice_must_map_every_dll(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts/experimental"))
        import validate_portable_asset as validator

        notices = (
            b"MSYS2_RUNTIME_NOTICES_FORMAT=1\n"
            b"PACKAGE=msys2-runtime 3.6.5-1\n"
            b"===== BEGIN /usr/share/licenses/msys2-runtime/COPYING (5 bytes) =====\n"
            b"test\n"
            b"===== END /usr/share/licenses/msys2-runtime/COPYING =====\n"
        )
        with self.assertRaisesRegex(validator.ContractError, "map every packaged DLL"):
            validator.validate_windows_runtime_notices(notices, {"redis/bin/msys-2.0.dll"})

    def test_windows_runtime_notice_requires_the_complete_cygwin_license_pair(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts/experimental"))
        import validate_portable_asset as validator

        notices = (
            b"MSYS2_RUNTIME_NOTICES_FORMAT=1\n"
            b"DLL=msys-2.0.dll PACKAGE=msys2-runtime\n"
            b"PACKAGE=msys2-runtime 3.6.5-1\n"
            b"===== BEGIN /usr/share/doc/Cygwin/COPYING (5 bytes) =====\n"
            b"test\n"
            b"===== END /usr/share/doc/Cygwin/COPYING =====\n"
        )
        with self.assertRaisesRegex(validator.ContractError, "lack license text"):
            validator.validate_windows_runtime_notices(
                notices, {"redis/bin/msys-2.0.dll"}
            )

    def test_windows_cygwin_marker_is_rejected_only_for_executables(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts/experimental"))
        import validate_portable_asset as validator

        binary = bytearray(pe_fixture(include_version=False))
        binary[0x1C0 : 0x1C0 + len(b"cygwin1.dll")] = b"cygwin1.dll"
        with self.assertRaisesRegex(validator.ContractError, "Cygwin-linked"):
            validator.validate_pe(bytes(binary))
        validator.validate_pe(bytes(binary), reject_cygwin_marker=False)

    def test_windows_service_wrapper_preserves_redis_startup_diagnostics(self) -> None:
        source = WINDOWS_SERVICE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("RedirectStandardOutput = true", source)
        self.assertIn("RedirectStandardError = true", source)
        self.assertIn('LogRedisOutput("stdout", eventArgs.Data)', source)
        self.assertIn('LogRedisOutput("stderr", eventArgs.Data)', source)
        self.assertIn("exited before readiness with code", source)

    def test_windows_service_uses_prefix_relative_msys_paths(self) -> None:
        source = WINDOWS_SERVICE_SOURCE.read_text(encoding="utf-8")
        common = WINDOWS_COMMON_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Path.GetRelativePath(prefix, configPath)", source)
        self.assertNotIn("ToMsysPath", source)
        self.assertIn('dir "data"', common)
        self.assertIn('logfile "../log/redis.log"', common)
        self.assertIn('pidfile "../run/redis.pid"', common)
        self.assertNotIn('/c/Program Files/Redis-Unofficial', common)

    def test_traversal_and_link_entries_are_rejected(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts/experimental"))
        import validate_portable_asset as validator

        for name in ("../escape", "redis/../escape", "/redis/bin/tool", "redis\\bin\\tool"):
            with self.subTest(name=name), self.assertRaises(validator.ContractError):
                validator.validate_name(name)

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as package:
                member = zipfile.ZipInfo("redis/bin/redis-server.exe", (1980, 1, 1, 0, 0, 0))
                member.create_system = 3
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
                package.writestr(member, b"target")
            with self.assertRaisesRegex(validator.ContractError, "regular file"):
                validator.read_zip(archive)

    def test_experimental_workflow_is_read_only_and_nonpublishing(self) -> None:
        workflow = (ROOT / ".github/workflows/build-experimental.yml").read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("workflow_call:", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("gh release", workflow.lower())
        self.assertNotIn("create-release", workflow.lower())
        platforms = json.loads((ROOT / "config/platforms.json").read_text(encoding="utf-8"))
        experimental = [item for item in platforms["platforms"] if item["status"] == "experimental"]
        self.assertEqual(len(experimental), 7)
        self.assertTrue(all(item["controller_enabled"] is False for item in experimental))
        self.assertTrue(all(item["build_workflow"] == "build-experimental.yml" for item in experimental))

    def test_portable_build_stabilizes_tests_and_uses_a_short_macos_temp_root(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(
            script,
            r'if \[\[ "\$PACKAGE_VARIANT" == macos12 \]\]; then\s+temp_parent=/tmp\s+fi',
        )
        self.assertIn(
            'temp_parent="$(cd "$temp_parent" 2>/dev/null && pwd -P)"', script
        )
        self.assertIn('./runtest --clients 1 --timeout 1200', script)
        self.assertIn('make test redis "${make_args[@]}"', script)
        self.assertIn(
            'bash "$PROJECT_ROOT/scripts/run-test-with-one-retry.sh"', script
        )
        self.assertNotIn('${TMPDIR:-/tmp}/redis-experimental', script)

        workflow = (ROOT / ".github/workflows/build-experimental.yml").read_text(
            encoding="utf-8"
        )
        glibc_job, remainder = workflow.split("\n  glibc217:\n", 1)[1].split(
            "\n  musl:\n", 1
        )
        musl_job, remainder = remainder.split(
            "\n  macos:\n", 1
        )
        macos_job = remainder.split("\n  windows:\n", 1)[0]
        self.assertIn("timeout-minutes: 90", glibc_job)
        self.assertIn("timeout-minutes: 90", musl_job)
        self.assertIn("timeout-minutes: 90", macos_job)

    def test_experimental_workflow_runs_platform_lifecycle_acceptance(self) -> None:
        workflow = (ROOT / ".github/workflows/build-experimental.yml").read_text(
            encoding="utf-8"
        )
        glibc_job, remainder = workflow.split("\n  glibc217:\n", 1)[1].split(
            "\n  musl:\n", 1
        )
        musl_job, remainder = remainder.split("\n  macos:\n", 1)
        macos_job, windows_job = remainder.split("\n  windows:\n", 1)

        self.assertIn("Test legacy userspace lifecycle without systemd", glibc_job)
        self.assertIn('"$package/scripts/install.sh" --no-service', glibc_job)
        self.assertIn('redis-cli" -s "$socket" save', glibc_job.lower())
        self.assertIn('"$package/scripts/update.sh"', glibc_job)
        self.assertIn('uninstall.sh" --purge', glibc_job)

        self.assertIn("Test OpenRC install, persistence, recovery, and purge", musl_job)
        self.assertIn('rc-service "$service" restart', musl_job)
        self.assertIn('redis-cli" -s "$socket" save', musl_job.lower())
        self.assertIn('"$package/scripts/update.sh"', musl_job)
        self.assertIn('uninstall.sh" --purge', musl_job)

        self.assertIn("Test launchd install, persistence, recovery, and purge", macos_job)
        self.assertIn("launchctl kickstart -k system/io.github.ainuoyan.redis-unofficial", macos_job)
        self.assertIn('redis-cli" -s "$socket" save', macos_job.lower())
        self.assertIn('"$package/scripts/update.sh"', macos_job)
        self.assertIn('uninstall.sh" --purge', macos_job)

        self.assertIn("Restart-Service -Name RedisUnofficial", windows_job)
        self.assertIn("redis-unofficial-acceptance-persistence", windows_job)

    def test_windows_build_supports_the_official_runtime_license_location(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('license_root=/usr/share/doc/Cygwin', script)

    def test_windows_scripts_remain_windows_powershell_compatible_text(self) -> None:
        for script in sorted((ROOT / "packaging/windows/scripts").glob("*.ps1")):
            data = script.read_bytes()
            self.assertTrue(data)
            self.assertNotIn(b"\xef\xbb\xbf", data)
            data.decode("ascii")

    def test_windows_source_adjustment_accepts_old_and_new_makefile_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            makefile = Path(directory) / "Makefile"
            makefile.write_text("all: redis-server\n", encoding="utf-8")
            self.assertEqual(
                prepare_windows_source.remove_optional_module_tests_target(makefile), 0
            )
            self.assertEqual(makefile.read_text(encoding="utf-8"), "all: redis-server\n")

            makefile.write_text("all: redis-server module_tests\n", encoding="utf-8")
            self.assertEqual(
                prepare_windows_source.remove_optional_module_tests_target(makefile), 1
            )
            self.assertEqual(makefile.read_text(encoding="utf-8"), "all: redis-server\n")

            makefile.write_text(
                "all: redis-server module_tests\nall: redis-cli module_tests\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "multiple module_tests"):
                prepare_windows_source.remove_optional_module_tests_target(makefile)

    def test_windows_source_adjustment_guards_unsupported_dladdr_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            debug_c = Path(directory) / "debug.c"
            debug_c.write_text(
                "#define UNUSED(value) ((void)(value))\n"
                "void dumpX86Calls(void *addr, size_t len) {\n"
                "    Dl_info info;\n"
                "    (void)addr; (void)len; (void)info;\n"
                "}\n\n"
                "void dumpCodeAroundEIP(void *eip) {\n"
                "    Dl_info info;\n"
                "    dladdr(eip, &info);\n"
                "}\n\n"
                "void invalidFunctionWasCalled(void) {}\n",
                encoding="utf-8",
            )

            self.assertEqual(
                prepare_windows_source.guard_unsupported_dladdr_diagnostics(debug_c), 1
            )
            guarded = debug_c.read_text(encoding="utf-8")
            self.assertIn(
                "#if !defined(__CYGWIN__) && !defined(__MSYS__)", guarded
            )
            self.assertIn("Dl_info info;", guarded)
            self.assertIn("dladdr(eip, &info);", guarded)
            self.assertIn(
                "#else\nvoid dumpCodeAroundEIP(void *eip) {\n    UNUSED(eip);\n}\n"
                "#endif /* !defined(__CYGWIN__) && !defined(__MSYS__) */",
                guarded,
            )
            self.assertEqual(
                prepare_windows_source.guard_unsupported_dladdr_diagnostics(debug_c), 0
            )
            self.assertEqual(debug_c.read_text(encoding="utf-8"), guarded)

            compiler = shutil.which("cc")
            if compiler is not None:
                for platform_macro in ("__CYGWIN__", "__MSYS__"):
                    with self.subTest(platform_macro=platform_macro):
                        compiled = subprocess.run(
                            [
                                compiler,
                                f"-D{platform_macro}",
                                "-Werror",
                                "-fsyntax-only",
                                str(debug_c),
                            ],
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(compiled.returncode, 0, compiled.stderr)

    def test_windows_source_adjustment_rejects_ambiguous_dladdr_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            debug_c = Path(directory) / "debug.c"
            debug_c.write_text(
                "void dumpX86Calls(void *addr, size_t len) {}\n"
                "void dumpX86Calls(void *addr, size_t len) {}\n"
                "void dumpCodeAroundEIP(void *eip) {}\n"
                "void invalidFunctionWasCalled(void) {}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "ambiguous dumpX86Calls"):
                prepare_windows_source.guard_unsupported_dladdr_diagnostics(debug_c)


if __name__ == "__main__":
    unittest.main()
