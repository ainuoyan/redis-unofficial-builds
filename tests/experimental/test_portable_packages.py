from __future__ import annotations

import hashlib
import importlib.util
import io
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
sys.path.insert(0, str(ROOT / "scripts/experimental"))
import portable_contract  # noqa: E402

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


def elf_fixture(
    arch: str,
    *,
    glibc_version_requirement: bool = False,
    unreferenced_glibc_marker: bool = False,
) -> bytes:
    data = bytearray(1024)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", data, 18, {"x64": 0x3E, "arm64": 0xB7}[arch])
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<H", data, 54, 56)
    struct.pack_into("<H", data, 56, 3)
    base_address = 0x400000
    struct.pack_into(
        "<IIQQQQQQ",
        data,
        64,
        1,
        4,
        0,
        base_address,
        0,
        len(data),
        len(data),
        0x1000,
    )
    interpreter = {
        "x64": b"/lib/ld-musl-x86_64.so.1\x00",
        "arm64": b"/lib/ld-musl-aarch64.so.1\x00",
    }[arch]
    struct.pack_into(
        "<IIQQQQQQ",
        data,
        120,
        3,
        4,
        256,
        0,
        0,
        len(interpreter),
        len(interpreter),
        1,
    )
    data[256 : 256 + len(interpreter)] = interpreter

    expected_libc = {
        "x64": b"libc.musl-x86_64.so.1",
        "arm64": b"libc.musl-aarch64.so.1",
    }[arch]
    strings = bytearray(b"\x00" + expected_libc + b"\x00")
    glibc_file_index = 0
    glibc_version_index = 0
    if glibc_version_requirement:
        glibc_file_index = len(strings)
        strings.extend(b"libstdc++.so.6\x00")
        glibc_version_index = len(strings)
        strings.extend(b"GLIBC_2.17\x00")
    elif unreferenced_glibc_marker:
        strings.extend(b"GLIBC_999.0\x00")
    string_offset = 512
    data[string_offset : string_offset + len(strings)] = strings

    dynamic_entries = [
        (1, 1),
        (5, base_address + string_offset),
        (10, len(strings)),
    ]
    if glibc_version_requirement:
        version_offset = 640
        struct.pack_into(
            "<HHIII", data, version_offset, 1, 1, glibc_file_index, 16, 0
        )
        struct.pack_into(
            "<IHHII", data, version_offset + 16, 0, 0, 0, glibc_version_index, 0
        )
        dynamic_entries.extend(
            ((0x6FFFFFFE, base_address + version_offset), (0x6FFFFFFF, 1))
        )
    dynamic_entries.append((0, 0))
    dynamic_offset = 320
    for index, entry in enumerate(dynamic_entries):
        struct.pack_into("<qQ", data, dynamic_offset + index * 16, *entry)
    dynamic_size = len(dynamic_entries) * 16
    struct.pack_into(
        "<IIQQQQQQ",
        data,
        176,
        2,
        6,
        dynamic_offset,
        base_address + dynamic_offset,
        0,
        dynamic_size,
        dynamic_size,
        8,
    )
    marker = b"\x00" + VERSION.encode("ascii") + b"\x00"
    data[768 : 768 + len(marker)] = marker
    return bytes(data)


def macho_fixture(arch: str, deployment_major: int = 15) -> bytes:
    data = bytearray(160)
    data[:4] = b"\xcf\xfa\xed\xfe"
    struct.pack_into("<I", data, 4, {"x64": 0x01000007, "arm64": 0x0100000C}[arch])
    struct.pack_into("<I", data, 16, 1)
    struct.pack_into("<I", data, 20, 24)
    struct.pack_into(
        "<IIIIII", data, 32, 0x32, 24, 1, deployment_major << 16, 15 << 16, 0
    )
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
    def write_source_archive(
        self,
        archive: Path,
        *,
        link_target: str = "../.skills",
        include_link_descendant: bool = False,
    ) -> str:
        prefix = f"redis-{VERSION}"
        with tarfile.open(archive, "w:gz") as output:
            for name in (
                prefix,
                f"{prefix}/modules",
                f"{prefix}/modules/redisearch",
                f"{prefix}/modules/redisearch/src",
                f"{prefix}/modules/redisearch/src/.claude",
                f"{prefix}/modules/redisearch/src/.skills",
            ):
                member = tarfile.TarInfo(name)
                member.type = tarfile.DIRTYPE
                output.addfile(member)
            agents = tarfile.TarInfo(
                f"{prefix}/modules/redisearch/src/AGENTS.md"
            )
            agents.size = 4
            output.addfile(agents, io.BytesIO(b"test"))
            skills = tarfile.TarInfo(
                f"{prefix}/modules/redisearch/src/.claude/skills"
            )
            skills.type = tarfile.SYMTYPE
            skills.linkname = link_target
            output.addfile(skills)
            claude = tarfile.TarInfo(
                f"{prefix}/modules/redisearch/src/CLAUDE.md"
            )
            claude.type = tarfile.SYMTYPE
            claude.linkname = "AGENTS.md"
            output.addfile(claude)
            if include_link_descendant:
                descendant = tarfile.TarInfo(
                    f"{prefix}/modules/redisearch/src/.claude/skills/injected"
                )
                descendant.size = 4
                output.addfile(descendant, io.BytesIO(b"test"))
        return hashlib.sha256(archive.read_bytes()).hexdigest()

    def test_source_archive_accepts_internal_terminal_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / f"redis-{VERSION}.tar.gz"
            digest = self.write_source_archive(archive)
            portable_contract.validate_source_archive(archive, digest, VERSION)

    def test_source_archive_rejects_escaping_and_traversed_symlinks(self) -> None:
        cases = (
            ("/tmp/outside", False, "unsafe"),
            ("../../../../../../outside", False, "escapes"),
            ("../.skills", True, "descends through"),
        )
        for link_target, include_descendant, message in cases:
            with self.subTest(link_target=link_target):
                with tempfile.TemporaryDirectory() as directory:
                    archive = Path(directory) / f"redis-{VERSION}.tar.gz"
                    digest = self.write_source_archive(
                        archive,
                        link_target=link_target,
                        include_link_descendant=include_descendant,
                    )
                    with self.assertRaisesRegex(
                        portable_contract.ContractError, message
                    ):
                        portable_contract.validate_source_archive(
                            archive, digest, VERSION
                        )

    def test_repository_files_do_not_contain_removed_brand_token(self) -> None:
        forbidden = ("r" + "zon").casefold()
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT).as_posix()
            if ".git" in path.parts or not path.is_file():
                continue
            self.assertNotIn(forbidden, relative.casefold(), relative)
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            self.assertNotIn(forbidden, text.casefold(), relative)

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
        elif variant == "macos15":
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

    def create_and_validate(
        self,
        root: Path,
        variant: str,
        arch: str,
        *,
        package_status: str = "experimental",
    ) -> Path:
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
            "--package-status", package_status,
            "--build-workflow",
            (
                ".github/workflows/build-all-platforms.yml"
                if package_status == "release"
                else ".github/workflows/build-experimental.yml"
            ),
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
                "--package-status", package_status,
                "--build-workflow",
                (
                ".github/workflows/build-all-platforms.yml"
                    if package_status == "release"
                    else ".github/workflows/build-experimental.yml"
                ),
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
            ("macos15", "x64"),
            ("macos15", "arm64"),
            ("windows-msys2", "x64"),
        ):
            with self.subTest(variant=variant, arch=arch), tempfile.TemporaryDirectory() as directory:
                archive = self.create_and_validate(Path(directory), variant, arch)
                self.assertGreater(archive.stat().st_size, 0)

    def test_all_portable_variants_support_the_release_contract(self) -> None:
        for variant, arch in (
            ("linux-musl1.2", "x64"),
            ("linux-musl1.2", "arm64"),
            ("macos15", "x64"),
            ("macos15", "arm64"),
            ("windows-msys2", "x64"),
        ):
            with self.subTest(variant=variant, arch=arch), tempfile.TemporaryDirectory() as directory:
                archive = self.create_and_validate(
                    Path(directory), variant, arch, package_status="release"
                )
                self.assertGreater(archive.stat().st_size, 0)

    def test_release_status_rejects_the_experimental_workflow_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root)
            binaries, _ = self.make_binaries(root, "macos15", "x64")
            result = subprocess.run(
                [
                    sys.executable,
                    str(CREATE),
                    "--source-root", str(source),
                    "--binary-dir", str(binaries),
                    "--output-dir", str(root / "output"),
                    "--packaging-root", str(ROOT),
                    "--redis-version", VERSION,
                    "--source-sha256", SOURCE_SHA256,
                    "--hashes-commit", HASHES_COMMIT,
                    "--packaging-revision", REVISION,
                    "--variant", "macos15",
                    "--arch", "x64",
                    "--build-environment", "unit-test fixture",
                    "--compiler", "fixture compiler",
                    "--package-status", "release",
                    "--build-workflow", ".github/workflows/build-experimental.yml",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("do not match", result.stderr)

    def test_macos_validator_requires_the_declared_15_0_deployment_target(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts/experimental"))
        import validate_portable_asset as validator

        for deployment_major in (12, 16):
            with self.subTest(deployment_major=deployment_major), self.assertRaisesRegex(
                validator.ContractError, "macOS 15.0 deployment target"
            ):
                validator.validate_macho(
                    macho_fixture("x64", deployment_major), "x64", VERSION
                )

    def test_musl_runtime_ignores_unreferenced_glibc_debug_text(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts/experimental"))
        import validate_portable_asset as validator

        validator.validate_elf(
            elf_fixture("arm64", unreferenced_glibc_marker=True), "arm64", VERSION
        )

    def test_musl_runtime_rejects_actual_glibc_version_requirement(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts/experimental"))
        import validate_portable_asset as validator

        with self.assertRaisesRegex(validator.ContractError, "glibc runtime"):
            validator.validate_elf(
                elf_fixture("arm64", glibc_version_requirement=True),
                "arm64",
                VERSION,
            )

    def test_active_loadmodule_is_disabled_in_generated_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = self.create_and_validate(Path(directory), "linux-musl1.2", "x64")
            with tarfile.open(archive, "r:gz") as package:
                member = package.extractfile("redis/conf/redis.conf")
                self.assertIsNotNone(member)
                assert member is not None
                config = member.read().decode("utf-8")
            self.assertIn("# Disabled by the core package profile: loadmodule", config)
            self.assertNotRegex(config, r"(?m)^\s*loadmodule\s")

    def test_archive_generation_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            left = self.create_and_validate(Path(first), "macos15", "x64")
            right = self.create_and_validate(Path(second), "macos15", "x64")
            self.assertEqual(hashlib.sha256(left.read_bytes()).digest(), hashlib.sha256(right.read_bytes()).digest())

    def test_windows_patchset_hash_ignores_dotnet_build_outputs(self) -> None:
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
            configuration = packaging_root / "packaging/windows/service/RedisService/RedisConfiguration.cs"
            configuration.write_text(
                configuration.read_text(encoding="utf-8") + "\n// Configuration change\n",
                encoding="utf-8",
            )
            self.assertNotEqual(
                portable_contract.packaging_patchset_sha256(packaging_root, "windows-msys2"),
                baseline,
            )

    def test_windows_patchset_files_are_checked_out_with_lf_endings(self) -> None:
        paths = sorted(
            {
                *portable_contract.patchset_paths("windows-msys2"),
                *portable_contract.patchset_paths(
                    "windows-msys2", Path(".github/workflows/build-all-platforms.yml")
                ),
            },
            key=lambda path: path.as_posix(),
        )
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
            archive = self.create_and_validate(Path(directory), "macos15", "x64")
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
                    "--variant", "macos15",
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
        self.assertIn('LogRedisOutput("stdout", eventArgs.Data, settings.Password)', source)
        self.assertIn('LogRedisOutput("stderr", eventArgs.Data, settings.Password)', source)
        self.assertIn('line.Replace(password, "[redacted]", StringComparison.Ordinal)', source)
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

    def test_platform_workflow_is_read_only_and_release_status_is_caller_bound(self) -> None:
        workflow = (ROOT / ".github/workflows/build-experimental.yml").read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("workflow_call:", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("gh release", workflow.lower())
        self.assertNotIn("create-release", workflow.lower())
        self.assertIn("PACKAGE_STATUS: ${{ inputs.release_contract && 'release'", workflow)
        self.assertIn("RELEASE_CONTRACT: ${{ inputs.release_contract }}", workflow)
        self.assertNotIn("github.event_name == 'workflow_call'", workflow)
        platforms = json.loads((ROOT / "config/platforms.json").read_text(encoding="utf-8"))
        implemented = [item for item in platforms["platforms"] if item["status"] == "implemented"]
        self.assertEqual(len(implemented), 9)
        self.assertTrue(all(item["controller_enabled"] is True for item in implemented))
        self.assertTrue(all(item["build_workflow"] == "build-all-platforms.yml" for item in implemented))

    def test_portable_build_stabilizes_tests_and_uses_a_short_macos_temp_root(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(
            script,
            r'if \[\[ "\$PACKAGE_VARIANT" == macos15 \]\]; then\s+temp_parent=/tmp\s+fi',
        )
        self.assertIn(
            'temp_parent="$(cd "$temp_parent" 2>/dev/null && pwd -P)"', script
        )
        self.assertIn('test_clients=1', script)
        self.assertIn(
            '[[ "$PACKAGE_VARIANT:$PACKAGE_ARCH" == macos15:arm64 ]] && test_clients=2',
            script,
        )
        self.assertIn(
            'test_command=(./runtest --clients "$test_clients" --timeout 1200)', script
        )
        self.assertNotIn('test_command=(make test', script)
        self.assertIn(
            'bash "$PROJECT_ROOT/scripts/run-test-with-one-retry.sh"', script
        )
        helper_call = (
            '  python3 "$UPSTREAM_TEST_FIX_HELPER" \\\n'
            '    --redis-version "$REDIS_VERSION" \\\n'
            '    --source-root "$source_root"'
        )
        self.assertIn(helper_call, script)
        self.assertIn(
            'if [[ "$RUN_FULL_TESTS" == true ]]; then\n'
            '  [[ -f "$UPSTREAM_TEST_FIX_HELPER"',
            script,
        )
        self.assertLess(script.index(helper_call), script.index('cd "$source_root"'))
        self.assertIn(
            Path("packaging/linux/patches/apply_upstream_test_fixes.py"),
            portable_contract.patchset_paths("linux-musl1.2"),
        )
        self.assertIn(
            Path("packaging/linux/patches/redis-8.0-test-tcp-deadlock.patch"),
            portable_contract.patchset_paths("macos15"),
        )
        self.assertIn(
            Path("packaging/linux/patches/redis-8.10.1-hfe-test-timeout.patch"),
            portable_contract.patchset_paths("macos15"),
        )
        self.assertIn(
            Path("packaging/linux/patches/redis-8.2.9-latency-test-timeout.patch"),
            portable_contract.patchset_paths("macos15"),
        )
        self.assertIn(
            Path("packaging/linux/patches/redis-8.8.2-test-stability.patch"),
            portable_contract.patchset_paths("macos15"),
        )
        self.assertNotIn(
            Path("packaging/linux/patches/apply_upstream_test_fixes.py"),
            portable_contract.patchset_paths("windows-msys2"),
        )
        self.assertNotIn(
            Path("packaging/linux/patches/redis-8.10.1-hfe-test-timeout.patch"),
            portable_contract.patchset_paths("windows-msys2"),
        )
        self.assertNotIn(
            Path("packaging/linux/patches/redis-8.2.9-latency-test-timeout.patch"),
            portable_contract.patchset_paths("windows-msys2"),
        )
        self.assertNotIn(
            Path("packaging/linux/patches/redis-8.8.2-test-stability.patch"),
            portable_contract.patchset_paths("windows-msys2"),
        )
        self.assertNotIn('${TMPDIR:-/tmp}/redis-portable', script)
        self.assertIn('"$temp_parent/redis-portable.XXXXXX"', script)

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
        self.assertIn("timeout-minutes: 240", macos_job)

    def test_platform_workflow_runs_release_lifecycle_acceptance(self) -> None:
        workflow = (ROOT / ".github/workflows/build-experimental.yml").read_text(
            encoding="utf-8"
        )
        glibc_job, remainder = workflow.split("\n  glibc217:\n", 1)[1].split(
            "\n  musl:\n", 1
        )
        musl_job, remainder = remainder.split("\n  macos:\n", 1)
        macos_job, windows_job = remainder.split("\n  windows:\n", 1)

        self.assertIn("Test legacy userspace lifecycle without systemd", glibc_job)
        self.assertNotIn("stage=/root/redis-unofficial-lifecycle", glibc_job)
        self.assertIn(
            'stage="$(mktemp -d /tmp/redis-unofficial-lifecycle.XXXXXX)"', glibc_job
        )
        self.assertIn('chmod 0755 "$stage"', glibc_job)
        self.assertIn('"$package/scripts/install.sh" --no-service', glibc_job)
        self.assertIn('redis-cli" -s "$socket" save', glibc_job.lower())
        self.assertIn('"$package/scripts/update.sh"', glibc_job)
        self.assertIn('uninstall.sh" --purge', glibc_job)

        self.assertIn("Test OpenRC install, persistence, recovery, and purge", musl_job)
        self.assertIn('rc-service "$service" restart', musl_job)
        self.assertIn('redis-cli" -s "$socket" save', musl_job.lower())
        self.assertIn('"$package/scripts/update.sh"', musl_job)
        self.assertIn('uninstall.sh" --purge', musl_job)
        self.assertIn("OpenRC recovery changed redis.conf", musl_job)
        self.assertIn("OpenRC recovery did not reload persisted data", musl_job)
        self.assertIn("OpenRC lifecycle failed; collecting diagnostics", musl_job)
        self.assertIn('tail -n 200 "$prefix/log/redis.log"', musl_job)
        self.assertIn("Fault-injected OpenRC update unexpectedly succeeded", musl_job)
        self.assertIn("OpenRC rollback did not restart the previous installation", musl_job)

        self.assertIn("Test launchd install, persistence, recovery, and purge", macos_job)
        self.assertIn("launchctl kickstart -k system/io.github.ainuoyan.redis-unofficial", macos_job)
        self.assertIn('redis-cli" -s "$socket" save', macos_job.lower())
        self.assertIn('"$package/scripts/update.sh"', macos_job)
        self.assertIn('uninstall.sh" --purge', macos_job)
        self.assertIn("Fault-injected launchd update unexpectedly succeeded", macos_job)

        for job in (musl_job, macos_job):
            self.assertIn("Installed config must have exactly one active $key directive", job)
            self.assertIn("s/^port 0$/port 16379/", job)
            self.assertIn("s/^bind .*/bind 127.0.0.1/", job)
            self.assertIn("-h 127.0.0.1 -p 16379 ping", job)
            self.assertIn('config get port | tail -n 1', job)
            self.assertIn('config get bind | tail -n 1', job)

        self.assertIn("Start-RedisServiceBounded", windows_job)
        self.assertIn("Stop-RedisServiceBounded", windows_job)
        self.assertIn("Wait-RedisServiceStatus", windows_job)
        self.assertNotIn("Start-Service -Name RedisUnofficial", windows_job)
        self.assertNotIn("Restart-Service -Name RedisUnofficial", windows_job)
        self.assertIn("redis-unofficial-acceptance-persistence", windows_job)
        self.assertIn("Redis 生命周期 验收-", windows_job)
        self.assertIn("/inheritance:r /grant:r", windows_job)
        self.assertIn("Port-conflict installation unexpectedly succeeded", windows_job)
        self.assertIn("bgsave", windows_job.lower())
        self.assertIn("redis-benchmark.exe", windows_job)
        self.assertIn("redis-sentinel.exe", windows_job)
        self.assertIn("Stop-Process -Id", windows_job)
        self.assertNotIn("Add-Member -NotePropertyName PasswordFile", windows_job)
        self.assertIn("Authenticated Windows redis.conf failed self-test", windows_job)
        self.assertIn("$testPort = 16379", windows_job)
        self.assertIn("$testPort = 16380", windows_job)
        self.assertIn("Get-NetIPAddress -AddressFamily IPv4", windows_job)
        self.assertIn("Installed config must have exactly one active $key directive", windows_job)
        self.assertIn('$configLines[$portRecord.LineNumber - 1] = "port $testPort"', windows_job)
        self.assertIn('$configLines[$bindRecord.LineNumber - 1] = "bind $testAddress"', windows_job)
        self.assertIn("Wait-RedisReady -Password $secret -Address $testAddress -Port $testPort", windows_job)
        self.assertIn("Test configuration generation with Windows PowerShell 5.1", windows_job)
        self.assertIn("./tests/windows/Test-ManagedRedisConfig.ps1", windows_job)
        self.assertIn("include ../conf/service-test.conf", windows_job)
        self.assertIn("Configuration-only changes must allow graceful Windows service restart", windows_job)
        self.assertIn("Fault-injected Windows update unexpectedly succeeded", windows_job)
        self.assertIn("[DateTime]::UtcNow.AddSeconds(90)", windows_job)
        self.assertNotIn("foreach ($attempt in 1..60)", windows_job)

    def test_windows_service_supports_managed_authentication_without_command_line_secrets(self) -> None:
        service = (
            ROOT / "packaging/windows/service/RedisService/Program.cs"
        ).read_text(encoding="utf-8")
        common = WINDOWS_COMMON_SCRIPT.read_text(encoding="utf-8")
        update = (ROOT / "packaging/windows/scripts/Update-Redis.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("RedisConfiguration.Load(PrefixPath())", service)
        self.assertNotIn("RedisService.json", service)
        self.assertNotIn("PasswordFile", service)
        self.assertIn('Environment["REDISCLI_AUTH"] = password', service)
        self.assertIn('Environment.Remove("REDISCLI_AUTH")', service)
        self.assertNotIn('ArgumentList.Add(password)', service)
        self.assertIn('RunRedisCli(settings, "shutdown", out _)', service)
        self.assertIn("Path.IsPathFullyQualified(candidate)", service)
        self.assertIn("Environment.Exit(1);", service)
        self.assertNotIn(
            "ReportStatus(ServiceStopped, acceptedControls: 0, win32ExitCode: 1066",
            service,
        )
        self.assertIn("reports Running only after its authenticated Redis PING passes", common)
        self.assertIn("& sc.exe start $script:RedisServiceName", common)
        self.assertIn("& sc.exe stop $script:RedisServiceName", common)
        self.assertNotIn("Start-Service -Name $script:RedisServiceName", common)
        self.assertIn("$listener.Server.ExclusiveAddressUse = $true", common)
        self.assertIn("function Set-RedisServiceRecovery", common)
        self.assertIn("$service.Status -in $pendingStatuses", common)
        self.assertIn(
            "[ServiceProcess.ServiceControllerStatus]::StartPending", common
        )
        install = (ROOT / "packaging/windows/scripts/Install-Redis.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("Assert-RedisPortAvailable", install)
        self.assertLess(
            install.index("Assert-RedisPortAvailable"),
            install.index("New-Item -ItemType Directory -Path $script:RedisPrefix"),
        )
        creation = common.split("function New-RedisService {", 1)[1].split("function Get-RedisServiceAccount", 1)[0]
        self.assertIn("Set-RedisServiceRecovery", creation)
        self.assertIn("-Credential $credential", creation)
        self.assertIn("Set-RedisServiceAccount -Account $oldAccount", update)
        self.assertNotIn("$sameWrapper", update)
        self.assertIn("Remove-Item -LiteralPath $legacySettings", update)
        self.assertNotIn("Write-RedisServiceSettings", common)
        self.assertNotIn("Write-RedisServiceSettings", install)
        validation = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        self.assertIn("dotnet run --project tests/windows/RedisService.Tests.csproj", validation)
        self.assertIn("./tests/windows/Test-ManagedRedisConfig.ps1", validation)
        self.assertNotIn("Experimental MSYS2", common)

    def test_musl_readiness_requires_a_stable_service(self) -> None:
        common = (ROOT / "packaging/musl/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("consecutive_ready", common)
        self.assertIn("consecutive_ready=$((consecutive_ready + 1))", common)

    def test_portable_service_managers_force_foreground_and_safe_limits(self) -> None:
        openrc = (ROOT / "packaging/musl/openrc/redis").read_text(encoding="utf-8")
        self.assertIn(
            'command_args="/usr/local/redis/conf/redis.conf --daemonize no"',
            openrc,
        )
        self.assertIn('retry="TERM/600/KILL/5"', openrc)

        import plistlib

        with (ROOT / "packaging/macos/launchd/io.github.ainuoyan.redis-unofficial.plist").open(
            "rb"
        ) as handle:
            launchd = plistlib.load(handle)
        self.assertEqual(
            launchd["ProgramArguments"][-2:], ["--daemonize", "no"]
        )
        self.assertNotIn("LimitNOFILE", launchd)
        self.assertEqual(
            launchd["SoftResourceLimits"]["NumberOfFiles"], 65536
        )
        self.assertEqual(
            launchd["HardResourceLimits"]["NumberOfFiles"], 65536
        )

    def test_portable_lifecycle_validates_staging_before_loading_common_code(self) -> None:
        for platform in ("musl", "macos"):
            common = (ROOT / f"packaging/{platform}/scripts/common.sh").read_text(
                encoding="utf-8"
            )
            self.assertIn("validate_package_tree_security", common)
            self.assertLess(
                common.index('validate_package_tree_security "$root"'),
                common.index('metadata_value "$root/PACKAGE-INFO" PACKAGE_STATUS'),
            )
            for name in ("install.sh", "update.sh", "uninstall.sh"):
                script = (ROOT / f"packaging/{platform}/scripts/{name}").read_text(
                    encoding="utf-8"
                )
                with self.subTest(platform=platform, script=name):
                    self.assertTrue(script.startswith("#!/bin/bash -p\n"))
                    self.assertLess(
                        script.index('bootstrap_validate_file "$SCRIPT_DIR/common.sh"'),
                        script.index('source "$SCRIPT_DIR/common.sh"'),
                    )

        common = WINDOWS_COMMON_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Assert-RedisTrustedTree -Path $PackageRoot", common)
        for name in ("Install-Redis.ps1", "Update-Redis.ps1", "Uninstall-Redis.ps1"):
            script = (ROOT / "packaging/windows/scripts" / name).read_text(
                encoding="utf-8"
            )
            with self.subTest(platform="windows", script=name):
                self.assertLess(
                    script.index("Assert-RedisBootstrapAcl -Path $bootstrapPath"),
                    script.index(
                        ". ([IO.Path]::Combine($PSScriptRoot, 'Common-Redis.ps1'))"
                    ),
                )

    def test_windows_update_uses_protected_unpredictable_backups(self) -> None:
        common = WINDOWS_COMMON_SCRIPT.read_text(encoding="utf-8")
        update = (ROOT / "packaging/windows/scripts/Update-Redis.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("function New-RedisBackupDirectory", common)
        self.assertIn("[Guid]::NewGuid().ToString('N')", common)
        self.assertIn("Set-RedisAdministrativeAcl -Path $path", common)
        self.assertIn("Set-RedisAdministrativeTreeAcl -Path $backup", update)
        self.assertIn("Assert-RedisBackupDirectory -Path $backup", update)
        self.assertNotIn("New-Item -ItemType Directory -Path $backup -Force", update)

    def test_runtime_identity_is_bound_to_install_state(self) -> None:
        musl = (ROOT / "packaging/musl/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        macos = (ROOT / "packaging/macos/scripts/common.sh").read_text(
            encoding="utf-8"
        )
        windows = WINDOWS_COMMON_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("printf 'STATE_FORMAT=2", musl)
        self.assertIn("printf 'SERVICE_ID=%s", musl)
        self.assertIn('metadata_value "$REDIS_STATE_FILE" SERVICE_ID', musl)
        self.assertIn("printf 'STATE_FORMAT=2", macos)
        self.assertIn("printf 'SERVICE_ID=%s", macos)
        self.assertIn('metadata_value "$REDIS_STATE_FILE" SERVICE_ID', macos)
        self.assertIn("StateFormat -ne 2", windows)
        self.assertIn("StateFormat = 2", windows)

    def test_musl_mount_check_rejects_target_descendants_and_errors(self) -> None:
        common = ROOT / "packaging/musl/scripts/common.sh"
        command = r'''
source "$1"
findmnt() {
  printf '%s' "$MOCK_FINDMNT_OUTPUT"
  return "$MOCK_FINDMNT_STATUS"
}
refuse_nested_mounts "$2"
'''
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "redis"
            target.mkdir()
            cases = (
                ("0", "", 0),
                ("0", f"{target}-other\n", 0),
                ("0", f"{target}\n", 1),
                ("0", f"{target}\n{target}/data\n", 1),
                ("2", "", 1),
            )
            for status, output, expected in cases:
                with self.subTest(status=status, output=output):
                    result = subprocess.run(
                        ["bash", "-c", command, "bash", str(common), str(target)],
                        env={
                            "MOCK_FINDMNT_STATUS": status,
                            "MOCK_FINDMNT_OUTPUT": output,
                        },
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_portable_recursive_removals_are_mount_guarded(self) -> None:
        for platform, backend in (("musl", "openrc"), ("macos", "launchd")):
            scripts = ROOT / f"packaging/{platform}/scripts"
            install = (scripts / "install.sh").read_text(encoding="utf-8")
            update = (scripts / "update.sh").read_text(encoding="utf-8")
            uninstall = (scripts / "uninstall.sh").read_text(encoding="utf-8")
            with self.subTest(platform=platform, operation="install"):
                self.assertIn('refuse_nested_mounts "$REDIS_PREFIX"', install)
                self.assertLess(
                    install.index('refuse_nested_mounts "$REDIS_PREFIX"'),
                    install.index('rm -rf -- "$REDIS_PREFIX"'),
                )
            managed = (
                f'refuse_nested_mounts "$REDIS_PREFIX/bin" '
                f'"$REDIS_PREFIX/scripts" "$REDIS_PREFIX/{backend}"'
            )
            with self.subTest(platform=platform, operation="update"):
                self.assertIn(managed, update)
                self.assertLess(update.index(managed), update.index("rm -rf --"))
            with self.subTest(platform=platform, operation="uninstall"):
                self.assertIn('refuse_nested_mounts "$REDIS_PREFIX"', uninstall)
                self.assertIn(managed, uninstall)
                self.assertLess(uninstall.index(managed), uninstall.rindex("rm -rf --"))

    def test_windows_build_supports_the_official_runtime_license_location(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('license_root=/usr/share/doc/Cygwin', script)

    def test_windows_scripts_remain_windows_powershell_compatible_text(self) -> None:
        for script in sorted((ROOT / "packaging/windows/scripts").glob("*.ps1")):
            data = script.read_bytes()
            self.assertTrue(data)
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"), script.name)
            self.assertNotIn(b"\xef\xbb\xbf", data[3:])
            data.decode("utf-8-sig")
        for script in sorted((ROOT / "packaging/windows/scripts").glob("*.bat")):
            script.read_bytes().decode("ascii")

    def test_windows_archive_scripts_have_canonical_encoding_and_direct_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = self.create_and_validate(Path(directory), "windows-msys2", "x64")
            with zipfile.ZipFile(archive) as package:
                self.assertIn("redis/scripts/Start-Redis.bat", package.namelist())
                self.assertIn("redis/scripts/Start-Redis.ps1", package.namelist())
                for name in package.namelist():
                    if not name.endswith((".bat", ".ps1")):
                        continue
                    with self.subTest(name=name):
                        data = package.read(name)
                        self.assertIn(b"\r\n", data)
                        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
                        if name.endswith(".ps1"):
                            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
                            self.assertNotIn(b"\xef\xbb\xbf", data[3:])
                            data.decode("utf-8-sig")
                        else:
                            data.decode("ascii")

    def test_windows_script_normalization_is_checkout_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "message.ps1"
            for ending in ("\n", "\r\n"):
                source.write_bytes(("\ufeffWrite-Host '中文'" + ending).encode("utf-8"))
                self.assertEqual(
                    portable_contract.packaged_asset_bytes(source, "windows-msys2", "scripts/message.ps1"),
                    "\ufeffWrite-Host '中文'\r\n".encode("utf-8"),
                )

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
        for signature in ("(void)", "()"):
            with self.subTest(signature=signature), tempfile.TemporaryDirectory() as directory:
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
                    f"void invalidFunctionWasCalled{signature} {{}}\n",
                    encoding="utf-8",
                )

                self.assertEqual(
                    prepare_windows_source.guard_unsupported_dladdr_diagnostics(debug_c),
                    1,
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
                    prepare_windows_source.guard_unsupported_dladdr_diagnostics(debug_c),
                    0,
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
