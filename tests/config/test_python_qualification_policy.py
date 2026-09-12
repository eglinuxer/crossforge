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
CORE = runpy.run_path(str(ROOT / "scripts/release-components-core.py"))
POLICY = runpy.run_path(str(ROOT / "scripts/python_qualification_policy.py"))
ABI = runpy.run_path(str(ROOT / "scripts/abi_contract.py"))
ZSTD = runpy.run_path(str(ROOT / "scripts/python_zstd_evidence.py"))
SHA = CORE["canonical_sha256"]


class PythonQualificationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads((ROOT / "config/release.json").read_text())
        cls.documents = CORE["render_component_documents"](cls.release)
        cls.rows = CORE["IMPLEMENTED_ROWS"]
        cls.names = {"implementation/python-%s-qualification-policy" % row["row"] for row in cls.rows}
        cls.names.update("python/%s%s-qualification" % (row["row"], suffix)
                         for row in cls.rows for suffix in ("", "-x86_64", "-aarch64"))

    def target_names(self, rows=None, arches=("x86_64", "aarch64")):
        rows = [item["row"] for item in self.rows] if rows is None else rows
        return {"python/%s%s-qualification" % (row, suffix)
                for row in rows for suffix in ("",) + tuple("-" + arch for arch in arches)}

    def changed(self, mutate=None, rows=None):
        release = copy.deepcopy(self.release)
        if mutate:
            mutate(release)
        after = CORE["render_component_documents"](release, self.rows if rows is None else rows)
        return {name for name in self.names if SHA(self.documents[name]) != SHA(after[name])}

    def target_documents(self, version="3.9.25", arch="x86_64"):
        name = POLICY["component_name"](version, arch)
        row = CORE["ROW_CONTRACT"]["contract_for_version"](version)["row"]
        return (copy.deepcopy(self.__class__.documents[name]),
                copy.deepcopy(self.__class__.documents["implementation/python-%s-qualification-policy" % row]))

    def read(self, root, row_policy, version="3.9.25", arch="x86_64", digest=None):
        return POLICY["from_documents"](root, row_policy, version, arch, SHA(root) if digest is None else digest)

    def test_all_twelve_inputs_match_existing_release_expectations(self):
        for entry in self.release["python"]["versions"]:
            version = entry["version"]
            contract = CORE["ROW_CONTRACT"]["contract_for_version"](version)
            for arch in ("x86_64", "aarch64"):
                with self.subTest(version=version, arch=arch):
                    expected = POLICY["from_release"](self.release, version, arch, CORE["render_component_documents"])
                    actual = POLICY["load"](ROOT / "config/generated/components", version, arch,
                                             expected["component"]["canonical_sha256"])
                    self.assertEqual(actual, expected)
                    self.assertEqual(actual["contract"], contract)
                    self.assertEqual(actual["source"], entry["source"])
                    self.assertEqual(actual["abi"], ABI["release_abi_inputs"](self.release, arch))
                    self.assertEqual(actual["target"], next(item for item in self.release["targets"] if item["arch"] == arch))
                    self.assertEqual(actual["runtime_overlay_binding"]["canonical_sha256"],
                                     SHA(self.__class__.documents["rpm/sysroot-" + arch]))
                    self.assertEqual(actual["runtime_executor"]["kind"], "native" if arch == "x86_64" else "qemu")
                    self.assertNotIn("release_sha256", actual)
                    self.assertEqual(actual["zstd_components"], ZSTD["expected_components"](
                        self.release, arch, CORE["render_component_documents"]) if contract["zstd"] else None)
                    row_document = self.__class__.documents["python/%s-qualification" % contract["row"]]
                    self.assertEqual({item["component"] for item in row_document["dependencies"]}, {
                        "python/%s-%s-qualification" % (contract["row"], target_arch)
                        for target_arch in ("x86_64", "aarch64")})

    def test_cropped_reader_needs_only_two_projections_and_three_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ("python_qualification_policy.py", "python_row_contract.py", "release_component.py"):
                shutil.copyfile(ROOT / "scripts" / name, directory / name)
            for arch in ("x86_64", "aarch64"):
                version = self.release["python"]["versions"][-1]["version"]
                # cp314 exercises zstd and ARM exercises the explicit QEMU pins.
                self.assertTrue(version.startswith("3.14."))
                root, policy = self.target_documents(version, arch)
                for document in (root, policy):
                    path = directory / (document["component"] + ".json")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(document))
                result = subprocess.run([sys.executable, "-c",
                    "import json,runpy,sys; p=runpy.run_path(sys.argv[1]); "
                    "print(json.dumps(p['load'](sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5]),sort_keys=True))",
                    str(directory / "python_qualification_policy.py"), str(directory), version, arch, SHA(root)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), self.read(root, policy, version, arch))
            self.assertFalse((directory / "config/release.json").exists())
            self.assertFalse((directory / "release-components-core.py").exists())

    def test_product_and_unrelated_supply_inputs_do_not_rebind_rows(self):
        mutations = (
            lambda r: r["product"].update(version="0.1.1"),
            lambda r: r["sigstore"]["trust"].update(trusted_root_sha256="0" * 64),
            lambda r: r["source_bundle"]["rpm"]["lock"].update(canonical_sha256="0" * 64),
            lambda r: r["vcpkg"].update(commit="0" * 40),
            lambda r: r["qemu"]["executor"]["source"]["archive"].update(sha256="0" * 64),
            lambda r: r["qemu"]["executor"]["provenance"]["builder_source"].update(sha256="0" * 64),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.assertEqual(self.changed(mutate), set())

    def test_row_source_support_and_signature_changes_have_exact_row_impact(self):
        mutations = (
            lambda e: e["patches"][0].update(sha256="0" * 64),
            lambda e: e["source"].update(sha256="0" * 64),
            lambda e: e.update(support="bugfix"),
            lambda e: e["source"]["sigstore"].update(bundle_sha256="0" * 64),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.assertEqual(self.changed(lambda r: mutate(r["python"]["versions"][0])),
                                 self.target_names(["cp39"]))
        rows = copy.deepcopy(self.rows)
        rows[-1]["introduced_phase"] += 1
        self.assertEqual(self.changed(rows=rows), self.target_names(["cp39"]) |
                         {"implementation/python-cp39-qualification-policy"})

    def test_target_abi_and_sysroot_changes_preserve_other_architecture(self):
        for arch, index in (("x86_64", 0), ("aarch64", 1)):
            mutations = (
                lambda r: r["targets"][index]["sysroot"].update(canonical_sha256="0" * 64),
                lambda r: r["abi"]["targets"][arch]["baseline"].update(canonical_sha256="0" * 64),
                lambda r: r["abi"]["targets"][arch]["sysroot_inventory"].update(canonical_sha256="0" * 64),
                lambda r: r["abi"]["python"]["provider_catalogs"][arch].update(canonical_sha256="0" * 64),
            )
            for mutate in mutations:
                with self.subTest(arch=arch, mutate=mutate):
                    self.assertEqual(self.changed(mutate), self.target_names(arches=(arch,)))

    def test_shared_inputs_qemu_and_zstd_have_explicit_closures(self):
        for mutate in (
            lambda r: r["abi"]["provider_manifest"].update(canonical_sha256="0" * 64),
            lambda r: r["abi"]["python"]["runtime_provider_policy"].update(canonical_sha256="0" * 64),
            lambda r: r["host_locks"]["host-python-build"].update(canonical_sha256="0" * 64),
            lambda r: r["base_image"]["manifests"].update(arm64="sha256:" + "0" * 64),
        ):
            # The base image is intentionally still shared by the RPM build
            # projections. Do not claim target isolation where they share it.
            self.assertEqual(self.changed(mutate), self.target_names())
        self.assertEqual(self.changed(lambda r: r["qemu"]["executor"].update(cpu="max")),
                         self.target_names(arches=("aarch64",)))
        self.assertEqual(self.changed(lambda r: r["python"]["zstd"]["source"].update(sha256="0" * 64)),
                         self.target_names(["cp314"]))

    def test_pins_role_version_and_dependencies_fail_closed(self):
        root, policy = self.target_documents()
        mutations = (
            lambda r: r.update(schema_version=True),
            lambda r: r.update(scope="build"),
            lambda r: r.update(component="python/cp310-x86_64-qualification"),
            lambda r: r.update(extra=True),
            lambda r: r["dependencies"].pop(),
        )
        for mutate in mutations:
            changed = copy.deepcopy(root)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(PolicyError):
                self.read(changed, policy)
        with self.assertRaises(PolicyError):
            self.read(root, policy, digest="0" * 64)
        for version, arch in (("3.9.24", "x86_64"), ("3.10.21", "x86_64"), ("3.9.25", "aarch64"), ("3.9.25", "armv7")):
            with self.subTest(version=version, arch=arch), self.assertRaises(PolicyError):
                self.read(root, policy, version, arch)
        policy["materials"][0]["value"] = "tampered"
        with self.assertRaises(PolicyError):
            self.read(root, policy)

    def test_authenticated_but_malformed_consumed_fields_are_rejected(self):
        mutations = {
            "/python/versions/0/source/size": True,
            "/python/versions/0/source/sigstore/bundle_size": True,
            "/python/versions/0/source/sigstore/verification": "pending",
            "/abi/targets/x86_64/baseline/file": "abi/el8/aarch64.json",
            "/targets/0/triple": "aarch64-unknown-linux-gnu",
            "/targets/0/sysroot/status": "pending",
            "/base_image/digest": "latest",
        }
        for path, value in mutations.items():
            root, policy = self.target_documents()
            next(item for item in root["materials"] if item["path"] == path)["value"] = value
            with self.subTest(path=path), self.assertRaises(PolicyError):
                self.read(root, policy)
        root, policy = self.target_documents()
        next(item for item in policy["materials"] if item["path"].endswith("/sysconfig_isolation"))["value"] = 1
        next(item for item in root["dependencies"] if item["component"] == policy["component"])["canonical_sha256"] = SHA(policy)
        with self.assertRaises(PolicyError):
            self.read(root, policy)

    def test_duplicate_json_and_ambiguous_row_materials_are_rejected(self):
        root, policy = self.target_documents()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / (root["component"] + ".json")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(root)[:-1] + ', "scope": "qualification"}')
            with self.assertRaises(PolicyError):
                POLICY["load"](directory, "3.9.25", "x86_64", SHA(root))
        root["materials"].append({"path": "/python/versions/1/version", "value": "3.10.21"})
        root["materials"].sort(key=lambda item: item["path"])
        with self.assertRaises(PolicyError):
            self.read(root, policy)

    def test_binding_is_strict_configuration_identity_without_a_release_claim(self):
        root, row_policy = self.target_documents()
        policy = self.read(root, row_policy)
        report = {"input_binding": POLICY["binding"](policy)}
        POLICY["require_binding"](report, policy)
        for mutate in (
            lambda r: r.update(release_sha256="0" * 64),
            lambda r: r["input_binding"].update(schema_version=True),
            lambda r: r["input_binding"].update(policy_sha256="0" * 64),
            lambda r: r["input_binding"].update(extra=True),
        ):
            wrong = copy.deepcopy(report)
            mutate(wrong)
            with self.subTest(mutate=mutate), self.assertRaises(PolicyError):
                POLICY["require_binding"](wrong, policy)


PolicyError = POLICY["PolicyError"]


if __name__ == "__main__":
    unittest.main()
