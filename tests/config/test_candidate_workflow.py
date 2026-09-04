import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]


class CandidateWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (
            REPOSITORY / ".github/workflows/candidate.yml"
        ).read_text(encoding="utf-8")
        cls.ci = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        cls.setup = (
            REPOSITORY / ".github/actions/setup-locked-buildx/action.yml"
        ).read_text(encoding="utf-8")

    def test_candidate_is_manual_public_digest_only_output(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertNotIn("pull_request:", self.workflow)
        self.assertNotIn("push:\n", self.workflow)
        self.assertIn("packages: write", self.workflow)
        self.assertIn("sdk-candidate.output=type=image,push=true", self.workflow)
        self.assertIn("--provenance=mode=max", self.workflow)
        self.assertIn("--sbom=true", self.workflow)
        self.assertNotIn("sdk-complete-dev.output", self.workflow)
        self.assertNotIn(":gts15-el8", self.workflow)
        self.assertNotIn(":v0.1.0", self.workflow)

    def test_tag_and_manifest_bind_run_attempt_source_and_both_digests(self):
        for value in (
            '"$GITHUB_SHA"',
            '"$GITHUB_RUN_ID"',
            '"$GITHUB_RUN_ATTEMPT"',
            '"$candidate_digest"',
            '"$platform_digest"',
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.workflow)
        self.assertIn("candidate_manifest.py create", self.workflow)
        self.assertIn("candidate_manifest.py validate", self.workflow)
        self.assertIn("resolve_candidate_image.py buildx-digest", self.workflow)
        self.assertIn("resolve_candidate_image.py platform-digest", self.workflow)

    def test_public_availability_is_checked_without_registry_credentials(self):
        logout = self.workflow.index("docker logout ghcr.io")
        anonymous = self.workflow.index("anonymous-candidate-index.json")
        upload = self.workflow.index("actions/upload-artifact@")
        self.assertLess(logout, anonymous)
        self.assertLess(anonymous, upload)
        self.assertIn("if-no-files-found: error", self.workflow)
        self.assertIn("retention-days: 90", self.workflow)

    def test_ci_and_candidate_share_one_locked_buildx_setup(self):
        local_action = "uses: ./.github/actions/setup-locked-buildx"
        self.assertEqual(self.ci.count(local_action), 1)
        self.assertEqual(self.workflow.count(local_action), 1)
        self.assertIn("buildx-v0.36.1.linux-amd64", self.setup)
        self.assertIn(
            "48af8a397ebd60178778bf63611dbcebe5f5e7a9be90eb9147b24b9587455778",
            self.setup,
        )
        self.assertIn(
            "moby/buildkit:v0.32.2@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8",
            self.setup,
        )
        self.assertIn(
            "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
            self.setup,
        )


if __name__ == "__main__":
    unittest.main()
