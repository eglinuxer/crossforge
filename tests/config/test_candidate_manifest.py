import ast
import copy
import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/candidate_manifest.py"
CANDIDATE = runpy.run_path(str(SCRIPT))
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
COMPONENTS = runpy.run_path(
    str(REPOSITORY / "scripts/render-release-components.py")
)


class CandidateManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = CANDIDATE["load_release"](
            REPOSITORY / "config/release.json",
            REPOSITORY / "config/schemas/release.schema.json",
        )
        cls.schema = CANDIDATE["load_candidate_schema"](
            REPOSITORY / "config/schemas/candidate.schema.json"
        )
        cls.source_commit = "1" * 40
        cls.digest = "sha256:" + "2" * 64
        cls.platform_digest = "sha256:" + "3" * 64

    def document(self):
        return CANDIDATE["candidate_document"](
            self.release,
            self.source_commit,
            self.digest,
            self.platform_digest,
        )

    def test_document_is_strict_and_release_bound(self):
        document = self.document()
        digest = CANDIDATE["validate_candidate"](
            document,
            self.release,
            self.schema,
            expected_source_commit=self.source_commit,
        )
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(document["version"], "0.1.0")
        self.assertEqual(
            document["release_sha256"],
            CANDIDATE["canonical_sha256"](self.release),
        )
        self.assertEqual(
            document["repository"], "ghcr.io/eglinuxer/crossforge"
        )
        self.assertNotIn("tag", document)

    def test_candidate_tag_is_unique_and_not_an_identity(self):
        self.assertEqual(
            CANDIDATE["candidate_tag"](
                self.release, self.source_commit, "123456", "2"
            ),
            "candidate-v0.1.0-g111111111111-r123456-a2",
        )
        for commit, run_id, run_attempt in (
            ("1" * 39, "123", "1"),
            ("G" * 40, "123", "1"),
            ("1" * 40, "0", "1"),
            ("1" * 40, "01", "1"),
            ("1" * 40, "123", "0"),
        ):
            with self.subTest(
                commit=commit, run_id=run_id, run_attempt=run_attempt
            ):
                with self.assertRaises(CANDIDATE["CandidateError"]):
                    CANDIDATE["candidate_tag"](
                        self.release, commit, run_id, run_attempt
                    )

    def test_candidate_tag_cli_prints_only_the_tag(self):
        result = subprocess.run(
            [
                str(SCRIPT),
                "tag",
                "--source-commit",
                self.source_commit,
                "--run-id",
                "123456",
                "--run-attempt",
                "2",
            ],
            cwd=str(REPOSITORY),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout, "candidate-v0.1.0-g111111111111-r123456-a2\n"
        )

    def test_candidate_policy_is_bound_to_product_release(self):
        components = COMPONENTS["render_component_documents"](self.release)
        policy = components["implementation/candidate-manifest"]
        self.assertEqual(policy["scope"], "supply")
        materials = {
            item["path"]: item["value"] for item in policy["materials"]
        }
        self.assertEqual(
            materials["/@implementation/candidate-manifest/schema"],
            CANDIDATE["SCHEMA_ID"],
        )
        release_dependencies = {
            item["component"]
            for item in components["product/release"]["dependencies"]
        }
        self.assertEqual(
            release_dependencies, {"implementation/candidate-manifest"}
        )

    def test_unknown_and_malformed_fields_are_rejected(self):
        unknown = self.document()
        unknown["tag"] = "candidate-v0.1.0"
        malformed = self.document()
        malformed["digest"] = "2" * 64
        for document in (unknown, malformed):
            with self.subTest(document=document):
                with self.assertRaisesRegex(
                    CANDIDATE["CandidateError"], "candidate schema validation failed"
                ):
                    CANDIDATE["validate_candidate"](
                        document, self.release, self.schema
                    )

    def test_release_and_expected_commit_mismatches_are_rejected(self):
        mutations = (
            ("version", "0.1.1"),
            ("release_sha256", "0" * 64),
            ("repository", "ghcr.io/example/crossforge"),
            ("platform", "linux/arm64"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                document = self.document()
                document[field] = value
                with self.assertRaises(CANDIDATE["CandidateError"]):
                    CANDIDATE["validate_candidate"](
                        document, self.release, self.schema
                    )

        with self.assertRaises(CANDIDATE["CandidateError"]):
            CANDIDATE["validate_candidate"](
                self.document(),
                self.release,
                self.schema,
                expected_source_commit="4" * 40,
            )

    def test_release_mutation_invalidates_candidate(self):
        release = copy.deepcopy(self.release)
        release["product"]["version"] = "0.1.1"
        with self.assertRaises(CANDIDATE["CandidateError"]):
            CANDIDATE["validate_candidate"](
                self.document(), release, self.schema
            )

    def test_cli_create_is_idempotent_and_refuses_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "candidate.json"
            command = [
                str(SCRIPT),
                "create",
                "--source-commit",
                self.source_commit,
                "--digest",
                self.digest,
                "--platform-manifest-digest",
                self.platform_digest,
                "--output",
                str(output),
            ]
            first = subprocess.run(
                command,
                cwd=str(REPOSITORY),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            second = subprocess.run(
                command,
                cwd=str(REPOSITORY),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("current candidate", second.stdout)

            changed = list(command)
            changed[changed.index(self.digest)] = "sha256:" + "5" * 64
            refused = subprocess.run(
                changed,
                cwd=str(REPOSITORY),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            self.assertEqual(refused.returncode, 1)
            self.assertIn("refusing to replace", refused.stderr)

            validated = subprocess.run(
                [
                    str(SCRIPT),
                    "validate",
                    str(output),
                    "--expected-source-commit",
                    self.source_commit,
                ],
                cwd=str(REPOSITORY),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")), self.document()
            )

    def test_duplicate_keys_and_output_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            duplicate = Path(temporary) / "duplicate.json"
            duplicate.write_text(
                '{"schema_version":1,"schema_version":1}\n', encoding="utf-8"
            )
            with self.assertRaises(STRICT["ValidationError"]):
                STRICT["load_json"](duplicate)

            target = Path(temporary) / "target.json"
            target.write_text("sentinel\n", encoding="utf-8")
            link = Path(temporary) / "candidate.json"
            link.symlink_to(target.name)
            with self.assertRaises(CANDIDATE["CandidateError"]):
                CANDIDATE["write_json_once"](link, self.document())

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
