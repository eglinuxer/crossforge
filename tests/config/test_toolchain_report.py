import json
from pathlib import Path
import runpy
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE = runpy.run_path(str(ROOT / "scripts/toolchain_report.py"))
SDK = runpy.run_path(str(ROOT / "scripts/qualify-final-sdk.py"))
VCPKG = runpy.run_path(str(ROOT / "scripts/qualify-vcpkg-sdk.py"))
CONTRACT = runpy.run_path(str(ROOT / "scripts/qualify-vcpkg-contract.py"))


class ToolchainReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "x86_64.json"
        self.release = json.loads((ROOT / "config/release.json").read_text())
        self.release_sha = SDK["canonical_sha256"](self.release)
        self.component = SDK["RELEASE_COMPONENTS"]["toolchain_qualification_component"](self.release, "x86_64")
        self.sysroot = next(target["sysroot"]["canonical_sha256"] for target in self.release["targets"] if target["arch"] == "x86_64")
        self.report = {"target": "x86_64-unknown-linux-gnu", "release_sha256": self.release_sha,
                       "sysroot_sha256": self.sysroot, "compiler_version": self.release["gts"]["gcc_version"],
                       "binutils_version": "GNU ld " + self.release["binutils"]["version"],
                       "sources": {"gcc": self.release["gts"]["source"], "binutils": self.release["binutils"]["source"]},
                       "qualification_component": self.component, "locked_sysroot_execution": {"status": "passed"}}
        (self.root / "x86_64-clean-runtime.ok").write_bytes(b"passed\n")

    def check(self, report=None):
        self.path.write_text(json.dumps(report or self.report))
        return SDK["qualify_prior_toolchain_report"]("x86_64", "x86_64-unknown-linux-gnu", self.path,
            self.release, self.release_sha, self.sysroot)

    def test_compatibility_wrapper_and_domain_validator_agree(self):
        expected = self.check()
        self.assertEqual(expected, MODULE["qualify_prior_toolchain_report"]("x86_64", "x86_64-unknown-linux-gnu",
            self.path, self.release, self.release_sha, self.sysroot, self.component))

    def test_wrong_subject_policy_and_failed_runtime_rejected(self):
        for field, value in (("target", "aarch64-unknown-linux-gnu"), ("sysroot_sha256", "0" * 64),
                             ("release_sha256", "0" * 64), ("qualification_component", {}),
                             ("locked_sysroot_execution", {"status": "failed"}), ("binutils_version", None)):
            with self.subTest(field=field), self.assertRaises(SDK["QualificationError"]):
                self.check(dict(self.report, **{field: value}))

    def test_missing_wrong_or_symlink_clean_marker_rejected(self):
        marker = self.root / "x86_64-clean-runtime.ok"
        marker.unlink()
        with self.assertRaises(SDK["QualificationError"]):
            self.check()
        marker.write_bytes(b"failed\n")
        with self.assertRaises(SDK["QualificationError"]):
            self.check()
        marker.unlink()
        (self.root / "other").write_bytes(b"passed\n")
        marker.symlink_to(self.root / "other")
        with self.assertRaises(SDK["QualificationError"]):
            self.check()

    def scoped_report(self):
        policy = MODULE["POLICY"]["from_release"](self.release, "x86_64", self.component)
        value = dict(self.report)
        value.pop("release_sha256")
        value.update(input_binding=MODULE["POLICY"]["binding"](policy), qualification_schema_version=2,
                     report_kind="crossforge-toolchain-qualification", runtime_executor=policy["runtime_executor"],
                     runtime_base=policy["runtime_base"], abi_baseline={"canonical_sha256": policy["abi_baseline"]["canonical_sha256"]})
        return value

    def test_scoped_report_survives_unrelated_release_change_without_relabeling(self):
        report = self.scoped_report()
        expected = self.check(report)
        self.release["python"]["versions"][0]["patches"][0]["sha256"] = "0" * 64
        self.release_sha = SDK["canonical_sha256"](self.release)
        self.assertEqual(self.check(report), expected)
        self.assertNotIn("release_sha256", json.loads(self.path.read_text()))

    def test_scoped_report_rejects_wrong_policy_or_mixed_release_binding(self):
        for field, value in (("release_sha256", self.release_sha), ("input_binding", {}),
                             ("qualification_schema_version", True), ("runtime_executor", {}),
                             ("runtime_base", {}), ("abi_baseline", {}), ("abi_baseline", None)):
            report = self.scoped_report()
            report[field] = value
            with self.subTest(field=field), self.assertRaises(SDK["QualificationError"]):
                self.check(report)

    def test_scoped_schema_cannot_downgrade_to_legacy_release_binding(self):
        report = self.scoped_report()
        report.pop("input_binding")
        report["release_sha256"] = self.release_sha
        with self.assertRaises(SDK["QualificationError"]):
            self.check(report)

    def test_vcpkg_accepts_both_scoped_architectures_and_rejects_changed_subject(self):
        report = self.scoped_report()
        self.path.write_text(json.dumps(report))
        arch = "aarch64"
        identity = SDK["RELEASE_COMPONENTS"]["toolchain_qualification_component"](self.release, arch)
        policy = MODULE["POLICY"]["from_release"](self.release, arch, identity)
        report.update(target=policy["target"]["triple"], qualification_component=identity,
                      input_binding=MODULE["POLICY"]["binding"](policy),
                      sysroot_sha256=policy["target"]["sysroot"]["canonical_sha256"],
                      abi_baseline=policy["abi_baseline"], runtime_executor=policy["runtime_executor"],
                      runtime_base=policy["runtime_base"], clean_runtime_execution={"status": "passed"})
        path = self.root / "aarch64.json"
        path.write_text(json.dumps(report))
        expected = VCPKG["qualify_toolchain_reports"](self.release, self.release_sha, self.root)
        component = json.loads((ROOT / "config/generated/components/vcpkg/contract-qualification.json").read_text())
        dependencies = {value["component"]: value["canonical_sha256"] for value in component["dependencies"]}
        policy_sha = dependencies["implementation/vcpkg-contract-qualification"]
        sdk_report = {"status": "passed", "release_sha256": self.release_sha,
                      "integration": {"sdk_component_sha256": dependencies["vcpkg/sdk-build"]},
                      "toolchain_report_sha256": {key: value["report_sha256"] for key, value in expected.items()}}
        paths = {"x86_64": self.path, "aarch64": path}
        def check_contract():
            return CONTRACT["validate_component_closure"](self.release, component, policy_sha, sdk_report, paths)
        self.assertEqual(check_contract(), dependencies)
        self.release["python"]["versions"][0]["patches"][0]["sha256"] = "0" * 64
        self.assertEqual(expected, VCPKG["qualify_toolchain_reports"](
            self.release, SDK["canonical_sha256"](self.release), self.root))
        with self.assertRaises(CONTRACT["QualificationError"]):
            check_contract()
        sdk_report["release_sha256"] = SDK["canonical_sha256"](self.release)
        self.assertEqual(check_contract(), dependencies)
        sdk_report["toolchain_report_sha256"]["aarch64"] = "0" * 64
        with self.assertRaises(CONTRACT["QualificationError"]):
            check_contract()
        report["sysroot_sha256"] = "0" * 64
        path.write_text(json.dumps(report))
        with self.assertRaises(VCPKG["QualificationError"]):
            VCPKG["qualify_toolchain_reports"](self.release, self.release_sha, self.root)


if __name__ == "__main__":
    unittest.main()
