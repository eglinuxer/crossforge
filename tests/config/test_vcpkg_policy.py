"""Scoped vcpkg policies and report handoff; expensive execution is explicit mock."""

import copy
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
POLICY = runpy.run_path(str(ROOT / "scripts/vcpkg_policy.py"))
SDK = runpy.run_path(str(ROOT / "scripts/qualify-vcpkg-sdk.py"))
CONTRACT = runpy.run_path(str(ROOT / "scripts/qualify-vcpkg-contract.py"))
UPSTREAM = runpy.run_path(str(ROOT / "scripts/qualify-vcpkg-upstream.py"))
COMPLETE = runpy.run_path(str(ROOT / "scripts/qualify-complete-sdk.py"))
REPORT = runpy.run_path(str(ROOT / "scripts/toolchain_report.py"))
READER, STAGES = POLICY["READER"], POLICY["STAGES"]
COMPONENTS = ROOT / "config/generated/components"
FILES = ["sources/gcc", "sources/binutils", "sources/ninja", "sources/cmake", "host-tools/ninja", "host-tools/cmake"]
FILES += list(POLICY["NAMES"].values())
FILES += ["%s/%s-%s" % (group, arch, suffix) for arch in POLICY["ARCHES"]
          for group, suffix in (("toolchain", "qualification"), ("toolchain", "build"), ("abi", "baseline"))]


class VcpkgPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "components"
        self.release = READER.load_json(ROOT / "config/release.json")
        for name in FILES:
            path = self.directory / (name + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(COMPONENTS / (name + ".json"), path)
        self.pins = {arch: self.digest("toolchain/%s-qualification" % arch) for arch in POLICY["ARCHES"]}
        self.policies = {stage: self.policy(stage) for stage in STAGES}
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.toolchain_paths = {}
        for arch, policy in self.policies["sdk"]["toolchains"].items():
            report = {"target": policy["target"]["triple"], "sysroot_sha256": policy["target"]["sysroot"]["canonical_sha256"],
                      "compiler_version": policy["gcc"]["version"], "binutils_version": "GNU ld " + policy["binutils"]["version"],
                      "sources": {name: policy[name]["source"] for name in ("gcc", "binutils")},
                      "qualification_schema_version": 2, "report_kind": "crossforge-toolchain-qualification",
                      "qualification_component": policy["component"], "input_binding": REPORT["POLICY"]["binding"](policy),
                      "runtime_base": policy["runtime_base"], "runtime_executor": policy["runtime_executor"],
                      "abi_baseline": policy["abi_baseline"], "locked_sysroot_execution": {"status": "passed"},
                      "clean_runtime_execution": {"status": "passed"}}
            self.toolchain_paths[arch] = self.write(self.reports / (arch + ".json"), report)
        (self.reports / "x86_64-clean-runtime.ok").write_bytes(b"passed\n")

    def write(self, path, value):
        path.write_text(json.dumps(value, sort_keys=True))
        return path

    def document(self, name):
        return READER.load_json(self.directory / (name + ".json"))

    def digest(self, name):
        return READER.canonical_sha256(self.document(name))

    def policy(self, stage, pins=None):
        return POLICY["load"](self.directory, stage, self.digest(POLICY["NAMES"][stage]),
                              (self.pins if pins is None else pins) if stage == "sdk" else None)

    def report(self, stage):
        policy = self.policies[stage]
        kind = stage if stage in ("sdk", "contract") else "upstream-" + stage
        report = {"schema_version": 2, "kind": "crossforge-vcpkg-" + kind + "-qualification", "status": "passed",
                  "input_binding": POLICY["binding"](policy)}
        if stage == "sdk":
            report.update(environment={}, source={}, ninja={}, cmake_host_tool={}, triplets=[], cmake=[])
            report.update(integration={"sdk_component_sha256": self.digest("vcpkg/sdk-build")},
                          toolchain_report_sha256={arch: REPORT["sha256_file"](path) for arch, path in self.toolchain_paths.items()})
        else:
            report["components"] = {"qualification": policy["components"][stage]}
            report.update(dependencies={}, fixture_files=[], results=[])
            if stage != "contract":
                report.update(assets=[], ports=[])
        if stage == "contract":
            report["patchelf_asset"] = CONTRACT["PATCHELF_ASSET"]
        return report

    def test_five_component_policies_match_independent_complete_release(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(self.policies[stage], POLICY["from_release"](self.release, stage))
                POLICY["require_binding"](self.report(stage), self.policies[stage])
        self.assertEqual(len(list(self.directory.rglob("*.json"))), 17)

    def test_each_predecessor_policy_matches_its_independent_producer(self):
        for stage in STAGES:
            for previous in STAGES[:STAGES.index(stage) + 1]:
                self.assertEqual(POLICY["prior"](self.policies[stage], previous), self.policies[previous])
        with self.assertRaises(POLICY["PolicyError"]):
            POLICY["prior"](self.policies["sdk"], "tier3")

    def test_later_roots_derive_pins_and_sdk_requires_both_trusted_pins(self):
        for pins in ({}, {"x86_64": self.pins["x86_64"]}, dict(self.pins, extra="0" * 64), dict(self.pins, aarch64="0" * 64)):
            with self.subTest(pins=pins), self.assertRaises(POLICY["PolicyError"]):
                self.policy("sdk", pins)
        with self.assertRaises(POLICY["PolicyError"]):
            POLICY["load"](self.directory, "contract", self.digest(POLICY["NAMES"]["contract"]), self.pins)

    def test_unknown_materials_dependencies_or_component_schema_fail(self):
        for stage in STAGES:
            name = POLICY["NAMES"][stage]
            original = self.document(name)
            for mutation in (lambda d: d.update(schema_version=True), lambda d: d.update(extra="unknown"),
                             lambda d: d["materials"].append({"path": "/surprise", "value": "extra"}),
                             lambda d: d["dependencies"].pop()):
                document = copy.deepcopy(original)
                mutation(document)
                self.write(self.directory / (name + ".json"), document)
                with self.subTest(stage=stage), self.assertRaises((POLICY["PolicyError"], READER.ComponentError)):
                    self.policy(stage)
            self.write(self.directory / (name + ".json"), original)

    def test_transitive_source_host_tool_and_toolchain_tampering_is_rejected(self):
        for name in ("sources/gcc", "sources/ninja", "sources/cmake", "host-tools/ninja", "abi/aarch64-baseline"):
            path = self.directory / (name + ".json")
            original = path.read_bytes()
            document = self.document(name)
            document["materials"][0]["value"] = "tampered"
            self.write(path, document)
            with self.subTest(name=name), self.assertRaises(POLICY["PolicyError"]):
                self.policy("tier3")
            path.write_bytes(original)
        (self.directory / "sources/cmake.json").unlink()
        with self.assertRaises(POLICY["PolicyError"]):
            self.policy("sdk")

    def test_sdk_and_qualified_toolchain_cannot_bind_different_build_inputs(self):
        name = "vcpkg/sdk-build"
        document = self.document(name)
        next(item for item in document["dependencies"] if item["component"] == "toolchain/x86_64-build")["canonical_sha256"] = "0" * 64
        self.write(self.directory / (name + ".json"), document)
        with self.assertRaisesRegex(POLICY["PolicyError"], "build inputs differ"):
            self.policy("sdk")

    def test_unrelated_release_changes_leave_every_binding_unchanged(self):
        release = copy.deepcopy(self.release)
        release["product"]["version"] = "0.1.1"
        release["python"]["versions"][0]["source"]["sha256"] = "0" * 64
        for stage in STAGES:
            self.assertEqual(POLICY["from_release"](release, stage), self.policies[stage])

    def test_changed_executor_abi_compiler_or_host_tool_changes_the_binding(self):
        for path in (("qemu", "executor", "binary_sha256"), ("abi", "targets", "x86_64", "baseline", "canonical_sha256"),
                     ("gts", "source", "sha256"), ("host_tools", "ninja", "binary", "extracted_sha256"),
                     ("host_tools", "cmake", "payloads", 0, "sha256")):
            release = copy.deepcopy(self.release)
            value = release
            for key in path[:-1]:
                value = value[key]
            value[path[-1]] = "0" * 64
            with self.subTest(path=path):
                self.assertNotEqual(POLICY["binding"](POLICY["from_release"](release, "tier3")), POLICY["binding"](self.policies["tier3"]))

    def test_own_tier_policy_change_does_not_relabel_predecessors(self):
        name = POLICY["NAMES"]["tier3"]
        document = self.document(name)
        next(item for item in document["dependencies"] if item["component"].startswith("implementation/"))["canonical_sha256"] = "0" * 64
        self.write(self.directory / (name + ".json"), document)
        after = self.policy("tier3")
        self.assertNotEqual(POLICY["binding"](after), POLICY["binding"](self.policies["tier3"]))
        self.assertEqual(POLICY["prior"](after, "tier2"), self.policies["tier2"])

    def test_report_binding_rejects_legacy_mixed_wrong_stage_and_unknown_binding_fields(self):
        for stage in STAGES:
            for changes in ({"schema_version": True}, {"schema_version": 1}, {"release_sha256": "0" * 64},
                            {"unknown": "extra"},
                            {"status": "failed"}, {"kind": "wrong"}, {"input_binding": dict(POLICY["binding"](self.policies[stage]), extra="unknown")}):
                with self.subTest(stage=stage, changes=changes), self.assertRaises(POLICY["PolicyError"]):
                    POLICY["require_binding"](dict(self.report(stage), **changes), self.policies[stage])
            report = self.report(stage)
            report.pop("environment" if stage == "sdk" else "results")
            with self.subTest(stage=stage), self.assertRaises(POLICY["PolicyError"]):
                POLICY["require_binding"](report, self.policies[stage])

    def contract_check(self, report=None, policy=None, release=None):
        document = self.document(POLICY["NAMES"]["contract"])
        dependencies = {item["component"]: item["canonical_sha256"] for item in document["dependencies"]}
        return CONTRACT["validate_component_closure"](release, document,
            dependencies["implementation/vcpkg-contract-qualification"], self.report("sdk") if report is None else report,
            self.toolchain_paths, policy)

    def test_contract_rechecks_actual_toolchain_reports_and_original_sdk_report_hashes(self):
        self.contract_check(policy=self.policies["contract"])
        self.contract_check(release=self.release)
        report = self.report("sdk")
        report["toolchain_report_sha256"]["aarch64"] = "0" * 64
        with self.assertRaises(CONTRACT["QualificationError"]):
            self.contract_check(report, self.policies["contract"])
        path = self.toolchain_paths["aarch64"]
        report = READER.load_json(path)
        report["clean_runtime_execution"]["status"] = "failed"
        self.write(path, report)
        with self.assertRaises(CONTRACT["QualificationError"]):
            self.contract_check(policy=self.policies["contract"])

    def test_contract_component_mode_rejects_a_legacy_sdk_report(self):
        report = self.report("sdk")
        report.pop("input_binding")
        report.update(schema_version=1, release_sha256=READER.canonical_sha256(self.release))
        with self.assertRaises(CONTRACT["QualificationError"]):
            self.contract_check(report, self.policies["contract"])
        self.contract_check(report, release=self.release)

    def test_every_upstream_tier_checks_the_expected_predecessor(self):
        for tier in STAGES[2:]:
            gate = UPSTREAM["TIER_PROFILES"][tier]
            document = self.document(POLICY["NAMES"][tier])
            digest = next(item["canonical_sha256"] for item in document["dependencies"] if item["component"] == gate["policy_component"])
            previous = STAGES[STAGES.index(tier) - 1]
            def check(report, policy, release=None):
                return UPSTREAM["validate_component_closure"](release, document, digest, report, tier, policy)
            report = self.report(previous)
            check(report, self.policies[tier])
            check(report, None, self.release)
            for changed in (dict(report, input_binding={}), self.report(tier), dict(report, release_sha256="0" * 64)):
                with self.subTest(tier=tier), self.assertRaises(UPSTREAM["QualificationError"]):
                    check(changed, self.policies[tier])

    def test_final_complete_sdk_derives_inputs_from_release_and_binds_report_bytes(self):
        release_path = self.write(self.root / "release.json", self.release)
        report_path = self.write(self.root / "sdk.json", self.report("sdk"))
        result = COMPLETE["qualify_vcpkg_report"](release_path, report_path)
        self.assertEqual(result, {"schema_version": 2, "sha256": REPORT["sha256_file"](report_path)})
        release = copy.deepcopy(self.release)
        release["product"]["version"] = "0.1.1"
        self.write(release_path, release)
        self.assertEqual(COMPLETE["qualify_vcpkg_report"](release_path, report_path), result)
        release["host_tools"]["ninja"]["binary"]["extracted_sha256"] = "0" * 64
        self.write(release_path, release)
        with self.assertRaises(COMPLETE["QualificationError"]):
            COMPLETE["qualify_vcpkg_report"](release_path, report_path)

    def test_final_complete_sdk_preserves_exact_legacy_release_and_rejects_mixed_or_unsafe_reports(self):
        release_path = self.write(self.root / "release.json", self.release)
        report = {"schema_version": 1, "kind": "crossforge-vcpkg-sdk-qualification", "status": "passed",
                  "release_sha256": READER.canonical_sha256(self.release)}
        path = self.write(self.root / "sdk.json", report)
        COMPLETE["qualify_vcpkg_report"](release_path, path)
        for changes in ({"schema_version": 2}, {"schema_version": True}, {"release_sha256": "0" * 64}, {"input_binding": {}}):
            self.write(path, dict(report, **changes))
            with self.assertRaises(COMPLETE["QualificationError"]):
                COMPLETE["qualify_vcpkg_report"](release_path, path)
        link = self.root / "link.json"
        link.symlink_to(path)
        with self.assertRaises(COMPLETE["QualificationError"]):
            COMPLETE["qualify_vcpkg_report"](release_path, link)

    def test_policy_loader_needs_only_three_modules_and_seventeen_projections(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("vcpkg_policy.py", "toolchain_policy.py", "release_component.py"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        minimal = runpy.run_path(str(scripts / "vcpkg_policy.py"))
        self.assertEqual(minimal["load"](self.directory, "tier3", self.digest(POLICY["NAMES"]["tier3"])), self.policies["tier3"])
        self.assertFalse((self.root / "config/release.json").exists())

    def environment(self, root=None, triplets=None):
        return {"VCPKG_ROOT": str(root or self.root / "vcpkg"), "VCPKG_OVERLAY_TRIPLETS": str(triplets or self.root / "triplets"),
                "VCPKG_DEFAULT_HOST_TRIPLET": "crossforge-host-x64-el8", "VCPKG_DISABLE_METRICS": "1",
                "VCPKG_FORCE_SYSTEM_BINARIES": "1", "NINJA_ROOT": "/opt/crossforge/host-tools/ninja/1.13.2",
                "CROSSFORGE_CMAKE_ROOT": "/opt/crossforge/host-tools/cmake/4.4.0"}

    def test_sdk_producer_uses_scoped_host_tools_executor_and_reports_without_reading_release(self):
        names = {"source": "sources/vcpkg", "integration": "implementation/vcpkg-integration", "sdk": "vcpkg/sdk-build",
                 "ninja": "host-tools/ninja", "cmake": "host-tools/cmake"}
        paths = {key: COMPONENTS / (name + ".json") for key, name in names.items()}
        digests = {key: READER.canonical_sha256(READER.load_json(path)) for key, path in paths.items()}
        check = SDK["qualify_toolchain_reports"]
        ninja, cmake, smoke = (mock.Mock(return_value={}) for _ in range(3))
        triplets = sorted(CONTRACT["TRIPLETS"])
        patches = {"qualify_ninja": ninja, "qualify_cmake": cmake, "cmake_smoke": smoke,
                   "qualify_source": mock.Mock(return_value={}),
                   "qualify_integration": mock.Mock(return_value={"sdk_component_sha256": digests["sdk"]}),
                   "run": mock.Mock(return_value=("Overlay Triplets from fixture\n" + "\n".join(triplets) + "\nSee help", "")),
                   "qualify_toolchain_reports": lambda release, digest, directory, components, pins:
                        check(release, digest, self.reports, components, pins),
                   "sha256_file": lambda path: REPORT["sha256_file"](self.toolchain_paths[path.stem])}
        release_path = self.write(self.root / "release.json", self.release)
        for scoped in (True, False):
            with mock.patch.dict(SDK["qualify"].__globals__, patches), mock.patch.dict(os.environ, self.environment(), clear=True):
                report = SDK["qualify"](None if scoped else release_path, self.root / "vcpkg", self.root / "source.json",
                    self.root / "integration.json", self.root / "cmake", self.root / "triplets", self.root / "qemu",
                    self.root / "ninja.json", self.root / "cmake.json", paths, digests,
                    toolchain_component_sha256=self.pins if scoped else None, components=self.directory if scoped else None)
            self.assertEqual(ninja.call_args[0][1], self.policies["sdk"]["host_tools"]["ninja"])
            self.assertEqual(cmake.call_args[0][1], self.policies["sdk"]["host_tools"]["cmake"])
            if scoped:
                POLICY["require_binding"](report, self.policies["sdk"])
                self.assertEqual(smoke.call_args[0][2], self.policies["sdk"]["toolchains"]["aarch64"]["runtime_executor"])
            else:
                self.assertEqual(report["schema_version"], 1)
                self.assertEqual(report["release_sha256"], READER.canonical_sha256(self.release))
                self.assertNotIn("input_binding", report)

    def test_contract_producer_dispatches_all_five_triplets_with_authenticated_executor(self):
        check, read = CONTRACT["validate_component_closure"], CONTRACT["load_json"]
        triplet = mock.Mock(return_value={})
        patches = {"qualify_triplet": triplet,
                   "file_identity": mock.Mock(return_value={key: CONTRACT["PATCHELF_ASSET"][key] for key in ("sha256", "sha512", "size")}),
                   "load_json": lambda path: self.report("sdk") if str(path) == "/opt/crossforge/qualification/vcpkg/sdk.json" else read(path),
                   "validate_component_closure": lambda release, component, digest, report, paths, policy:
                        check(release, component, digest, report, self.toolchain_paths, policy)}
        name = "implementation/vcpkg-contract-qualification"
        impl = COMPONENTS / (name + ".json")
        with mock.patch.dict(CONTRACT["qualify"].__globals__, patches), mock.patch.dict(os.environ, self.environment(), clear=True):
            report = CONTRACT["qualify"](None, self.root / "vcpkg", ROOT / "tests/vcpkg/contract",
                self.root / CONTRACT["PATCHELF_ASSET"]["filename"], self.root / "qemu", impl,
                READER.canonical_sha256(READER.load_json(impl)), self.directory / "vcpkg/contract-qualification.json",
                self.digest("vcpkg/contract-qualification"), components=self.directory)
        POLICY["require_binding"](report, self.policies["contract"])
        self.assertEqual([call[0][3] for call in triplet.call_args_list], list(CONTRACT["TRIPLETS"]))
        self.assertTrue(all(call[0][6] == self.policies["contract"]["toolchains"]["aarch64"]["runtime_executor"] for call in triplet.call_args_list))

    def upstream_produce(self, tier, contract_report=None):
        gate = UPSTREAM["TIER_PROFILES"][tier]
        impl = COMPONENTS / (gate["policy_component"] + ".json")
        args = types.SimpleNamespace(tier=tier, release=None, components=self.directory,
            policy_component=impl, policy_component_sha256=READER.canonical_sha256(READER.load_json(impl)),
            qualification_component=self.directory / (gate["qualification_component"] + ".json"),
            qualification_component_sha256=self.digest(gate["qualification_component"]),
            fixture_root=ROOT / "tests/vcpkg" / ("upstream-" + tier), asset_root=self.root / "assets",
            patchelf_archive=self.root / CONTRACT["PATCHELF_ASSET"]["filename"], vcpkg_root=self.root / "vcpkg", qemu=self.root / "qemu")
        previous = STAGES[STAGES.index(tier) - 1]
        def read(path):
            if Path(path).name == "contract.json":
                return self.report("contract") if contract_report is None else contract_report
            self.assertEqual(str(path), gate["prerequisite_report"])
            return self.report(previous)
        triplet = mock.Mock(return_value={})
        with mock.patch.dict(UPSTREAM["qualify"].__globals__, {"load_json": read, "qualify_triplet": triplet}), \
             mock.patch.dict(UPSTREAM["ASSETS"], {"verify_asset_root": mock.Mock(), "file_identity": mock.Mock(
                 return_value={key: CONTRACT["PATCHELF_ASSET"][key] for key in ("sha256", "sha512", "size")})}), \
             mock.patch.dict(os.environ, self.environment(), clear=True):
            report = UPSTREAM["qualify"](args)
        return report, triplet

    def test_all_upstream_producers_keep_five_triplets_and_scoped_predecessor_chain(self):
        for tier in STAGES[2:]:
            report, triplet = self.upstream_produce(tier)
            POLICY["require_binding"](report, self.policies[tier])
            self.assertEqual([call[0][5] for call in triplet.call_args_list], list(CONTRACT["TRIPLETS"]))
            self.assertTrue(all(call[0][8] == self.policies[tier]["toolchains"]["aarch64"]["runtime_executor"] for call in triplet.call_args_list))

    def test_later_upstream_tiers_also_authenticate_the_contract_report_used_for_patchelf(self):
        bad = self.report("contract")
        bad["input_binding"] = {}
        with self.assertRaises(UPSTREAM["VCPKG_POLICY"]["PolicyError"]):
            self.upstream_produce("tier3", bad)


class VcpkgPolicyGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker Buildx is required for canonical graph checks")
        cls.targets = ["sdk-phase13-base", "vcpkg-contract-qualified"] + ["vcpkg-upstream-%s-qualified" % tier for tier in STAGES[2:]]
        cls.graph = json.loads(subprocess.check_output(["docker", "buildx", "bake", "-f", "docker-bake.hcl",
            "-f", "docker-bake.override.json", "--print"] + cls.targets, cwd=str(ROOT)))

    def test_entire_vcpkg_chain_omits_release_and_renderers(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            from crossforge_internal import bake_materials
        finally:
            sys.path.pop(0)
        for stage, target in zip(STAGES, self.targets):
            inputs = bake_materials.capture(ROOT, self.graph, target, POLICY["NAMES"][stage], "qualification",
                [arch + "-unknown-linux-gnu" for arch in POLICY["ARCHES"]], {"fixture": "graph only"})
            files = {item["path"] for item in inputs["files"]}
            with self.subTest(stage=stage):
                self.assertNotIn("config/release.json", files)
                self.assertNotIn("config/schemas/release.schema.json", files)
                self.assertFalse(any(path.startswith("scripts/release-components-") for path in files))
                self.assertIn("scripts/vcpkg_policy.py", files)
                self.assertIn("config/generated/components/sources/cmake.json", files)
                self.assertIn("config/generated/components/sources/ninja.json", files)

    def test_each_trimmed_docker_entrypoint_imports_with_only_its_copied_scripts(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            from crossforge_internal import bake_materials
        finally:
            sys.path.pop(0)
        _, stages, _ = bake_materials.recipe((ROOT / "docker/vcpkg.Dockerfile").read_text())
        for target in self.targets:
            stage = self.graph["target"][target]["target"]
            sources = [source for line in stages[stage]["instructions"] if line.startswith("COPY ")
                       for source in bake_materials._copy(line)[1] if source.startswith("scripts/")]
            qualifier = next(path for path in sources if Path(path).name.startswith("qualify-vcpkg-"))
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                for source in sources:
                    shutil.copyfile(ROOT / source, directory / Path(source).name)
                process = subprocess.run([sys.executable, str(directory / Path(qualifier).name), "--help"],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertIn(b"--components", process.stdout)
                self.assertIn(b"--release", process.stdout)
                self.assertEqual(len(list(directory.iterdir())), 6)

    def test_packaging_reads_release_from_its_own_stage_after_the_vcpkg_boundary(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            from crossforge_internal import bake_materials
        finally:
            sys.path.pop(0)
        _, stages, _ = bake_materials.recipe((ROOT / "docker/packaging.Dockerfile").read_text())
        self.assertIn("COPY config/release.json /opt/crossforge/release.json", stages["packaging-sdk"]["instructions"])
        self.assertEqual(stages["packaging-sdk"]["base"], "crossforge_sdk_base")

    def test_trimmed_final_sdk_checker_derives_vcpkg_policy_from_its_copied_modules(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            from crossforge_internal import bake_materials
        finally:
            sys.path.pop(0)
        _, stages, _ = bake_materials.recipe((ROOT / "docker/packaging.Dockerfile").read_text())
        sources = [source for line in stages["sdk-complete-dev"]["instructions"] if line.startswith("COPY ")
                   for source in bake_materials._copy(line)[1] if source.startswith("scripts/")]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "scripts"
            directory.mkdir()
            for source in sources:
                shutil.copyfile(ROOT / source, directory / Path(source).name)
            policy = POLICY["from_release"](READER.load_json(ROOT / "config/release.json"), "sdk")
            path = directory / "sdk.json"
            path.write_text(json.dumps({"kind": "crossforge-vcpkg-sdk-qualification", "status": "passed",
                "schema_version": 2, "input_binding": POLICY["binding"](policy), "environment": {}, "source": {},
                "ninja": {}, "cmake_host_tool": {}, "integration": {}, "triplets": [], "cmake": [], "toolchain_report_sha256": {}}))
            checker = runpy.run_path(str(directory / "qualify-complete-sdk.py"))
            result = checker["qualify_vcpkg_report"](ROOT / "config/release.json", path)
            self.assertEqual(result, {"schema_version": 2, "sha256": REPORT["sha256_file"](path)})


if __name__ == "__main__":
    unittest.main()
