import ast
import copy
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
READER_PATH = REPOSITORY / "scripts/release_component.py"
READER = runpy.run_path(str(READER_PATH))


def dependency(component, character):
    return {
        "component": component,
        "canonical_sha256": character * 64,
    }


def valid_component():
    return {
        "$schema": "https://crossforge.dev/schemas/release-component.schema.json",
        "schema_version": 1,
        "kind": "crossforge-release-component",
        "component": "python/cp312-source",
        "scope": "build",
        "dependencies": [
            dependency("implementation/python-cp312-build-policy", "1"),
            dependency("sources/python-cp312", "a"),
        ],
        "materials": [
            {"path": "/array", "value": [1, True]},
            {"path": "/boolean", "value": True},
            {"path": "/integer", "value": 7},
            {"path": "/null", "value": None},
            {"path": "/number", "value": 1.5},
            {"path": "/object", "value": {"z": 2, "a": [1, True]}},
            {"path": "/string", "value": "3.12.14"},
            {"path": "/tilde~0token/slash~1token", "value": "escaped"},
        ],
    }


class ReleaseComponentReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "component.json"
        self.document = valid_component()
        self.digest = READER["canonical_sha256"](self.document)
        self.write(self.document)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, value):
        self.path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def load(self):
        return READER["load_component"](
            self.path,
            "python/cp312-source",
            "build",
            self.digest,
        )

    def assert_invalid(self, document, pattern=None):
        self.write(document)
        context = self.assertRaises(READER["ComponentError"])
        with context as captured:
            self.load()
        if pattern is not None:
            self.assertRegex(str(captured.exception), pattern)

    def test_valid_component_loads_without_other_repository_documents(self):
        self.assertEqual(self.load(), self.document)
        self.assertEqual(
            READER["read_material"](
                self.path,
                "python/cp312-source",
                "build",
                self.digest,
                "/tilde~0token/slash~1token",
                "string",
            ),
            "escaped",
        )

    def test_generated_component_matches_its_release_binding_digest(self):
        binding = json.loads(
            (REPOSITORY / "config/generated/release-binding.json").read_text(
                encoding="utf-8"
            )
        )
        record = next(
            item
            for item in binding["components"]
            if item["component"] == "sources/gcc"
        )
        document = READER["load_component"](
            REPOSITORY / record["path"],
            record["component"],
            record["scope"],
            record["canonical_sha256"],
        )
        self.assertEqual(document["component"], "sources/gcc")
        self.assertEqual(
            READER["canonical_sha256"](document),
            record["canonical_sha256"],
        )

    def test_digest_boundary_rejects_material_and_expected_digest_tampering(self):
        changed = copy.deepcopy(self.document)
        next(
            material
            for material in changed["materials"]
            if material["path"] == "/string"
        )["value"] = "3.12.15"
        self.write(changed)
        with self.assertRaisesRegex(
            READER["ComponentError"], "canonical SHA256 differs"
        ):
            self.load()

        self.write(self.document)
        wrong_digest = ("0" if self.digest[0] != "0" else "1") + self.digest[1:]
        with self.assertRaisesRegex(
            READER["ComponentError"], "canonical SHA256 differs"
        ):
            READER["load_component"](
                self.path,
                "python/cp312-source",
                "build",
                wrong_digest,
            )
        for invalid in (None, True, "0" * 63, "A" * 64):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    READER["ComponentError"], "64 lowercase"
                ):
                    READER["load_component"](
                        self.path,
                        "python/cp312-source",
                        "build",
                        invalid,
                    )

    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self):
        self.path.write_text(
            '{"$schema":"x","$schema":"y"}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(READER["ComponentError"], "duplicate JSON key"):
            self.load()

        for token in ("NaN", "Infinity", "-Infinity", "1e9999"):
            with self.subTest(token=token):
                self.path.write_text(
                    json.dumps(self.document).replace('"3.12.14"', token),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    READER["ComponentError"], "non-finite JSON number"
                ):
                    self.load()

    def test_envelope_schema_kind_identity_and_scope_are_exact(self):
        mutations = []
        missing = copy.deepcopy(self.document)
        del missing["kind"]
        mutations.append(missing)
        extra = copy.deepcopy(self.document)
        extra["release"] = {}
        mutations.append(extra)
        for field, value in (
            ("$schema", "https://example.invalid/schema.json"),
            ("schema_version", 2),
            ("schema_version", True),
            ("kind", "crossforge-release-binding"),
        ):
            candidate = copy.deepcopy(self.document)
            candidate[field] = value
            mutations.append(candidate)
        for candidate in mutations:
            with self.subTest(candidate=candidate):
                self.assert_invalid(candidate)

        self.write(self.document)
        with self.assertRaisesRegex(READER["ComponentError"], "identity differs"):
            READER["load_component"](
                self.path,
                "python/cp313-source",
                "build",
                self.digest,
            )
        with self.assertRaisesRegex(READER["ComponentError"], "scope differs"):
            READER["load_component"](
                self.path,
                "python/cp312-source",
                "qualification",
                self.digest,
            )

    def test_component_names_use_safe_nonempty_segments(self):
        unsafe_names = (
            "",
            "/python",
            "python/",
            "python//source",
            "python/../source",
            "python/./source",
            "python/source.json",
            "Python/source",
            "python/source\n",
        )
        for name in unsafe_names:
            with self.subTest(name=name):
                candidate = copy.deepcopy(self.document)
                candidate["component"] = name
                self.assert_invalid(candidate, "unsafe path segments")

        with self.assertRaisesRegex(READER["ComponentError"], "unsafe path segments"):
            READER["load_component"](
                self.path,
                "python/../source",
                "build",
                self.digest,
            )

    def test_dependencies_have_strict_fields_sha_order_and_identity(self):
        candidates = []
        for invalid in (None, {}, "dependency"):
            candidate = copy.deepcopy(self.document)
            candidate["dependencies"] = invalid
            candidates.append(candidate)
        for record in (
            {"component": "sources/gcc"},
            {
                "component": "sources/gcc",
                "canonical_sha256": "0" * 64,
                "extra": True,
            },
            {"component": 1, "canonical_sha256": "0" * 64},
            {"component": "sources/../gcc", "canonical_sha256": "0" * 64},
            {"component": "sources/gcc", "canonical_sha256": "A" * 64},
            {"component": "sources/gcc", "canonical_sha256": "0" * 63},
            {"component": "sources/gcc", "canonical_sha256": 0},
        ):
            candidate = copy.deepcopy(self.document)
            candidate["dependencies"] = [record]
            candidates.append(candidate)
        unsorted = copy.deepcopy(self.document)
        unsorted["dependencies"].reverse()
        candidates.append(unsorted)
        duplicate = copy.deepcopy(self.document)
        duplicate["dependencies"].append(
            dependency("sources/python-cp312", "b")
        )
        candidates.append(duplicate)
        self_reference = copy.deepcopy(self.document)
        self_reference["dependencies"] = [
            dependency("python/cp312-source", "f")
        ]
        candidates.append(self_reference)
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assert_invalid(candidate)

    def test_materials_have_strict_fields_pointers_order_and_identity(self):
        candidates = []
        for invalid in (None, {}, [], "material"):
            candidate = copy.deepcopy(self.document)
            candidate["materials"] = invalid
            candidates.append(candidate)
        for record in (
            {"path": "/missing-value"},
            {"path": "/extra", "value": 1, "extra": True},
            {"path": 1, "value": 1},
            {"path": "no-leading-slash", "value": 1},
            {"path": "/", "value": 1},
            {"path": "/empty//token", "value": 1},
            {"path": "/a/../b", "value": 1},
            {"path": "/bad~escape", "value": 1},
            {"path": "/bad~2escape", "value": 1},
        ):
            candidate = copy.deepcopy(self.document)
            candidate["materials"] = [record]
            candidates.append(candidate)
        unsorted = copy.deepcopy(self.document)
        unsorted["materials"].reverse()
        candidates.append(unsorted)
        duplicate = copy.deepcopy(self.document)
        duplicate["materials"].append({"path": "/string", "value": "other"})
        duplicate["materials"].sort(key=lambda record: record["path"])
        candidates.append(duplicate)
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assert_invalid(candidate)

        invalid_value = copy.deepcopy(self.document)
        invalid_value["materials"][0]["value"] = (1, 2)
        with self.assertRaisesRegex(READER["ComponentError"], "non-JSON value"):
            READER["validate_component"](
                invalid_value, "python/cp312-source", "build"
            )

    def test_material_getter_requires_an_explicit_exact_json_type(self):
        document = self.load()
        accepted = {
            "/array": "array",
            "/boolean": "boolean",
            "/integer": "integer",
            "/null": "null",
            "/number": "number",
            "/object": "object",
            "/string": "string",
        }
        for pointer, expected_type in accepted.items():
            with self.subTest(pointer=pointer):
                READER["material_value"](
                    document,
                    "python/cp312-source",
                    "build",
                    self.digest,
                    pointer,
                    expected_type,
                )
        self.assertEqual(
            READER["material_value"](
                document,
                "python/cp312-source",
                "build",
                self.digest,
                "/integer",
                "number",
            ),
            7,
        )
        for pointer, expected_type in (
            ("/boolean", "integer"),
            ("/boolean", "number"),
            ("/integer", "boolean"),
            ("/number", "integer"),
        ):
            with self.subTest(pointer=pointer, expected_type=expected_type):
                with self.assertRaisesRegex(
                    READER["ComponentError"], "is not JSON type"
                ):
                    READER["material_value"](
                        document,
                        "python/cp312-source",
                        "build",
                        self.digest,
                        pointer,
                        expected_type,
                    )
        with self.assertRaisesRegex(READER["ComponentError"], "unsupported"):
            READER["material_value"](
                document,
                "python/cp312-source",
                "build",
                self.digest,
                "/integer",
                "int",
            )
        with self.assertRaisesRegex(READER["ComponentError"], "does not own"):
            READER["material_value"](
                document,
                "python/cp312-source",
                "build",
                self.digest,
                "/missing",
                "string",
            )
        with self.assertRaisesRegex(READER["ComponentError"], "identity differs"):
            READER["material_value"](
                document,
                "python/cp313-source",
                "build",
                self.digest,
                "/string",
                "string",
            )

    def test_cli_get_emits_canonical_json_and_checks_expected_identity(self):
        command = [
            sys.executable,
            str(READER_PATH),
            "get",
            str(self.path),
            "--expected-component",
            "python/cp312-source",
            "--expected-scope",
            "build",
            "--expected-sha256",
            self.digest,
            "--path",
            "/object",
            "--type",
            "object",
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"a":[1,true],"z":2}\n')
        self.assertEqual(result.stderr, "")

        scalar = command[:]
        scalar[scalar.index("/object")] = "/string"
        scalar[scalar.index("object", scalar.index("--type"))] = "string"
        result = subprocess.run(
            scalar,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '"3.12.14"\n')

        wrong = command[:]
        wrong[wrong.index("python/cp312-source")] = "python/cp313-source"
        result = subprocess.run(
            wrong,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("identity differs", result.stderr)

        wrong_digest = command[:]
        digest_index = wrong_digest.index(self.digest)
        wrong_digest[digest_index] = "0" * 64
        result = subprocess.run(
            wrong_digest,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("canonical SHA256 differs", result.stderr)

        validate = [
            sys.executable,
            str(READER_PATH),
            "validate",
            str(self.path),
            "--expected-component",
            "python/cp312-source",
            "--expected-scope",
            "build",
            "--expected-sha256",
            self.digest,
        ]
        result = subprocess.run(
            validate,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_cli_requires_expected_component_scope_path_and_type(self):
        commands = (
            [sys.executable, str(READER_PATH), "validate", str(self.path)],
            [
                sys.executable,
                str(READER_PATH),
                "validate",
                str(self.path),
                "--expected-component",
                "python/cp312-source",
                "--expected-scope",
                "build",
            ],
            [
                sys.executable,
                str(READER_PATH),
                "get",
                str(self.path),
                "--expected-component",
                "python/cp312-source",
                "--expected-scope",
                "build",
                "--expected-sha256",
                self.digest,
            ],
        )
        for command in commands:
            with self.subTest(command=command):
                result = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                self.assertEqual(result.returncode, 2)

    def test_reader_is_python36_syntax_compatible(self):
        ast.parse(
            READER_PATH.read_text(encoding="utf-8"),
            filename=str(READER_PATH),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
