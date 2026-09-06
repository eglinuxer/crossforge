import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]


class RollbackWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (
            REPOSITORY / ".github/workflows/rollback.yml"
        ).read_text(encoding="utf-8")

    def test_rollback_is_manual_serial_and_never_builds_or_signs(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertNotIn("pull_request:", self.workflow)
        self.assertNotIn("push:\n", self.workflow)
        self.assertIn("group: stable-promotion", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertIn("environment: production", self.workflow)
        self.assertNotIn("docker buildx bake sdk-candidate", self.workflow)
        self.assertNotIn("docker buildx bake source-bundle", self.workflow)
        self.assertNotIn('"$cosign" sign', self.workflow)
        self.assertNotIn("id-token: write", self.workflow)

    def test_only_an_immutable_release_with_exact_assets_is_accepted(self):
        for value in (
            ".immutable == true",
            ".target_commitish | test",
            "git/ref/tags/$tag",
            "git/tags/$object_sha",
            "candidate.json release-promotion.json",
            ".digest == $digest",
            "gh release download",
            "sha256sum -c",
            "release_evidence.py validate",
            "archive-candidate.json",
            "archive-promotion.json",
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.workflow)

    def test_all_live_identity_and_signature_evidence_is_revalidated(self):
        self.assertEqual(
            self.workflow.count(
                "uses: ./.github/actions/validate-public-attestations"
            ),
            2,
        )
        for value in (
            "uses: ./.github/actions/validate-sbom-generator",
            "resolve_candidate_image.py platform-digest",
            "candidate_manifest.py validate",
            "release_promotion.py validate",
            "Match durable attestation reports",
            "--trusted-root",
            '"$cosign" verify',
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.workflow)

    def test_protections_precede_and_only_channels_are_moved(self):
        controls = self.workflow.index(
            "uses: ./.github/actions/validate-release-control-plane"
        )
        self.assertLess(
            controls, self.workflow.index("docker buildx imagetools create")
        )
        self.assertNotIn('--tag "$candidate_version"', self.workflow)
        self.assertNotIn('--tag "$source_version"', self.workflow)
        source = self.workflow.index('--tag "$source_channel"')
        candidate = self.workflow.index('--tag "$candidate_channel"')
        self.assertLess(source, candidate)
        self.assertIn(
            "no rebuild or version-tag mutation", self.workflow
        )


if __name__ == "__main__":
    unittest.main()
