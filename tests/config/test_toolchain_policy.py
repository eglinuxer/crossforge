"""Scoped qualification must authenticate its closure and preserve release checks."""

import copy
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
POLICY = runpy.run_path(str(ROOT / "scripts/toolchain_policy.py"))
CORE = runpy.run_path(str(ROOT / "scripts/release-components-core.py"))


class ToolchainPolicyTests(unittest.TestCase):
    def setUp(self):
        self.release = json.loads((ROOT / "config/release.json").read_text())
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def identity(self, arch, release=None):
        return CORE["toolchain_qualification_component"](release or self.release, arch)

    def components(self, arch):
        root = self.root / arch
        for name in ("toolchain/%s-qualification" % arch, "toolchain/%s-build" % arch,
                     "abi/%s-baseline" % arch, "sources/gcc", "sources/binutils"):
            output = root / (name + ".json")
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / "config/generated/components" / (name + ".json"), output)
        return root

    def test_five_component_files_match_independent_release_policy(self):
        for arch in ("x86_64", "aarch64"):
            identity = self.identity(arch)
            actual = POLICY["load"](self.components(arch), arch, identity["canonical_sha256"])
            self.assertEqual(actual, POLICY["from_release"](self.release, arch, identity))
            self.assertEqual(len(list((self.root / arch).rglob("*.json"))), 5)
            self.assertNotIn("release_sha256", json.dumps(actual))

    def test_root_pin_and_transitive_source_or_abi_mismatch_fail(self):
        root = self.components("x86_64")
        with self.assertRaises(ValueError):
            POLICY["load"](root, "x86_64", "0" * 64)
        source = root / "sources/gcc.json"
        original = source.read_text()
        source.write_text(original.replace('"15.2.1"', '"15.2.2"'))
        with self.assertRaises(ValueError):
            POLICY["load"](root, "x86_64", self.identity("x86_64")["canonical_sha256"])
        source.write_text(original)
        (root / "abi/x86_64-baseline.json").unlink()
        with self.assertRaises(ValueError):
            POLICY["load"](root, "x86_64", self.identity("x86_64")["canonical_sha256"])

    def test_unrelated_python_pin_does_not_change_toolchain_policy(self):
        modified = copy.deepcopy(self.release)
        modified["python"]["versions"][0]["patches"][0]["sha256"] = "0" * 64
        for arch in ("x86_64", "aarch64"):
            identity = self.identity(arch)
            self.assertEqual(identity, self.identity(arch, modified))
            self.assertEqual(POLICY["from_release"](self.release, arch, identity),
                             POLICY["from_release"](modified, arch, identity))

    def test_compiler_sysroot_abi_and_executor_inputs_still_invalidate(self):
        for path in (("gts", "source", "sha256"), ("targets", 1, "sysroot", "canonical_sha256"),
                     ("abi", "targets", "aarch64", "baseline", "canonical_sha256"),
                     ("qemu", "executor", "binary_sha256")):
            modified = copy.deepcopy(self.release)
            selected = modified
            for part in path[:-1]:
                selected = selected[part]
            selected[path[-1]] = "0" * 64
            before = POLICY["from_release"](self.release, "aarch64", self.identity("aarch64"))
            after = POLICY["from_release"](modified, "aarch64", self.identity("aarch64", modified))
            with self.subTest(path=path):
                self.assertNotEqual(POLICY["binding"](before), POLICY["binding"](after))

    def test_binding_rejects_wrong_policy_bool_schema_extra_fields_and_release_claim(self):
        policy = POLICY["from_release"](self.release, "x86_64", self.identity("x86_64"))
        value = {"input_binding": POLICY["binding"](policy)}
        POLICY["require_binding"](value, policy)
        for key, replacement in (("schema_version", True), ("policy_sha256", "0" * 64), ("extra", "unknown")):
            bad = copy.deepcopy(value)
            bad["input_binding"][key] = replacement
            with self.assertRaises(ValueError):
                POLICY["require_binding"](bad, policy)
        value["release_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            POLICY["require_binding"](value, policy)

    def test_qualification_clis_import_without_release_graph_or_python_row_modules(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("qualify-toolchain.py", "finalize-aarch64-qualification.py", "toolchain_policy.py",
                     "release_component.py", "abi_contract.py", "loader_evidence.py"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        for name in ("qualify-toolchain.py", "finalize-aarch64-qualification.py"):
            result = subprocess.run([sys.executable, str(scripts / name), "--help"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"--components", result.stdout)
            self.assertIn(b"--release", result.stdout)


if __name__ == "__main__":
    unittest.main()
