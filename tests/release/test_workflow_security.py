from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/build-linux.yml"
EXPERIMENTAL_WORKFLOW_PATH = ROOT / ".github/workflows/build-experimental.yml"
LINUX_BUILD_SCRIPT_PATH = ROOT / "scripts/linux/build-redis.sh"
LINUX_UPDATE_SCRIPT_PATH = ROOT / "packaging/linux/scripts/update.sh"
REVIEWED_NODE24_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
}


class WorkflowSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.experimental_workflow = EXPERIMENTAL_WORKFLOW_PATH.read_text(
            encoding="utf-8"
        )
        cls.linux_build_script = LINUX_BUILD_SCRIPT_PATH.read_text(
            encoding="utf-8"
        )
        cls.linux_update_script = LINUX_UPDATE_SCRIPT_PATH.read_text(
            encoding="utf-8"
        )

    def test_all_actions_are_pinned_to_commit_shas(self) -> None:
        workflow_paths = sorted((ROOT / ".github/workflows").glob("*.yml"))
        self.assertGreaterEqual(len(workflow_paths), 3)
        for workflow_path in workflow_paths:
            workflow = workflow_path.read_text(encoding="utf-8")
            actions = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, re.MULTILINE)
            self.assertTrue(actions, workflow_path.name)
            for action in actions:
                self.assertRegex(
                    action,
                    r"^[^@]+@[0-9a-f]{40}$",
                    f"unpinned action in {workflow_path.name}",
                )

    def test_core_actions_use_reviewed_node24_releases(self) -> None:
        found: set[str] = set()
        for workflow_path in sorted((ROOT / ".github/workflows").glob("*.yml")):
            workflow = workflow_path.read_text(encoding="utf-8")
            for action, revision in re.findall(
                r"^\s*uses:\s*([^@\s#]+)@([0-9a-f]{40})", workflow, re.MULTILINE
            ):
                if action not in REVIEWED_NODE24_ACTIONS:
                    continue
                found.add(action)
                self.assertEqual(
                    revision,
                    REVIEWED_NODE24_ACTIONS[action],
                    f"unreviewed {action} revision in {workflow_path.name}",
                )
        self.assertEqual(found, set(REVIEWED_NODE24_ACTIONS))

    def test_release_requires_protected_default_branch_and_environment(self) -> None:
        self.assertIn("github.ref_protected == true", self.workflow)
        self.assertIn("github.event.repository.default_branch", self.workflow)
        self.assertIn("environment:\n      name: release", self.workflow)
        self.assertIn("artifact-metadata: write", self.workflow)
        self.assertIn("id-token: write", self.workflow)
        self.assertIn("attestations: write", self.workflow)

    def test_release_policy_and_graphql_fail_closed_are_wired(self) -> None:
        self.assertIn("validate_publish_policy.py", self.workflow)
        self.assertIn("validate_configs.py", self.workflow)
        self.assertIn("if (NF == 5)", self.workflow)
        self.assertIn("Malformed official checksum record", self.workflow)
        self.assertGreaterEqual(
            self.workflow.count("((.errors // []) | length) == 0"), 2
        )
        self.assertIn("Existing Redis Release is incomplete", self.workflow)
        self.assertIn("existing-release-assets/manifest.json", self.workflow)
        self.assertIn('"$tag_revision" != "$release_revision"', self.workflow)
        self.assertIn(
            "A compatible draft Release will be verified and resumed",
            self.workflow,
        )
        self.assertIn("A draft Release can only be resumed", self.workflow)
        self.assertNotIn("gh release delete", self.workflow)
        self.assertNotIn('case "${{ matrix.', self.workflow)
        self.assertNotIn("v=${{ needs.prepare.outputs.version }}", self.workflow)
        self.assertNotIn("gh release view", self.workflow)

    def test_direct_build_uses_an_immutable_hashes_snapshot(self) -> None:
        self.assertIn("commits/${hashes_ref}", self.workflow)
        self.assertIn("contents/${hashes_path}?ref=${hashes_commit}", self.workflow)
        self.assertIn("hashes_commit: ${{ steps.resolve.outputs.hashes_commit }}", self.workflow)
        self.assertIn("REDIS_HASHES_COMMIT", self.workflow)
        self.assertIn("[0-9]{0,5}", self.workflow)

    def test_linux_build_has_time_for_one_complete_serial_test_retry(self) -> None:
        build_job = self.workflow.split("\n  build:\n", 1)[1].split(
            "\n  service_test:\n", 1
        )[0]
        self.assertIn("timeout-minutes: 90", build_job)
        self.assertIn("./runtest --clients 1 --timeout 1200", self.linux_build_script)
        self.assertIn(
            'bash "$PROJECT_ROOT/scripts/run-test-with-one-retry.sh"',
            self.linux_build_script,
        )

    def test_rocky_build_uses_python_311_for_asset_validation(self) -> None:
        build_job = self.workflow.split("\n  build:\n", 1)[1].split(
            "\n  service_test:\n", 1
        )[0]
        self.assertIn("            python3.11 \\\n", build_job)
        self.assertIn(
            "          python3.11 scripts/release/validate_release_asset.py",
            build_job,
        )
        self.assertNotIn(
            "          python3 scripts/release/validate_release_asset.py",
            build_job,
        )

    def test_release_is_a_verified_resumable_draft_published_once(self) -> None:
        self.assertIn("before-create.json", self.workflow)
        self.assertIn("pre-tag-state.json", self.workflow)
        self.assertIn("pre-publish-state.json", self.workflow)
        self.assertIn("Draft Release changed during verification", self.workflow)
        self.assertIn("expected-asset-content.json", self.workflow)
        self.assertIn("draft-asset-records.json", self.workflow)
        self.assertIn("pre-publish-asset-records.json", self.workflow)
        self.assertIn("published-asset-records.json", self.workflow)
        self.assertIn("sha256:${asset_sha256}", self.workflow)
        self.assertIn("{id, name, size, digest}", self.workflow)
        self.assertGreaterEqual(
            self.workflow.count("fetch_release_asset_records"), 4
        )
        self.assertIn("release == null and .data.repository.ref == null", self.workflow)
        self.assertIn("gh release create", self.workflow)
        self.assertIn("--draft", self.workflow)
        self.assertIn(
            "Reusing an existing verified draft after an interrupted publication",
            self.workflow,
        )
        self.assertIn(".data.repository.ref == null or", self.workflow)
        self.assertIn('"repos/${GITHUB_REPOSITORY}/git/refs"', self.workflow)
        self.assertIn('-f ref="$qualified_tag"', self.workflow)
        self.assertIn('-f sha="$GITHUB_SHA"', self.workflow)
        self.assertIn("create-tag-response.json", self.workflow)
        self.assertIn("pre-publish-release.json", self.workflow)
        self.assertIn(".target_commitish == $revision", self.workflow)
        self.assertIn("gh release download", self.workflow)
        self.assertIn("release_metadata.py validate", self.workflow)
        self.assertIn('gh api --method PATCH', self.workflow)
        self.assertIn('"repos/${GITHUB_REPOSITORY}/releases/${draft_id}"', self.workflow)
        self.assertIn("-F draft=false", self.workflow)
        self.assertIn("-f make_latest=false", self.workflow)
        self.assertIn("publish-response.json", self.workflow)
        self.assertIn("published-readback-assets", self.workflow)
        self.assertNotIn("gh release edit", self.workflow)
        self.assertIn("published_assets", self.workflow)
        self.assertIn(".data.repository.ref.target.oid == $revision", self.workflow)
        self.assertNotIn("gh release upload", self.workflow)
        self.assertNotIn("--clobber", self.workflow)
        self.assertNotIn("gh release delete", self.workflow)
        self.assertNotIn("--method DELETE", self.workflow)

    def test_release_metadata_and_attestations_are_current(self) -> None:
        self.assertIn("manifest.json", self.workflow)
        self.assertIn("SHA256SUMS", self.workflow)
        self.assertIn(".spdx.json", self.workflow)
        pinned_attest = (
            "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6"
        )
        self.assertEqual(self.workflow.count(pinned_attest), 2)
        self.assertIn("sbom-path:", self.workflow)
        self.assertGreaterEqual(self.workflow.count("gh attestation verify"), 3)
        self.assertIn("https://slsa.dev/provenance/v1", self.workflow)
        self.assertIn("https://spdx.dev/Document/v2.3", self.workflow)
        self.assertIn("--cert-identity", self.workflow)
        self.assertIn("--signer-digest", self.workflow)
        self.assertIn("--source-digest", self.workflow)
        self.assertIn("--deny-self-hosted-runners", self.workflow)
        self.assertIn("for asset in published-assets/*", self.workflow)

    def test_release_notes_include_both_attestation_verification_commands(self) -> None:
        notes = self.workflow.split("- name: Prepare release notes", 1)[1].split(
            "- name: Create verified draft and publish once", 1
        )[0]
        self.assertEqual(notes.count("gh attestation verify"), 2)
        self.assertIn("https://slsa.dev/provenance/v1", notes)
        self.assertIn("https://spdx.dev/Document/v2.3", notes)
        self.assertIn("--repo ${GITHUB_REPOSITORY}", notes)
        self.assertEqual(notes.count("--deny-self-hosted-runners"), 2)

    def test_release_notes_describe_no_service_as_a_managed_install(self) -> None:
        notes = self.workflow.split("- name: Prepare release notes", 1)[1].split(
            "- name: Create verified draft and publish once", 1
        )[0]
        self.assertIn("complete managed installation", notes)
        self.assertIn("完整受管安装", notes)
        self.assertIn("/usr/local/redis/bin/redis-server", notes)
        self.assertNotIn("binary-only", notes)
        self.assertNotIn("仅安装程序", notes)

    def test_upstream_build_is_unprivileged_with_read_only_packaging(self) -> None:
        self.assertIn("Create unprivileged builder", self.workflow)
        self.assertIn("/opt/redis-packaging", self.workflow)
        self.assertIn("chmod -R go-w", self.workflow)
        self.assertIn("setpriv", self.workflow)
        self.assertIn("--no-new-privs", self.workflow)
        self.assertIn("env -i", self.workflow)

    def test_experimental_linux_builds_resolve_numeric_setpriv_ids(self) -> None:
        workflow = self.experimental_workflow
        self.assertEqual(
            workflow.count('builder_uid="$(id -u redis-builder)"'), 2
        )
        self.assertEqual(
            workflow.count('builder_gid="$(id -g redis-builder)"'), 2
        )
        self.assertEqual(workflow.count('--reuid "$builder_uid"'), 2)
        self.assertEqual(workflow.count('--regid "$builder_gid"'), 2)
        self.assertNotIn("--reuid redis-builder", workflow)
        self.assertNotIn("--regid redis-builder", workflow)

    def test_musl_tests_use_procps_process_detection(self) -> None:
        musl_job = self.experimental_workflow.split("  musl:\n", 1)[1].split(
            "\n  macos:\n", 1
        )[0]
        self.assertIn("make musl-dev procps python3", musl_job)

    def test_glibc217_build_preserves_only_the_pinned_manylinux_toolchain(self) -> None:
        glibc_job = self.experimental_workflow.split("  glibc217:\n", 1)[1].split(
            "\n  musl:\n", 1
        )[0]
        self.assertIn(
            'toolchain_root="/opt/rh/devtoolset-10/root"', glibc_job
        )
        self.assertIn('[[ -x "$toolchain_root/usr/bin/cc"', glibc_job)
        self.assertIn(
            'PATH="$toolchain_root/usr/bin:/usr/local/sbin:', glibc_job
        )
        self.assertIn(
            'LD_LIBRARY_PATH="$toolchain_root/usr/lib64:', glibc_job
        )
        self.assertNotIn('PATH="$PATH"', glibc_job)
        self.assertNotIn('LD_LIBRARY_PATH="$LD_LIBRARY_PATH"', glibc_job)

    def test_build_info_records_only_installed_rpm_dependencies(self) -> None:
        dependency_report = self.linux_build_script.split(
            '    echo "Selected build dependency packages:"\n', 1
        )[1].split("  fi\n", 1)[0]
        self.assertIn("for dependency_package in", dependency_report)
        self.assertIn(
            'if rpm -q "$dependency_package" >/dev/null 2>&1; then',
            dependency_report,
        )
        self.assertIn("gcc-c++", dependency_report)
        self.assertIn("devtoolset-10-gcc", dependency_report)
        self.assertNotIn("python3", dependency_report)

    def test_stable_linux_build_installs_cpp_compiler(self) -> None:
        dependency_step = self.workflow.split(
            "      - name: Install build dependencies\n", 1
        )[1].split("\n      - name: Checkout packaging project\n", 1)[0]
        self.assertIn("gcc-c++", dependency_step)

    def test_service_gate_covers_local_socket_expiry_and_quoted_paths(self) -> None:
        self.assertIn("Fresh installation unexpectedly exposed TCP", self.workflow)
        self.assertIn("bind 127.0.0.1", self.workflow)
        self.assertIn("redis-unofficial-expiry-test", self.workflow)
        self.assertIn('dir \"/usr/local/redis/custom data-数据\"', self.workflow)
        self.assertIn("redis.sock", self.workflow)

        install_step = self.workflow.split(
            "      - name: Test installation and service\n", 1
        )[1].split("\n      - name: Test update and data preservation", 1)[0]
        custom_dir_config = install_step.index(
            "'s|^dir /usr/local/redis/data$|dir "
        )
        persisted_data = install_step.index(
            "redis_cli SET redis-unofficial-service-test preserved"
        )
        self.assertLess(custom_dir_config, persisted_data)
        self.assertNotIn(
            '/usr/local/redis/data/dump.rdb "$custom_data_dir/dump.rdb"',
            install_step,
        )
        self.assertIn(
            'sudo test -f "$custom_data_dir/dump.rdb"', install_step
        )
        update_step = self.workflow.split(
            "      - name: Test update and data preservation\n", 1
        )[1].split("\n      - name: Test failed-update rollback", 1)[0]
        self.assertIn(
            "sudo test -f '/usr/local/redis/custom data-数据/dump.rdb'",
            update_step,
        )
        self.assertIn(
            "Service was not active after the update.", self.workflow
        )
        self.assertIn(
            "Redis data was not preserved by the update.", self.workflow
        )

        rollback_step = self.workflow.split(
            "      - name: Test failed-update rollback\n", 1
        )[1].split("\n      - name: Test uninstall, reinstall, and purge", 1)[0]
        self.assertIn(
            "sudo test -f '/usr/local/redis/custom data-数据/dump.rdb'",
            rollback_step,
        )
        uninstall_step = self.workflow.split(
            "      - name: Test uninstall, reinstall, and purge\n", 1
        )[1].split("\n\n  release:", 1)[0]
        self.assertIn(
            "sudo test -f /usr/local/redis/conf/redis.conf", uninstall_step
        )
        self.assertIn(
            "sudo test -f '/usr/local/redis/custom data-数据/dump.rdb'",
            uninstall_step,
        )

    def test_failed_update_resets_systemd_failure_before_restart(self) -> None:
        rollback = self.linux_update_script.split(
            "rollback_update() {", 1
        )[1].split("\ntrap 'rollback_update $?' ERR", 1)[0]
        reset_failed = rollback.index(
            'systemctl reset-failed "$REDIS_SERVICE_NAME"'
        )
        restart = rollback.index('systemctl start "$REDIS_SERVICE_NAME"')
        self.assertLess(reset_failed, restart)


if __name__ == "__main__":
    unittest.main()
