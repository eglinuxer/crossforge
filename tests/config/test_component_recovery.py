"""A retry cannot silently select a different component or a different source."""

import copy
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import test_ci_component_binding as toolchain_fixtures
import test_ci_python_binding as python_fixtures

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_build, component_recovery as recovery, component_resolution as resolution
    from crossforge_internal.identity import IdentityError, content_sha256, load_json
    BUILD = runpy.run_path(str(ROOT / "scripts/ci-build.py"))
finally:
    sys.path.pop(0)


def selection(component="toolchain/x86_64", role="toolchain-install"):
    return {"status": "verified-build-component", "component": component, "role": role,
        "inputs_sha256": "a" * 64, "catalog": {"reference": recovery.REPOSITORY + "@sha256:" + "b" * 64},
        "reference": recovery.REPOSITORY + "@sha256:" + "c" * 64,
        "subject": {"receipt": "/old/receipt.json", "receipt_sha256": "d" * 64, "layout": "/old/oci"},
        "context": "oci-layout:///old/oci@sha256:" + "c" * 64,
        "producer": {"kind": "github-actions", "source_commit": "e" * 40, "source_dirty": False,
            "invocation": "https://github.com/eglinuxer/crossforge/actions/runs/123/attempts/2",
            "started_at": "2026-09-10T00:00:00Z"}}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.context = {"stage": "toolchain-x86_64", "targets": ["toolchain-x86_64-dev"],
            "source_commit": "f" * 40, "source_inventory_sha256": "0" * 64}
        self.name = "x86_64-toolchain-install"
        self.results = {self.name: selection()}
        self.expected = {self.name: {"component": "toolchain/x86_64", "role": "toolchain-install"}}
        self.value = recovery.document(self.context, self.results, self.expected)

    def test_recovery_preserves_producer_and_digest_without_local_paths(self):
        pins = recovery.verify(self.value, content_sha256(self.value), self.context, self.expected)
        self.assertEqual(pins[self.name]["producer"], self.results[self.name]["producer"])
        self.assertNotIn("/old/", json.dumps(pins))
        pins[self.name]["producer"]["source_commit"] = "1" * 40
        self.assertEqual(self.value["components"][self.name]["producer"]["source_commit"], "e" * 40)

    def test_independent_digest_rejects_a_rewritten_recovery_document(self):
        changed = copy.deepcopy(self.value)
        changed["components"][self.name]["catalog_reference"] = recovery.REPOSITORY + "@sha256:" + "2" * 64
        with self.assertRaisesRegex(IdentityError, "selected SHA256"):
            recovery.verify(changed, content_sha256(self.value), self.context, self.expected)

    def test_same_source_stage_roots_and_execution_inputs_are_required(self):
        for field, value in (("stage", "gcc-full"), ("targets", ["other-root"]),
                             ("source_commit", "1" * 40), ("source_inventory_sha256", "1" * 64)):
            current = dict(self.context, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(IdentityError, "recovery source"):
                recovery.verify(self.value, content_sha256(self.value), current, self.expected)

    def test_missing_extra_and_wrong_role_components_are_rejected(self):
        for mode in ("missing", "extra", "role", "component"):
            value = copy.deepcopy(self.value)
            if mode == "missing":
                value["components"].clear()
            elif mode == "extra":
                value["components"]["extra"] = value["components"][self.name]
            else:
                value["components"][self.name][mode] = "gcc-test-context" if mode == "role" else "toolchain/aarch64"
            with self.subTest(mode=mode), self.assertRaises(IdentityError):
                recovery.verify(value, content_sha256(value), self.context, self.expected)

    def test_strict_schema_digest_only_registry_and_github_producer(self):
        for mode in ("schema", "unknown", "tag", "registry", "producer", "dirty", "short-commit", "unsorted"):
            value = copy.deepcopy(self.value)
            pin = value["components"][self.name]
            if mode == "schema":
                value["schema_version"] = True
            elif mode == "unknown":
                pin["qualification"] = True
            elif mode == "tag":
                pin["catalog_reference"] = recovery.REPOSITORY + ":latest"
            elif mode == "registry":
                pin["reference"] = "ghcr.io/other/artifact@sha256:" + "a" * 64
            elif mode == "producer":
                pin["producer"]["kind"] = "local"
            elif mode == "dirty":
                pin["producer"]["source_dirty"] = True
            elif mode == "short-commit":
                value["context"]["source_commit"] = "123abcd"
            else:
                value["context"]["targets"] = ["z", "a"]
            with self.subTest(mode=mode), self.assertRaises(IdentityError):
                recovery.validate(value)

    def test_revalidated_result_cannot_replace_any_original_pin(self):
        original = recovery.pin(self.results[self.name])
        for mode in ("producer", "catalog", "reference", "inputs_sha256", "receipt", "missing"):
            result = copy.deepcopy(self.results[self.name])
            if mode == "producer":
                result[mode]["invocation"] = "https://github.com/eglinuxer/crossforge/actions/runs/124/attempts/1"
            elif mode == "catalog":
                result[mode]["reference"] = recovery.REPOSITORY + "@sha256:" + "1" * 64
            elif mode == "receipt":
                result["subject"]["receipt_sha256"] = "1" * 64
            elif mode == "missing":
                result = {"status": "build-required"}
            else:
                result[mode] = recovery.REPOSITORY + "@sha256:" + "1" * 64 if mode == "reference" else "1" * 64
            with self.subTest(mode=mode), self.assertRaises(IdentityError):
                recovery.verify_selection(original, result)
        moved = copy.deepcopy(self.results[self.name])
        moved["subject"].update(receipt="/new/receipt.json", layout="/new/oci")
        moved["context"] = "oci-layout:///new/oci@sha256:" + "c" * 64
        self.assertEqual(recovery.verify_selection(original, moved), moved)


class RecoveryBindingTests(unittest.TestCase):
    def fixture(self, cls):
        fixture = cls()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_toolchain_binder_forwards_only_exact_catalogs_and_rejects_replacement(self):
        fixture = self.fixture(toolchain_fixtures.ComponentBindingTests)
        expected = recovery.requirements(ROOT, fixture.graph, False)
        results = {name: selection(**identity) for name, identity in expected.items()}
        pins = {name: recovery.pin(result) for name, result in results.items()}
        def resolve(source, graph, arch, role, *args, **kwargs):
            name = arch + "-" + role
            self.assertEqual(kwargs, {"catalog_reference": pins[name]["catalog_reference"]})
            return results[name]
        def bind(values):
            output = Path(tempfile.mkdtemp(dir=str(fixture.root)))
            return resolution.bind_toolchains(ROOT, fixture.graph, {}, Path("cosign"), output / "data",
                output / "evidence", "builder", Path("oras"), recovery=values)
        with mock.patch.object(resolution, "toolchain", side_effect=resolve) as resolver:
            graph, result = bind(pins)
            self.assertEqual(result["required_producers"], [])
            self.assertEqual(resolver.call_count, 3)
            self.assertTrue(graph["target"]["consumer"]["contexts"]["arm"].startswith("oci-layout://"))
            resolver.reset_mock()
            with self.assertRaisesRegex(IdentityError, "recovery set differs"):
                bind({})
            resolver.assert_not_called()
            results[sorted(results)[0]]["producer"]["source_commit"] = "1" * 40
            with self.assertRaisesRegex(IdentityError, "original recovery pin"):
                bind(pins)

    def test_python_binder_preserves_dependency_order_and_rejects_missing_dependencies(self):
        fixture = self.fixture(python_fixtures.PythonBindingTests)
        expected = recovery.requirements(ROOT, fixture.graph, True)
        results = {name: selection(**identity) for name, identity in expected.items()}
        pins = {name: recovery.pin(result) for name, result in results.items()}
        seen = []
        def resolve(source, graph, row, arch, kind, execution, subjects, *args, **kwargs):
            name = row + "-" + ("build" if arch == "build" else arch + "-" + kind)
            seen.append(name)
            self.assertEqual(kwargs, {"catalog_reference": pins[name]["catalog_reference"]})
            if arch != "build":
                self.assertEqual(subjects["build-python"], results["cp39-build"]["subject"])
            return results[name]
        def bind(values):
            output = Path(tempfile.mkdtemp(dir=str(fixture.root)))
            return resolution.bind_python(ROOT, fixture.graph, fixture.graph, fixture.toolchains, {}, Path("cosign"),
                output / "data", output / "evidence", "builder", Path("oras"), recovery=values)
        with mock.patch.object(resolution, "python", side_effect=resolve) as resolver:
            graph, result = bind(pins)
            self.assertEqual(seen[0], "cp39-build")
            self.assertEqual(set(seen), set(expected))
            self.assertEqual(result["required_producers"], [])
            resolver.reset_mock()
            with self.assertRaisesRegex(IdentityError, "recovery set differs"):
                bind({})
            resolver.assert_not_called()
            fixture.toolchains["aarch64-toolchain-install"] = {"status": "build-required"}
            with self.assertRaisesRegex(IdentityError, "dependency cannot be replaced"):
                bind(pins)
            fixture.toolchains["aarch64-toolchain-install"] = fixture.toolchains["x86_64-toolchain-install"]
            results["cp39-build"] = {"status": "build-required"}
            with self.assertRaisesRegex(IdentityError, "unverified or missing"):
                bind(pins)


class RecoveryStageTests(unittest.TestCase):
    def setUp(self):
        RecoveryTests.setUp(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def run_fixture(self, name="first", previous=None, build_status=0, drift_at=None):
        graph = {"group": {"default": {"targets": self.context["targets"]}},
            "target": {self.context["targets"][0]: {"output": [{"type": "cacheonly"}]}}}
        function = BUILD["run_stage"]
        directory = self.root / name
        options = {"builder": "builder", "required": True, "oras": Path("oras"), "cosign": Path("cosign"),
            "directory": self.root / (name + "-data"), "record_recovery": True}
        if previous is not None:
            options["recovery"] = previous
        contexts = [self.context] * 3
        if drift_at is not None:
            contexts[drift_at] = dict(self.context, source_inventory_sha256="2" * 64)
        def execute(*args, **kwargs):
            self.assertTrue((directory / "component-recovery.json").is_file())
            return build_status
        self.execute = mock.Mock(side_effect=execute)
        patches = {"selected_graph": lambda *args: graph, "cache_catalog": lambda: self.context["targets"],
            "monitor_resources": lambda *args: None, "sample_resources": lambda *args: None,
            "HEARTBEAT": {"execute": self.execute}}
        with mock.patch.dict(function.__globals__, patches), \
             mock.patch.object(component_build, "execution_identity", return_value={}), \
             mock.patch.object(recovery, "context", side_effect=contexts), \
             mock.patch.object(recovery, "requirements", return_value=self.expected), \
             mock.patch.object(resolution, "bind_toolchains", return_value=(graph,
                 {"components": self.results, "required_producers": []})) as binder:
            status = function(self.context["stage"], directory, "ghcr.io/test/cache", components=options)
            self.binder = binder
            return status

    def test_failed_gate_still_exports_selection_and_retry_uses_identical_pins(self):
        self.assertEqual(self.run_fixture(build_status=23), 23)
        digest = load_json(self.root / "first/result.json")["component_recovery_sha256"]
        document = self.root / "first/component-recovery.json"
        self.assertEqual(digest, content_sha256(load_json(document)))
        self.assertEqual(self.run_fixture("retry", {"path": document, "sha256": digest}), 0)
        self.assertEqual(load_json(self.root / "retry/component-recovery.json"), load_json(document))
        self.assertEqual(self.binder.call_args[1]["recovery"], self.value["components"])

    def test_invalid_recovery_fails_before_build_and_does_not_export_new_selection(self):
        document = self.root / "input.json"
        component_build.write_json(document, self.value)
        with self.assertRaisesRegex(IdentityError, "selected SHA256"):
            self.run_fixture(previous={"path": document, "sha256": "3" * 64})
        self.execute.assert_not_called()
        self.assertFalse((self.root / "first/component-recovery.json").exists())
        self.assertIsNone(load_json(self.root / "first/result.json")["component_recovery_sha256"])

    def test_source_drift_before_or_during_build_cannot_report_success(self):
        for index in (1, 2):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "inputs changed"):
                self.run_fixture(name=str(index), drift_at=index)
            self.assertEqual(self.execute.call_count, 0 if index == 1 else 1)
            self.assertNotEqual(load_json(self.root / str(index) / "result.json")["exit_code"], 0)

    def test_recovery_rejects_old_diagnostics_fallback_and_incomplete_cli_pairs(self):
        for options in ({"required": False, "record_recovery": True}, {"required": True, "record_recovery": True}):
            with self.assertRaises(ValueError):
                BUILD["run_stage"]("sdk", self.root, "ghcr.io/test/cache", components=options)
        main = BUILD["main"]
        common = ["ci-build.py", "run", "sdk", "--directory", str(self.root / "diagnostics")]
        for flags in (["--component-recovery", "input.json"], ["--component-recovery-sha256", "a" * 64],
                      ["--record-component-recovery"], ["--record-component-recovery", "--require-components"]):
            with mock.patch.object(sys, "argv", common + flags), self.assertRaises(ValueError):
                main()


class RecoveryActionTests(unittest.TestCase):
    def test_composite_passes_quoted_recovery_and_rejects_partial_input(self):
        action = (ROOT / ".github/actions/run-build-stage/action.yml").read_text()
        script = textwrap.dedent(action.split("    - name: Build and measure\n", 1)[1]
            .split("      run: |\n", 1)[1].split("    - name:", 1)[0])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            (root / "scripts/ci-build.py").write_text("import json, sys; print(json.dumps(sys.argv[1:]))\n")
            valid = dict(os.environ, BUILD_STAGE="sdk", WRITE_CACHE="false", COLD_BUILD="false", SELECTED_TARGETS="",
                COMPONENT_READER="true", PYTHON_COMPONENTS="true", REPLAY_QUALIFICATION="true",
                REBUILD_SOURCES="false", SOURCE_BUILDER="",
                COMPONENT_BUILDER="builder", COMPONENT_ORAS="oras", COMPONENT_COSIGN="cosign",
                RUNNER_TEMP=temporary, COMPONENT_RECOVERY="path with spaces/input.json", COMPONENT_RECOVERY_SHA256="a" * 64)
            result = subprocess.run(["bash", "-c", script], cwd=temporary, env=valid,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(result.stdout)
            self.assertIn("--record-component-recovery", args)
            self.assertEqual(args[args.index("--component-recovery") + 1], valid["COMPONENT_RECOVERY"])
            for change in ({"COMPONENT_RECOVERY": ""}, {"COMPONENT_RECOVERY_SHA256": ""}, {"COMPONENT_READER": "false"}):
                self.assertNotEqual(subprocess.run(["bash", "-c", script], cwd=temporary,
                    env=dict(valid, **change), stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode, 0)


if __name__ == "__main__":
    unittest.main()
