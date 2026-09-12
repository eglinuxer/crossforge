"""Synthetic signed-row boundary fixtures over the real candidate Bake graph.

No signature or target execution is claimed by these orchestration tests. The
original resolver's signature/OCI/report rejection tests remain independent.
"""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_candidate_components as fixtures
import test_candidate_qualification as execution_fixtures

from crossforge_internal import candidate_components, candidate_qualification, candidate_rows
from crossforge_internal import candidate_publication
from crossforge_internal import component_artifacts, component_build, component_catalog
from crossforge_internal import python_handoff, python_qualification, python_row_resolution, python_sdk
from crossforge_internal.identity import IdentityError, content_sha256, load_json

ROOT = fixtures.ROOT


class CandidateRowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CandidateComponentGraphTests()
        self.fixture.setUpClass()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.selected = {"cp39"}
        self.calls = []

    def resolve(self, source, original, row, execution, subjects, cosign, directory, builder, oras, docker_config):
        self.calls.append(row)
        self.assertEqual(set(subjects), {"x86_64-toolchain", "aarch64-toolchain"} |
                         {part for part, _, _ in python_handoff.PARTS})
        self.assertEqual(original["target"]["python-dev-append-" + row]["contexts"]["crossforge_python_row"],
                         "target:python-row-" + row)
        if row not in self.selected:
            return {"status": "qualification-required", "reason": "catalog-index-absent"}
        raw = load_json(self.fixture.directory / "raw.bake.json")
        bindings = candidate_components.identities(raw, self.fixture.raw_results)
        expected = candidate_rows.inputs(source, raw, row, execution, bindings)
        producer = copy.deepcopy(self.fixture.producer)
        # A different source/run is legal when every declared input matches.
        producer.update(source_commit="a" * 40, invocation="https://github.com/eglinuxer/crossforge/actions/runs/111/attempts/2")
        prior = {"schema_version": 1, "kind": "crossforge-component-qualification", "mode": "executed",
            "inputs_sha256": content_sha256(expected), "producer": producer,
            "started_at": "2026-09-10T00:00:00Z", "completed_at": "2026-09-10T00:00:03Z",
            "vertices": [{"fixture": "synthetic prior execution"}],
            "coverage": {"row": row, "status": "passed", "manifest_sha256": "c" * 64}}
        metadata = [{"path": path, "sha256": "c" * 64, "mode": "0644"}
                    for path in python_qualification.metadata_paths()]
        encoded = (json.dumps(prior, indent=2, sort_keys=True) + "\n").encode()
        next(item for item in metadata if item["path"] == python_qualification.RECORD_PATH)["sha256"] = hashlib.sha256(encoded).hexdigest()
        receipt = {"$schema": component_artifacts.RECEIPT_SCHEMA, "schema_version": 1,
            "kind": "crossforge-component-receipt", "contract": component_artifacts.contract("qualification", expected, producer),
            "artifact": {"root_digest": "sha256:" + "d" * 64, "platform_digest": "sha256:" + "e" * 64,
                         "config_digest": "sha256:" + "f" * 64, "platform": "linux/amd64"}, "metadata": metadata}
        component_build.write_json(directory / "receipt.json", receipt)
        policy = load_json(ROOT / "config/release.json")["sigstore"]
        authentication = {"schema_version": 1, "kind": "crossforge-component-catalog-authentication",
            "catalog_sha256": "1" * 64, "bundle_sha256": "2" * 64, "producer": producer,
            "signer": "https://github.com/eglinuxer/crossforge/" + component_catalog.PYTHON_ROW_WORKFLOW + "@refs/heads/main",
            "issuer": component_catalog.ISSUER, "event": "push", "verifier_sha256": policy["verifier"]["binary"]["sha256"],
            "trusted_root_sha256": policy["trust"]["trusted_root_sha256"]}
        return {"status": "verified-qualified-row", "component": expected["component"], "role": "qualification",
            "inputs_sha256": content_sha256(expected), "producer": producer,
            "reference": component_catalog.REPOSITORY + "@" + receipt["artifact"]["root_digest"],
            "catalog": {"reference": component_catalog.REPOSITORY + "@sha256:" + "a" * 64},
            "subject": {"receipt": str(directory / "receipt.json"), "receipt_sha256": content_sha256(receipt), "layout": str(directory / "oci")},
            "authentication": authentication, "verification": {"mode": "verified-prior-execution",
                "reference": "oci-layout://%s@%s" % (directory / "oci", receipt["artifact"]["platform_digest"]),
                "receipt_sha256": content_sha256(receipt), "qualification": prior}}

    def prepare(self):
        with self.fixture.patches(), mock.patch.object(python_row_resolution, "resolve", side_effect=self.resolve):
            return self.fixture.prepare()

    def test_matching_prior_row_removes_only_its_qualification_owners(self):
        ready = self.prepare()
        self.assertEqual(self.calls, python_sdk.matrix(ROOT))
        self.assertEqual(ready["schema_version"], 3)
        raw, rows = candidate_components.selection_parts(ready["selection"])
        self.assertEqual(len(raw["components"]), 34)
        self.assertEqual(set(rows), {"cp39"})
        self.assertEqual(rows["cp39"]["pin"]["producer"]["source_commit"], "a" * 40)
        selected = ready["qualification"]
        self.assertFalse(set(python_qualification.spec(ROOT, "cp39")["replay"]) & set(selected["owners"]))
        for row in python_sdk.matrix(ROOT):
            self.assertIn("python-dev-append-" + row, selected["owners"])
            if row != "cp39":
                self.assertTrue(set(python_qualification.spec(ROOT, row)["replay"]) <= set(selected["owners"]))
        self.assertFalse(candidate_components.COMPILERS & {item["path"] for item in ready["inputs"]["files"]})
        with self.fixture.patches():
            self.assertEqual(candidate_components.check(ROOT, self.fixture.binding, self.fixture.directory,
                content_sha256(ready), "fixture"), ready)

    def test_all_rows_reused_still_require_final_sdk_packaging_gcc_and_vcpkg(self):
        self.selected = set(python_sdk.matrix(ROOT))
        ready = self.prepare()
        owners = ready["qualification"]["owners"]
        for target in ("python-dev", "sdk-candidate", "packaging-qualified", "gcc-testsuite-smoke-evidence",
                "gcc-testsuite-full-qualification-evidence", "vcpkg-upstream-tier3-qualified", "toolchain-x86_64-dev",
                "toolchain-aarch64-dev"):
            self.assertIn(target, owners)
        self.assertEqual(owners["sdk-candidate"]["sdk-candidate"]["runs"], 1)
        self.assertEqual(len(ready["qualification"]["reused_rows"]), 6)
        with self.assertRaises(IdentityError):
            candidate_qualification.plan(ROOT, ready["inputs"])

    def test_missing_index_keeps_legacy_all_fresh_selection_and_plan(self):
        self.selected = set()
        ready = self.prepare()
        self.assertEqual(ready["schema_version"], 2)
        self.assertEqual(ready["selection"]["kind"], "crossforge-ci-component-recovery")
        self.assertEqual(ready["qualification"]["schema_version"], 1)
        self.assertEqual(set(ready["qualification"]["owners"]), set(candidate_qualification.policy(ROOT)))

    def test_authentication_transfer_or_invalid_fallback_never_produces_ready(self):
        for failure in (IdentityError("signature failed"), OSError("OCI transfer failed"),
                        {"status": "qualification-required", "reason": "authentication-failed"},
                        {"status": "unknown"}):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                self.fixture.directory = Path(temporary) / "prepared"
                options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with self.fixture.patches(), mock.patch.object(python_row_resolution, "resolve", **options), self.assertRaises((ValueError, OSError)):
                    candidate_components.prepare(ROOT, self.fixture.binding, self.fixture.directory,
                        Path(temporary) / "data", "fixture", Path("oras"), Path("cosign"))
                self.assertFalse((self.fixture.directory / "ready.json").exists())

    def test_durable_evidence_rejects_changed_original_producer_report_or_signature_identity(self):
        ready = self.prepare()
        raw, rows = candidate_components.selection_parts(ready["selection"])
        for change in ("receipt", "producer", "report", "signer", "schema", "unknown", "environment", "raw", "trust"):
            altered, selected_raw = copy.deepcopy(rows), copy.deepcopy(raw)
            row = altered["cp39"]
            if change == "receipt":
                row["pin"]["receipt_sha256"] = "0" * 64
            elif change == "producer":
                row["pin"]["producer"]["invocation"] += "0"
            elif change == "report":
                row["qualification"]["vertices"] = []
            elif change == "signer":
                row["authentication"]["signer"] = component_catalog.SIGNER
            elif change == "schema":
                row["authentication"]["schema_version"] = True
            elif change == "unknown":
                row["verified"] = True
            elif change == "raw":
                for item in selected_raw["components"].values():
                    item["inputs_sha256"] = "0" * 64
            elif change == "trust":
                row["authentication"]["trusted_root_sha256"] = "0" * 64
            execution = ready["qualification_execution"] if change != "environment" else {"build": {}, "host": {"changed": True}}
            with self.subTest(change=change), self.assertRaises(IdentityError):
                candidate_rows.check_inputs(ROOT, execution, altered, selected_raw)

    def test_candidate_execution_keeps_original_rows_and_requires_fresh_final_events(self):
        ready = self.prepare()
        runner = execution_fixtures.CandidateExecutionTests()
        runner.fixture = self.fixture
        inspection = python_qualification.inspection_identity()
        with mock.patch.object(self.fixture, "prepare", return_value=ready), \
                mock.patch.object(python_qualification, "inspection_identity", return_value=inspection):
            runner.run_build()
        result = load_json(self.fixture.root / "output/candidate-qualification.json")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["reused_rows"], ready["qualification"]["reused_rows"])

    def test_reused_row_cannot_excuse_cached_final_candidate_execution(self):
        ready = self.prepare()
        runner = execution_fixtures.CandidateExecutionTests()
        runner.fixture = self.fixture
        inspection = python_qualification.inspection_identity()
        with mock.patch.object(self.fixture, "prepare", return_value=ready), \
                mock.patch.object(python_qualification, "inspection_identity", return_value=inspection):
            runner.run_build(cached=True)

    def test_raw_graph_or_subject_change_is_rejected_before_reusing_prior_row(self):
        ready = self.prepare()
        graph = load_json(self.fixture.directory / "raw.bake.json")
        graph["target"]["python-row-cp39"]["args"]["CPYTHON_VERSION"] = "3.9.0"
        (self.fixture.directory / "raw.bake.json").write_text(json.dumps(graph))
        altered = copy.deepcopy(ready)
        altered["raw_graph_sha256"] = content_sha256(graph)
        (self.fixture.directory / "ready.json").write_text(json.dumps(altered))
        with self.fixture.patches(), self.assertRaises(IdentityError):
            candidate_components.check(ROOT, self.fixture.binding, self.fixture.directory, content_sha256(altered), "fixture")

    def test_published_sdk_checkpoint_preserves_prior_rows_across_later_attempts(self):
        ready = self.prepare()

        def populate(directory, owner, binding_path, digest):
            write = self.fixture.fixture.write
            write(directory / "component-selection.json", ready["selection"])
            write(directory / candidate_qualification.FILES[1], ready["inputs"])
            write(directory / candidate_qualification.FILES[2], ready["qualification"])
            progress = directory / candidate_qualification.FILES[3]
            progress.write_text("".join(json.dumps({"vertexes": [event]}) + "\n"
                for event in execution_fixtures.events_for(ready["qualification"])))
            start, end = "2026-09-11T00:00:00Z", "2026-09-11T00:00:03Z"
            result = {"schema_version": 2, "kind": "crossforge-candidate-qualification-execution", "status": "passed",
                "producer": owner, "image_digest": digest, "inputs_sha256": content_sha256(ready["inputs"]),
                "source_binding_sha256": content_sha256(load_json(binding_path)),
                "component_selection_sha256": content_sha256(ready["selection"]), "execution": ready["qualification_execution"],
                "started_at": start, "completed_at": end, "reused_rows": ready["qualification"]["reused_rows"],
                "vertices": candidate_qualification.fresh_vertices(progress, ready["qualification"], start, end)}
            write(directory / candidate_qualification.FILES[0], result)

        with mock.patch.object(execution_fixtures, "publication_fixture", side_effect=populate):
            directory, checkpoint, candidate = self.fixture.fixture.sdk()
        for attempt in (3, 4):
            output = self.fixture.root / ("recovery-%d" % attempt)
            current = dict(self.fixture.fixture.original, attempt=attempt)
            restored = candidate_publication.restore(ROOT, directory, content_sha256(checkpoint), current, "sdk", output)
            self.assertEqual(restored["producer"]["attempt"], 2)
            self.assertEqual(load_json(output / "candidate.json"), candidate)
            self.assertEqual(load_json(output / "component-selection.json"), ready["selection"])
        value = load_json(directory / "candidate-qualification-plan.json")
        value["owners"].pop("sdk-candidate")
        (directory / "candidate-qualification-plan.json").write_text(json.dumps(value))
        with self.assertRaises(IdentityError):
            candidate_publication.restore(ROOT, directory, content_sha256(checkpoint), current, "sdk", self.fixture.root / "bad")


if __name__ == "__main__":
    unittest.main()
