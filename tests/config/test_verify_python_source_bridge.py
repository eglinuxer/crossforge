import ast
import copy
import contextlib
import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
VERIFY_PATH = REPOSITORY / "docker/verify-python-row.py"
VERIFY = runpy.run_path(str(VERIFY_PATH))


class VerifyPythonSourceBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        binding = json.loads(
            (REPOSITORY / "config/generated/release-binding.json").read_text(
                encoding="utf-8"
            )
        )
        cls.digests = {
            record["component"]: record["canonical_sha256"]
            for record in binding["components"]
        }

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.row = "cp312"
        self.version = "3.12.14"
        self.adapter = "modern"
        self.source_name = "python/cp312-source"
        self.policy_name = "implementation/python-cp312-build-policy"
        self.source_path = (
            REPOSITORY / "config/generated/components/python/cp312-source.json"
        )
        self.policy_path = (
            REPOSITORY
            / "config/generated/components/implementation/python-cp312-build-policy.json"
        )
        self.manifest = VERIFY["component_row_contract"](
            self.row,
            self.version,
            self.adapter,
            self.source_path,
            self.digests[self.source_name],
            self.policy_path,
            self.digests[self.policy_name],
        )
        self.manifest_path = self.directory / "source-manifest.json"
        self.write_manifest(self.manifest)

    def tearDown(self):
        self.temporary.cleanup()

    def write_manifest(self, value):
        self.manifest_path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def bridge(self, manifest=None, release=None):
        return VERIFY["bridge_source_manifest"](
            self.manifest if manifest is None else manifest,
            self.release if release is None else release,
            self.row,
            self.version,
            self.adapter,
        )

    def run_bridge_cli(self, release=None):
        release_path = self.directory / "release.json"
        release_path.write_text(
            json.dumps(self.release if release is None else release),
            encoding="utf-8",
        )
        return subprocess.run(
            [
                sys.executable,
                str(VERIFY_PATH),
                "--release",
                str(release_path),
                "--row",
                self.row,
                "--version",
                self.version,
                "--adapter",
                self.adapter,
                "--manifest",
                str(self.manifest_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

    def test_v2_manifest_bridges_to_release_through_api_and_cli(self):
        bound = self.bridge()
        self.assertEqual(bound["schema_version"], 2)
        self.assertEqual(bound["support"], "security")
        self.assertEqual(bound["source_component"], self.manifest["source_component"])
        self.assertEqual(bound["build_policy"], self.manifest["build_policy"])

        result = self.run_bridge_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("valid CPython row", result.stdout)

    def test_v1_manifest_remains_release_compatible(self):
        manifest = VERIFY["row_contract"](
            self.release, self.row, self.version, self.adapter
        )
        bound = self.bridge(manifest=manifest)
        self.assertEqual(bound["schema_version"], 1)
        self.assertEqual(bound["support"], "security")
        self.assertIsNone(bound["source_component"])
        self.assertIsNone(bound["build_policy"])

    def test_support_only_release_change_keeps_v2_build_identity_valid(self):
        release = copy.deepcopy(self.release)
        entry = next(
            item
            for item in release["python"]["versions"]
            if item["version"] == self.version
        )
        entry["support"] = "bugfix"
        bound = self.bridge(release=release)
        self.assertEqual(bound["support"], "bugfix")
        self.assertEqual(bound["source_component"], self.manifest["source_component"])
        self.assertEqual(bound["build_policy"], self.manifest["build_policy"])

    def test_coordinated_component_resigning_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["source_component"]["canonical_sha256"] = "1" * 64
        manifest["build_policy"]["canonical_sha256"] = "2" * 64
        with self.assertRaises(VERIFY["RowError"]):
            self.bridge(manifest=manifest)

    def test_fully_resigned_build_contract_is_rejected_by_release_bridge(self):
        resigned_adapter = "transition"
        policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        next(
            material
            for material in policy["materials"]
            if material["path"].endswith("/adapter")
        )["value"] = resigned_adapter
        policy_path = self.directory / "resigned-policy.json"
        policy_path.write_text(
            json.dumps(policy, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        policy_digest = VERIFY["canonical_sha256"](policy)

        source = json.loads(self.source_path.read_text(encoding="utf-8"))
        source["dependencies"][0]["canonical_sha256"] = policy_digest
        next(
            material
            for material in source["materials"]
            if material["path"].endswith("/adapter")
        )["value"] = resigned_adapter
        source_path = self.directory / "resigned-source.json"
        source_path.write_text(
            json.dumps(source, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        source_digest = VERIFY["canonical_sha256"](source)

        manifest = VERIFY["component_row_contract"](
            self.row,
            self.version,
            resigned_adapter,
            source_path,
            source_digest,
            policy_path,
            policy_digest,
        )
        self.assertEqual(manifest["adapter"], resigned_adapter)
        self.assertEqual(manifest["build_policy"]["adapter"], resigned_adapter)
        self.assertEqual(
            manifest["source_component"]["canonical_sha256"], source_digest
        )
        self.assertEqual(
            manifest["build_policy"]["canonical_sha256"], policy_digest
        )

        with self.assertRaises(VERIFY["RowError"]):
            VERIFY["bridge_source_manifest"](
                manifest,
                self.release,
                self.row,
                self.version,
                resigned_adapter,
            )

    def test_wrong_component_projection_path_and_digest_are_rejected(self):
        wrong_source = (
            REPOSITORY / "config/generated/components/python/cp313-source.json"
        )
        with self.assertRaises(VERIFY["RowError"]):
            VERIFY["component_row_contract"](
                self.row,
                self.version,
                self.adapter,
                wrong_source,
                self.digests["python/cp313-source"],
                self.policy_path,
                self.digests[self.policy_name],
            )
        with self.assertRaises(VERIFY["RowError"]):
            VERIFY["component_row_contract"](
                self.row,
                self.version,
                self.adapter,
                self.source_path,
                "0" * 64,
                self.policy_path,
                self.digests[self.policy_name],
            )

    def test_bridge_rejects_json_type_confusion(self):
        mutations = []
        for value in (True, 2.0):
            manifest = copy.deepcopy(self.manifest)
            manifest["schema_version"] = value
            mutations.append(manifest)
        manifest = copy.deepcopy(self.manifest)
        manifest["source"]["size"] = True
        mutations.append(manifest)
        for manifest in mutations:
            with self.subTest(value=manifest.get("schema_version")):
                with self.assertRaises(VERIFY["RowError"]):
                    self.bridge(manifest=manifest)

    def test_component_only_verification_does_not_load_release_bridge(self):
        function = VERIFY["component_row_contract"]
        globals_ = function.__globals__
        original = globals_["source_binding_tools"]

        def forbidden_bridge():
            raise AssertionError("component-only verification loaded release bridge")

        globals_["source_binding_tools"] = forbidden_bridge
        try:
            expected = VERIFY["component_row_contract"](
                self.row,
                self.version,
                self.adapter,
                self.source_path,
                self.digests[self.source_name],
                self.policy_path,
                self.digests[self.policy_name],
            )
            self.assertEqual(
                VERIFY["verify_source_manifest"](self.manifest_path, expected),
                expected,
            )
            original_argv = sys.argv
            sys.argv = [
                str(VERIFY_PATH),
                "--row",
                self.row,
                "--version",
                self.version,
                "--adapter",
                self.adapter,
                "--source-component",
                str(self.source_path),
                "--source-component-sha256",
                self.digests[self.source_name],
                "--policy-component",
                str(self.policy_path),
                "--policy-component-sha256",
                self.digests[self.policy_name],
                "--manifest",
                str(self.manifest_path),
            ]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(VERIFY["main"](), 0)
            finally:
                sys.argv = original_argv
        finally:
            globals_["source_binding_tools"] = original

    def test_verifier_remains_python36_syntax_compatible(self):
        ast.parse(
            VERIFY_PATH.read_text(encoding="utf-8"),
            filename=str(VERIFY_PATH),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
