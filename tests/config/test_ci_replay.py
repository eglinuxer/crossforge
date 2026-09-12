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
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from crossforge_internal import ci_replay, component_build, component_resolution, qualification_execution
from crossforge_internal.identity import IdentityError, load_json

BUILD = runpy.run_path(str(ROOT / "scripts/ci-build.py"))
EXECUTION = {"buildkit_image": "moby/buildkit:v0.33.0@sha256:6c2fa84a6b61ccd72899dde4239f8d5717f05f9a8ca6f3cad185fb1a95a94de3"}


class ReplayGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("canonical replay graph checks require Buildx")

    def test_all_replays_cover_canonical_gates_without_forcing_source_compilers(self):
        expected_counts = {"toolchain-x86_64": 2, "toolchain-aarch64": 5,
            "gcc-smoke": 5, "gcc-full": 2, "vcpkg": 4, "sdk": 14}
        expected_counts.update({"python-" + row: 9 for row in ci_replay.ROWS})
        for stage in ci_replay.STAGES:
            with self.subTest(stage=stage):
                graph = BUILD["selected_graph"](stage)
                roots = BUILD["graph_roots"](graph)
                original = copy.deepcopy(graph)
                plan = ci_replay.plan(ROOT, graph, stage, roots, EXECUTION)
                count = 0
                ownership = set()
                for root, solve in plan["solves"].items():
                    override = ci_replay.override(graph, solve)
                    for target, stages in solve["owners"].items():
                        for name, details in stages.items():
                            self.assertNotIn((target, name), ownership)
                            ownership.add((target, name))
                            count += details["runs"]
                            instructions = solve["source"]["parameters"]["recipes"][target]["stages"][name]
                            self.assertFalse(any(script in "\n".join(instructions) for script in (
                                "build-gcc.sh", "build-cpython-native.sh", "build-cpython-cross.sh")))
                    self.assertEqual({target: value["no-cache-filter"] for target, value in override["target"].items()
                        if value["no-cache-filter"]}, {target: sorted(stages) for target, stages in solve["owners"].items()})
                    self.assertTrue(all(value["no-cache"] is False for value in override["target"].values()))
                    self.assertFalse(any("cache-from" in value or "cache-to" in value or "args" in value
                                         for value in override["target"].values()))
                self.assertEqual(count, expected_counts[stage])
                self.assertEqual(graph, original)
                with self.assertRaises(IdentityError):
                    ci_replay.plan(ROOT, graph, stage, [], EXECUTION)
                if stage == "sdk":
                    self.assertEqual(plan["solves"]["sdk-complete-dev"]["owners"],
                        {"sdk-complete-dev": {"sdk-complete-dev": {"runs": 1, "marker": None}}})

    def test_bake_accepts_replay_override_after_root_and_keeps_upstream_arguments(self):
        graph = BUILD["selected_graph"]("python-cp39")
        plan = ci_replay.plan(ROOT, graph, "python-cp39", ["python-cp39-dev"], EXECUTION)
        override = ci_replay.override(graph, plan["solves"]["python-cp39-dev"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "replay.json"
            path.write_text(json.dumps(override))
            resolved = json.loads(subprocess.check_output(BUILD["BAKE"] + ["python-cp39-dev", "--print", "-f", str(path)], cwd=ROOT))
        for target, definition in graph["target"].items():
            self.assertEqual(resolved["target"][target].get("args"), definition.get("args"))
            self.assertEqual(resolved["target"][target].get("contexts"), definition.get("contexts"))
            self.assertEqual(resolved["target"][target].get("no-cache-filter", []),
                             override["target"][target]["no-cache-filter"])

    def test_missing_gate_or_partial_stage_cannot_claim_replay(self):
        graph = BUILD["selected_graph"]("python-cp39")
        del graph["target"]["cpython-cp39-aarch64-qualify-build"]
        with self.assertRaises(IdentityError):
            ci_replay.plan(ROOT, graph, "python-cp39", ["python-cp39-dev"], EXECUTION)
        with self.assertRaises(IdentityError):
            ci_replay.policy(ROOT, "inputs")


class ReplayEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "progress.jsonl"
        self.owners = {"owner": {"gate": {"runs": 1, "marker": None}}}
        self.vertex = {"digest": "sha256:" + "a" * 64, "name": "[owner gate 1/1] RUN check",
            "started": "2026-09-10T00:00:01Z", "completed": "2026-09-10T00:00:02Z"}

    def check(self, vertices):
        self.path.write_text("".join(json.dumps({"vertexes": [v]}) + "\n" for v in vertices))
        return ci_replay.fresh_vertices(self.path, self.owners, "2026-09-10T00:00:00Z", "2026-09-10T00:00:03Z")

    def test_owning_events_keep_original_times_and_reject_bad_aliases(self):
        alias = dict(self.vertex, name="[consumer gate 1/1] RUN check", completed="2026-09-11T00:00:00Z")
        for sequence in ([self.vertex, alias], [alias, self.vertex]):
            self.assertEqual(self.check(sequence)[0]["completed"], self.vertex["completed"])
        for change in ({"cached": True}, {"error": "failed"}):
            for sequence in ([self.vertex, dict(alias, **change)], [dict(alias, **change), self.vertex]):
                with self.assertRaisesRegex(IdentityError, "cached or failed"):
                    self.check(sequence)

    def test_missing_cached_failed_stale_duplicate_and_wrong_row_events_cannot_pass(self):
        for vertices in ([], [dict(self.vertex, cached=True)], [dict(self.vertex, error="failed")],
                         [dict(self.vertex, completed=None)], [dict(self.vertex, completed="2026-09-11T00:00:00Z")],
                         [dict(self.vertex, name="[consumer gate 1/1] RUN check")]):
            with self.subTest(vertices=vertices), self.assertRaises(IdentityError):
                self.check(vertices)
        self.owners["owner"]["gate"]["runs"] = 2
        with self.assertRaises(IdentityError):
            self.check([self.vertex, self.vertex])
        self.owners["owner"]["gate"] = {"runs": 1, "marker": '--row "cp39"'}
        with self.assertRaises(IdentityError):
            self.check([dict(self.vertex, name=self.vertex["name"] + ' --row "cp310"')])
        self.assertEqual(len(self.check([dict(self.vertex, name=self.vertex["name"] + ' --row "cp39"')])), 1)

    def test_raw_progress_stream_separates_stdout_and_preserves_nonzero_exit(self):
        command = BUILD["stream_build_command"]([sys.executable, "-c",
            "import sys; print('ordinary stdout'); print('{\"vertexes\": []}', file=sys.stderr); sys.exit(23)"],
            self.root / "build.log", self.path)
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(json.loads(self.path.read_text()), {"vertexes": []})
        self.assertEqual((self.root / "progress.stdout.log").read_text(), "ordinary stdout\n")
        self.assertEqual(result.stdout, self.path.read_bytes())

    def run_fixture(self, change=None, build_status=0, drift=False):
        graph = {"group": {"default": {"targets": ["owner"]}}, "target": {"owner": {"output": [{"type": "cacheonly"}]}}}
        replay = {"solves": {"owner": {"owners": self.owners}}}
        function = BUILD["run_stage"]
        options = {"builder": "builder", "required": True, "oras": Path("oras"), "cosign": Path("cosign"),
                   "directory": self.root / "data"}
        environment = {"build": {}, "host": {"fixture": True}}
        def execute(command, *args, **kwargs):
            self.assertIn("--progress=rawjson", command)
            index = command.index("crossforge-replay-log")
            path = Path(command[index + 1])
            vertex = dict(self.vertex, **(change or {}))
            path.write_text(json.dumps({"vertexes": [vertex]}) + "\n")
            override = load_json(self.root / "diagnostics/replay-owner.json")
            self.assertEqual(override["target"]["owner"]["no-cache-filter"], ["gate"])
            return build_status
        patches = {"selected_graph": lambda *args: graph, "cache_catalog": lambda: ["owner"],
            "monitor_resources": lambda path, stop: None, "sample_resources": lambda path: None,
            "HEARTBEAT": {"execute": execute}, "datetime": mock.Mock(utcnow=mock.Mock(side_effect=[
                datetime(2026, 9, 10, 0, 0, 0), datetime(2026, 9, 10, 0, 0, 3)]))}
        with mock.patch.dict(function.__globals__, patches), \
             mock.patch.object(ci_replay, "plan", side_effect=[replay, {} if drift else replay]), \
             mock.patch.object(qualification_execution, "execution_identity", return_value=environment), \
             mock.patch.object(component_build, "execution_identity", return_value={}), \
             mock.patch.object(component_resolution, "bind_toolchains", return_value=(graph, {"components": {}, "required_producers": []})):
            return function("gcc-full", self.root / "diagnostics", "ghcr.io/test/cache", components=options,
                            replay_qualification=True)

    def test_ci_success_requires_fresh_evidence_and_records_exact_scope(self):
        self.assertEqual(self.run_fixture(), 0)
        report = load_json(self.root / "diagnostics/replay-result.json")
        self.assertTrue(report["complete"])
        self.assertFalse(report["qualification_receipt"])
        self.assertEqual(report["solves"]["owner"]["vertices"][0]["target"], "owner")

    def test_successful_build_with_cached_evidence_is_failed_ci(self):
        with self.assertRaises(IdentityError):
            self.run_fixture({"cached": True})
        self.assertEqual(load_json(self.root / "diagnostics/result.json")["exit_code"], 1)
        self.assertFalse((self.root / "diagnostics/replay-result.json").exists())

    def test_source_drift_cannot_report_successful_replay(self):
        with self.assertRaisesRegex(ValueError, "source or graph changed"):
            self.run_fixture(drift=True)
        self.assertEqual(load_json(self.root / "diagnostics/result.json")["exit_code"], 1)

    def test_build_failure_keeps_status_and_does_not_mint_replay_result(self):
        self.assertEqual(self.run_fixture(build_status=23), 23)
        self.assertFalse((self.root / "diagnostics/replay-result.json").exists())

    def test_replay_refuses_source_fallback_cache_writes_and_cold_modes(self):
        for options in ({}, {"components": {"required": False}},
                        {"components": {"required": True}, "write": True},
                        {"components": {"required": True}, "cold": True}):
            with self.assertRaisesRegex(ValueError, "requires authenticated components"):
                BUILD["run_stage"]("gcc-full", self.root / "never-created", "ghcr.io/test/cache",
                                    replay_qualification=True, **options)
        self.assertFalse((self.root / "never-created").exists())

    def test_replay_cannot_mix_new_failure_with_old_success_diagnostics(self):
        old = self.root / "replay-result.json"
        old.write_text('{"complete": true}\n')
        with self.assertRaisesRegex(ValueError, "new diagnostics directory"):
            BUILD["run_stage"]("gcc-full", self.root, "ghcr.io/test/cache",
                components={"required": True}, replay_qualification=True)
        self.assertEqual(old.read_text(), '{"complete": true}\n')


class ReplayWorkflowTests(unittest.TestCase):
    def test_manual_replay_is_read_only_and_actually_rejects_non_main_sources(self):
        text = (ROOT / ".github/workflows/replay-qualification.yml").read_text()
        self.assertIn("  workflow_dispatch:", text)
        self.assertNotIn("  push:", text)
        self.assertNotIn("  schedule:", text)
        self.assertIn("packages: read", text)
        self.assertNotIn("packages: write", text)
        self.assertNotIn("id-token:", text)
        self.assertIn("replay-qualification: true", text)
        self.assertIn("component-reader: true", text)
        self.assertIn("plan-components: false", text)
        options = text.split("options: [", 1)[1].split("]", 1)[0].split(", ")
        self.assertEqual(set(options), set(ci_replay.STAGES))
        script = text.split("      - name: Require the trusted main source\n", 1)[1].split("        run: |\n", 1)[1].split("      - uses:", 1)[0]
        valid = dict(os.environ, GITHUB_SERVER_URL="https://github.com", GITHUB_REPOSITORY="eglinuxer/crossforge",
                     GITHUB_REF="refs/heads/main")
        self.assertEqual(subprocess.run(["bash", "-c", script], env=valid).returncode, 0)
        for key, value in (("GITHUB_SERVER_URL", "https://example.invalid"), ("GITHUB_REPOSITORY", "fork/crossforge"),
                           ("GITHUB_REF", "refs/heads/feature")):
            self.assertNotEqual(subprocess.run(["bash", "-c", script], env=dict(valid, **{key: value})).returncode, 0)
        recovery = {"RECOVERY_RUN_ID": "123", "RECOVERY_ARTIFACT_ID": "456", "RECOVERY_SHA256": "a" * 64}
        self.assertEqual(subprocess.run(["bash", "-c", script], env=dict(valid, **recovery)).returncode, 0)
        for key, value in (("RECOVERY_RUN_ID", ""), ("RECOVERY_ARTIFACT_ID", ""), ("RECOVERY_SHA256", ""),
                           ("RECOVERY_RUN_ID", "0"), ("RECOVERY_ARTIFACT_ID", "1,2"), ("RECOVERY_SHA256", "tag")):
            with self.subTest(key=key, value=value):
                self.assertNotEqual(subprocess.run(["bash", "-c", script],
                    env=dict(valid, **dict(recovery, **{key: value}))).returncode, 0)


if __name__ == "__main__":
    unittest.main()
