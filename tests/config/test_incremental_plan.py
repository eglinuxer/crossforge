"""Source selection must follow actual dependencies without authorizing reuse."""

import copy
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import incremental_plan as planner
    from crossforge_internal import bake_materials
    from crossforge_internal.identity import IdentityError
    CLI = runpy.run_path(str(ROOT / "scripts/ci-component-plan.py"))
finally:
    sys.path.pop(0)


class IncrementalPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for path in (".dockerignore", "x86.txt", "arm.txt", "cp39.patch", "cp310.patch", "gcc-baseline.json", "build.sh"):
            (self.root / path).write_text("fixture\n")
        self.recipe = self.root / "Dockerfile"
        self.recipe.write_text("# syntax=docker/dockerfile:1@sha256:" + "a" * 64 + "\n"
            "FROM rocky AS build\nARG ARCH\nCOPY ${ARCH}.txt /sysroot\nCOPY build.sh /build.sh\nRUN /build.sh\n"
            "FROM rocky AS python\nARG ROW\nCOPY ${ROW}.patch /patch\nCOPY --from=compiler /opt/ /opt/\nRUN build-python\n"
            "FROM rocky AS gcc-test\nCOPY --from=compiler /opt/ /opt/\nCOPY gcc-baseline.json /baseline\nRUN gcc-test\n"
            "FROM rocky AS sdk\nCOPY --from=row39 /python/ /python/\nCOPY --from=row310 /python/ /python/\nRUN sdk-test\n")
        self.graph = {"target": {}}
        for name, stage, args, contexts in (
            ("toolchain-x86_64-build-export", "build", {"ARCH": "x86"}, {}),
            ("toolchain-aarch64-build-export", "build", {"ARCH": "arm"}, {}),
            ("python-cp39-dev", "python", {"ROW": "cp39"}, {"compiler": "target:toolchain-x86_64-build-export"}),
            ("python-cp310-dev", "python", {"ROW": "cp310"}, {"compiler": "target:toolchain-x86_64-build-export"}),
            ("gcc-full", "gcc-test", {}, {"compiler": "target:toolchain-x86_64-build-export"}),
            ("sdk-complete-dev", "sdk", {}, {"row39": "target:python-cp39-dev", "row310": "target:python-cp310-dev"}),
        ):
            self.graph["target"][name] = {"context": ".", "dockerfile": "Dockerfile", "target": stage,
                "args": args, "platforms": ["linux/amd64"],
                "contexts": dict(contexts, rocky="docker-image://rocky@sha256:" + "b" * 64)}
        self.stages = {"toolchain-x86_64": ["toolchain-x86_64-build-export"],
                       "toolchain-aarch64": ["toolchain-aarch64-build-export"],
                       "python-cp39": ["python-cp39-dev"], "python-cp310": ["python-cp310-dev"],
                       "gcc-full": ["gcc-full"], "sdk": ["sdk-complete-dev"]}
        self.compilers = ["toolchain-x86_64-build-export", "toolchain-aarch64-build-export"]
        self.before = self.snapshot()

    def snapshot(self):
        return planner.snapshot(self.root, self.graph, self.stages, self.compilers, {"builder": "fixture"})

    def change(self, path):
        (self.root / path).write_text("modified\n")
        return planner.select(self.before, self.snapshot(), [path])

    def test_row_patch_selects_only_row_and_downstream_sdk(self):
        plan = self.change("cp39.patch")
        self.assertEqual(set(plan["targets"]), {"python-cp39", "sdk"})
        self.assertEqual(plan["compiler_inputs_changed"], [])
        self.assertEqual(plan["changes"]["sdk-complete-dev"]["files"], ["cp39.patch"])
        self.assertEqual(plan["artifact_reuse"], "not-authorized-by-source-selection")

    def test_sdk_controller_changes_select_sdk_without_claiming_compiler_input_changes(self):
        for path in planner.SDK_CONTROLLERS:
            plan = planner.select(self.before, self.snapshot(), [path])
            self.assertEqual(plan["mode"], "incremental")
            self.assertEqual(plan["targets"], {"sdk": ["sdk-complete-dev"]})
            self.assertEqual(plan["compiler_inputs_changed"], [])
            change = plan["changes"]["sdk-complete-dev"]
            self.assertEqual(change["orchestration_files"], [path])
            self.assertEqual(change["before_sha256"], change["after_sha256"])
        plan = planner.select(self.before, self.snapshot(), ["scripts/ci-sdk.py", "unknown-controller.py"])
        self.assertEqual(plan["mode"], "full")

    def test_sdk_controller_and_real_recipe_changes_keep_both_reasons(self):
        (self.root / "cp39.patch").write_text("modified\n")
        plan = planner.select(self.before, self.snapshot(), ["cp39.patch", "scripts/ci-sdk.py"])
        self.assertEqual(set(plan["targets"]), {"python-cp39", "sdk"})
        self.assertEqual(plan["changes"]["sdk-complete-dev"]["files"], ["cp39.patch"])
        self.assertEqual(plan["changes"]["sdk-complete-dev"]["orchestration_files"], ["scripts/ci-sdk.py"])

    def test_independent_installation_change_selects_each_row_without_compiler_work(self):
        path = "scripts/crossforge_internal/python_row_install.py"
        plan = planner.select(self.before, self.snapshot(), [path])
        self.assertEqual(plan["targets"], {name: roots for name, roots in self.stages.items() if name.startswith("python-")})
        self.assertEqual(plan["compiler_inputs_changed"], [])
        self.assertEqual(plan["mode"], "incremental")
        self.assertTrue(all(value["orchestration_files"] == [path] for value in plan["changes"].values()))
        self.assertEqual(planner.select(self.before, self.snapshot(), [path, "unknown.py"])["mode"], "full")

    def test_architecture_input_keeps_other_toolchain_independent(self):
        plan = self.change("x86.txt")
        self.assertEqual(set(plan["targets"]), set(self.stages) - {"toolchain-aarch64"})
        self.assertEqual(plan["compiler_inputs_changed"], ["toolchain-x86_64-build-export"])

    def test_shared_build_script_reaches_both_architectures(self):
        plan = self.change("build.sh")
        self.assertEqual(set(plan["targets"]), set(self.stages))
        self.assertEqual(plan["compiler_inputs_changed"], sorted(self.compilers))

    def test_gcc_baseline_requalifies_without_rebuilding_installations(self):
        plan = self.change("gcc-baseline.json")
        self.assertEqual(plan["targets"], {"gcc-full": ["gcc-full"]})
        self.assertEqual(plan["compiler_inputs_changed"], [])

    def test_recipe_and_build_arguments_invalidate_without_whole_dockerfile_fanout(self):
        self.recipe.write_text(self.recipe.read_text().replace("RUN gcc-test", "RUN gcc-test --strict"))
        plan = planner.select(self.before, self.snapshot(), ["Dockerfile"])
        self.assertEqual(set(plan["targets"]), {"gcc-full"})
        self.assertTrue(plan["changes"]["gcc-full"]["parameters_changed"])
        self.assertEqual(plan["changes"]["gcc-full"]["files"], [])

    def test_removed_and_added_materials_are_observed_on_both_sides(self):
        (self.root / "replacement.patch").write_text("replacement\n")
        (self.root / "cp39.patch").unlink()
        self.graph["target"]["python-cp39-dev"]["args"]["ROW"] = "replacement"
        plan = planner.select(self.before, self.snapshot(), ["cp39.patch", "replacement.patch"])
        self.assertEqual(set(plan["targets"]), {"python-cp39", "sdk"})
        self.assertEqual(plan["changes"]["python-cp39-dev"]["files"], ["cp39.patch", "replacement.patch"])

    def test_unknown_paths_missing_base_and_changed_catalog_fail_to_full(self):
        for plan in (planner.select(self.before, self.snapshot(), ["future/build.recipe"]),
                     planner.select(None, self.snapshot(), ["cp39.patch"])):
            self.assertEqual(plan["mode"], "full")
            self.assertEqual(set(plan["targets"]), set(self.stages))
            self.assertTrue(plan["fallback_reasons"])
        changed = self.snapshot()
        changed["stages"]["new"] = ["gcc-full"]
        self.assertEqual(planner.select(self.before, changed, ["Dockerfile"])["mode"], "full")

    def test_docs_config_tests_and_control_workflow_keep_fast_validation(self):
        for path in ("docs/new.md", "tests/config/test_new.py", ".github/workflows/candidate.yml",
                     ".github/workflows/component-pilot.yml"):
            self.assertEqual(planner.select(self.before, self.snapshot(), [path])["targets"], {})
        # A rename cannot hide a changed build recipe behind a documentation path.
        self.recipe.write_text(self.recipe.read_text().replace("RUN gcc-test", "RUN gcc-test --strict"))
        plan = planner.select(self.before, self.snapshot(), ["Dockerfile", "docs/old-recipe.md"])
        self.assertEqual(set(plan["targets"]), {"gcc-full"})

    def test_selected_json_rejects_unknown_fields_types_and_duplicate_targets(self):
        value = planner.compact(self.change("cp39.patch"))
        self.assertEqual(planner.validate_selection(value, self.stages), value)
        for field, replacement in (("schema_version", True), ("kind", "receipt"), ("mode", "reuse"),
                                   ("targets", {"future": ["target"]}), ("targets", {"sdk": []}),
                                   ("targets", {"sdk": ["target", "target"]}),
                                   ("targets", {"sdk": ["--push"]}), ("targets", {"sdk": ["bad/name"]})):
            bad = copy.deepcopy(value)
            bad[field] = replacement
            with self.subTest(field=field, replacement=replacement), self.assertRaises(IdentityError):
                planner.validate_selection(bad, self.stages)
        value["trusted"] = True
        with self.assertRaises(IdentityError):
            planner.validate_selection(value, self.stages)

    def test_source_inventory_cannot_masquerade_as_an_artifact_receipt(self):
        value = bake_materials.source_closure(self.root, self.graph, self.compilers[0], {"builder": "fixture"})
        self.assertEqual(set(value), {"files", "parameters"})
        self.assertNotIn("artifact_digest", json.dumps(value))


class GitSourceSnapshotTests(unittest.TestCase):
    def test_inventory_failure_selects_complete_work_instead_of_empty_success(self):
        function = CLI["plan"]
        stages = {"inputs": ["validate"], "sdk": ["sdk-complete-dev"], "qt-inputs": ["qt-rpm-locked"]}
        def git(repository, *arguments):
            if arguments[0] == "rev-parse":
                return b"b" * 40 + b"\n"
            return b"scripts/build.sh\0"
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(function.__globals__, {
                "stage_catalog": lambda: stages, "_git": git,
                "export_source": mock.Mock(side_effect=IdentityError("missing checked material"))}):
            value = function(Path(temporary), "a" * 40, "b" * 40, Path(temporary))
        self.assertEqual(value["mode"], "full")
        self.assertEqual(set(value["targets"]), {"inputs", "sdk"})
        self.assertIn("missing checked material", value["fallback_reasons"][0])

    def test_export_preserves_bytes_and_execute_bit_without_mutating_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "build.sh").write_text("original\n")
            (repo / "build.sh").chmod(0o755)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "Fixture"], cwd=repo, check=True)
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
            (repo / "build.sh").write_text("uncommitted\n")
            CLI["export_source"](repo, commit, root / "snapshot")
            self.assertEqual((root / "snapshot/build.sh").read_text(), "original\n")
            self.assertEqual((root / "snapshot/build.sh").stat().st_mode & 0o777, 0o755)
            self.assertEqual((repo / "build.sh").read_text(), "uncommitted\n")

    def test_archive_symlinks_and_traversal_cannot_escape_source_snapshot(self):
        for name, kind in (("../outside", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            with tempfile.TemporaryDirectory() as temporary:
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w") as bundle:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "../outside"
                    bundle.addfile(member)
                with mock.patch.dict(CLI["export_source"].__globals__, {"_git": lambda *args: archive.getvalue()}):
                    with self.assertRaises(IdentityError):
                        CLI["export_source"](Path(temporary), "a" * 40, Path(temporary) / "snapshot")
                self.assertFalse((Path(temporary) / "outside").exists())


if __name__ == "__main__":
    unittest.main()
