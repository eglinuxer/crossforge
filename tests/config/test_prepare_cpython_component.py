import ast
import copy
import hashlib
import json
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.config.test_prepare_cpython_source import (
    PrepareCPythonSourceTests as _PrepareFixture,
)


REPOSITORY = Path(__file__).resolve().parents[2]
PREPARER_PATH = REPOSITORY / "scripts/prepare-cpython-source.py"
VERIFY_PATH = REPOSITORY / "docker/verify-python-row.py"
PREPARER = runpy.run_path(str(PREPARER_PATH))
VERIFY = runpy.run_path(str(VERIFY_PATH))
READER = runpy.run_path(str(REPOSITORY / "scripts/release_component.py"))
write_archive = _PrepareFixture.write_archive
del _PrepareFixture


def write_document(path, document):
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return READER["canonical_sha256"](document)


def policy_document(row="cp311", minor="3.11", adapter="transition"):
    name = "implementation/python-%s-build-policy" % row
    prefix = "/@implementation/python_rows/%s" % row
    return {
        "$schema": "https://crossforge.dev/schemas/release-component.schema.json",
        "schema_version": 1,
        "kind": "crossforge-release-component",
        "component": name,
        "scope": "build",
        "dependencies": [],
        "materials": [
            {"path": prefix + "/adapter", "value": adapter},
            {"path": prefix + "/minor", "value": minor},
            {"path": prefix + "/row", "value": row},
            {"path": prefix + "/sysconfig_isolation", "value": True},
        ],
    }


def source_document(
    archive,
    policy_digest,
    row="cp311",
    version="3.11.16",
    adapter="transition",
    index="47",
    patches=None,
):
    if patches is None:
        patches = []
    digest, size = PREPARER["sha256_file"](archive)
    prefix = "/python/versions/%s" % index
    materials = [
        {"path": prefix + "/adapter", "value": adapter},
        {"path": prefix + "/source/sha256", "value": digest},
        {"path": prefix + "/source/size", "value": size},
        {"path": prefix + "/source/status", "value": "locked"},
        {
            "path": prefix + "/source/url",
            "value": "https://example.invalid/Python-%s.tar.xz" % version,
        },
        {"path": prefix + "/version", "value": version},
    ]
    if patches:
        for patch_index, patch in enumerate(patches):
            for field in ("file", "sha256"):
                materials.append(
                    {
                        "path": "%s/patches/%d/%s"
                        % (prefix, patch_index, field),
                        "value": patch[field],
                    }
                )
    else:
        materials.append({"path": prefix + "/patches", "value": []})
    materials.sort(key=lambda record: record["path"])
    return {
        "$schema": "https://crossforge.dev/schemas/release-component.schema.json",
        "schema_version": 1,
        "kind": "crossforge-release-component",
        "component": "python/%s-source" % row,
        "scope": "build",
        "dependencies": [
            {
                "component": "implementation/python-%s-build-policy" % row,
                "canonical_sha256": policy_digest,
            }
        ],
        "materials": materials,
    }


class PrepareCPythonComponentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.archive = self.directory / "Python-3.11.16.tar.xz"
        write_archive(self.archive)
        self.policy_path = self.directory / "policy.json"
        self.source_path = self.directory / "source.json"
        self.policy = policy_document()
        self.policy_digest = write_document(self.policy_path, self.policy)
        self.source = source_document(self.archive, self.policy_digest)
        self.source_digest = write_document(self.source_path, self.source)

    def tearDown(self):
        self.temporary.cleanup()

    def row(self, **overrides):
        arguments = {
            "row": "cp311",
            "expected_version": "3.11.16",
            "expected_adapter": "transition",
            "source_component": self.source_path,
            "source_component_sha256": self.source_digest,
            "policy_component": self.policy_path,
            "policy_component_sha256": self.policy_digest,
        }
        arguments.update(overrides)
        return PREPARER["row_from_components"](**arguments)

    def test_component_prepare_writes_v2_without_global_release_identity(self):
        destination = self.directory / "prepared"
        manifest = self.directory / "manifest.json"
        identity = PREPARER["prepare_component"](
            "cp311",
            "3.11.16",
            "transition",
            self.archive,
            destination,
            manifest,
            REPOSITORY,
            self.source_path,
            self.source_digest,
            self.policy_path,
            self.policy_digest,
        )
        self.assertEqual(identity["schema_version"], 2)
        self.assertEqual(identity["source_component"], {
            "component": "python/cp311-source",
            "canonical_sha256": self.source_digest,
        })
        self.assertEqual(identity["build_policy"]["canonical_sha256"], self.policy_digest)
        self.assertNotIn("release_sha256", identity)
        self.assertNotIn("support", identity)
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), identity)
        expected = VERIFY["component_row_contract"](
            "cp311",
            "3.11.16",
            "transition",
            self.source_path,
            self.source_digest,
            self.policy_path,
            self.policy_digest,
        )
        self.assertEqual(expected, identity)
        self.assertEqual(VERIFY["verify_source_manifest"](manifest, expected), identity)

    def test_dynamic_version_prefix_and_ordered_patch_extraction(self):
        archive = self.directory / "Python-3.12.14.tar.xz"
        write_archive(
            archive, version="3.12.14", vulnerable=True
        )
        policy = policy_document("cp312", "3.12", "modern")
        policy_path = self.directory / "policy312.json"
        policy_digest = write_document(policy_path, policy)
        patches = [
            {
                "file": "patches/cpython/3.12/first.patch",
                "sha256": "a" * 64,
            },
            {
                "file": "patches/cpython/3.12/second.patch",
                "sha256": "b" * 64,
            },
        ]
        source = source_document(
            archive,
            policy_digest,
            row="cp312",
            version="3.12.14",
            adapter="modern",
            index="91",
            patches=patches,
        )
        source_path = self.directory / "source312.json"
        source_digest = write_document(source_path, source)
        entry, _identities = PREPARER["row_from_components"](
            "cp312", "3.12.14", "modern",
            source_path, source_digest, policy_path, policy_digest
        )
        self.assertEqual(entry["patches"], patches)

    def test_wrong_digest_row_adapter_and_policy_dependency_fail_closed(self):
        with self.assertRaises(PREPARER["PreparationError"]):
            self.row(source_component_sha256="0" * 64)
        with self.assertRaises(PREPARER["PreparationError"]):
            self.row(policy_component_sha256="0" * 64)
        with self.assertRaises(PREPARER["PreparationError"]):
            self.row(expected_version="3.11.15")
        with self.assertRaises(PREPARER["PreparationError"]):
            self.row(expected_adapter="modern")
        with self.assertRaises(PREPARER["PreparationError"]):
            self.row(row="cp312")

        source = copy.deepcopy(self.source)
        next(
            record for record in source["materials"]
            if record["path"].endswith("/adapter")
        )["value"] = "modern"
        source_digest = write_document(self.source_path, source)
        with self.assertRaisesRegex(PREPARER["PreparationError"], "build policy"):
            self.row(source_component_sha256=source_digest)

        source = copy.deepcopy(self.source)
        source["dependencies"][0]["canonical_sha256"] = "0" * 64
        source_digest = write_document(self.source_path, source)
        with self.assertRaisesRegex(PREPARER["PreparationError"], "dependency"):
            self.row(source_component_sha256=source_digest)

    def test_duplicate_material_patch_path_hash_and_types_are_rejected(self):
        mutations = []
        duplicate = copy.deepcopy(self.source)
        duplicate["materials"].append(copy.deepcopy(duplicate["materials"][0]))
        duplicate["materials"].sort(key=lambda record: record["path"])
        mutations.append(duplicate)

        wrong_type = copy.deepcopy(self.source)
        next(
            record for record in wrong_type["materials"]
            if record["path"].endswith("/source/size")
        )["value"] = True
        mutations.append(wrong_type)

        for document in mutations:
            with self.subTest(kind=len(document["materials"])):
                digest = write_document(self.source_path, document)
                with self.assertRaises(PREPARER["PreparationError"]):
                    self.row(source_component_sha256=digest)

        archive = self.directory / "Python-3.12.14.tar.xz"
        write_archive(archive, version="3.12.14")
        policy = policy_document("cp312", "3.12", "modern")
        policy_path = self.directory / "policy312.json"
        policy_digest = write_document(policy_path, policy)
        bad_patches = (
            [{"file": "patches/cpython/3.11/wrong.patch", "sha256": "a" * 64}],
            [{"file": "patches/cpython/3.12/a.patch", "sha256": "A" * 64}],
            [{"file": "patches/cpython/3.12/./a.patch", "sha256": "a" * 64}],
            [{"file": "patches/cpython/3.12//a.patch", "sha256": "a" * 64}],
            [{"file": "patches/cpython/3.12/nested/a.patch", "sha256": "a" * 64}],
            [{"file": "patches/cpython/3.12/a.diff", "sha256": "a" * 64}],
        )
        for patches in bad_patches:
            source = source_document(
                archive, policy_digest, "cp312", "3.12.14", "modern", patches=patches
            )
            path = self.directory / "bad-source.json"
            digest = write_document(path, source)
            with self.assertRaisesRegex(PREPARER["PreparationError"], "patch"):
                PREPARER["row_from_components"](
                    "cp312", "3.12.14", "modern",
                    path, digest, policy_path, policy_digest
                )

    def test_duplicate_json_keys_are_rejected(self):
        payload = json.dumps(self.source, sort_keys=True).replace(
            '"scope": "build"', '"scope": "build", "scope": "build"'
        )
        self.source_path.write_text(payload, encoding="utf-8")
        with self.assertRaisesRegex(PREPARER["PreparationError"], "duplicate JSON key"):
            self.row()

    def test_component_mode_does_not_load_full_release_or_schema(self):
        isolated = self.directory / "isolated"
        scripts = isolated / "scripts"
        scripts.mkdir(parents=True)
        for source in (
            PREPARER_PATH,
            REPOSITORY / "scripts/release_component.py",
        ):
            shutil.copy2(str(source), str(scripts / source.name))
        module = runpy.run_path(str(scripts / PREPARER_PATH.name))
        destination = isolated / "prepared"
        manifest = isolated / "manifest.json"
        identity = module["prepare_component"](
            "cp311",
            "3.11.16",
            "transition",
            self.archive,
            destination,
            manifest,
            isolated,
            self.source_path,
            self.source_digest,
            self.policy_path,
            self.policy_digest,
        )
        self.assertEqual(identity["schema_version"], 2)
        self.assertFalse((scripts / "validate-release.py").exists())
        self.assertFalse((scripts / "python_row_contract.py").exists())

    def test_coordinated_policy_and_source_adapter_resigning_is_rejected(self):
        policy = copy.deepcopy(self.policy)
        next(
            record
            for record in policy["materials"]
            if record["path"].endswith("/adapter")
        )["value"] = "legacy"
        policy_digest = write_document(self.policy_path, policy)
        source = copy.deepcopy(self.source)
        source["dependencies"][0]["canonical_sha256"] = policy_digest
        next(
            record
            for record in source["materials"]
            if record["path"].endswith("/adapter")
        )["value"] = "legacy"
        source_digest = write_document(self.source_path, source)
        with self.assertRaisesRegex(
            PREPARER["PreparationError"], "build policy"
        ):
            self.row(
                source_component_sha256=source_digest,
                policy_component_sha256=policy_digest,
            )

    def test_manifest_tamper_is_rejected_by_verifier(self):
        expected = VERIFY["component_row_contract"](
            "cp311", "3.11.16", "transition",
            self.source_path, self.source_digest,
            self.policy_path, self.policy_digest,
        )
        for field in ("source_component", "build_policy", "source", "patches"):
            with self.subTest(field=field):
                value = copy.deepcopy(expected)
                if isinstance(value[field], dict):
                    first = sorted(value[field])[0]
                    value[field][first] = "tampered"
                else:
                    value[field].append({"file": "x", "sha256": "0" * 64})
                path = self.directory / (field + ".json")
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(VERIFY["RowError"]):
                    VERIFY["verify_source_manifest"](path, expected)
        for value in (2.0, True):
            with self.subTest(schema_version=value):
                manifest = copy.deepcopy(expected)
                manifest["schema_version"] = value
                path = self.directory / ("schema-%s.json" % value)
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(VERIFY["RowError"]):
                    VERIFY["verify_source_manifest"](path, expected)

    def test_component_scripts_are_python36_syntax_compatible(self):
        for path in (PREPARER_PATH, VERIFY_PATH):
            with self.subTest(path=path.name):
                ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                    feature_version=(3, 6),
                )


if __name__ == "__main__":
    unittest.main()
