import json
from pathlib import Path
import runpy
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE = runpy.run_path(str(ROOT / "scripts/toolchain_report.py"))
SDK = runpy.run_path(str(ROOT / "scripts/qualify-final-sdk.py"))


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


if __name__ == "__main__":
    unittest.main()
