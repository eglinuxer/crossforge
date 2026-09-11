"""Partial candidate retries preserve exact published and native producers."""

import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

import test_release_promotion as promotion_fixtures
import test_release_evidence as evidence_fixtures

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import candidate_recovery as recovery
    from crossforge_internal.identity import IdentityError, content_sha256
    CLI = runpy.run_path(str(ROOT / "scripts/candidate-recovery.py"))
finally:
    sys.path.pop(0)


def needs(candidate, publish=1, native=2):
    return {"publish": {"result": "success", "outputs": {
        "candidate_digest": candidate["digest"], "platform_digest": candidate["platform_manifest_digest"],
        "candidate_sha256": content_sha256(candidate), "probe_bundle_sha256": "7" * 64,
        "publish_attempt": str(publish), "identity_artifact_id": "101", "probe_artifact_id": "102"}},
        "native-aarch64": {"result": "success", "outputs": {"native_attempt": str(native),
            "native_artifact_id": "103", "native_report_sha256": "8" * 64}}}


class CandidateRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        promotion_fixtures.ReleasePromotionTests.setUpClass()
        cls.fixture = promotion_fixtures.ReleasePromotionTests()
        cls.candidate = cls.fixture.candidate
        cls.release = cls.fixture.release
        cls.schema = cls.fixture.schema
        cls.promotion = promotion_fixtures.PROMOTION

    def setUp(self):
        self.needs = needs(self.candidate)
        self.document = recovery.document(self.candidate, self.needs, 123456, 3)
        self.metadata = dict(self.fixture.run_metadata(), run_attempt=3)
        self.run = self.promotion["validate_candidate_run"](self.metadata, recovery.REPOSITORY, 123456)
        self.pages = [{"artifacts": [{"id": item["id"], "name": item["name"], "expired": False,
            "workflow_run": {"id": 123456, "head_branch": "main", "head_sha": self.candidate["source_commit"],
                "repository_id": 999, "head_repository_id": 999}}
            for item in self.document["artifacts"].values()]}]

    def test_publish_once_native_retry_then_sign_retry_preserves_all_original_identities(self):
        value = recovery.bind(self.document, content_sha256(self.candidate), self.run)
        self.assertEqual({role: item["attempt"] for role, item in value["artifacts"].items()},
                         {"identity": 1, "probes": 1, "native": 2})
        self.assertEqual(recovery.artifact_names(value), ["candidate-identity-123456-1", "candidate-signature-123456-3",
            "native-aarch64-evidence-123456-2", "native-aarch64-probes-123456-1"])
        self.assertEqual(recovery.verify_artifacts(value, self.pages), value)

    def test_incomplete_or_failed_upstream_outputs_never_choose_another_producer(self):
        for mode in ("missing-job", "missing-output", "unknown-output", "skipped", "cancelled", "failure", "bad-id", "bad-digest", "future", "reversed"):
            value = copy.deepcopy(self.needs)
            if mode == "missing-job":
                del value["publish"]
            elif mode == "missing-output":
                del value["publish"]["outputs"]["identity_artifact_id"]
            elif mode == "unknown-output":
                value["publish"]["outputs"]["unknown"] = "x"
            elif mode in ("skipped", "cancelled", "failure"):
                value["publish"]["result"] = mode
            elif mode == "bad-id":
                value["publish"]["outputs"]["probe_artifact_id"] = "101,102"
            elif mode == "bad-digest":
                value["publish"]["outputs"]["candidate_digest"] = "latest"
            elif mode == "future":
                value["native-aarch64"]["outputs"]["native_attempt"] = "4"
            else:
                value["publish"]["outputs"]["publish_attempt"] = "3"
            with self.subTest(mode=mode), self.assertRaises(IdentityError):
                recovery.document(self.candidate, value, 123456, 3)

    def test_mutated_candidate_or_source_bundle_cannot_reuse_upstream_outputs(self):
        for mode in ("digest", "source", "platform"):
            candidate = copy.deepcopy(self.candidate)
            if mode == "source":
                candidate["source_bundle"]["digest"] = "sha256:" + "a" * 64
            else:
                candidate["digest" if mode == "digest" else "platform_manifest_digest"] = "sha256:" + "a" * 64
            with self.subTest(mode=mode), self.assertRaisesRegex(IdentityError, "original published identity"):
                recovery.document(candidate, self.needs, 123456, 3)

    def test_wrong_run_source_attempt_and_manifest_are_rejected(self):
        for key, value in (("id", 123457), ("attempt", 4), ("head_sha", "a" * 40), ("repository", "fork/repo")):
            with self.subTest(key=key), self.assertRaisesRegex(IdentityError, "exact successful run"):
                recovery.bind(self.document, content_sha256(self.candidate), dict(self.run, **{key: value}))
        with self.assertRaisesRegex(IdentityError, "manifest differs"):
            recovery.bind(self.document, "a" * 64, self.run)

    def test_strict_lineage_schema_and_artifact_attempts(self):
        for mode in ("schema", "unknown", "duplicate-id", "wrong-name", "different-publish", "future-native", "boolean-id"):
            value = copy.deepcopy(self.document)
            if mode == "schema":
                value["schema_version"] = True
            elif mode == "unknown":
                value["qualification"] = "passed"
            elif mode == "duplicate-id":
                value["artifacts"]["native"]["id"] = 101
            elif mode == "wrong-name":
                value["artifacts"]["identity"]["name"] = "candidate-identity-123457-1"
            elif mode == "boolean-id":
                value["artifacts"]["identity"]["id"] = True
            else:
                role, attempt = ("probes", 2) if mode == "different-publish" else ("native", 4)
                value["artifacts"][role].update(attempt=attempt, name="%s-123456-%d" % (recovery.PREFIXES[role], attempt))
            with self.subTest(mode=mode), self.assertRaises(IdentityError):
                recovery.validate(value)

    def test_service_metadata_rejects_missing_expired_duplicate_or_foreign_artifacts(self):
        for mode in ("missing", "duplicate", "expired", "name", "run", "source", "fork", "branch"):
            pages = copy.deepcopy(self.pages)
            item = pages[0]["artifacts"][0]
            if mode == "missing":
                pages[0]["artifacts"].pop(0)
            elif mode == "duplicate":
                pages.append(copy.deepcopy(pages[0]))
            elif mode == "expired":
                item["expired"] = True
            elif mode == "name":
                item["name"] = "replacement"
            else:
                key, value = {"run": ("id", 123457), "source": ("head_sha", "a" * 40),
                    "fork": ("head_repository_id", 1000), "branch": ("head_branch", "other")}[mode]
                item["workflow_run"][key] = value
            with self.subTest(mode=mode), self.assertRaises(IdentityError):
                recovery.verify_artifacts(self.document, pages)

    def test_promotion_schema_two_carries_lineage_and_keeps_legacy_schema_one(self):
        value = self.promotion["promotion_document"](self.release, self.candidate, content_sha256(self.candidate), self.run, self.document)
        self.assertEqual(value["schema_version"], 2)
        self.promotion["validate_document"](value, self.release, self.schema)
        self.assertEqual(value["candidate_recovery"], self.document)
        legacy = self.promotion["promotion_document"](self.release, self.candidate, content_sha256(self.candidate), self.run)
        self.assertEqual(legacy["schema_version"], 1)
        self.promotion["validate_document"](legacy, self.release, self.schema)
        for mode in ("missing", "downgrade", "wrong-attempt", "artifact"):
            changed = copy.deepcopy(value)
            if mode == "missing":
                del changed["candidate_recovery"]
            elif mode == "downgrade":
                changed["schema_version"] = 1
            elif mode == "wrong-attempt":
                changed["candidate_recovery"]["sign_attempt"] = 4
            else:
                changed["artifacts"] = legacy["artifacts"]
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.promotion["validate_document"](changed, self.release, self.schema)

    def test_cli_recovery_resolves_ids_and_legacy_keeps_exact_attempt_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (("candidate.json", self.candidate), ("candidate-recovery.json", self.document),
                                ("run.json", self.metadata), ("artifacts.json", self.pages)):
                (root / name).write_text(json.dumps(value))
            args = ["select", "--signature-directory", temporary, "--candidate-run", str(root / "run.json"),
                    "--artifacts", str(root / "artifacts.json"), "--expected-run-id", "123456"]
            with mock.patch.dict(os.environ, GITHUB_OUTPUT=str(root / "output")):
                self.assertEqual(CLI["main"](args), 0)
                values = dict(line.split("=", 1) for line in (root / "output").read_text().splitlines())
                self.assertEqual(values["identity_id"], "101")
                self.assertEqual(values["identity_name"], "")
                (root / "candidate-recovery.json").unlink()
                (root / "output").unlink()
                self.assertEqual(CLI["main"](args), 0)
                values = dict(line.split("=", 1) for line in (root / "output").read_text().splitlines())
                self.assertEqual(values["identity_id"], "")
                self.assertEqual(values["identity_name"], "candidate-identity-123456-3")

    def test_cli_records_report_bytes_and_rejects_a_different_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "candidate.json").write_text(json.dumps(self.candidate))
            report = root / "native.json"
            report.write_bytes(b"explicit synthetic native report fixture")
            self.needs["native-aarch64"]["outputs"]["native_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
            environment = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": recovery.REPOSITORY,
                "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ID": "123456",
                "GITHUB_RUN_ATTEMPT": "3", "GITHUB_SHA": self.candidate["source_commit"]}
            args = ["create", "--needs-json", json.dumps(self.needs), "--candidate", str(root / "candidate.json"),
                    "--native-report", str(report), "--output", str(root / "recovery.json")]
            with mock.patch.dict(os.environ, environment):
                self.assertEqual(CLI["main"](args), 0)
                report.write_bytes(b"changed")
                self.assertEqual(CLI["main"](args), 1)
            self.assertEqual(json.loads((root / "recovery.json").read_text())["artifacts"]["native"]["attempt"], 2)


class RecoveryDurableEvidenceTests(unittest.TestCase):
    def test_durable_evidence_preserves_lineage_and_rejects_changed_native_payloads(self):
        helper = evidence_fixtures.ReleaseEvidenceTests()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = helper.fixture(temporary)
            paths = fixture["paths"]
            original = fixture["promotion"]
            producer_needs = needs(fixture["candidate"], native=1)
            producer_needs["publish"]["outputs"]["probe_bundle_sha256"] = hashlib.sha256(paths["native-aarch64-probes.tar"].read_bytes()).hexdigest()
            producer_needs["native-aarch64"]["outputs"]["native_report_sha256"] = hashlib.sha256(paths["native-aarch64.json"].read_bytes()).hexdigest()
            lineage = recovery.document(fixture["candidate"], producer_needs, 123456, 2)
            promotion = evidence_fixtures.PROMOTION["promotion_document"](fixture["release"], fixture["candidate"],
                original["candidate_manifest_sha256"], original["candidate_run"], lineage)
            helper.write_json(paths["release-promotion.json"], promotion)
            helper.create(fixture)
            for name in ("native-aarch64.json", "native-aarch64-probes.tar"):
                old = paths[name].read_bytes()
                paths[name].write_bytes(old + b" ")
                with helper.validators(), self.assertRaisesRegex(evidence_fixtures.EVIDENCE["ReleaseEvidenceError"], "original candidate"):
                    evidence_fixtures.EVIDENCE["validate_inputs"](paths, fixture["release"], fixture["schema"])
                paths[name].write_bytes(old)


class CandidateRecoveryWorkflowTests(unittest.TestCase):
    def test_actual_upstream_ids_are_used_instead_of_current_attempt_names(self):
        workflow = (ROOT / ".github/workflows/candidate.yml").read_text()
        native = workflow.split("  native-aarch64:\n", 1)[1].split("  sign-candidate:\n", 1)[0]
        sign = workflow.split("  sign-candidate:\n", 1)[1]
        for block in (native, sign):
            downloads = block.split("      - name: Download immutable native probe bundle\n", 1)[1].split("      - name:", 1)[0]
            self.assertIn("artifact-ids: ${{ needs.publish.outputs.probe_artifact_id }}", downloads)
            self.assertNotIn("name: native-aarch64-probes-", downloads)
            self.assertIn("merge-multiple: true", downloads)
            self.assertIn("candidate-recovery.py upstream", block)
        self.assertIn("artifact-ids: ${{ needs.native-aarch64.outputs.native_artifact_id }}", sign)
        self.assertLess(sign.index("native-aarch64-release.py validate"), sign.index('"$cosign" sign --yes'))
        self.assertLess(sign.index("candidate-recovery.py create"), sign.index('"$cosign" sign --yes'))
        self.assertIn("--host-machine", native)
        self.assertIn('test "$host_machine" = aarch64', native)


if __name__ == "__main__":
    unittest.main()
