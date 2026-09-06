import ast
import copy
import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/release_promotion.py"
PROMOTION = runpy.run_path(str(SCRIPT))
CANDIDATE = runpy.run_path(str(REPOSITORY / "scripts/candidate_manifest.py"))


class ReleasePromotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = PROMOTION["load_release"](
            REPOSITORY / "config/release.json",
            REPOSITORY / "config/schemas/release.schema.json",
        )
        cls.schema = PROMOTION["load_schema"](
            REPOSITORY / "config/schemas/release-promotion.schema.json"
        )
        cls.commit = "1" * 40
        cls.candidate = CANDIDATE["candidate_document"](
            cls.release,
            cls.commit,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            "sha256:" + "4" * 64,
            "sha256:" + "5" * 64,
            {
                "source_commit": cls.commit,
                "release_sha256": CANDIDATE["canonical_sha256"](cls.release),
                "archive": {
                    "file": "crossforge-source-%s.tar.zst" % cls.commit,
                    "sha256": "6" * 64,
                    "size": 2866173957,
                },
            },
        )

    def run_metadata(self):
        return {
            "id": 123456,
            "run_attempt": 2,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": self.commit,
            "path": ".github/workflows/candidate.yml",
            "repository": {"full_name": "eglinuxer/crossforge"},
            "head_repository": {"full_name": "eglinuxer/crossforge"},
            "html_url": (
                "https://github.com/eglinuxer/crossforge/actions/runs/123456"
            ),
        }

    def document(self, run=None, candidate=None):
        run = run or self.run_metadata()
        candidate = candidate or self.candidate
        validated_run = PROMOTION["validate_candidate_run"](
            run, "eglinuxer/crossforge", 123456
        )
        candidate_sha256 = CANDIDATE["canonical_sha256"](candidate)
        return PROMOTION["promotion_document"](
            self.release,
            candidate,
            candidate_sha256,
            validated_run,
        )

    def test_exact_successful_candidate_run_forms_digest_only_promotion(self):
        document = self.document()
        self.assertNotIn("promotion_run", document)
        self.assertIs(
            PROMOTION["validate_document"](
                document, self.release, self.schema
            ),
            document,
        )
        self.assertEqual(
            document["candidate"]["version_reference"],
            "ghcr.io/eglinuxer/crossforge:v0.1.0",
        )
        self.assertEqual(
            document["candidate"]["channel_reference"],
            "ghcr.io/eglinuxer/crossforge:gts15-el8",
        )
        self.assertEqual(
            document["source_bundle"]["version_reference"],
            "ghcr.io/eglinuxer/crossforge:source-v0.1.0",
        )
        self.assertEqual(
            document["source_bundle"]["channel_reference"],
            "ghcr.io/eglinuxer/crossforge:source-gts15-el8",
        )
        self.assertEqual(
            document["candidate_manifest_sha256"],
            CANDIDATE["canonical_sha256"](self.candidate),
        )
        self.assertEqual(
            document["artifacts"],
            [
                "candidate-identity-123456-2",
                "candidate-signature-123456-2",
                "native-aarch64-evidence-123456-2",
                "native-aarch64-probes-123456-2",
            ],
        )

    def test_run_metadata_fails_closed(self):
        mutations = (
            lambda run: run.__setitem__("id", 123457),
            lambda run: run.__setitem__("run_attempt", 0),
            lambda run: run.__setitem__("event", "push"),
            lambda run: run.__setitem__("status", "in_progress"),
            lambda run: run.__setitem__("conclusion", "failure"),
            lambda run: run.__setitem__("head_branch", "feature"),
            lambda run: run.__setitem__("path", ".github/workflows/ci.yml"),
            lambda run: run["repository"].__setitem__(
                "full_name", "example/crossforge"
            ),
            lambda run: run["head_repository"].__setitem__(
                "full_name", "example/crossforge"
            ),
            lambda run: run.__setitem__("head_sha", "0" * 39),
            lambda run: run.__setitem__(
                "html_url", "https://github.com/eglinuxer/crossforge/actions/runs/1"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                run = self.run_metadata()
                mutate(run)
                with self.assertRaises(PROMOTION["PromotionError"]):
                    PROMOTION["validate_candidate_run"](
                        run, "eglinuxer/crossforge", 123456
                    )

    def test_candidate_must_be_the_candidate_run_head(self):
        validated_run = PROMOTION["validate_candidate_run"](
            self.run_metadata(), "eglinuxer/crossforge", 123456
        )
        candidate = copy.deepcopy(self.candidate)
        candidate["source_commit"] = "8" * 40
        candidate["source_bundle"]["archive"]["file"] = (
            "crossforge-source-%s.tar.zst" % ("8" * 40)
        )
        with self.assertRaisesRegex(
            PROMOTION["PromotionError"], "candidate source commit differs"
        ):
            PROMOTION["promotion_document"](
                self.release,
                candidate,
                CANDIDATE["canonical_sha256"](candidate),
                validated_run,
            )

    def test_schema_and_semantics_reject_tag_or_artifact_drift(self):
        mutations = (
            lambda value: value["candidate"].__setitem__(
                "version_reference", "ghcr.io/eglinuxer/crossforge:v0.1.1"
            ),
            lambda value: value["source_bundle"].__setitem__(
                "channel_reference", "ghcr.io/eglinuxer/crossforge:source-latest"
            ),
            lambda value: value["artifacts"].pop(),
            lambda value: value["verification"].__setitem__(
                "registry", "trusted-login"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                document = self.document()
                mutate(document)
                with self.assertRaises(
                    (PROMOTION["PromotionError"], PROMOTION["ValidationError"])
                ):
                    PROMOTION["validate_document"](
                        document, self.release, self.schema
                    )

    def test_cli_create_and_validate_are_strict_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate.json"
            candidate_run = root / "candidate-run.json"
            output = root / "promotion.json"
            candidate.write_text(
                json.dumps(self.candidate, sort_keys=True), encoding="utf-8"
            )
            candidate_run.write_text(
                json.dumps(self.run_metadata(), sort_keys=True), encoding="utf-8"
            )
            command = [
                str(SCRIPT),
                "create",
                "--candidate",
                str(candidate),
                "--candidate-run",
                str(candidate_run),
                "--expected-github-repository",
                "eglinuxer/crossforge",
                "--expected-candidate-run-id",
                "123456",
                "--output",
                str(output),
            ]
            for expected in ("wrote", "current"):
                result = subprocess.run(
                    command,
                    cwd=str(REPOSITORY),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected + " release promotion evidence", result.stdout)
            result = subprocess.run(
                [str(SCRIPT), "validate", str(output)],
                cwd=str(REPOSITORY),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("valid release promotion evidence", result.stdout)

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


class PromotionWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (
            REPOSITORY / ".github/workflows/promote.yml"
        ).read_text(encoding="utf-8")

    def test_promotion_is_manual_serial_and_never_rebuilds(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertNotIn("pull_request:", self.workflow)
        self.assertNotIn("push:\n", self.workflow)
        self.assertIn("group: stable-promotion", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertIn("environment: production", self.workflow)
        self.assertNotIn("docker buildx bake sdk-candidate", self.workflow)
        self.assertNotIn("docker buildx bake source-bundle", self.workflow)
        self.assertNotIn("id-token: write", self.workflow)
        self.assertNotIn('"$cosign" sign', self.workflow)
        self.assertIn("docker buildx imagetools create", self.workflow)

    def test_exact_successful_candidate_run_and_artifacts_are_required(self):
        for value in (
            '.path == ".github/workflows/candidate.yml"',
            '.conclusion == "success"',
            '.head_branch == "main"',
            "candidate-identity-${{ inputs.candidate_run_id }}-",
            "native-aarch64-probes-${{ inputs.candidate_run_id }}-",
            "native-aarch64-evidence-${{ inputs.candidate_run_id }}-",
            "candidate-signature-${{ inputs.candidate_run_id }}-",
            "github-token: ${{ secrets.GITHUB_TOKEN }}",
            "run-id: ${{ inputs.candidate_run_id }}",
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.workflow)

    def test_all_native_source_and_signature_evidence_is_revalidated(self):
        for value in (
            "candidate_manifest.py validate",
            "source_binding.py validate",
            "candidate_manifest.py create",
            "native-aarch64-release.py validate",
            "validate-qt-native-release.py",
            "validate-sigstore-report.py",
            '"$cosign" verify',
            "--trusted-root",
            "anonymous-candidate-index.json",
            "anonymous-source-index.json",
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.workflow)

    def test_version_tags_are_immutable_and_channels_move_last(self):
        self.assertIn("refusing to replace immutable version tag", self.workflow)
        self.assertIn(
            'promote_tag "$source_version" "$source_digest" true', self.workflow
        )
        self.assertIn(
            'promote_tag "$candidate_version" "$candidate_digest" true',
            self.workflow,
        )
        source_channel = self.workflow.index(
            'promote_tag "$source_channel" "$source_digest" false'
        )
        candidate_channel = self.workflow.index(
            'promote_tag "$candidate_channel" "$candidate_digest" false'
        )
        self.assertLess(source_channel, candidate_channel)
        self.assertIn("release_promotion.py create", self.workflow)
        self.assertIn("release_promotion.py validate", self.workflow)


if __name__ == "__main__":
    unittest.main()
