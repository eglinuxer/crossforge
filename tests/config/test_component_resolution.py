"""Resolution cannot turn a missing index or failed verifier into accepted bytes."""

import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

import test_component_handoff as fixtures


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import catalog_registry, component_build, component_resolution as resolution, registry_transfer
    from crossforge_internal.identity import IdentityError, load_json
finally:
    sys.path.pop(0)


class ComponentResolutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ComponentHandoffTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        (self.root / ".github/locked-tools").mkdir(parents=True)
        shutil.copyfile(str(ROOT / ".github/locked-tools/oras.json"), str(self.root / ".github/locked-tools/oras.json"))
        self.expected = copy.deepcopy(self.fixture.components["toolchain-install"]["receipt"]["contract"]["inputs"])
        self.expected["parameters"]["recipes"] = {"toolchain-x86_64-build-export": {"frontend": "dockerfile@sha256:" + "f" * 64}}
        self.output = self.root / "resolution"
        self.environment = mock.patch.object(component_build, "execution_identity", return_value=self.fixture.execution).start()
        self.inputs = mock.patch.object(component_build, "toolchain_inputs", return_value=self.expected).start()
        self.lookup = mock.patch.object(catalog_registry, "lookup").start()
        self.fetch = mock.patch.object(registry_transfer, "fetch").start()
        self.verify = mock.patch.object(component_build, "verify_local", return_value="verified fixture context").start()
        self.addCleanup(mock.patch.stopall)
        self.selection = {"status": "authenticated-reference", "entry": self.fixture.components["toolchain-install"],
            "catalog": {"reference": fixtures.handoff.REPOSITORY + "@sha256:" + "d" * 64},
            "authentication": {"producer": self.fixture.producer}}
        self.lookup.return_value = self.selection

    def resolve(self, reference=None, role="toolchain-install"):
        return resolution.toolchain(self.root, {"target": {}}, "x86_64", role, self.fixture.execution,
            self.root / "cosign", self.output, "builder", self.root / "oras", self.root / "docker", reference)

    def test_index_absence_requests_a_producer_without_accepting_any_subject(self):
        self.lookup.return_value = {"status": "missing", "reason": "catalog-index-absent", "input_tag": "input-fixture"}
        result = self.resolve()
        self.assertEqual(result["status"], "build-required")
        self.assertNotIn("subject", result)
        self.assertNotIn("context", result)
        self.assertNotIn("producer", result)
        self.fetch.assert_not_called()
        self.verify.assert_not_called()
        self.assertEqual(load_json(self.output / "resolution.json"), result)

    def test_authentication_precedes_transport_and_the_original_producer_survives(self):
        events = []
        self.lookup.side_effect = lambda *args, **kwargs: events.append("authenticate") or self.selection
        self.fetch.side_effect = lambda *args, **kwargs: events.append("fetch")
        self.verify.side_effect = lambda *args, **kwargs: events.append("verify actual artifact") or "verified fixture context"
        result = self.resolve()
        self.assertEqual(events, ["authenticate", "fetch", "verify actual artifact"])
        self.assertEqual(result["status"], "verified-build-component")
        self.assertEqual(result["producer"], self.fixture.producer)
        self.assertEqual(result["authentication"], self.selection["authentication"])
        self.assertEqual(result["catalog"], self.selection["catalog"])
        self.assertEqual(load_json(result["subject"]["receipt"]), self.selection["entry"]["receipt"])
        self.assertEqual(self.verify.call_args[0][:4], (self.selection["entry"]["receipt"],
            self.selection["entry"]["receipt_sha256"], self.expected, "toolchain-install"))
        self.assertEqual(self.fetch.call_args[0][0], self.selection["entry"]["reference"])
        self.assertEqual(self.lookup.call_args[0][1:3], (self.expected, "toolchain-install"))
        self.assertNotIn("qualification", result)

    def test_signature_failure_cannot_trigger_transport_or_fallback(self):
        self.lookup.side_effect = subprocess.CalledProcessError(1, "cosign")
        with self.assertRaises(subprocess.CalledProcessError):
            self.resolve()
        self.fetch.assert_not_called()
        self.verify.assert_not_called()
        self.assertFalse((self.output / "resolution.json").exists())

    def test_transport_and_artifact_verification_failures_never_become_build_requests(self):
        self.fetch.side_effect = subprocess.CalledProcessError(1, "oras")
        with self.assertRaises(subprocess.CalledProcessError):
            self.resolve()
        self.verify.assert_not_called()
        shutil.rmtree(str(self.output))
        self.fetch.side_effect = None
        self.verify.side_effect = IdentityError("artifact contract differs")
        with self.assertRaisesRegex(IdentityError, "artifact contract differs"):
            self.resolve()
        self.assertFalse((self.output / "resolution.json").exists())

    def test_recovery_reference_is_forwarded_without_a_replacement_producer(self):
        reference = self.selection["catalog"]["reference"]
        self.resolve(reference)
        self.assertEqual(self.lookup.call_args[1]["catalog_reference"], reference)
        shutil.rmtree(str(self.output))
        self.lookup.return_value = {"status": "missing"}
        with self.assertRaisesRegex(IdentityError, "recovery catalog"):
            self.resolve(reference)

    def test_changed_inputs_or_environment_cannot_seal_a_resolution(self):
        changed = copy.deepcopy(self.expected)
        changed["parameters"]["execution"]["changed"] = True
        self.inputs.side_effect = [self.expected, changed]
        with self.assertRaises(IdentityError):
            self.resolve()
        self.assertFalse((self.output / "resolution.json").exists())
        shutil.rmtree(str(self.output))
        self.inputs.side_effect = None
        self.environment.side_effect = [self.fixture.execution, {"changed": True}]
        with self.assertRaisesRegex(IdentityError, "environment changed"):
            self.resolve()
        self.assertFalse((self.output / "resolution.json").exists())

    def test_qualification_role_and_existing_outputs_fail_before_catalog_access(self):
        with self.assertRaises(IdentityError):
            self.resolve(role="qualification")
        self.output.mkdir()
        with self.assertRaises(IdentityError):
            self.resolve()
        self.lookup.assert_not_called()

    def test_registry_auth_uses_the_same_docker_config_environment_as_buildx(self):
        self.lookup.return_value = {"status": "missing", "reason": "catalog-index-absent", "input_tag": "input-fixture"}
        with mock.patch.dict(os.environ, DOCKER_CONFIG=str(self.root / "environment-config")):
            resolution.toolchain(self.root, {"target": {}}, "x86_64", "toolchain-install", self.fixture.execution,
                self.root / "cosign", self.output, "builder", self.root / "oras")
            self.assertEqual(self.lookup.call_args[0][8], self.root / "environment-config/config.json")
            shutil.rmtree(str(self.output))
            self.resolve()
            self.assertEqual(self.lookup.call_args[0][8], self.root / "docker/config.json")


if __name__ == "__main__":
    unittest.main()
