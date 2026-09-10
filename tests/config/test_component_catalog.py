"""Catalog provenance authenticates references, never substitutes for OCI gates."""

import base64
import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_artifacts, component_catalog as catalog, component_inputs
    from crossforge_internal.identity import IdentityError, canonical_bytes, content_sha256
finally:
    sys.path.pop(0)


class ComponentCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "config").mkdir()
        (self.root / "component").mkdir()
        (self.root / "recipe").write_text("fixture")
        self.expected = component_inputs.capture(self.root, "toolchain/x86_64-install", "build", ["recipe"],
            ["x86_64-unknown-linux-gnu"], parameters={"execution": {"fixture": "environment"}})
        self.producer = {"kind": "github-actions", "source_commit": "a" * 40, "source_dirty": False,
            "invocation": "https://github.com/eglinuxer/crossforge/actions/runs/123/attempts/2",
            "started_at": "2026-09-10T00:00:00Z"}
        contract = component_artifacts.contract("toolchain-install", self.expected, self.producer)
        (self.root / component_artifacts.CONTRACT_PATH).write_bytes(canonical_bytes(contract))
        observation = {"kind": "crossforge-oci-layout-observation", "schema_version": 1, "platform": "linux/amd64",
            "root_digest": "sha256:" + "1" * 64, "platform_digest": "sha256:" + "2" * 64,
            "config_digest": "sha256:" + "3" * 64}
        receipt = component_artifacts.receipt(contract, observation, self.root, [component_artifacts.CONTRACT_PATH])
        self.entry = {"reference": catalog.REPOSITORY + "@" + observation["root_digest"],
            "receipt_sha256": content_sha256(receipt), "receipt": receipt}
        self.value = catalog.document(self.producer, [self.entry])
        self.path = self.root / "catalog.json"
        self.write_catalog(self.value)
        self.bundle = self.root / "catalog.sigstore.json"
        self.bundle.write_bytes(b"fixture bundle; cryptography is delegated, not simulated here")
        self.cosign = self.root / "cosign"
        self.cosign.write_bytes(b"fixture binary; never executed by these orchestration tests")
        self.cosign.chmod(0o755)
        trust = b"fixture trust root"
        (self.root / "root.b64").write_bytes(base64.b64encode(trust) + b"\n")
        self.release = {"sigstore": {"verifier": {"binary": {"sha256": hashlib.sha256(self.cosign.read_bytes()).hexdigest()}},
            "trust": {"trusted_root_evidence": "root.b64", "trusted_root_sha256": hashlib.sha256(trust).hexdigest()}}}
        (self.root / "config/release.json").write_bytes(canonical_bytes(self.release))

    def write_catalog(self, value):
        self.path.write_bytes(canonical_bytes(value) + b"\n")

    def verify(self):
        return catalog.verify(self.root, self.path, self.bundle, self.cosign, self.root)

    def select(self, expected=None, role="toolchain-install"):
        return catalog.select(self.root, self.path, self.bundle, self.cosign, expected or self.expected, role, self.root)

    def test_verifier_receives_exact_snapshots_and_all_certificate_constraints(self):
        original = self.path.read_bytes()
        def verifier(command, **kwargs):
            self.assertEqual(command[:2], [str(self.cosign), "verify-blob"])
            expected = {"--certificate-identity": catalog.SIGNER, "--certificate-oidc-issuer": catalog.ISSUER,
                "--certificate-github-workflow-repository": "eglinuxer/crossforge",
                "--certificate-github-workflow-ref": "refs/heads/main",
                "--certificate-github-workflow-trigger": "workflow_dispatch",
                "--certificate-github-workflow-sha": self.producer["source_commit"]}
            for flag, value in expected.items():
                self.assertEqual(command[command.index(flag) + 1], value)
            for flag in ("--key", "--certificate-identity-regexp", "--insecure-ignore-tlog", "--insecure-ignore-sct",
                         "--private-infrastructure", "--allow-certificate-chain"):
                self.assertNotIn(flag, command)
            self.assertEqual(Path(command[-1]).read_bytes(), original)
            self.assertNotEqual(command[-1], str(self.path))
            self.assertEqual(Path(command[command.index("--bundle") + 1]).read_bytes(), self.bundle.read_bytes())
            self.assertEqual(Path(command[command.index("--trusted-root") + 1]).read_bytes(), b"fixture trust root")
            self.assertTrue(kwargs["check"])
            # Producer files can change while the verifier runs; only the
            # already validated, private snapshot is admitted afterwards.
            self.path.write_text("untrusted path changed")
        with mock.patch.object(catalog.subprocess, "run", side_effect=verifier) as run:
            result = self.select()
        run.assert_called_once()
        self.assertEqual(result["status"], "authenticated-reference")
        self.assertEqual(result["entry"], self.entry)
        self.assertEqual(result["authentication"]["producer"], self.producer)
        self.assertEqual(result["authentication"]["catalog_sha256"], hashlib.sha256(original).hexdigest())
        self.assertNotIn("qualification", result)
        self.assertNotIn("mode", result)

    def test_signature_failure_cannot_be_treated_as_a_miss_or_fall_back(self):
        with mock.patch.object(catalog.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "cosign")):
            with self.assertRaises(subprocess.CalledProcessError):
                self.select()

    def test_changed_inputs_or_role_are_explicit_misses_after_authentication(self):
        changed = copy.deepcopy(self.expected)
        changed["parameters"]["execution"]["fixture"] = "different environment"
        for expected, role in ((changed, "toolchain-install"), (self.expected, "gcc-test-context")):
            with self.subTest(role=role), mock.patch.object(catalog.subprocess, "run") as run:
                result = self.select(expected, role)
            run.assert_called_once()
            self.assertEqual(result["status"], "missing")
            self.assertIsNone(result["entry"])

    def test_missing_role_scope_is_rejected_before_verifier(self):
        with mock.patch.object(catalog.subprocess, "run") as run:
            for role in ("unknown", "qualification"):
                with self.subTest(role=role), self.assertRaises(IdentityError):
                    self.select(role=role)
        run.assert_not_called()

    def test_catalog_rejects_relabelled_producer_bad_reference_or_receipt(self):
        for change in ("local", "fork", "dirty", "mixed-run", "mixed-attempt", "mixed-time", "receipt", "registry", "digest"):
            value = copy.deepcopy(self.value)
            entry = value["entries"][0]
            producer = value["producer"]
            if change == "local":
                producer.update(kind="local", invocation="urn:crossforge:local:fixture")
            elif change == "fork":
                producer["invocation"] = producer["invocation"].replace("eglinuxer/", "fork/")
            elif change == "dirty":
                producer["source_dirty"] = True
            elif change.startswith("mixed"):
                key = "started_at" if change == "mixed-time" else "invocation"
                entry["receipt"]["contract"]["producer"][key] = (
                    "2026-09-11T00:00:00Z" if key == "started_at" else
                    producer["invocation"].replace("123/", "456/") if change == "mixed-run" else
                    producer["invocation"].replace("attempts/2", "attempts/3"))
                entry["receipt_sha256"] = content_sha256(entry["receipt"])
            elif change == "receipt":
                entry["receipt_sha256"] = "0" * 64
            elif change == "registry":
                entry["reference"] = entry["reference"].replace("crossforge-components", "other")
            else:
                entry["reference"] = catalog.REPOSITORY + "@sha256:" + "0" * 64
            self.write_catalog(value)
            with self.subTest(change=change), mock.patch.object(catalog.subprocess, "run") as run:
                with self.assertRaises(IdentityError):
                    self.verify()
            run.assert_not_called()

    def test_ambiguous_catalogs_unknown_fields_and_schema_types_are_rejected(self):
        for change in ("duplicate", "unknown", "schema", "empty"):
            value = copy.deepcopy(self.value)
            if change == "duplicate":
                value["entries"].append(copy.deepcopy(value["entries"][0]))
            elif change == "unknown":
                value["trusted"] = True
            elif change == "schema":
                value["schema_version"] = True
            else:
                value["entries"] = []
            with self.subTest(change=change), self.assertRaises(IdentityError):
                catalog.validate(value)

    def test_duplicate_json_and_noncanonical_signed_encoding_are_rejected(self):
        for data in (b'{"schema_version":1,"schema_version":1}', self.path.read_bytes() + b"\n"):
            self.path.write_bytes(data)
            with mock.patch.object(catalog.subprocess, "run") as run, self.assertRaises(IdentityError):
                self.verify()
            run.assert_not_called()

    def test_unpinned_verifier_and_trust_root_fail_before_subprocess(self):
        for target in (self.cosign, self.root / "root.b64"):
            original = target.read_bytes()
            target.write_bytes(b"changed")
            with self.subTest(target=target.name), mock.patch.object(catalog.subprocess, "run") as run:
                with self.assertRaises((IdentityError, ValueError)):
                    self.verify()
            run.assert_not_called()
            target.write_bytes(original)

    def test_original_commit_is_constrained_by_certificate_verification(self):
        value = copy.deepcopy(self.value)
        value["producer"]["source_commit"] = "b" * 40
        value["entries"][0]["receipt"]["contract"]["producer"]["source_commit"] = "b" * 40
        value["entries"][0]["receipt_sha256"] = content_sha256(value["entries"][0]["receipt"])
        self.write_catalog(value)
        def reject_wrong_commit(command, **kwargs):
            self.assertEqual(command[command.index("--certificate-github-workflow-sha") + 1], "b" * 40)
            raise subprocess.CalledProcessError(1, command)
        with mock.patch.object(catalog.subprocess, "run", side_effect=reject_wrong_commit):
            with self.assertRaises(subprocess.CalledProcessError):
                self.select()

    def test_verifier_or_private_snapshot_mutation_is_rejected(self):
        original = self.cosign.read_bytes()
        for changed in ("verifier", "blob", "bundle", "root"):
            def mutate(command, **kwargs):
                flags = {"bundle": "--bundle", "root": "--trusted-root"}
                path = (self.cosign if changed == "verifier" else Path(command[-1]) if changed == "blob" else
                        Path(command[command.index(flags[changed]) + 1]))
                path.write_bytes(b"changed")
            with self.subTest(changed=changed), mock.patch.object(catalog.subprocess, "run", side_effect=mutate):
                with self.assertRaises(IdentityError):
                    self.verify()
            self.cosign.write_bytes(original)

    def test_symlink_and_oversized_input_are_not_read(self):
        link = self.root / "link"
        link.symlink_to(self.path)
        with self.assertRaises(IdentityError):
            catalog.regular_bytes(link, 64 * 1024 * 1024)
        with self.assertRaises(IdentityError):
            catalog.regular_bytes(self.path, 1)


if __name__ == "__main__":
    unittest.main()
