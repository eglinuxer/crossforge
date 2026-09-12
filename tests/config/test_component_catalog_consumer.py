"""The catalog reader must reach the same fresh gates with original subjects."""

import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

import test_component_handoff as fixtures


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_build, component_resolution
    from crossforge_internal.identity import IdentityError, load_json
finally:
    sys.path.pop(0)


class CatalogConsumerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ComponentHandoffTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.output = self.fixture.root / "consume"
        self.current = dict(self.fixture.producer, source_commit="b" * 40,
            invocation="https://github.com/eglinuxer/crossforge/actions/runs/456/attempts/1")
        self.function = fixtures.PILOT["catalog_consumer_run"]

    def resolve(self, source, graph, arch, role, execution, cosign, directory, *args):
        directory.mkdir(parents=True)
        (directory / "oci").mkdir()
        (directory / "oci/large-blob").write_text("OCI bytes must not enter diagnostics")
        (directory / "catalog").mkdir()
        for name in ("catalog.json", "catalog.sigstore.json", "manifest.json"):
            (directory / "catalog" / name).write_text("original " + name)
        receipt = self.fixture.components[role]
        result = {"status": "verified-build-component", "context": "verified " + role,
            "producer": self.fixture.producer, "reason": "authenticated-catalog-and-matching-artifact",
            "subject": {"receipt": str(directory / "receipt.json"), "receipt_sha256": receipt["receipt_sha256"],
                "layout": str(directory / "oci")}}
        for name, value in (("inputs.json", receipt["receipt"]["contract"]["inputs"]),
                            ("receipt.json", receipt["receipt"]), ("resolution.json", result)):
            component_build.write_json(directory / name, value)
        return result

    def test_prior_subjects_reach_fresh_gates_and_only_small_source_evidence_is_preserved(self):
        graph = {"target": {}}
        def source_graph(*args):
            self.output.mkdir()
            return graph
        def gates(graph, execution, producer, subjects, references, directory, builder, config):
            self.assertEqual(producer, self.current)
            self.assertEqual(set(subjects), set(fixtures.handoff.ROLES))
            for role in subjects:
                self.assertEqual(load_json(subjects[role]["receipt"])["contract"]["producer"], self.fixture.producer)
                self.assertEqual(references[role], "verified " + role)
            return {"qualifications": "fresh gates fixture"}
        with mock.patch.dict(self.function.__globals__, checked_source=lambda: self.current,
                source_graph=source_graph, consume_subjects=gates), \
             mock.patch.object(component_build, "execution_identity", return_value=self.fixture.execution), \
             mock.patch.object(component_resolution, "toolchain", side_effect=self.resolve) as resolve:
            result = self.function(self.output, "builder", Path("oras"), Path("cosign"), None, "catalog@sha256:fixture")
        self.assertEqual(result["qualifications"], "fresh gates fixture")
        self.assertEqual(resolve.call_count, 2)
        for call in resolve.call_args_list:
            self.assertEqual(call[0][-1], "catalog@sha256:fixture")
        for role in fixtures.handoff.ROLES:
            self.assertEqual(result["component_resolutions"][role]["producer"], self.fixture.producer)
            self.assertEqual((self.output / "report" / role / "catalog/catalog.sigstore.json").read_text(),
                "original catalog.sigstore.json")
        self.assertFalse(any(path.name in ("oci", "large-blob") for path in (self.output / "report").rglob("*")))

    def test_missing_component_does_not_start_qualification_or_build_a_replacement(self):
        def source_graph(*args):
            self.output.mkdir()
            return {"target": {}}
        missing = {"status": "build-required", "reason": "catalog-index-absent"}
        gates = mock.Mock()
        with mock.patch.dict(self.function.__globals__, checked_source=lambda: self.current,
                source_graph=source_graph, consume_subjects=gates), \
             mock.patch.object(component_build, "execution_identity", return_value=self.fixture.execution), \
             mock.patch.object(component_resolution, "toolchain", return_value=missing):
            with self.assertRaisesRegex(IdentityError, "component producer required"):
                self.function(self.output, "builder", Path("oras"), Path("cosign"))
        gates.assert_not_called()
        self.assertEqual(load_json(self.output / "report/toolchain-install-resolution.json"), missing)

    def test_cli_rejects_ambiguous_trust_modes_before_source_or_registry_access(self):
        main = fixtures.PILOT["main"]
        common = ["--output", str(self.output), "--builder", "builder", "--oras", "oras"]
        source = mock.Mock(side_effect=AssertionError("source should not be reached"))
        with mock.patch.dict(main.__globals__, checked_source=source):
            for arguments in (["produce", "--catalog-reference", "bad"], ["consume", "--cosign", "cosign"],
                              ["consume-catalog"], ["consume-catalog", "--cosign", "cosign", "--handoff", "handoff"]):
                self.assertEqual(main(arguments + common), 1)
        source.assert_not_called()


if __name__ == "__main__":
    unittest.main()
