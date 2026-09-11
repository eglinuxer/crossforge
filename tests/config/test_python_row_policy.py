import argparse
import copy
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
POLICY = runpy.run_path(str(ROOT / "scripts/python_row_policy.py"))
CORE = runpy.run_path(str(ROOT / "scripts/release-components-core.py"))
ROW = runpy.run_path(str(ROOT / "docker/finalize-python-row.py"))
SDK = runpy.run_path(str(ROOT / "scripts/qualify-final-sdk.py"))
SHA = CORE["canonical_sha256"]


class PythonRowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads((ROOT / "config/release.json").read_text())
        cls.documents = CORE["render_component_documents"](cls.release)
        cls.components = ROOT / "config/generated/components"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def derive(self, row="cp313", release=None):
        release = self.release if release is None else release
        entry = CORE["ROW_CONTRACT"]["bind_release"](release, row=row)["entry"]
        return POLICY["from_release"](release, row, entry["version"], entry["adapter"], CORE["render_component_documents"])

    def load(self, row="cp313", directory=None, digest=None):
        entry = CORE["ROW_CONTRACT"]["bind_release"](self.release, row=row)["entry"]
        digest = digest if digest is not None else SHA(self.documents["python/%s-qualification" % row])
        return POLICY["load"](self.components if directory is None else directory, row, entry["version"], entry["adapter"], digest)

    def arguments(self, policy):
        source = self.directory / "source.json"
        source.write_text(json.dumps(policy["source_manifest"], sort_keys=True))
        return argparse.Namespace(row=policy["row"], version=policy["version"], adapter=policy["adapter"],
            release=None, qualification_components=self.components,
            qualification_component_sha256=policy["component"]["canonical_sha256"], source_manifest=source,
            row_manifest=None)

    def manifest_identity(self, policy):
        return {"schema_version": 3, "kind": "crossforge-cpython-row", "input_binding": POLICY["binding"](policy),
                **{key: policy[key] for key in ("row", "version", "adapter", "support")},
                **{key: copy.deepcopy(policy["source_manifest"][key]) for key in ("source", "patches")}}

    def test_six_row_policies_authenticate_both_targets_and_prepared_source(self):
        for row in CORE["ROW_CONTRACT"]["LATEST_ROWS"]:
            with self.subTest(row=row):
                policy = self.derive(row)
                self.assertEqual(self.load(row), policy)
                self.assertEqual(set(policy["targets"]), {"x86_64", "aarch64"})
                for arch, target in policy["targets"].items():
                    self.assertEqual(target["target"]["arch"], arch)
                    self.assertEqual(target["contract"]["row"], row)
                inputs = ROW["row_inputs"](self.arguments(policy))
                self.assertEqual(inputs["identity"], {"schema_version": 3, "input_binding": POLICY["binding"](policy)})
                self.assertIsNone(inputs["release"])
                self.assertEqual(inputs["source_context"]["source"], policy["source_manifest"]["source"])
                self.assertEqual(inputs["source_context"]["patches"], policy["source_manifest"]["patches"])

    def test_all_six_component_documents_are_authenticated(self):
        policy = self.derive()
        names = [policy["component"]["component"], "implementation/python-cp313-qualification-policy",
                 "python/cp313-source", "implementation/python-cp313-build-policy"]
        names += [target["component"]["component"] for target in policy["targets"].values()]
        copied = self.directory / "components"
        shutil.copytree(self.components, copied)
        for name in names:
            path = copied / (name + ".json")
            original = path.read_bytes()
            value = json.loads(original)
            value["materials"][0]["value"] = "tampered"
            path.write_text(json.dumps(value))
            with self.subTest(name=name), self.assertRaises(POLICY["RowPolicyError"]):
                self.load(directory=copied)
            path.write_bytes(original)
        root = copied / "python/cp313-qualification.json"
        value = json.loads(root.read_text())
        value["dependencies"].pop()
        root.write_text(json.dumps(value))
        with self.assertRaisesRegex(POLICY["RowPolicyError"], "both target"):
            self.load(directory=copied, digest=SHA(value))

    def test_strict_roots_inputs_and_prepared_source_reject_mixing(self):
        policy = self.derive()
        for mutate in (
            lambda a: setattr(a, "release", ROOT / "config/release.json"),
            lambda a: setattr(a, "qualification_components", None),
            lambda a: setattr(a, "qualification_component_sha256", "0" * 64),
            lambda a: setattr(a, "qualification_component_sha256", None),
            lambda a: setattr(a, "row", "cp314"),
            lambda a: setattr(a, "adapter", "legacy"),
        ):
            args = self.arguments(policy)
            mutate(args)
            with self.subTest(mutate=mutate), self.assertRaises(ROW["FinalizationError"]):
                ROW["row_inputs"](args)
        for mutate in (
            lambda s: s.update(schema_version=2.0),
            lambda s: s.update(schema_version=1, release_sha256=SHA(self.release)),
            lambda s: s.update(extra=True),
            lambda s: s["source"].update(sha256="0" * 64),
            lambda s: s["build_policy"].update(canonical_sha256="0" * 64),
        ):
            args = self.arguments(policy)
            source = json.loads(args.source_manifest.read_text())
            mutate(source)
            args.source_manifest.write_text(json.dumps(source))
            with self.subTest(mutate=mutate), self.assertRaises(ROW["FinalizationError"]):
                ROW["row_inputs"](args)

    def test_policy_scope_distinguishes_rows_and_shared_target_qualification(self):
        original = {row: SHA(self.derive(row)) for row in CORE["ROW_CONTRACT"]["LATEST_ROWS"]}
        cases = (
            (lambda r: r["product"].update(version="0.1.1"), set()),
            (lambda r: r["python"]["versions"][0]["source"]["sigstore"].update(bundle_sha256="0" * 64), {"cp39"}),
            (lambda r: r["abi"]["targets"]["x86_64"]["baseline"].update(canonical_sha256="0" * 64), set(original)),
            (lambda r: r["qemu"]["executor"].update(binary_sha256="0" * 64), set(original)),
            (lambda r: r["python"]["zstd"]["source"].update(sha256="0" * 64), {"cp314"}),
        )
        for mutate, expected in cases:
            release = copy.deepcopy(self.release)
            mutate(release)
            changed = {row for row in original if SHA(self.derive(row, release)) != original[row]}
            self.assertEqual(changed, expected)

    def test_sdk_binding_rejects_metadata_and_legacy_claims(self):
        policy = self.derive()
        original = self.manifest_identity(policy)
        SDK["validate_row_input_binding"](original, self.release, "cp313", policy["version"], SHA(self.release))
        for mutate in (
            lambda v: v.update(schema_version=3.0),
            lambda v: v.update(release_sha256=SHA(self.release)),
            lambda v: v.update(qualification_components={}),
            lambda v: v.update(support="wrong"),
            lambda v: v["source"].update(sha256="0" * 64),
            lambda v: v.update(patches=[{"file": "wrong", "sha256": "0" * 64}]),
            lambda v: v["input_binding"].update(policy_sha256="0" * 64),
            lambda v: v["input_binding"].update(extra=True),
        ):
            value = copy.deepcopy(original)
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(SDK["QualificationError"]):
                SDK["validate_row_input_binding"](value, self.release, "cp313", policy["version"], SHA(self.release))

    def test_cropped_row_stage_loads_without_full_release_helpers(self):
        policy = self.derive("cp314")
        scripts = self.directory / "scripts"
        scripts.mkdir()
        block = (ROOT / "docker/python.Dockerfile").read_text().split(" AS cpython-row-assemble\n", 1)[1].split("\nFROM ", 1)[0]
        paths = ["scripts/" + name for name in ("abi_contract.py", "python_abi_audit.py", "python_runtime_providers.py",
            "finalize-cpython-qualification.py", "python_sdk_identity.py", "python_zstd_evidence.py", "python_runtime_overlay.py",
            "python_qualification_policy.py", "python_row_policy.py", "release_component.py", "prepare-cpython-source.py",
            "python_row_contract.py", "target_artifact_audit.py")] + ["docker/finalize-python-row.py"]
        for path in paths:
            self.assertIn(path, block)
            shutil.copyfile(ROOT / path, scripts / Path(path).name)
        for name in ("release-components-core.py", "python_source_release_binding.py", "validate-release.py", "config/release.json"):
            self.assertNotIn(name, block)
        components = self.directory / "components"
        names = [policy["component"]["component"], "implementation/python-cp314-qualification-policy",
                 "python/cp314-source", "implementation/python-cp314-build-policy"]
        names += [value["component"]["component"] for value in policy["targets"].values()]
        for name in names:
            path = components / (name + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.components / (name + ".json"), path)
        args = self.arguments(policy)
        args.qualification_components = components
        code = '''import argparse,json,runpy,sys
from pathlib import Path
r=runpy.run_path(sys.argv[1]); a=json.loads(sys.argv[2])
for key in ('qualification_components','source_manifest'): a[key]=Path(a[key])
v=r['row_inputs'](argparse.Namespace(**a))
assert r['source_binding'].__globals__['SOURCE_BINDING'] is None
assert r['release_components'].__globals__['RELEASE_COMPONENTS'] is None
assert r['QUALIFICATION_VALIDATOR']['release_components'].__globals__['RELEASE_COMPONENTS'] is None
print(json.dumps(v['identity'],sort_keys=True))
'''
        result = subprocess.run([sys.executable, "-c", code, str(scripts / "finalize-python-row.py"), json.dumps(vars(args), default=str)],
            env=dict(os.environ, PYTHONPATH=str(scripts)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"schema_version": 3, "input_binding": POLICY["binding"](policy)})


if __name__ == "__main__":
    unittest.main()
