import ast
import copy
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/source_binding.py"
BINDING = runpy.run_path(str(SCRIPT))


class SourceBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = BINDING["load_release"](
            REPOSITORY / "config/release.json",
            REPOSITORY / "config/schemas/release.schema.json",
        )
        cls.schema = BINDING["STRICT"]["load_json"](
            REPOSITORY / "config/schemas/source-binding.schema.json"
        )
        cls.commit = "1" * 40
        cls.document = BINDING["source_binding"](
            cls.release,
            cls.commit,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            "4" * 64,
            2866136432,
        )

    def test_binding_is_strict_release_and_commit_bound(self):
        digest = BINDING["validate_binding"](
            self.document,
            self.release,
            self.schema,
            self.commit,
        )
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            self.document["repository"], "ghcr.io/eglinuxer/crossforge"
        )
        self.assertEqual(
            self.document["archive"]["file"],
            "crossforge-source-%s.tar.zst" % self.commit,
        )
        offer = BINDING["source_offer"](self.document)
        self.assertIn(
            self.document["repository"] + "@" + self.document["digest"], offer
        )
        self.assertIn(self.document["archive"]["sha256"], offer)

    def test_release_repository_commit_and_archive_tampering_fail_closed(self):
        mutations = (
            lambda value: value.__setitem__("release_sha256", "0" * 64),
            lambda value: value.__setitem__(
                "repository", "ghcr.io/example/crossforge"
            ),
            lambda value: value["archive"].__setitem__(
                "file", "crossforge-source-%s.tar.zst" % ("9" * 40)
            ),
        )
        for mutate in mutations:
            document = copy.deepcopy(self.document)
            mutate(document)
            with self.assertRaises(BINDING["ValidationError"]):
                BINDING["validate_binding"](
                    document, self.release, self.schema, self.commit
                )

    def test_cli_create_and_validate_are_consistent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "SOURCES.json"
            offer = root / "SOURCE-OFFER"
            arguments = [
                "create",
                "--source-commit",
                self.commit,
                "--digest",
                self.document["digest"],
                "--platform-manifest-digest",
                self.document["platform_manifest_digest"],
                "--archive-sha256",
                self.document["archive"]["sha256"],
                "--archive-size",
                str(self.document["archive"]["size"]),
                "--output",
                str(output),
                "--offer",
                str(offer),
            ]
            self.assertEqual(BINDING["main"](arguments), 0)
            self.assertEqual(json.loads(output.read_text()), self.document)
            self.assertIn(self.document["digest"], offer.read_text())
            self.assertEqual(
                BINDING["main"](
                    [
                        "validate",
                        str(output),
                        "--expected-source-commit",
                        self.commit,
                    ]
                ),
                0,
            )

    def test_candidate_image_embeds_and_rechecks_the_exact_binding(self):
        dockerfile = (
            REPOSITORY / "docker/packaging.Dockerfile"
        ).read_text(encoding="utf-8")
        candidate = dockerfile.split(" AS sdk-candidate", 1)[1]
        workflow = (
            REPOSITORY / ".github/workflows/candidate.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("source_binding.py create", candidate)
        self.assertIn("/opt/crossforge/SOURCES.json", candidate)
        self.assertIn("/opt/crossforge/SOURCE-OFFER", candidate)
        self.assertLess(
            workflow.index("Build once and push the corresponding source bundle"),
            workflow.index("Build once and push the source-bound candidate"),
        )
        self.assertIn("candidate-sources.json", workflow)
        self.assertIn("candidate-source-offer.txt", workflow)
        self.assertIn("expected-source-binding.json", workflow)

    def test_binding_tool_is_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
