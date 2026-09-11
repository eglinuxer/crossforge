"""Forced source rebuilds cannot be confused with cache removal or gate replay."""

import copy
from datetime import datetime
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from crossforge_internal import ci_source_replay as replay, qualification_execution
from crossforge_internal.identity import IdentityError, load_json

BUILD = runpy.run_path(str(ROOT / "scripts/ci-build.py"))
EXECUTION = {"buildkit_image": "moby/buildkit:v0.33.0@sha256:6c2fa84a6b61ccd72899dde4239f8d5717f05f9a8ca6f3cad185fb1a95a94de3"}


class SourceReplayGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("source replay graph checks require Buildx")

    def test_all_source_scopes_force_only_their_selected_compilers(self):
        for stage in replay.STAGES:
            with self.subTest(stage=stage):
                graph = BUILD["selected_graph"](stage)
                original = copy.deepcopy(graph)
                roots = BUILD["graph_roots"](graph)
                plan = replay.plan(ROOT, graph, stage, roots, EXECUTION)
                solve = plan["solves"][roots[0]]
                instructions = []
                for target, stages in solve["owners"].items():
                    for name in stages:
                        instructions += solve["source"]["parameters"]["recipes"][target]["stages"][name]
                text = "\n".join(instructions)
                count = sum(details["runs"] for stages in solve["owners"].values() for details in stages.values())
                if stage.startswith("toolchain-"):
                    self.assertEqual(count, 2)
                    self.assertIn("build-binutils.sh", text)
                    self.assertIn("build-gcc.sh", text)
                    self.assertNotIn("build-cpython", text)
                else:
                    self.assertEqual(count, 6)
                    self.assertNotIn("build-gcc.sh", text)
                    self.assertNotIn("build-binutils.sh", text)
                    self.assertIn("build-cpython-native.sh", text)
                    self.assertIn("build-cpython-cross.sh", text)
                override = replay.override(graph, solve)
                for target, value in override["target"].items():
                    self.assertEqual(value["no-cache-filter"], sorted(solve["owners"].get(target, {})))
                    self.assertFalse(value["no-cache"])
                    self.assertEqual(value["output"], [{"type": "cacheonly"}])
                    self.assertEqual(value["cache-to"], [])
                    self.assertEqual(value["tags"], [])
                self.assertEqual(graph, original)
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "override.json"
                    path.write_text(json.dumps(override))
                    resolved = json.loads(subprocess.check_output(BUILD["BAKE"] + [roots[0], "--print", "-f", str(path)], cwd=ROOT))
                for target, value in resolved["target"].items():
                    self.assertEqual(value.get("args"), graph["target"][target].get("args"))
                    self.assertEqual(value.get("contexts"), graph["target"][target].get("contexts"))
                    self.assertEqual(value.get("no-cache-filter", []), override["target"][target]["no-cache-filter"])

    def test_missing_source_producer_or_partial_scope_cannot_claim_rebuild(self):
        graph = BUILD["selected_graph"]("python-cp39")
        del graph["target"]["cpython-cross-cp39-aarch64"]
        with self.assertRaises(IdentityError):
            replay.plan(ROOT, graph, "python-cp39", ["python-cp39-dev"], EXECUTION)
        with self.assertRaises(IdentityError):
            replay.plan(ROOT, {}, "python-cp39", ["cpython-build-cp39"], EXECUTION)
        for stage in ("sdk", "inputs", "gcc-smoke", "vcpkg", "python-cp315"):
            with self.assertRaises(IdentityError):
                replay.policy(stage)


class SourceReplayExecutionTests(unittest.TestCase):
    def run_case(self, directory, change=None, build_status=0, drift=None, cold=False):
        graph = {"group": {"default": {"targets": ["owner"]}}, "target": {"owner": {"output": [{"type": "cacheonly"}]}}}
        owners = {"owner": {"compiler": {"runs": 1, "marker": None}}}
        plan = {"solves": {"owner": {"owners": owners}}}
        environment = {"build": EXECUTION, "host": {"fixture": True}}
        function = BUILD["run_stage"]
        def execute(command, *args, **kwargs):
            self.assertIn("--progress=rawjson", command)
            self.assertEqual(command[command.index("--builder") + 1], "source-builder")
            index = command.index("crossforge-replay-log")
            path = Path(command[index + 1])
            vertex = {"digest": "sha256:" + "a" * 64, "name": "[owner compiler 1/1] RUN build",
                      "started": "2026-09-10T00:00:01Z", "completed": "2026-09-10T00:00:02Z"}
            vertex.update(change or {})
            path.write_text(json.dumps({"vertexes": [vertex]}) + "\n")
            return build_status
        patches = {"selected_graph": lambda *args: graph, "cache_catalog": lambda: ["owner"],
            "monitor_resources": lambda path, stop: None, "sample_resources": lambda path: None,
            "HEARTBEAT": {"execute": execute}, "datetime": mock.Mock(utcnow=mock.Mock(side_effect=[
                datetime(2026, 9, 10, 0, 0, 0), datetime(2026, 9, 10, 0, 0, 3)]))}
        with mock.patch.dict(function.__globals__, patches), \
             mock.patch.object(replay, "plan", side_effect=[plan, {} if drift == "source" else plan]), \
             mock.patch.object(qualification_execution, "execution_identity", side_effect=[environment,
                 {} if drift == "environment" else environment]):
            return function("toolchain-x86_64", directory, "ghcr.io/test/cache", cold=cold,
                            rebuild_sources=True, source_builder="source-builder")

    def test_success_records_fresh_compiler_evidence_and_keeps_cold_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            for cold in (False, True):
                directory = Path(temporary) / str(cold)
                self.assertEqual(self.run_case(directory, cold=cold), 0)
                value = load_json(directory / "replay-result.json")
                self.assertEqual(value["kind"], "crossforge-ci-source-replay-observation")
                self.assertTrue(value["complete"])
                self.assertFalse(value["qualification_receipt"])
                self.assertEqual(value["solves"]["owner"]["vertices"][0]["started"], "2026-09-10T00:00:01Z")
                result = load_json(directory / "result.json")
                self.assertTrue(result["rebuild_sources"])
                self.assertFalse(result["replay_qualification"])
                self.assertFalse(result["cache_write"])
                self.assertEqual(bool(load_json(directory / "cache.json")["target"]["owner"]["cache-from"]), not cold)

    def test_zero_build_exit_is_insufficient_without_fresh_unchanged_evidence(self):
        cases = [{"change": {"cached": True}}, {"change": {"error": "failed"}},
                 {"change": {"completed": None}}, {"change": {"name": "[another compiler 1/1] RUN build"}},
                 {"change": {"started": "2026-09-09T00:00:00Z"}}, {"drift": "source"}, {"drift": "environment"}]
        with tempfile.TemporaryDirectory() as temporary:
            for index, options in enumerate(cases):
                directory = Path(temporary) / str(index)
                with self.subTest(options=options), self.assertRaises(ValueError):
                    self.run_case(directory, **options)
                self.assertEqual(load_json(directory / "result.json")["exit_code"], 1)
                self.assertFalse((directory / "replay-result.json").exists())

    def test_failed_build_preserves_exit_without_creating_success_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "diagnostics"
            self.assertEqual(self.run_case(directory, build_status=23), 23)
            self.assertFalse((directory / "replay-result.json").exists())

    def test_incompatible_modes_and_old_diagnostics_fail_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "new"
            options = {"rebuild_sources": True, "source_builder": "source-builder"}
            for change in ({"source_builder": None}, {"components": {}}, {"write": True},
                           {"replay_qualification": True}, {"rebuild_sources": False}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    BUILD["run_stage"]("toolchain-x86_64", directory, "ghcr.io/test/cache", **dict(options, **change))
                self.assertFalse(directory.exists())
            directory.mkdir()
            (directory / "replay-result.json").write_text('{"complete":true}\n')
            with self.assertRaisesRegex(ValueError, "new diagnostics directory"):
                BUILD["run_stage"]("toolchain-x86_64", directory, "ghcr.io/test/cache", **options)
            self.assertEqual((directory / "replay-result.json").read_text(), '{"complete":true}\n')

    def test_cli_passes_explicit_builder_and_source_mode(self):
        arguments = ["ci-build.py", "run", "python-cp39", "--directory", "/tmp/source-replay-cli-fixture",
                     "--rebuild-sources", "--source-builder", "source-builder"]
        main = BUILD["main"]
        with mock.patch.object(sys, "argv", arguments), mock.patch.dict(main.__globals__, run_stage=mock.Mock(return_value=0)):
            self.assertEqual(main(), 0)
            self.assertEqual(main.__globals__["run_stage"].call_args[1], {
                "replay_qualification": False, "rebuild_sources": True, "source_builder": "source-builder"})


class SourceReplayWorkflowTests(unittest.TestCase):
    def test_composite_routes_the_explicit_source_mode_and_rejects_conflicting_options(self):
        action = (ROOT / ".github/actions/run-build-stage/action.yml").read_text()
        script = textwrap.dedent(action.split("    - name: Build and measure\n", 1)[1]
            .split("      run: |\n", 1)[1].split("    - name:", 1)[0])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            (root / "scripts/ci-build.py").write_text("import json, sys; print(json.dumps(sys.argv[1:]))\n")
            valid = dict(os.environ, BUILD_STAGE="python-cp39", WRITE_CACHE="false", COLD_BUILD="false", SELECTED_TARGETS="",
                COMPONENT_READER="false", PYTHON_COMPONENTS="false", REPLAY_QUALIFICATION="false",
                REBUILD_SOURCES="true", SOURCE_BUILDER="source-builder", RUNNER_TEMP=temporary,
                COMPONENT_RECOVERY="", COMPONENT_RECOVERY_SHA256="")
            result = subprocess.run(["bash", "-c", script], cwd=temporary, env=valid,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(result.stdout)
            self.assertIn("--rebuild-sources", args)
            self.assertEqual(args[args.index("--source-builder") + 1], "source-builder")
            self.assertNotIn("--replay-qualification", args)
            self.assertNotIn("--require-components", args)
            for change in ({"SOURCE_BUILDER": ""}, {"REBUILD_SOURCES": "false"}, {"WRITE_CACHE": "true"},
                           {"COMPONENT_READER": "true"}, {"REPLAY_QUALIFICATION": "true"}, {"PYTHON_COMPONENTS": "true"},
                           {"COMPONENT_RECOVERY": "prior.json"}):
                result = subprocess.run(["bash", "-c", script], cwd=temporary, env=dict(valid, **change),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")

    def test_manual_workflow_has_no_writer_and_rejects_non_main_events(self):
        text = (ROOT / ".github/workflows/replay-sources.yml").read_text()
        for forbidden in ("  push:", "  schedule:", "packages:", "id-token:", "component-reader: true"):
            self.assertNotIn(forbidden, text)
        self.assertIn("rebuild-sources: true", text)
        self.assertIn("source-builder: ${{ steps.buildx.outputs.builder }}", text)
        options = text.split("options: [", 1)[1].split("]", 1)[0].split(", ")
        self.assertEqual(set(options), set(replay.STAGES))
        script = text.split("        run: |\n", 1)[1].split("      - uses:", 1)[0]
        valid = dict(os.environ, GITHUB_SERVER_URL="https://github.com", GITHUB_REPOSITORY="eglinuxer/crossforge",
                     GITHUB_REF="refs/heads/main", GITHUB_EVENT_NAME="workflow_dispatch")
        self.assertEqual(subprocess.run(["bash", "-c", script], env=valid).returncode, 0)
        for key, value in (("GITHUB_SERVER_URL", "https://example.invalid"), ("GITHUB_REPOSITORY", "fork/crossforge"),
                           ("GITHUB_REF", "refs/heads/feature"), ("GITHUB_EVENT_NAME", "push")):
            self.assertNotEqual(subprocess.run(["bash", "-c", script], env=dict(valid, **{key: value})).returncode, 0)


if __name__ == "__main__":
    unittest.main()
