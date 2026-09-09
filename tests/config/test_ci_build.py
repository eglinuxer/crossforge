import copy
import json
import os
from pathlib import Path
import runpy
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
BUILD = runpy.run_path(str(ROOT / "scripts/ci-build.py"))
PLAN = runpy.run_path(str(ROOT / "scripts/ci-plan.py"))


class HostedBuildTests(unittest.TestCase):
    def test_timeout_still_terminates_streamed_build(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            command = ["timeout", "--signal=TERM", "--kill-after=1s", "1s"]
            command += BUILD["stream_build_command"](
                ["bash", "-c", "echo compiling; sleep 30"], log)
            result = subprocess.run(command, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 124)
            self.assertIn(b"compiling\n", result.stdout)
            self.assertEqual(log.read_bytes(), result.stdout)

    def test_build_output_streams_before_exit_and_preserves_failure_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'build $(literal) " log.txt'
            command = BUILD["stream_build_command"]([
                sys.executable, "-u", "-c",
                "import sys; print('stdout'); print('stderr', file=sys.stderr); "
                "sys.stdin.readline(); sys.exit(23)",
            ], log)
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                self.assertTrue(select.select([process.stdout], [], [], 10)[0],
                                "build output must be visible before command completion")
                first = os.read(process.stdout.fileno(), 4096)
                self.assertIsNone(process.poll())
                rest, _ = process.communicate(b"continue\n", timeout=10)
                self.assertEqual(process.returncode, 23)
                self.assertEqual(first + rest, b"stdout\nstderr\n")
                self.assertEqual(log.read_bytes(), first + rest)
            finally:
                if process.poll() is None:
                    process.communicate(b"continue\n", timeout=10)

    def graph(self):
        return {"group": {"default": {"targets": ["full"]},
                          "full": {"targets": ["evidence", "sdk"]}},
                "target": {name: {"output": [{"type": "cacheonly"}]}
                           for name in ("compiler", "evidence", "sdk")}}

    def test_group_exports_resolve_to_concrete_targets_without_publishing_images(self):
        graph = self.graph()
        original = copy.deepcopy(graph)
        override = BUILD["cache_override"](graph, "ghcr.io/test/buildcache", write=True)
        self.assertEqual(graph, original)
        exports = {name for name, config in override["target"].items() if config["cache-to"]}
        self.assertEqual(exports, {"evidence", "sdk"})
        references = []
        for name, config in override["target"].items():
            self.assertEqual(set(config), {"cache-from", "cache-to"})
            for export in config["cache-to"]:
                self.assertEqual(export["mode"], "max")
                references.append(export["ref"])
        self.assertEqual(len(set(references)), 2)

    def test_sdk_aggregates_export_final_layers_without_changing_build_graph(self):
        names = ["python-dev", "sdk-complete-dev", "python-cp312-dev"]
        graph = {"group": {"default": {"targets": names}},
                 "target": {name: {"output": [{"type": "cacheonly"}]}
                            for name in names}}
        original = copy.deepcopy(graph)
        override = BUILD["cache_override"](graph, "ghcr.io/test/cache", write=True)
        self.assertEqual(graph, original)
        self.assertEqual(BUILD["STAGES"]["sdk"], ["python-matrix", "sdk-complete-dev"])
        for name, mode in zip(names, ["min", "min", "max"]):
            export = override["target"][name]["cache-to"]
            self.assertEqual(len(export), 1)
            self.assertEqual(export[0]["mode"], mode)
            self.assertNotIn("ignore-error", export[0])

    def test_read_only_and_cold_modes_cannot_write_cache(self):
        override = BUILD["cache_override"](self.graph(), "ghcr.io/test/cache", cold=True)
        self.assertTrue(all(value == {"cache-from": [], "cache-to": []}
                            for value in override["target"].values()))
        override = BUILD["cache_override"](self.graph(), "ghcr.io/test/cache")
        self.assertTrue(all(not value["cache-to"] for value in override["target"].values()))

    def test_qt_webengine_is_required_before_host_and_in_final_gate(self):
        workflow = (ROOT / ".github/workflows/verify-builds.yml").read_text()
        webengine = workflow.split("\n  qt-host-webengine:\n", 1)[1].split("\n  qt-host:\n", 1)[0]
        self.assertIn("needs: [plan, qt-inputs]", webengine)
        self.assertIn("stage: qt-host-webengine", webengine)
        self.assertEqual(BUILD["STAGES"]["qt-host-webengine"], ["qt-host-webengine-build"])
        final_gate = workflow.split("\n  verified:\n", 1)[1]
        self.assertIn("qt-inputs, qt-host-webengine, qt-host, qt]", final_gate)

    def test_internal_dockerfile_dependencies_import_shared_caches(self):
        override = BUILD["cache_override"](
            self.graph(), "ghcr.io/test/cache", imports=["host-build-common-locked"])
        self.assertEqual(override["target"]["sdk"]["cache-from"], [
            {"type": "registry", "ref": "ghcr.io/test/cache:main-host-build-common-locked"}])

    def test_imports_follow_dockerfile_and_python_row(self):
        def target(file, row=None):
            return {"dockerfile": file, "args": {"CPYTHON_ROW": row} if row else {}}
        graph = {"group": {"default": {"targets": ["sdk"]}}, "target": {
            "sdk": target("packaging"), "python": target("python", "cp314"),
            "host": target("host")}}
        catalog = {"sdk": target("packaging"), "row314": target("python", "cp314"),
                   "row313": target("python", "cp313"), "aggregate": target("python"),
                   "internal-host": target("host"), "qt": target("qt")}
        original = copy.deepcopy(graph)
        result = BUILD["cache_override"](graph, "ghcr.io/test/cache", imports=catalog)
        refs = lambda name: [x["ref"].split(":main-")[1]
                             for x in result["target"][name]["cache-from"]]
        self.assertEqual(refs("sdk"), ["sdk"])
        self.assertEqual(refs("python"), ["row314", "aggregate"])
        self.assertEqual(refs("host"), ["internal-host"])
        self.assertEqual(graph, original)

    def test_parent_exports_reach_transitive_linked_inputs(self):
        graph = {"group": {"default": {"targets": ["qt"]}}, "target": {
            "qt": {"dockerfile": "qt", "contexts": {
                "ffmpeg": "target:ffmpeg", "compiler": "target:compiler"}},
            "ffmpeg": {"dockerfile": "qt", "contexts": {"host": "target:host"}},
            "compiler": {"dockerfile": "tools", "contexts": {"host": "target:host"}},
            "host": {"dockerfile": "rpm"},
            "unrelated": {"dockerfile": "python"}}}
        catalog = {"webengine": {"dockerfile": "qt"},
                   "tools": {"dockerfile": "tools"},
                   "rpm": {"dockerfile": "rpm"},
                   "python": {"dockerfile": "python"}}
        original = copy.deepcopy(graph)
        result = BUILD["cache_override"](
            graph, "ghcr.io/test/cache", imports=catalog, write=True)["target"]
        refs = lambda name: [x["ref"].split(":main-")[1]
                             for x in result[name]["cache-from"]]
        self.assertCountEqual(refs("qt"), ["webengine", "tools", "rpm"])
        self.assertCountEqual(refs("compiler"), ["tools", "webengine", "rpm"])
        self.assertCountEqual(refs("host"), ["rpm", "tools", "webengine"])
        self.assertEqual(len(refs("host")), 3)
        self.assertEqual(refs("unrelated"), ["python"])
        self.assertFalse(result["host"]["cache-to"])
        self.assertTrue(result["qt"]["cache-to"])
        self.assertEqual(graph, original)
        cold = BUILD["cache_override"](
            graph, "ghcr.io/test/cache", imports=catalog, cold=True)["target"]
        self.assertTrue(all(not value["cache-from"] for value in cold.values()))

    def test_min_aggregate_imports_stay_local_while_max_inputs_propagate(self):
        graph = {"group": {"default": {"targets": ["sdk-complete-dev"]}}, "target": {
            "sdk-complete-dev": {"dockerfile": "sdk", "contexts": {"python": "target:python-dev"}},
            "python-dev": {"dockerfile": "python", "contexts": {"row": "target:row"}},
            "row": {"dockerfile": "python", "contexts": {"host": "target:host"}},
            "host": {"dockerfile": "host"}}}
        catalog = {**graph["target"], "python-cp314-dev": {"dockerfile": "python"}}
        original = copy.deepcopy(graph)
        result = BUILD["cache_override"](
            graph, "ghcr.io/test/cache", imports=catalog, write=True)["target"]
        for name, config in result.items():
            references = {item["ref"].split(":main-")[1] for item in config["cache-from"]}
            for aggregate in ("sdk-complete-dev", "python-dev"):
                self.assertEqual(aggregate in references, name == aggregate)
        self.assertIn("ghcr.io/test/cache:main-python-cp314-dev",
                      [item["ref"] for item in result["host"]["cache-from"]])
        self.assertEqual(result["sdk-complete-dev"]["cache-to"][0]["mode"], "min")
        self.assertEqual(graph, original)

    def test_consumers_import_transitive_dependency_caches(self):
        graph = {"group": {"default": {"targets": ["qt"]}}, "target": {
            "qt": {"dockerfile": "qt.Dockerfile", "contexts": {"base": "target:host"}},
            "host": {"dockerfile": "host.Dockerfile", "contexts": {"rpm": "target:rpm"}},
            "rpm": {"dockerfile": "rpm.Dockerfile"}}}
        catalog = {name: dict(value) for name, value in graph["target"].items()}
        original = copy.deepcopy(graph)
        result = BUILD["cache_override"](
            graph, "ghcr.io/test/cache", imports=catalog)["target"]
        for name in ("qt", "host"):
            self.assertIn("ghcr.io/test/cache:main-rpm",
                          [source["ref"] for source in result[name]["cache-from"]])
        self.assertIn("ghcr.io/test/cache:main-host",
                      [source["ref"] for source in result["qt"]["cache-from"]])
        self.assertEqual(graph, original)
        cold = BUILD["cache_override"](
            graph, "ghcr.io/test/cache", imports=catalog, cold=True)["target"]
        self.assertTrue(all(not value["cache-from"] for value in cold.values()))

    def test_workflow_plan_accepts_main_push_and_rejects_untrusted_writers(self):
        workflow = (ROOT / ".github/workflows/verify-builds.yml").read_text()
        block = workflow.split("        run: |\n", 1)[1].split("\n  inputs:", 1)[0]
        script = "\n".join(line[10:] for line in block.splitlines())
        valid = {"PROFILE": "full", "WRITE_CACHE": "true",
                 "GITHUB_REPOSITORY": "eglinuxer/crossforge",
                 "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push"}
        cases = [({"GITHUB_EVENT_NAME": event}, True)
                 for event in ("push", "schedule", "workflow_dispatch")]
        cases.append(({"PROFILE": "qt", "GITHUB_EVENT_NAME": "workflow_dispatch"}, True))
        cases += [({field: value}, False) for field, value in (
            ("GITHUB_REPOSITORY", "other/crossforge"),
            ("GITHUB_REF", "refs/heads/feature"),
            ("GITHUB_REF", "refs/tags/v0.1.0"),
            ("GITHUB_EVENT_NAME", "pull_request"),
            ("GITHUB_EVENT_NAME", "pull_request_target"))]
        for changes, accepted in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "outputs"
                result = subprocess.run(
                    ["bash", "-c", script], capture_output=True, text=True,
                    env={**os.environ, **valid, **changes, "GITHUB_OUTPUT": str(output)})
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                if accepted:
                    expected = ("active=true\nsdk=false\ngcc=false\nqt=true\n"
                                if changes.get("PROFILE") == "qt" else
                                "active=true\nsdk=true\ngcc=true\nqt=false\n")
                    self.assertEqual(output.read_text(), expected)

    def test_cache_writer_rejects_pr_fork_and_non_main_dispatch(self):
        valid = {"GITHUB_REPOSITORY": "eglinuxer/crossforge",
                 "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch"}
        for event in ("push", "schedule", "workflow_dispatch"):
            BUILD["require_writer"]({**valid, "GITHUB_EVENT_NAME": event})
        for field, value in (("GITHUB_REPOSITORY", "other/crossforge"),
                             ("GITHUB_REF", "refs/heads/feature"),
                             ("GITHUB_EVENT_NAME", "pull_request"),
                             ("GITHUB_EVENT_NAME", "pull_request_target"),
                             ("GITHUB_REF", "refs/tags/v0.1.0")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                BUILD["require_writer"]({**valid, field: value})
        with self.assertRaises(ValueError):
            BUILD["cache_override"](self.graph(), "ghcr.io/eglinuxer/crossforge", write=True)

    def test_failed_build_preserves_exit_status_log_and_resource_observation(self):
        function = BUILD["run_stage"]
        namespace = function.__globals__
        heartbeat = dict(BUILD["HEARTBEAT"])
        execute = heartbeat["execute"]
        def fail(command, *args, **kwargs):
            prefix = command[:command.index("crossforge-build-log") + 2]
            command = prefix + ["bash", "-c", "echo compiler-failed; exit 23"]
            return execute(command, *args, **kwargs)
        heartbeat["execute"] = fail
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            namespace, {"read_graph": lambda targets: self.graph(),
                        "cache_catalog": lambda: ["sdk"], "HEARTBEAT": heartbeat}
        ), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            root = Path(directory)
            result = function("sdk", root, "ghcr.io/test/cache")
            self.assertEqual(result, 23)
            self.assertIn("compiler-failed", (root / "build.log").read_text())
            self.assertEqual(json.loads((root / "result.json").read_text())["exit_code"], 23)
            self.assertGreaterEqual(len((root / "resources.jsonl").read_text().splitlines()), 2)

    def test_sequential_roots_preserve_gates_and_stop_on_failure(self):
        function = BUILD["run_stage"]
        for statuses, expected in (([0, 0], 0), ([19], 19)):
            calls = []

            def execute(command, *args, **kwargs):
                calls.append(command)
                return statuses[len(calls) - 1]

            with self.subTest(statuses=statuses), tempfile.TemporaryDirectory() as directory:
                with mock.patch.dict(function.__globals__, {
                    "read_graph": lambda targets: self.graph(),
                    "cache_catalog": lambda: ["sdk"],
                    "HEARTBEAT": {"execute": execute},
                }), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
                    self.assertEqual(function("sdk", Path(directory), "ghcr.io/test/cache"),
                                     expected)
                roots = [command[command.index("--progress=plain") - 1] for command in calls]
                self.assertEqual(roots, ["evidence", "sdk"][:len(statuses)])
                for root, command in zip(roots, calls):
                    self.assertEqual(command[-1], str(Path(directory) / ("metadata-" + root + ".json")))

    def test_sequential_consumers_export_only_the_current_root(self):
        function = BUILD["run_stage"]
        observed = []
        graph = self.graph()
        graph["target"]["sdk"]["contexts"] = {"input": "target:evidence"}

        def execute(command, *args, **kwargs):
            target = command[command.index("--progress=plain") - 1]
            override = json.loads(Path(command[command.index("--progress=plain") - 2]).read_text())
            exports = [name for name, value in override["target"].items() if value["cache-to"]]
            self.assertEqual(exports, [target])
            self.assertTrue(override["target"]["evidence"]["cache-from"])
            observed.append(target)
            return 0

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            function.__globals__, {
                "read_graph": lambda targets: graph,
                "cache_catalog": lambda: ["sdk", "evidence"],
                "HEARTBEAT": {"execute": execute},
            }
        ), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            self.assertEqual(function("sdk", Path(directory), "ghcr.io/test/cache", write=True), 0)
        self.assertEqual(observed, ["evidence", "sdk"])

    def test_stage_budget_does_not_restart_for_each_root(self):
        function = BUILD["run_stage"]
        execute = mock.Mock(return_value=0)
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            function.__globals__, {
                "read_graph": lambda targets: self.graph(),
                "cache_catalog": lambda: ["sdk"],
                "HEARTBEAT": {"execute": execute},
                "time": mock.Mock(time=lambda: 0,
                                  monotonic=mock.Mock(side_effect=[0, 1, 19801, 19802])),
            }
        ), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            self.assertEqual(function("sdk", Path(directory), "ghcr.io/test/cache"), 124)
            self.assertEqual(execute.call_count, 1)
            self.assertIn("19799s", execute.call_args.args[0])

    def test_profiles_fail_open_to_more_testing_for_unknown_or_shared_changes(self):
        select = PLAN["select_profile"]
        self.assertEqual(select(["docs/getting-started.md"]), "none")
        self.assertEqual(select(["tools/crossforge/environment.py"]), "sdk")
        self.assertEqual(select(["scripts/build-cpython-cross.sh"]), "python")
        self.assertEqual(select(["scripts/build-qt-target.sh"]), "none")
        self.assertEqual(select(["scripts/build-qt-target.sh",
                                 "tools/crossforge/environment.py"]), "sdk")
        for paths in (["new-build-system/file"], ["config/release.json"],
                      [".github/workflows/ci.yml"]):
            self.assertEqual(select(paths), "full")

    def test_required_check_rejects_missing_failed_skipped_or_cancelled_jobs(self):
        check = PLAN["required_results"]
        self.assertFalse(check({}))
        self.assertFalse(check({"quick": {"result": "success"}}))
        for state in ("failure", "cancelled", "skipped", None):
            self.assertFalse(check({"quick": {"result": "success"}, "builds": {"result": state}}))
        self.assertTrue(check({"quick": {"result": "success"}, "builds": {"result": "success"}}))

    def test_stage_summary_accepts_only_deliberate_skips(self):
        flags = {"active": "true", "sdk": "true", "gcc": "false", "qt": "false"}
        results = {"plan": {"result": "success", "outputs": flags}}
        results.update({job: {"result": "success"} for job in
                        ("inputs", "toolchains", "python", "vcpkg", "sdk")})
        results.update({job: {"result": "skipped"} for job in ("gcc", "qt-inputs", "qt-host-webengine", "qt-host", "qt")})
        self.assertTrue(PLAN["stage_results"](results))
        for state in ("skipped", "failure", "cancelled"):
            bad = copy.deepcopy(results)
            bad["python"]["result"] = state
            self.assertFalse(PLAN["stage_results"](bad))
        del results["plan"]["outputs"]["sdk"]
        self.assertFalse(PLAN["stage_results"](results))

    def test_main_quick_checks_do_not_wait_for_an_older_heavy_build(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        concurrency = workflow.split("\nconcurrency:\n", 1)[1].split("\non:\n", 1)[0]
        self.assertIn("github.sha", concurrency)
        self.assertIn("github.event_name == 'pull_request' && 'pr'", concurrency)
        builds = workflow.split("\n  builds:\n", 1)[1].split("\n  pr-required:", 1)[0]
        self.assertIn("group: ci-builds-${{ github.ref }}", builds)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", builds)
        self.assertNotIn("github.sha", builds)
        self.assertIn("inputs.quick-only && '-quick' || ''", builds)

    def test_qualification_queue_keeps_waiting_candidates(self):
        workflow = (ROOT / ".github/workflows/qualification.yml").read_text()
        concurrency = workflow.split("\nconcurrency:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("  group: crossforge-qualified-cache\n", concurrency)
        self.assertIn("  cancel-in-progress: false\n", concurrency)
        self.assertIn("  queue: max\n", concurrency)
        # Bound the pinned linter's unsupported-field exception to the one
        # documented setting; do not silently accept arbitrary queue syntax.
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            lines = [line for line in path.read_text().splitlines() if "queue:" in line]
            self.assertEqual(lines, ["  queue: max"] if path.name == "qualification.yml" else [])

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI is required for Bake graph checks")
    def test_qt_host_preparation_does_not_rebuild_cross_compilers(self):
        graph = json.loads(subprocess.check_output(
            ["docker", "buildx", "bake", "--print", *BUILD["STAGES"]["qt-inputs"],
             *BUILD["STAGES"]["qt-host"]], cwd=ROOT, stderr=subprocess.PIPE))
        self.assertFalse(any(name.startswith("toolchain-") for name in graph["target"]))
        self.assertIn("xcb-util-cursor-host-build", graph["target"])
        workflow = (ROOT / ".github/workflows/verify-builds.yml").read_text()
        host = workflow.split("\n  qt-host:\n", 1)[1].split("\n  qt:\n", 1)[0]
        self.assertIn("needs: [plan, qt-host-webengine]", host)
        self.assertNotIn("toolchains", host)
        target = workflow.split("\n  qt:\n", 1)[1].split("\n  verified:\n", 1)[0]
        self.assertIn("needs: [plan, toolchains, qt-host]", target)
        # Both target runtime graphs still own their target xcb qualification.
        for arch in ("x86_64", "aarch64"):
            graph = json.loads(subprocess.check_output(
                ["docker", "buildx", "bake", "--print", *BUILD["STAGES"]["qt-" + arch]],
                cwd=ROOT, stderr=subprocess.PIPE))
            self.assertIn("xcb-util-cursor-" + arch + "-build", graph["target"])
            self.assertIn("toolchain-" + arch + "-dev", graph["target"])

    def test_shell_syntax_checks_the_second_file_too(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        loop = workflow.split("          for script in scripts/*.sh docker/*.sh; do", 1)[1]
        loop = "for script in scripts/*.sh docker/*.sh; do" + loop.split("          done", 1)[0] + "done"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "docker").mkdir()
            (root / "scripts/a.sh").write_text("true\n")
            (root / "scripts/b.sh").write_text("if then\n")
            (root / "docker/a.sh").write_text("true\n")
            result = subprocess.run(["bash", "-e", "-c", loop], cwd=root,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"scripts/b.sh", result.stderr)


if __name__ == "__main__":
    unittest.main()
