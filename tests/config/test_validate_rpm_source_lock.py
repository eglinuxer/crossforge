import ast
import copy
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/validate-rpm-source-lock.py"
VALIDATOR = runpy.run_path(str(SCRIPT))


class ValidateRPMSourceLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = VALIDATOR["BUILD"]["load_schema_document"](
            REPOSITORY / "config/release.json",
            REPOSITORY / "config/schemas/release.schema.json",
        )
        cls.requirements = VALIDATOR["BUILD"]["load_schema_document"](
            REPOSITORY
            / "evidence/sources/rpm-source-requirements.json",
            REPOSITORY
            / "config/schemas/rpm-source-requirements.schema.json",
        )
        cls.lock = VALIDATOR["BUILD"]["load_schema_document"](
            REPOSITORY / "locks/rpm-source-el8.json",
            REPOSITORY / "config/schemas/rpm-source-lock.schema.json",
        )

    def test_release_binds_the_complete_source_lock(self):
        self.assertEqual(
            VALIDATOR["validate_lock"](
                self.release, self.requirements, self.lock
            ),
            {"sources": 333, "bytes": 1456725209},
        )
        self.assertEqual(
            self.release["source_bundle"]["rpm"],
            {
                "status": "locked",
                "requirements": {
                    "file": "evidence/sources/rpm-source-requirements.json",
                    "canonical_sha256": (
                        "cfd286fbbc4da50d8f2126d6cc8bfecba64c0031a7a74083"
                        "489b77157862a4eb"
                    ),
                },
                "lock": {
                    "file": "locks/rpm-source-el8.json",
                    "canonical_sha256": (
                        "88195aa18af80cca8df912f30109d4cbec3318dfabc5e33e"
                        "0d3ae12931ea9e12"
                    ),
                },
                "source_rpms": 333,
                "source_bytes": 1456725209,
            },
        )

    def test_release_input_and_lock_tampering_fail_closed(self):
        mutations = []
        wrong_pin = copy.deepcopy(self.release)
        wrong_pin["source_bundle"]["rpm"]["lock"][
            "canonical_sha256"
        ] = "0" * 64
        mutations.append((wrong_pin, self.requirements, self.lock))
        wrong_input = copy.deepcopy(self.requirements)
        wrong_input["input_sha256"] = "0" * 64
        mutations.append((self.release, wrong_input, self.lock))
        wrong_source = copy.deepcopy(self.lock)
        wrong_source["sources"][0]["sha256"] = "0" * 64
        mutations.append((self.release, self.requirements, wrong_source))
        wrong_alias = copy.deepcopy(self.lock)
        alias = next(record for record in wrong_alias["sources"] if record["aliases"])
        alias["aliases"] = []
        mutations.append((self.release, self.requirements, wrong_alias))
        for release, requirements, lock in mutations:
            with self.subTest(lock=lock["sources"][0]["source_rpm"]):
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["validate_lock"](
                        release, requirements, lock
                    )

    def test_maintenance_resolution_is_not_a_normal_ci_or_candidate_edge(self):
        ci = (
            (REPOSITORY / ".github/workflows/verify-quick.yml").read_text(encoding="utf-8")
            + (REPOSITORY / "scripts/ci-build.py").read_text(encoding="utf-8")
        )
        candidate = (REPOSITORY / ".github/workflows/candidate.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("rpm-source-lock-validated", ci)
        self.assertIn("validate-rpm-source-lock.py", candidate)
        self.assertNotIn("rpm-source-lock-maintenance", ci)
        self.assertNotIn("rpm-source-lock-maintenance", candidate)
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        block = dockerfile.split(" AS rpm-source-lock-validated", 1)[1]
        block = block.split("\nFROM ", 1)[0]
        self.assertIn("RUN --network=none", block)
        self.assertNotIn("repoquery", block)

    def test_validator_and_resolver_are_python36_compatible(self):
        for path in (
            SCRIPT,
            REPOSITORY / "scripts/resolve-rpm-source-lock.py",
        ):
            with self.subTest(path=path):
                ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                    feature_version=(3, 6),
                )


if __name__ == "__main__":
    unittest.main()
