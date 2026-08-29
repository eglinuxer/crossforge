import copy
import hashlib
import json
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))


class ReleaseValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = VALIDATOR["load_json"](REPOSITORY / "config/release.json")
        cls.schema = VALIDATOR["load_json"](
            REPOSITORY / "config/schemas/release.schema.json"
        )

    def test_current_configuration_is_valid(self):
        VALIDATOR["validate_schema_subset"](self.schema)
        VALIDATOR["validate"](self.config, self.schema, self.schema, "$")

    def test_duplicate_keys_are_rejected(self):
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["reject_duplicate_keys"]([("same", 1), ("same", 2)])

    def test_unknown_configuration_fields_are_rejected(self):
        config = copy.deepcopy(self.config)
        config["unexpected"] = True
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate"](config, self.schema, self.schema, "$")

    def test_unsupported_schema_keywords_are_rejected(self):
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_schema_subset"](
                {"type": "object", "dependentRequired": {}}
            )

    def test_pending_sources_are_explicit(self):
        self.assertTrue(VALIDATOR["find_pending"](self.config))

    def test_locked_sysroot_digest_matches_file(self):
        pin = self.config["targets"][0]["sysroot"]
        lock = VALIDATOR["load_json"](REPOSITORY / pin["lock_file"])
        canonical = json.dumps(
            lock, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(pin["canonical_sha256"], digest)

    def test_locked_rpm_sources_have_size_and_spec_hash(self):
        for component in (self.config["gts"], self.config["binutils"]):
            source = component["source"]
            self.assertEqual(source["status"], "locked")
            self.assertGreater(source["size"], 0)
            self.assertRegex(source["spec_sha256"], r"^[0-9a-f]{64}$")

    def test_rocky_trust_root_matches_key_file(self):
        trust = self.config["trust"]["rocky_rpm_key"]
        digest = hashlib.sha256((REPOSITORY / trust["file"]).read_bytes()).hexdigest()
        self.assertEqual(trust["sha256"], digest)

    def test_nonpositive_locked_source_size_is_rejected(self):
        config = copy.deepcopy(self.config)
        config["gts"]["source"]["size"] = 0
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate"](config, self.schema, self.schema, "$")

    def test_bake_override_is_current(self):
        renderer = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))
        expected = renderer["render"](REPOSITORY)
        actual = (REPOSITORY / "docker-bake.override.json").read_text(encoding="utf-8")
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
