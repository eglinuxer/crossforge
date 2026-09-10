"""Material identities must bind bytes, recipe inputs, and dependency outputs."""

import ast
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
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_inputs as inputs
    from crossforge_internal import identity
finally:
    sys.path.pop(0)


class ComponentInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = ["docker/Dockerfile", "scripts/build.sh"]
        for path in self.paths:
            file = self.root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("input\n")
            file.chmod(0o644)

    def capture(self, **changes):
        args = {"component": "toolchain/x86_64-build", "scope": "build",
                "paths": self.paths, "targets": ["x86_64-unknown-linux-gnu"],
                "parameters": {"jobs": 4}, "dependencies": [{
                    "component": "sysroot/x86_64", "inputs_sha256": "1" * 64,
                    "artifact_digest": "sha256:" + "2" * 64}]}
        args.update(changes)
        return inputs.capture(self.root, **args)

    def test_identity_is_independent_of_location_timestamps_and_order(self):
        expected = self.capture()
        os.utime(self.root / self.paths[0], (1, 1))
        self.assertEqual(inputs.identity(self.capture(paths=list(reversed(self.paths)))),
                         inputs.identity(expected))
        with tempfile.TemporaryDirectory() as directory:
            for path in self.paths:
                target = Path(directory) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.root / path, target)
                target.chmod(0o644)
            inputs.verify_files(expected, directory)
        inputs.require_match(expected, self.capture())

    def test_byte_change_and_permission_change_invalidate_identity(self):
        expected = self.capture()
        file = self.root / self.paths[0]
        file.write_text("changed\n")
        with self.assertRaisesRegex(identity.IdentityError, "file differs"):
            inputs.verify_files(expected, self.root)
        with self.assertRaisesRegex(identity.IdentityError, "inputs differ"):
            inputs.require_match(expected, self.capture())
        file.write_text("input\n")
        file.chmod(0o755)
        with self.assertRaisesRegex(identity.IdentityError, "file differs"):
            inputs.verify_files(expected, self.root)

    def test_undeclared_file_cannot_be_hidden_by_reusing_partial_materials(self):
        recorded = self.capture(paths=[self.paths[0]])
        expected = self.capture()
        with self.assertRaisesRegex(identity.IdentityError, "inputs differ"):
            inputs.require_match(recorded, expected)

    def test_parameters_targets_and_scope_change_identity(self):
        expected = self.capture()
        for changed in (self.capture(parameters={"jobs": 8}),
                        self.capture(targets=["aarch64-unknown-linux-gnu"]),
                        self.capture(scope="qualification")):
            with self.assertRaisesRegex(identity.IdentityError, "inputs differ"):
                inputs.require_match(expected, changed)

    def test_dependency_input_and_artifact_identity_are_both_bound(self):
        expected = self.capture()
        for key, value in (("inputs_sha256", "3" * 64),
                           ("artifact_digest", "sha256:" + "4" * 64)):
            changed = copy.deepcopy(expected)
            changed["dependencies"][0][key] = value
            with self.assertRaisesRegex(identity.IdentityError, "inputs differ"):
                inputs.require_match(expected, changed)

    def test_unknown_fields_and_schema_versions_are_rejected(self):
        for key, value in (("unknown", 1), ("source_commit", "a" * 40),
                           ("schema_version", 2), ("schema_version", True),
                           ("kind", "release"), ("scope", "future"),
                           ("platform", "linux/arm64")):
            with self.subTest(key=key, value=value):
                document = self.capture()
                document[key] = value
                with self.assertRaises(identity.IdentityError):
                    inputs.validate(document)

    def test_unsafe_and_missing_source_files_are_rejected(self):
        for path in ("/etc/passwd", "../outside", "scripts/../build.sh", ".git/config",
                     "scripts//build.sh", "scripts/./build.sh", "scripts\\build.sh",
                     "scripts/build.sh\n", "bad\ud800", "absent", "scripts"):
            with self.subTest(path=path):
                with self.assertRaises(identity.IdentityError):
                    self.capture(paths=[path])

    def test_symlinks_and_special_files_cannot_enter_material_capture(self):
        (self.root / "link").symlink_to(self.root / self.paths[0])
        (self.root / "directory-link").symlink_to(self.root / "scripts", target_is_directory=True)
        os.mkfifo(str(self.root / "fifo"))
        for path in ("link", "directory-link/build.sh", "fifo"):
            with self.subTest(path=path):
                with self.assertRaises(identity.IdentityError):
                    self.capture(paths=[path])

    def test_duplicates_and_unsorted_records_are_rejected(self):
        for field in ("files", "targets", "dependencies"):
            with self.subTest(field=field):
                document = self.capture()
                document[field].append(copy.deepcopy(document[field][0]))
                with self.assertRaises(identity.IdentityError):
                    inputs.validate(document)
        document = self.capture()
        document["files"].reverse()
        with self.assertRaises(identity.IdentityError):
            inputs.validate(document)

    def test_malformed_dependency_and_file_records_are_rejected(self):
        for field, key, value in (("files", "sha256", "a" * 64 + "\n"),
                                  ("files", "mode", "644"),
                                  ("files", "extra", 1),
                                  ("dependencies", "artifact_digest", "latest"),
                                  ("dependencies", "component", "toolchain/x86_64-build"),
                                  ("dependencies", "extra", 1)):
            document = self.capture()
            document[field][0][key] = value
            with self.subTest(field=field, key=key):
                with self.assertRaises(identity.IdentityError):
                    inputs.validate(document)

    def test_component_schema_matches_captured_document(self):
        strict = runpy.run_path(str(ROOT / "scripts/validate-release.py"))
        schema = json.loads((ROOT / "config/schemas/component-inputs.schema.json").read_text())
        strict["validate_schema_subset"](schema)
        strict["validate"](self.capture(), schema, schema, "$inputs")

    def test_package_runs_without_any_other_repository_domains(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copytree(ROOT / "scripts/crossforge_internal",
                            Path(directory) / "crossforge_internal")
            result = subprocess.run([sys.executable, "-c",
                "from crossforge_internal import component_inputs; "
                "from crossforge_internal.identity import content_sha256; "
                "assert len(content_sha256({'value': 1})) == 64"],
                cwd=directory, env=dict(os.environ, PYTHONPATH=directory),
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        for path in (ROOT / "scripts/crossforge_internal").glob("*.py"):
            ast.parse(path.read_text(), filename=str(path), feature_version=(3, 6))


class StrictIdentityJSONTests(unittest.TestCase):
    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        for source in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}',
                       '{"x":-Infinity}', '{"x":1e999}'):
            with self.subTest(source=source):
                with self.assertRaises(identity.IdentityError):
                    identity.parse_json(source)

    def test_canonical_bytes_are_utf8_sorted_and_reject_non_json_values(self):
        self.assertEqual(identity.canonical_bytes({"z": 1, "a": "中"}),
                         '{"a":"中","z":1}'.encode("utf-8"))
        for value in ({1: "bad"}, {"x": float("nan")}, {"x": b"bad"}, {"x": (1, 2)},
                      {"x": "\ud800"}):
            with self.assertRaises(identity.IdentityError):
                identity.canonical_bytes(value)

    def test_malformed_json_and_non_string_fields_are_rejected(self):
        for value in ('{', b'\xff'):
            with self.assertRaises(identity.IdentityError):
                identity.parse_json(value)
        with self.assertRaises(identity.IdentityError):
            identity.exact_fields({1: "x", "extra": 2}, {"required"}, "fixture")


if __name__ == "__main__":
    unittest.main()
