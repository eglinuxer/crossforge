import ast
import base64
import hashlib
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPOSITORY = Path(__file__).resolve().parents[2]
FETCH_SCRIPT = REPOSITORY / "scripts/fetch-release-source.py"
PREPARE_SCRIPT = REPOSITORY / "scripts/prepare-sbom-generator-source.py"
IMAGE_VALIDATOR_SCRIPT = REPOSITORY / "scripts/validate-sbom-generator-image.py"
FETCH = runpy.run_path(str(FETCH_SCRIPT))
PREPARE = runpy.run_path(str(PREPARE_SCRIPT))
IMAGE_VALIDATOR = runpy.run_path(
    str(REPOSITORY / "scripts/validate-sbom-generator-image.py")
)
RENDERER = runpy.run_path(
    str(REPOSITORY / "scripts/render-release-components.py")
)


class SbomGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        cls.components = RENDERER["render_component_documents"](cls.release)
        cls.component = cls.components["sources/sbom-generator"]
        cls.digest = RENDERER["canonical_sha256"](cls.component)

    def test_image_source_tag_key_and_provenance_are_exactly_locked(self):
        generator = self.release["sbom"]["generator"]
        self.assertEqual(generator["version"], "1.12.0")
        self.assertEqual(
            generator["digest"],
            "sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9",
        )
        self.assertEqual(
            generator["manifest_digest"],
            "sha256:187e1892a7752c9384c59aba9517dd8e40610b748c72773e87b63720514463c2",
        )
        source = generator["source"]
        self.assertEqual(source["tag"], "v1.12.0")
        self.assertEqual(
            source["tag_object"], "e131763ad439b53d72810211577e7526bfac5d20"
        )
        self.assertEqual(
            source["commit"], "aba762345737fbb33224d0670260708bfbaadb99"
        )
        key = REPOSITORY / source["key"]["file"]
        self.assertEqual(
            hashlib.sha256(key.read_bytes()).hexdigest(), source["key"]["sha256"]
        )
        self.assertEqual(
            source["key"]["source_sha256"],
            "ec54da2392e0137bc580d38b30ade40091bb630fb1ef690f20b4c101d6f4c120",
        )
        self.assertEqual(
            source["key"]["derivation"],
            "gpg-export-minimal-keep-noreply-email-uid",
        )

    def test_source_fetch_uses_only_the_supply_component_projection(self):
        source = FETCH["source_for_component"](
            REPOSITORY / "config/generated/components/sources/sbom-generator.json",
            "sources/sbom-generator",
            "supply",
            self.digest,
            "sbom-generator",
        )
        expected = self.release["sbom"]["generator"]["source"]
        self.assertEqual(
            source,
            {
                field: expected[field]
                for field in ("status", "url", "sha256", "size")
            },
        )
        policy = PREPARE["source_policy"](self.component, self.digest)
        self.assertEqual(policy["version"], "1.12.0")
        self.assertEqual(policy["source"]["commit"], expected["commit"])
        self.assertEqual(
            policy["key"]["fingerprint"],
            "cb73f06483432e94892fd8e1ade44d8c9d44fbe4",
        )

    def test_candidate_and_source_bundle_use_the_pinned_generator(self):
        candidate = (REPOSITORY / ".github/workflows/candidate.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(candidate.count("generator=$SBOM_GENERATOR"), 1)
        self.assertIn('--sbom-generator "$SBOM_GENERATOR"', candidate)
        self.assertIn("sbom-generator-index.json", candidate)
        dockerfile = (REPOSITORY / "docker/sbom.Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("fetch-release-source.py", dockerfile)
        self.assertIn("prepare-sbom-generator-source.py", dockerfile)
        self.assertIn("RUN --network=none", dockerfile)
        self.assertIn(
            "buildkit-syft-scanner-v1.12.0.tag.json.b64", dockerfile
        )
        self.assertIn("base64 --decode", dockerfile)
        self.assertNotIn("ADD http", dockerfile)
        source_bundle = (
            REPOSITORY / "docker/source-bundle.Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("crossforge_sbom_generator_source", source_bundle)
        for name in (
            "buildkit-syft-scanner-1.12.0.tar.gz",
            "buildkit-syft-scanner-v1.12.0.tag.json",
            "CRAZY-MAX-RELEASE-KEY.asc",
            "sbom-generator-source.json",
        ):
            self.assertIn(name, source_bundle)

    def test_checked_in_oci_and_provenance_evidence_revalidates_offline(self):
        generator = self.release["sbom"]["generator"]
        inputs = {
            "index": generator["index_evidence"],
            "manifest": generator["manifest_evidence"],
            "attestation_manifest": generator["provenance"][
                "attestation_evidence"
            ],
            "provenance": generator["provenance"]["predicate_evidence"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {}
            for name, relative in inputs.items():
                path = root / (name + ".json")
                encoded = b"".join((REPOSITORY / relative).read_bytes().split())
                path.write_bytes(base64.b64decode(encoded, validate=True))
                paths[name] = path
            output = root / "report.json"
            report = IMAGE_VALIDATOR["validate"](
                SimpleNamespace(
                    index=paths["index"],
                    manifest=paths["manifest"],
                    attestation_manifest=paths["attestation_manifest"],
                    provenance=paths["provenance"],
                    output=output,
                    release=REPOSITORY / "config/release.json",
                    release_schema=REPOSITORY
                    / "config/schemas/release.schema.json",
                    schema=REPOSITORY
                    / "config/schemas/sbom-generator-image.schema.json",
                )
            )
            self.assertEqual(report["index_digest"], generator["digest"])
            self.assertEqual(
                report["source"]["commit"], generator["source"]["commit"]
            )

    def test_generator_supply_identity_is_upstream_of_candidate_policy(self):
        self.assertEqual(self.component["scope"], "supply")
        candidate = self.components["implementation/candidate-manifest"]
        self.assertEqual(
            {record["component"] for record in candidate["dependencies"]},
            {"sources/sbom-generator"},
        )

    def test_scripts_are_python36_compatible(self):
        for path in (FETCH_SCRIPT, PREPARE_SCRIPT, IMAGE_VALIDATOR_SCRIPT):
            with self.subTest(path=path):
                ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                    feature_version=(3, 6),
                )


if __name__ == "__main__":
    unittest.main()
