from __future__ import annotations

import hashlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.release.package_fixture import (
    CONTRIBUTOR_LICENSE,
    DEPENDENCY_NOTICES,
    PATCHSET_SHA256,
    REVISION,
    SOURCE_SHA256,
    VARIANT,
    VERSION,
    write_package,
)


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/release"))
import validate_release_asset as validator  # noqa: E402


class ValidateReleaseAssetTests(unittest.TestCase):
    def validate(self, archive: Path, checksum: Path, arch: str) -> None:
        validator.validate_checksum(archive, checksum)
        values, _, build_info = validator.read_package_info(archive)
        validator.validate_metadata(
            values,
            version=VERSION,
            variant=VARIANT,
            arch=arch,
            source_sha256=SOURCE_SHA256,
            min_glibc="2.28",
        )
        validator.validate_build_info(
            build_info,
            version=VERSION,
            variant=VARIANT,
            arch=arch,
            source_sha256=SOURCE_SHA256,
            patchset_sha256=values["PATCHSET_SHA256"],
            packaging_revision=REVISION,
        )

    def test_valid_package_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, checksum = write_package(Path(directory), "x64")
            self.validate(archive, checksum, "x64")

    def test_version_and_glibc_numeric_components_are_bounded(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "version"):
            validator.parse_version("1000000.0.0")
        with self.assertRaisesRegex(validator.ValidationError, "glibc"):
            validator.parse_glibc("1000000.0")

    def test_dependency_notices_are_required_and_strictly_framed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = write_package(
                Path(directory), "x64", dependency_notices=None
            )
            with self.assertRaisesRegex(validator.ValidationError, "dependency notices"):
                validator.read_package_info(archive)

        with tempfile.TemporaryDirectory() as directory:
            malformed = DEPENDENCY_NOTICES.replace(b"===== END", b"===== WRONG", 1)
            archive, _ = write_package(
                Path(directory), "x64", dependency_notices=malformed
            )
            with self.assertRaisesRegex(validator.ValidationError, "framing|marker"):
                validator.read_package_info(archive)

        noncanonical_path = (
            b"UPSTREAM_DEPENDENCY_NOTICES_FORMAT=1\n"
            b"REDIS_VERSION=7.4.11\n"
            b"SOURCE_SUBTREE=deps\n\n"
            b"===== BEGIN deps/../LICENSE (1 bytes) =====\n"
            b"x\n"
            b"===== END deps/../LICENSE =====\n"
        )
        with self.assertRaisesRegex(validator.ValidationError, "noncanonical path"):
            validator.validate_upstream_notice_payloads(
                noncanonical_path,
                CONTRIBUTOR_LICENSE,
                version="7.4.11",
                dependency_notices_sha256=hashlib.sha256(
                    noncanonical_path
                ).hexdigest(),
                contributor_license_sha256=hashlib.sha256(
                    CONTRIBUTOR_LICENSE
                ).hexdigest(),
            )

    def test_dependency_notice_size_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = write_package(
                Path(directory),
                "x64",
                dependency_notices=b"x" * (
                    validator.MAX_DEPENDENCY_NOTICES_BYTES + 1
                ),
            )
            with self.assertRaisesRegex(validator.ValidationError, "size-bounded"):
                validator.read_package_info(archive)

        oversized_integer = (
            b"UPSTREAM_DEPENDENCY_NOTICES_FORMAT=1\n"
            b"REDIS_VERSION=7.4.11\n"
            b"SOURCE_SUBTREE=deps\n\n"
            b"===== BEGIN deps/LICENSE ("
            + b"9" * 5000
            + b" bytes) =====\n"
        )
        with self.assertRaisesRegex(validator.ValidationError, "declared size"):
            validator.validate_upstream_notice_payloads(
                oversized_integer,
                CONTRIBUTOR_LICENSE,
                version="7.4.11",
                dependency_notices_sha256=hashlib.sha256(
                    oversized_integer
                ).hexdigest(),
                contributor_license_sha256=hashlib.sha256(
                    CONTRIBUTOR_LICENSE
                ).hexdigest(),
            )

    def test_contributor_license_is_version_aware_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = write_package(
                Path(directory), "x64", contributor_license=None
            )
            with self.assertRaisesRegex(validator.ValidationError, "contributor"):
                validator.read_package_info(archive)

        legacy_notices = DEPENDENCY_NOTICES.replace(b"7.4.11", b"7.2.16")
        validator.validate_upstream_notice_payloads(
            legacy_notices,
            None,
            version="7.2.16",
            dependency_notices_sha256=hashlib.sha256(legacy_notices).hexdigest(),
            contributor_license_sha256="absent",
        )

        with tempfile.TemporaryDirectory() as directory:
            archive, _ = write_package(
                Path(directory),
                "x64",
                contributor_license=CONTRIBUTOR_LICENSE,
                declared_contributor_license_sha256="0" * 64,
            )
            with self.assertRaisesRegex(validator.ValidationError, "contributor license"):
                validator.read_package_info(archive)

    def test_checksum_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, checksum = write_package(Path(directory), "x64")
            checksum.write_text(f"{'0' * 64}  {archive.name}\n", encoding="ascii")
            with self.assertRaisesRegex(validator.ValidationError, "does not match"):
                validator.validate_checksum(archive, checksum)

    def test_noncanonical_alias_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = write_package(
                Path(directory),
                "x64",
                extra_member=("redis/bin/./redis-server", b"replacement\n", 0o755),
            )
            with self.assertRaisesRegex(validator.ValidationError, "unsafe|noncanonical"):
                validator.read_package_info(archive)

    def test_setuid_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = write_package(Path(directory), "x64", server_mode=0o4755)
            with self.assertRaisesRegex(validator.ValidationError, "mode"):
                validator.read_package_info(archive)

    def test_member_xattr_pax_header_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = write_package(
                Path(directory),
                "x64",
                server_pax_headers={
                    "SCHILY.xattr.security.capability": "malicious-capability"
                },
            )
            with self.assertRaisesRegex(validator.ValidationError, "PAX"):
                validator.read_package_info(archive)

    def test_gnu_extension_header_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = write_package(
                Path(directory),
                "x64",
                archive_format=tarfile.GNU_FORMAT,
                extra_member=(f"redis/{'x' * 120}", b"unexpected\n", 0o644),
            )
            with self.assertRaisesRegex(validator.ValidationError, "extension header"):
                validator.read_package_info(archive)

    def test_build_info_patchset_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = write_package(
                Path(directory),
                "x64",
                build_patchset_sha256="d" * 64,
            )
            values, _, build_info = validator.read_package_info(archive)
            with self.assertRaisesRegex(validator.ValidationError, "patch-set"):
                validator.validate_build_info(
                    build_info,
                    version=VERSION,
                    variant=VARIANT,
                    arch="x64",
                    source_sha256=SOURCE_SHA256,
                    patchset_sha256=values["PATCHSET_SHA256"],
                    packaging_revision=REVISION,
                )

    def test_packaging_revision_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = write_package(Path(directory), "x64", revision="d" * 40)
            _, _, build_info = validator.read_package_info(archive)
            with self.assertRaisesRegex(validator.ValidationError, "revision"):
                validator.validate_build_info(
                    build_info,
                    version=VERSION,
                    variant=VARIANT,
                    arch="x64",
                    source_sha256=SOURCE_SHA256,
                    patchset_sha256=PATCHSET_SHA256,
                    packaging_revision=REVISION,
                )

    def test_packaging_patchset_is_recomputed_from_reviewed_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_names = set(validator.PATCHSET_FIXED_PATHS) | set(
                validator.PACKAGING_BINDINGS.values()
            )
            for source_name in source_names:
                path = root / source_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture\n")

            patchset = validator.packaging_patchset_sha256(root)
            shell_patchset = subprocess.run(
                [
                    "bash",
                    "-c",
                    """
                    set -euo pipefail
                    {
                      printf '%s\\0' \\
                        .github/workflows/build-linux.yml \\
                        scripts/linux/build-redis.sh \\
                        THIRD_PARTY_NOTICES.md
                      find packaging/linux -type f -print0
                    } | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum \\
                      | awk '{print $1}'
                    """,
                ],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            self.assertEqual(patchset, shell_patchset)
            archive, _ = write_package(root, "x64", patchset_sha256=patchset)
            self.assertEqual(
                validator.validate_packaging_bindings(archive, root, patchset),
                patchset,
            )

            (root / "scripts/linux/build-redis.sh").write_bytes(b"changed\n")
            with self.assertRaisesRegex(validator.ValidationError, "patch-set"):
                validator.validate_packaging_bindings(archive, root, patchset)


if __name__ == "__main__":
    unittest.main()
