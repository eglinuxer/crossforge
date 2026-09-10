"""Artifact byte receipts must not substitute for qualification or trust."""

import copy
from pathlib import Path
import runpy
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_artifacts as artifacts
    from crossforge_internal import component_inputs as inputs
    from crossforge_internal.identity import IdentityError, canonical_bytes, content_sha256, load_json
finally:
    sys.path.pop(0)


class ComponentArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "recipe").write_text("fixture recipe")
        self.inputs = inputs.capture(self.root, "toolchain/x86_64", "build", ["recipe"],
                                     ["x86_64-unknown-linux-gnu"])
        self.producer = {"kind": "local", "source_commit": "a" * 40, "source_dirty": True,
                         "invocation": "urn:crossforge:local:fixture-1", "started_at": "2026-09-10T00:00:00Z"}
        self.contract = artifacts.contract("toolchain-install", self.inputs, self.producer)
        self.paths = [artifacts.CONTRACT_PATH, "component/reports/compile.json"]
        (self.root / "component/reports").mkdir(parents=True)
        (self.root / artifacts.CONTRACT_PATH).write_bytes(canonical_bytes(self.contract))
        (self.root / self.paths[1]).write_text('{"fixture":"not a real qualification report"}')
        self.observation = {"kind": "crossforge-oci-layout-observation", "schema_version": 1,
                            "root_digest": "sha256:" + "1" * 64,
                            "platform_digest": "sha256:" + "2" * 64,
                            "config_digest": "sha256:" + "3" * 64, "platform": "linux/amd64"}
        self.receipt = artifacts.receipt(self.contract, self.observation, self.root, self.paths)

    def verify(self, **changes):
        values = {"value": self.receipt, "expected_receipt_sha256": content_sha256(self.receipt),
                  "expected_inputs": self.inputs, "expected_role": "toolchain-install",
                  "observation": self.observation, "metadata_root": self.root,
                  "required_metadata": self.paths}
        values.update(changes)
        return artifacts.verify_receipt(**values)

    def test_receipt_binds_bytes_without_inventing_qualification(self):
        self.assertEqual(self.verify(), self.observation["platform_digest"])
        self.assertNotIn("qualified", self.receipt)
        self.assertEqual(self.receipt["contract"]["producer"], self.producer)

    def test_wrong_receipt_reference_image_and_role_are_rejected(self):
        for changes in ({"expected_receipt_sha256": "f" * 64}, {"expected_role": "gcc-test-context"},
                        {"observation": dict(self.observation, root_digest="sha256:" + "f" * 64)},
                        {"observation": dict(self.observation, platform_digest="sha256:" + "f" * 64)},
                        {"observation": dict(self.observation, config_digest="sha256:" + "f" * 64)}):
            with self.subTest(changes=changes):
                with self.assertRaises(IdentityError):
                    self.verify(**changes)

    def test_expected_materials_cannot_come_from_a_smaller_producer_subset(self):
        (self.root / "additional").write_text("must be part of the closure")
        expected = inputs.capture(self.root, "toolchain/x86_64", "build", ["recipe", "additional"],
                                  ["x86_64-unknown-linux-gnu"])
        with self.assertRaisesRegex(IdentityError, "inputs differ"):
            self.verify(expected_inputs=expected)

    def test_wrong_target_recipe_and_sysroot_inputs_are_rejected(self):
        for key, value in (("targets", ["aarch64-unknown-linux-gnu"]),
                           ("parameters", {"recipe": "changed"}),
                           ("dependencies", [{"component": "sysroot/x86_64", "inputs_sha256": "f" * 64,
                                              "artifact_digest": "sha256:" + "e" * 64}])):
            expected = copy.deepcopy(self.inputs)
            expected[key] = value
            with self.subTest(key=key):
                with self.assertRaisesRegex(IdentityError, "inputs differ"):
                    self.verify(expected_inputs=expected)

    def test_missing_tampered_and_mode_changed_metadata_are_rejected(self):
        path = self.root / self.paths[1]
        original, mode = path.read_bytes(), path.stat().st_mode & 0o7777
        path.write_bytes(b"changed")
        with self.assertRaises(IdentityError):
            self.verify()
        path.write_bytes(original)
        path.chmod(0o755)
        with self.assertRaises(IdentityError):
            self.verify()
        path.chmod(mode)
        path.unlink()
        with self.assertRaises(IdentityError):
            self.verify()

    def test_missing_required_report_cannot_be_hidden_by_resealing_receipt(self):
        value = artifacts.receipt(self.contract, self.observation, self.root, [artifacts.CONTRACT_PATH])
        with self.assertRaisesRegex(IdentityError, "required component metadata"):
            self.verify(value=value, expected_receipt_sha256=content_sha256(value))

    def test_original_producer_cannot_be_rewritten_without_changing_receipt(self):
        value = copy.deepcopy(self.receipt)
        value["contract"]["producer"]["invocation"] = "urn:crossforge:local:new-run"
        with self.assertRaisesRegex(IdentityError, "trusted reference"):
            self.verify(value=value)
        with self.assertRaisesRegex(IdentityError, "embedded component contract"):
            self.verify(value=value, expected_receipt_sha256=content_sha256(value))

    def test_embedded_contract_must_match_the_expected_producer_contract(self):
        value = copy.deepcopy(self.contract)
        value["role"] = "gcc-test-context"
        (self.root / artifacts.CONTRACT_PATH).write_bytes(canonical_bytes(value))
        with self.assertRaisesRegex(IdentityError, "embedded component contract"):
            artifacts.receipt(self.contract, self.observation, self.root, self.paths)

    def test_strict_fields_roles_and_versions(self):
        for key, value in (("future", True), ("role", "sdk-dev"), ("schema_version", 2),
                           ("schema_version", True), ("kind", "crossforge-release")):
            changed = copy.deepcopy(self.contract)
            changed[key] = value
            with self.assertRaises(IdentityError):
                artifacts.validate_contract(changed)
        value = copy.deepcopy(self.receipt)
        value["qualified"] = True
        with self.assertRaises(IdentityError):
            artifacts.validate_receipt(value)

    def test_role_scope_and_multiple_target_confusion_are_rejected(self):
        for role, changes in (("qualification", {}),
                              ("toolchain-install", {"scope": "qualification"}),
                              ("gcc-test-context", {"targets": []}),
                              ("toolchain-install", {"targets": sorted(inputs.TARGETS)})):
            with self.assertRaises(IdentityError):
                artifacts.contract(role, dict(self.inputs, **changes), self.producer)

    def test_producer_identifies_original_run_attempt_and_valid_time(self):
        valid = dict(self.producer, kind="github-actions", source_dirty=False,
                     invocation="https://github.com/eglinux/crossforge/actions/runs/123/attempts/2")
        artifacts.validate_producer(valid)
        for value in (dict(valid, invocation="https://github.com/eglinux/crossforge/actions/runs/123"),
                      dict(valid, source_dirty=True), dict(self.producer, started_at="2026-02-30T00:00:00Z"),
                      dict(self.producer, started_at="2026-09-10"), dict(self.producer, source_commit="HEAD")):
            with self.assertRaises(IdentityError):
                artifacts.validate_producer(value)

    def test_receipt_metadata_paths_are_strict_and_include_contract(self):
        for paths in ([self.paths[1]], [self.paths[0], self.paths[0]], ["recipe"]):
            with self.assertRaises(IdentityError):
                artifacts.receipt(self.contract, self.observation, self.root, paths)

    def test_schema_bundles_match_shared_contract_and_input_definitions(self):
        strict = runpy.run_path(str(ROOT / "scripts/validate-release.py"))
        input_schema = load_json(ROOT / "config/schemas/component-inputs.schema.json")
        artifact_schema = load_json(ROOT / "config/schemas/component-artifact.schema.json")
        receipt_schema = load_json(ROOT / "config/schemas/component-receipt.schema.json")
        for schema, document in ((artifact_schema, self.contract), (receipt_schema, self.receipt)):
            strict["validate_schema_subset"](schema)
            strict["validate"](document, schema, schema, "$component")
            self.assertEqual(schema["$defs"]["inputs"], {
                key: value for key, value in input_schema.items() if key not in ("$schema", "$id")})
        self.assertEqual(receipt_schema["$defs"]["contract"], {
            key: value for key, value in artifact_schema.items() if key not in ("$schema", "$id", "$defs")})


if __name__ == "__main__":
    unittest.main()
