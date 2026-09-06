import ast
import copy
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/build-rpm-source-requirements.py"
BUILDER = runpy.run_path(str(SCRIPT))


class RPMSourceRequirementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release, cls.expected_locks = BUILDER["release_context"](
            REPOSITORY / "config/release.json",
            REPOSITORY / "config/schemas/release.schema.json",
        )
        cls.base_map = BUILDER["load_schema_document"](
            REPOSITORY
            / "evidence/sources/rocky-base-rpm-sources.json",
            REPOSITORY / "config/schemas/rpm-source-map.schema.json",
        )
        cls.document = BUILDER["build_document"](
            cls.release, cls.expected_locks, cls.base_map
        )
        cls.schema = BUILDER["STRICT"]["load_json"](
            REPOSITORY
            / "config/schemas/rpm-source-requirements.schema.json"
        )

    def test_all_rpm_inputs_form_one_exact_source_requirement_set(self):
        BUILDER["validate_document"](
            self.document, self.schema, require_complete=False
        )
        BUILDER["validate_expected"](
            self.document,
            REPOSITORY
            / "evidence/sources/rpm-source-requirements.json",
            self.schema,
        )
        self.assertEqual(len(self.expected_locks), 12)
        self.assertEqual(
            set(self.expected_locks), BUILDER["EXPECTED_LOCK_PATHS"]
        )
        self.assertEqual(
            self.document["summary"],
            {
                "base_packages": 148,
                "base_source_rpms": 110,
                "locked_binary_payloads": 1433,
                "locked_source_rpms": 279,
                "combined_source_rpms": 333,
                "content_locked_source_rpms": 2,
                "missing_content_locks": 331,
            },
        )
        self.assertEqual(
            [
                record["source_rpm"]
                for record in self.document["sources"]
                if record["content"]["status"] == "locked"
            ],
            [
                "gcc-toolset-15-binutils-2.44-3.el8.src.rpm",
                "gcc-toolset-15-gcc-15.2.1-7.1.el8_10.src.rpm",
            ],
        )
        for record in self.document["sources"]:
            if record["content"]["status"] == "locked":
                self.assertEqual(
                    record["content"]["signature"],
                    {
                        "key_sha256": self.release["trust"][
                            "rocky_rpm_key"
                        ]["sha256"],
                        "fingerprint": self.release["trust"][
                            "rocky_rpm_key"
                        ]["fingerprint"],
                    },
                )

    def test_incomplete_source_content_lock_fails_the_release_mode(self):
        with self.assertRaisesRegex(
            BUILDER["ValidationError"], "content lock is incomplete"
        ):
            BUILDER["validate_document"](
                self.document, self.schema, require_complete=True
            )

    def test_summary_and_source_order_tampering_are_rejected(self):
        wrong_summary = copy.deepcopy(self.document)
        wrong_summary["summary"]["missing_content_locks"] -= 1
        wrong_order = copy.deepcopy(self.document)
        wrong_order["sources"].reverse()
        for document in (wrong_summary, wrong_order):
            with self.subTest(document=document):
                with self.assertRaises(BUILDER["ValidationError"]):
                    BUILDER["validate_document"](
                        document, self.schema, require_complete=False
                    )

    def test_base_only_sources_are_not_lost(self):
        base_sources = {
            record["source_rpm"] for record in self.base_map["packages"]
        }
        locked_sources = {
            record["source_rpm"]
            for record in self.document["sources"]
            if any(origin.startswith("locks/") for origin in record["origins"])
        }
        base_only = base_sources - locked_sources
        self.assertEqual(len(base_only), 54)
        self.assertTrue(
            all(
                next(
                    record
                    for record in self.document["sources"]
                    if record["source_rpm"] == source_rpm
                )["origins"]
                == ["base-image"]
                for source_rpm in base_only
            )
        )

    def test_docker_target_is_mapping_only_and_never_claims_completion(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        block = dockerfile.split(" AS rpm-source-requirements", 1)[1]
        block = block.split("\nFROM ", 1)[0]
        self.assertIn("RUN --network=none", block)
        self.assertIn("build-rpm-source-requirements.py", block)
        self.assertIn(
            "evidence/sources/rpm-source-requirements.json", block
        )
        self.assertNotIn("--require-complete", block)
        bake = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        self.assertIn('target "rpm-source-requirements"', bake)
        self.assertIn(
            'crossforge_rocky_source_map = "target:rocky-base-source-map"',
            bake,
        )
        ci = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("rpm-source-requirements", ci)

    def test_builder_is_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
