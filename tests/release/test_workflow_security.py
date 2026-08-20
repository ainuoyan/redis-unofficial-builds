from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/build-linux.yml"


class WorkflowSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

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
        self.assertNotIn("gh release view", self.workflow)

    def test_direct_build_uses_an_immutable_hashes_snapshot(self) -> None:
        self.assertIn("commits/${hashes_ref}", self.workflow)
        self.assertIn("contents/${hashes_path}?ref=${hashes_commit}", self.workflow)
        self.assertIn("hashes_commit: ${{ steps.resolve.outputs.hashes_commit }}", self.workflow)
        self.assertIn("REDIS_HASHES_COMMIT", self.workflow)
        self.assertIn("[0-9]{0,5}", self.workflow)

    def test_release_is_a_new_verified_draft_published_once(self) -> None:
        self.assertIn("before-create.json", self.workflow)
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

    def test_service_gate_covers_local_socket_expiry_and_quoted_paths(self) -> None:
        self.assertIn("Fresh installation unexpectedly exposed TCP", self.workflow)
        self.assertIn("bind 127.0.0.1", self.workflow)
        self.assertIn("redis-unofficial-expiry-test", self.workflow)
        self.assertIn('dir \"/usr/local/redis/custom data-数据\"', self.workflow)
        self.assertIn("redis.sock", self.workflow)


if __name__ == "__main__":
    unittest.main()
