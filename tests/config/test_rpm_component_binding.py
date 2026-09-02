import ast
import copy
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-rpm-lock.py"))
READER = runpy.run_path(str(REPOSITORY / "scripts/release_component.py"))
MATERIALIZER = runpy.run_path(str(REPOSITORY / "scripts/materialize-sysroot.py"))


CASES = {
    "rpm/host-build-common": "locks/host-build-common-el8-x86_64.json",
    "rpm/host-gcc-build": "locks/host-gcc-build-el8-x86_64.json",
    "rpm/host-python-build": "locks/host-python-build-el8-x86_64.json",
    "rpm/host-runtime": "locks/host-runtime-el8-x86_64.json",
    "rpm/sysroot-x86_64": "locks/sysroot-el8-x86_64.json",
    "rpm/sysroot-aarch64": "locks/sysroot-el8-aarch64.json",
}


class RPMComponentBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binding = VALIDATOR["load_json"](
            REPOSITORY / "config/generated/release-binding.json"
        )
        cls.digests = {
            record["component"]: record["canonical_sha256"]
            for record in binding["components"]
        }

    def component_path(self, name):
        return REPOSITORY / "config/generated/components" / (name + ".json")

    def validate(self, name, lock=None, path=None, digest=None):
        lock_path = REPOSITORY / (lock or CASES[name])
        document = VALIDATOR["load_json"](lock_path)
        return VALIDATOR["validate_lock_binding"](
            document,
            lock_path,
            release_component=path or self.component_path(name),
            release_component_name=name,
            release_component_sha256=digest or self.digests[name],
        )

    def write_component(self, directory, name, mutate):
        document = READER["load_json"](self.component_path(name))
        mutate(document)
        document["materials"].sort(key=lambda record: record["path"])
        path = Path(directory) / "component.json"
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path, READER["canonical_sha256"](document)

    def test_all_locked_rpm_roles_accept_their_exact_component(self):
        for name in CASES:
            with self.subTest(component=name):
                transaction, identity = self.validate(name)
                self.assertEqual(
                    identity,
                    {
                        "kind": "release-component",
                        "component": name,
                        "scope": "build",
                        "canonical_sha256": self.digests[name],
                    },
                )
                self.assertEqual(
                    transaction["identity"]["role"],
                    "target-sysroot"
                    if name.startswith("rpm/sysroot-")
                    else name[len("rpm/"):],
                )

    def test_wrong_expected_digest_name_role_and_arch_are_rejected(self):
        with self.assertRaises(VALIDATOR["ValidationError"]):
            self.validate("rpm/sysroot-x86_64", digest="0" * 64)
        with self.assertRaises(VALIDATOR["ValidationError"]):
            self.validate(
                "rpm/sysroot-x86_64",
                path=self.component_path("rpm/sysroot-aarch64"),
            )
        with self.assertRaises(VALIDATOR["ValidationError"]):
            self.validate(
                "rpm/host-build-common",
                lock=CASES["rpm/host-gcc-build"],
            )
        with self.assertRaises(VALIDATOR["ValidationError"]):
            self.validate(
                "rpm/sysroot-x86_64",
                lock=CASES["rpm/sysroot-aarch64"],
            )

    def test_material_tamper_duplicate_and_wrong_type_are_rejected(self):
        name = "rpm/sysroot-x86_64"
        with tempfile.TemporaryDirectory() as directory:
            path, _digest = self.write_component(
                directory,
                name,
                lambda document: document["materials"][0].__setitem__(
                    "value", "tampered"
                ),
            )
            with self.assertRaises(VALIDATOR["ValidationError"]):
                self.validate(name, path=path)

            def duplicate(document):
                document["materials"].append(copy.deepcopy(document["materials"][0]))

            path, digest = self.write_component(directory, name, duplicate)
            with self.assertRaises(VALIDATOR["ValidationError"]):
                self.validate(name, path=path, digest=digest)

            def wrong_type(document):
                next(
                    record
                    for record in document["materials"]
                    if record["path"].endswith("/arch")
                )["value"] = 7

            path, digest = self.write_component(directory, name, wrong_type)
            with self.assertRaises(VALIDATOR["ValidationError"]):
                self.validate(name, path=path, digest=digest)

    def test_lock_path_and_canonical_digest_materials_are_exact(self):
        name = "rpm/host-build-common"
        mutations = (
            ("/lock_file", "../escaped.json"),
            ("/canonical_sha256", "0" * 64),
            ("/status", "pending"),
        )
        for suffix, value in mutations:
            with self.subTest(suffix=suffix):
                with tempfile.TemporaryDirectory() as directory:
                    def mutate(document):
                        next(
                            record
                            for record in document["materials"]
                            if record["path"].endswith(suffix)
                            and "/host_locks/" in record["path"]
                        )["value"] = value

                    path, digest = self.write_component(directory, name, mutate)
                    with self.assertRaises(VALIDATOR["ValidationError"]):
                        self.validate(name, path=path, digest=digest)

    def test_target_prefix_is_unique_but_not_fixed_to_array_index(self):
        name = "rpm/sysroot-x86_64"
        with tempfile.TemporaryDirectory() as directory:
            def renumber(document):
                for record in document["materials"]:
                    record["path"] = record["path"].replace(
                        "/targets/0/", "/targets/9/"
                    )

            path, digest = self.write_component(directory, name, renumber)
            _transaction, identity = self.validate(name, path=path, digest=digest)
            self.assertEqual(identity["canonical_sha256"], digest)

            def add_other_prefix(document):
                records = [
                    copy.deepcopy(record)
                    for record in document["materials"]
                    if record["path"].startswith("/targets/0/")
                ]
                for record in records:
                    record["path"] = record["path"].replace(
                        "/targets/0/", "/targets/8/"
                    )
                document["materials"].extend(records)

            path, digest = self.write_component(directory, name, add_other_prefix)
            with self.assertRaises(VALIDATOR["ValidationError"]):
                self.validate(name, path=path, digest=digest)

    def test_duplicate_json_key_and_partial_component_cli_contract_fail(self):
        name = "rpm/sysroot-x86_64"
        lock_path = REPOSITORY / CASES[name]
        lock = VALIDATOR["load_json"](lock_path)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "component.json"
            path.write_text('{"kind":"x","kind":"y"}\n', encoding="utf-8")
            with self.assertRaises(VALIDATOR["ValidationError"]):
                VALIDATOR["validate_lock_binding"](
                    lock,
                    lock_path,
                    release_component=path,
                    release_component_name=name,
                    release_component_sha256="0" * 64,
                )
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_lock_binding"](
                lock,
                lock_path,
                release_component=self.component_path(name),
            )
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_lock_binding"](
                lock,
                lock_path,
                release_path=REPOSITORY / "config/release.json",
                release_component=self.component_path(name),
                release_component_name=name,
                release_component_sha256=self.digests[name],
            )

    def test_component_mode_never_reads_full_release_configuration(self):
        name = "rpm/sysroot-x86_64"
        function = VALIDATOR["validate_lock_binding"]
        globals_ = function.__globals__
        original = globals_["load_json"]

        def reject_release(path):
            if Path(path).name == "release.json":
                raise AssertionError("component mode read release.json")
            return original(path)

        globals_["load_json"] = reject_release
        try:
            _transaction, identity = self.validate(name)
        finally:
            globals_["load_json"] = original
        self.assertEqual(identity["component"], name)

    def test_component_mode_never_reopens_maintenance_plan(self):
        name = "rpm/sysroot-x86_64"
        function = VALIDATOR["validate_lock_binding"]
        globals_ = function.__globals__
        original = globals_["load_referenced_plan"]

        def forbidden_plan(_transaction):
            raise AssertionError("component mode loaded an RPM maintenance plan")

        globals_["load_referenced_plan"] = forbidden_plan
        try:
            _transaction, identity = self.validate(name)
        finally:
            globals_["load_referenced_plan"] = original
        self.assertEqual(identity["component"], name)

    def test_materializer_load_and_mutation_boundaries_carry_component_identity(self):
        name = "rpm/sysroot-x86_64"
        context = MATERIALIZER["load_lock"](
            REPOSITORY / CASES[name],
            release_component=self.component_path(name),
            release_component_name=name,
            release_component_sha256=self.digests[name],
        )
        self.assertEqual(context["release_binding"]["component"], name)
        self.assertEqual(
            context["release_binding"]["canonical_sha256"], self.digests[name]
        )

        invalid = copy.deepcopy(context)
        invalid["release_binding"]["canonical_sha256"] = "invalid"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            with self.assertRaises(MATERIALIZER["ValidationError"]):
                MATERIALIZER["fetch"](invalid, output, 1)
            self.assertFalse(output.exists())
            destination = Path(directory) / "sysroot"
            with self.assertRaises(MATERIALIZER["ValidationError"]):
                MATERIALIZER["install"](
                    invalid,
                    Path(directory) / "bundle",
                    Path(directory) / "key",
                    destination,
                )
            self.assertFalse(destination.exists())

    def test_component_binding_scripts_are_python36_syntax_compatible(self):
        for name in (
            "validate-rpm-lock.py",
            "materialize-sysroot.py",
            "install-host-rpm-lock.py",
        ):
            with self.subTest(script=name):
                path = REPOSITORY / "scripts" / name
                ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                    feature_version=(3, 6),
                )


if __name__ == "__main__":
    unittest.main()
