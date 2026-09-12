"""Published source and SDK checkpoints survive downstream failures unchanged."""

import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import test_release_evidence as evidence_fixtures
import test_candidate_recovery as recovery_fixtures
import test_component_recovery as component_fixtures

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import candidate_publication as publication, candidate_recovery as recovery
    from crossforge_internal.identity import IdentityError, content_sha256, load_json
    CLI = runpy.run_path(str(ROOT / "scripts/candidate-publication.py"))
finally:
    sys.path.pop(0)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixture_root = self.root / "fixture"
        fixture_root.mkdir()
        self.fixture = evidence_fixtures.ReleaseEvidenceTests().fixture(fixture_root)
        self.release, self.candidate = self.fixture["release"], self.fixture["candidate"]
        self.source = self.root / "source"
        self.source.mkdir()
        self.original = {"source_commit": self.candidate["source_commit"], "run_id": 123456, "attempt": 1, "phase": "source"}
        self.source_digest, self.source_platform = self.image(self.source, "source-index.json", "source-build-metadata.json", "source-bundle", "5")
        self.binding = evidence_fixtures.SOURCE["source_binding"](self.release, self.candidate["source_commit"],
            self.source_digest, self.source_platform, self.candidate["source_bundle"]["archive"]["sha256"], self.candidate["source_bundle"]["archive"]["size"])
        self.write(self.source / "source-binding.json", self.binding)
        for name in ("source-bundle.json", "sbom-generator-image.json"):
            shutil.copyfile(str(self.fixture["paths"][name]), str(self.source / name))
        self.source_value = publication.seal(ROOT, self.source, self.original)

    def write(self, path, value):
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

    def image(self, directory, index, metadata, target, digit):
        platform = "sha256:" + digit * 64
        value = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
            {"digest": platform, "platform": {"os": "linux", "architecture": "amd64"}}]}
        self.write(directory / index, value)
        digest = "sha256:" + hashlib.sha256((directory / index).read_bytes()).hexdigest()
        self.write(directory / metadata, {target: {"containerimage.digest": digest}})
        return digest, platform

    def sdk(self):
        directory = self.root / "sdk"
        directory.mkdir()
        for name in publication.SOURCE_FILES:
            shutil.copyfile(str(self.source / name), str(directory / name))
        digest, platform = self.image(directory, "candidate-index.json", "build-metadata.json", "sdk-candidate", "3")
        candidate = evidence_fixtures.CANDIDATE["candidate_document"](self.release, self.original["source_commit"], digest, platform,
            self.source_digest, self.source_platform, load_json(self.source / "source-bundle.json"))
        self.write(directory / "candidate.json", candidate)
        from crossforge_internal import component_recovery
        result = component_fixtures.selection()
        context = {"stage": "candidate-sdk", "targets": ["sdk-candidate"], "source_commit": self.original["source_commit"],
                   "source_inventory_sha256": "0" * 64}
        self.write(directory / "component-selection.json", component_recovery.document(context,
            {"x86_64-toolchain-install": result}, {"x86_64-toolchain-install": {"component": result["component"], "role": result["role"]}}))
        owner = dict(self.original, phase="sdk", attempt=2)
        return directory, publication.seal(ROOT, directory, owner, self.source_value), candidate

    def environment(self, attempt=3):
        return {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": recovery.REPOSITORY,
            "GITHUB_WORKFLOW_REF": recovery.REPOSITORY + "/" + recovery.WORKFLOW + "@refs/heads/main",
            "GITHUB_WORKFLOW_SHA": self.original["source_commit"], "GITHUB_SHA": self.original["source_commit"],
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "123456",
            "GITHUB_RUN_ATTEMPT": str(attempt), "GITHUB_OUTPUT": str(self.root / "outputs")}

    def test_source_then_sdk_retries_restore_original_digest_and_source_producer(self):
        directory, value, candidate = self.sdk()
        for attempt in (3, 4):
            output = self.root / str(attempt)
            observed = publication.restore(ROOT, directory, content_sha256(value), dict(self.original, attempt=attempt), "sdk", output)
            self.assertEqual(observed, value)
            self.assertEqual(load_json(output / "candidate.json"), candidate)
            self.assertEqual(load_json(output / "source-binding.json"), self.binding)
            self.assertEqual(observed["source"]["producer"], self.original)
        self.assertEqual(load_json(self.root / "3/candidate.json"), load_json(self.root / "4/candidate.json"))

    def test_wrong_checkpoint_or_payload_never_writes_consumer_inputs(self):
        for mode in ("digest", "payload", "missing", "extra", "symlink"):
            directory = self.root / mode
            shutil.copytree(str(self.source), str(directory))
            digest = content_sha256(self.source_value)
            if mode == "digest":
                digest = "a" * 64
            elif mode == "payload":
                (directory / "source-binding.json").write_text("changed")
            elif mode == "missing":
                (directory / "source-binding.json").unlink()
            elif mode == "extra":
                (directory / "oci").mkdir()
            else:
                (directory / "source-binding.json").unlink()
                (directory / "source-binding.json").symlink_to(self.source / "source-binding.json")
            output = self.root / (mode + "-output")
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                publication.restore(ROOT, directory, digest, dict(self.original, attempt=3), "source", output)
            self.assertFalse(output.exists())

    def test_other_run_source_phase_future_attempt_and_existing_outputs_are_rejected(self):
        for field, value in (("source_commit", "a" * 40), ("run_id", 123457), ("attempt", 0)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                publication.verify(ROOT, self.source, content_sha256(self.source_value), dict(self.original, **{field: value}), "source")
        with self.assertRaisesRegex(IdentityError, "another producer"):
            publication.verify(ROOT, self.source, content_sha256(self.source_value), self.original, "sdk")
        output = self.root / "existing"
        output.mkdir()
        (output / "source-binding.json").write_text("keep")
        with self.assertRaisesRegex(IdentityError, "replace existing"):
            publication.restore(ROOT, self.source, content_sha256(self.source_value), self.original, "source", output)
        self.assertEqual((output / "source-binding.json").read_text(), "keep")

    def test_recomputed_checkpoint_cannot_hide_raw_oci_or_source_binding_drift(self):
        for mode in ("metadata", "raw-index", "archive", "tag", "release"):
            directory = self.root / mode
            shutil.copytree(str(self.source), str(directory))
            value = copy.deepcopy(self.source_value)
            if mode == "metadata":
                self.write(directory / "source-build-metadata.json", {"source-bundle": {"containerimage.digest": "sha256:" + "a" * 64}})
            elif mode == "raw-index":
                (directory / "source-index.json").write_text('{}\n')
            elif mode == "archive":
                identity = load_json(directory / "source-bundle.json")
                identity["archive"]["sha256"] = "a" * 64
                self.write(directory / "source-bundle.json", identity)
            elif mode == "tag":
                value["image"]["reference"] += "-replacement"
            else:
                value["release_sha256"] = "a" * 64
            value["files"] = {name: publication.file_hash(directory / name) for name in publication.SOURCE_FILES}
            self.write(directory / "checkpoint.json", value)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                publication.verify(ROOT, directory, content_sha256(value), self.original, "source")

    def test_sdk_requires_unchanged_same_run_source_parent_and_exact_schema(self):
        directory, original, _ = self.sdk()
        for mode in ("parent", "files", "unknown", "schema", "nested"):
            value = copy.deepcopy(original)
            if mode == "parent":
                value["source"]["producer"]["run_id"] = 123457
            elif mode == "files":
                value["files"]["source-binding.json"] = "a" * 64
            elif mode == "unknown":
                value["qualification"] = True
            elif mode == "schema":
                value["schema_version"] = True
            else:
                value["source"] = copy.deepcopy(original)
            with self.subTest(mode=mode), self.assertRaises(IdentityError):
                publication.validate(value)

    def test_sdk_checkpoint_preserves_component_selection_and_accepts_exact_legacy_payloads(self):
        directory, value, _ = self.sdk()
        self.assertEqual(value["schema_version"], 2)
        selection = load_json(directory / "component-selection.json")
        for commit in ("a" * 40, self.original["source_commit"]):
            altered = copy.deepcopy(selection)
            altered["context"]["source_commit"] = commit
            self.write(directory / "component-selection.json", altered)
            changed = copy.deepcopy(value)
            changed["files"]["component-selection.json"] = publication.file_hash(directory / "component-selection.json")
            self.write(directory / "checkpoint.json", changed)
            if commit == self.original["source_commit"]:
                publication.verify(ROOT, directory, content_sha256(changed), dict(self.original, attempt=3), "sdk")
            else:
                with self.assertRaisesRegex(IdentityError, "another source or stage"):
                    publication.verify(ROOT, directory, content_sha256(changed), dict(self.original, attempt=3), "sdk")
        legacy = copy.deepcopy(value)
        legacy["schema_version"] = 1
        del legacy["files"]["component-selection.json"]
        (directory / "component-selection.json").unlink()
        self.write(directory / "checkpoint.json", legacy)
        restored = publication.restore(ROOT, directory, content_sha256(legacy), dict(self.original, attempt=3), "sdk", self.root / "legacy")
        self.assertEqual(restored, legacy)

    def test_cli_restores_original_publication_lineage_into_signing_recovery(self):
        directory, value, candidate = self.sdk()
        environment = self.environment()
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(CLI["main"](["restore", "--phase", "sdk", "--directory", str(directory),
                "--sha256", content_sha256(value), "--output", str(self.root / "restored")]), 0)
        restored = dict(line.split("=", 1) for line in (self.root / "outputs").read_text().splitlines())
        producer_needs = recovery_fixtures.needs(candidate, publish=3, native=4)
        producer_needs["publish"]["outputs"].update({key: restored[key] for key in recovery.PUBLICATION_OUTPUTS})
        document = recovery.document(candidate, producer_needs, 123456, 5)
        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(document["publication"], {"source": {"attempt": 1, "checkpoint_sha256": content_sha256(self.source_value)},
                                                 "sdk": {"attempt": 2, "checkpoint_sha256": content_sha256(value)}})
        selection = load_json(directory / "component-selection.json")
        bound_needs = copy.deepcopy(producer_needs)
        bound_needs["publish"]["outputs"]["component_selection_sha256"] = restored["component_selection_sha256"]
        bound = recovery.document(candidate, bound_needs, 123456, 5, selection)
        self.assertEqual(bound["schema_version"], 3)
        self.assertEqual(bound["component_selection"], selection)
        for altered in (None, dict(selection, extra=True)):
            with self.assertRaisesRegex(IdentityError, "original SDK checkpoint"):
                recovery.document(candidate, bound_needs, 123456, 5, altered)
        with self.assertRaisesRegex(IdentityError, "legacy candidate"):
            recovery.document(candidate, producer_needs, 123456, 5, selection)
        for key, replacement in (("source_attempt", "4"), ("sdk_attempt", "4"), ("sdk_checkpoint_sha256", "tag")):
            wrong = copy.deepcopy(producer_needs)
            wrong["publish"]["outputs"][key] = replacement
            with self.subTest(key=key), self.assertRaises(IdentityError):
                recovery.document(candidate, wrong, 123456, 5)
        for mode in ("missing", "downgrade", "future", "unknown"):
            wrong = copy.deepcopy(document)
            if mode == "missing":
                del wrong["publication"]
            elif mode == "downgrade":
                wrong["schema_version"] = 1
            elif mode == "future":
                wrong["publication"]["sdk"]["attempt"] = 4
            else:
                wrong["publication"]["sdk"]["qualified"] = True
            with self.subTest(mode=mode), self.assertRaises(IdentityError):
                recovery.validate(wrong)

    def test_cli_seals_both_phases_from_workflow_paths_with_an_independent_parent_digest(self):
        inputs = self.root / "runner-temp"
        publication.restore(ROOT, self.source, content_sha256(self.source_value), self.original, "source", inputs)
        sealed_source = self.root / "source-sealed"
        with mock.patch.dict(os.environ, self.environment(attempt=1)):
            self.assertEqual(CLI["main"](["seal", "--phase", "source", "--root", str(inputs), "--output", str(sealed_source)]), 0)
        self.assertEqual(load_json(sealed_source / "checkpoint.json"), self.source_value)
        sdk, expected, _ = self.sdk()
        for name in set(publication.SDK_FILES) - set(publication.SOURCE_FILES):
            shutil.copyfile(str(sdk / name), str(inputs / name))
        # SDK sealing must preserve the independently verified source checkpoint,
        # including when temporary working copies have subsequently changed.
        (inputs / "source-binding.json").write_text("changed temporary copy")
        with mock.patch.dict(os.environ, self.environment(attempt=2)):
            for digest, destination, status in (("a" * 64, "wrong-parent", 1),
                    (content_sha256(self.source_value), "sdk-sealed", 0)):
                output = self.root / destination
                self.assertEqual(CLI["main"](["seal", "--phase", "sdk", "--root", str(inputs), "--output", str(output),
                    "--source-checkpoint", str(sealed_source), "--source-sha256", digest]), status)
                if status:
                    self.assertFalse(output.exists())
                else:
                    self.assertEqual(load_json(output / "checkpoint.json"), expected)

    def test_cli_refuses_missing_upstream_and_untrusted_execution_before_consumption(self):
        needs = {"source-publication": {"result": "success", "outputs": {"checkpoint_artifact_id": "123", "checkpoint_sha256": content_sha256(self.source_value)}}}
        with mock.patch.dict(os.environ, self.environment()):
            self.assertEqual(CLI["main"](["upstream", "--phase", "source", "--needs-json", json.dumps(needs)]), 0)
            for change in ("skipped", "failure", "cancelled"):
                needs["source-publication"]["result"] = change
                self.assertEqual(CLI["main"](["upstream", "--phase", "source", "--needs-json", json.dumps(needs)]), 1)
            with mock.patch.dict(os.environ, GITHUB_WORKFLOW_REF="wrong"):
                self.assertEqual(CLI["main"](["upstream", "--phase", "source", "--needs-json", json.dumps(needs)]), 1)


class PublicationWorkflowTests(unittest.TestCase):
    def test_checkpoint_dependency_chain_keeps_source_and_sdk_builds_out_of_consumer_retry(self):
        workflow = (ROOT / ".github/workflows/candidate.yml").read_text()
        source = workflow.split("  source-publication:\n", 1)[1].split("  sdk-publication:\n", 1)[0]
        sdk = workflow.split("  sdk-publication:\n", 1)[1].split("  publish:\n", 1)[0]
        consumer = workflow.split("  publish:\n", 1)[1].split("  native-aarch64:\n", 1)[0]
        self.assertIn("needs: qualify", source)
        self.assertIn("needs: [source-publication]", sdk)
        self.assertIn("needs: [sdk-publication]", consumer)
        self.assertIn("source-bundle.output=type=image,push=true", source)
        self.assertNotIn("sdk-candidate.output=type=image,push=true", source)
        self.assertIn("sdk-candidate.output=type=image,push=true", sdk)
        self.assertNotIn("docker buildx bake source-bundle", sdk)
        self.assertNotIn("push=true", consumer)
        self.assertNotIn("packages: write", consumer)
        self.assertNotIn("docker login", consumer)
        self.assertLess(sdk.index("restore --phase source"), sdk.index("Build once and push"))
        self.assertLess(sdk.index("Prove the complete source payload"), sdk.index("Build once and push"))
        for phase, block in (("source", sdk), ("sdk", consumer)):
            self.assertIn("artifact-ids: ${{ needs.%s-publication.outputs.checkpoint_artifact_id }}" % phase, block)
            self.assertIn("--sha256 \"$CHECKPOINT_SHA256\"", block)
        for gate in ("Prove anonymous public availability", "Validate public SDK provenance and SBOM", "Run the anonymous non-root candidate",
                     "Build downstream consumers through the public launcher", "Build native AArch64 probes from the immutable candidate"):
            self.assertIn(gate, consumer)


if __name__ == "__main__":
    unittest.main()
