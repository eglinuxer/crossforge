import ast
import copy
import runpy
import sys
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "scripts"))
import abi_contract  # noqa: E402


AUDIT = runpy.run_path(str(REPOSITORY / "scripts/python_abi_audit.py"))
PythonAbiAuditError = AUDIT["PythonAbiAuditError"]
EXTERNAL = ["libexternal.so.1"]


def elf_record(
    identity,
    needed=(),
    versioned_exports=(),
    unversioned_exports=(),
    versioned_imports=(),
    unversioned_imports=(),
    executable=False,
):
    versioned_exports = sorted(
        (dict(item) for item in versioned_exports),
        key=lambda item: (item["name"], item["version"], item["default"]),
    )
    unversioned_exports = sorted(unversioned_exports)
    defaults = set(unversioned_exports)
    defaults.update(
        item["name"] for item in versioned_exports if item["default"]
    )
    return {
        "identity": identity,
        "soname": None if executable else identity,
        "needed": list(needed),
        "versioned_exports": versioned_exports,
        "unversioned_exports": unversioned_exports,
        "default_exports": sorted(defaults),
        "versioned_imports": sorted(
            (dict(item) for item in versioned_imports),
            key=lambda item: (
                item["provider"], item["name"], item["version"], item["binding"]
            ),
        ),
        "unversioned_imports": sorted(
            (dict(item) for item in unversioned_imports),
            key=lambda item: (item["name"], item["binding"]),
        ),
    }


def versioned_export(name, version, default=True):
    return {"name": name, "version": version, "default": default}


def versioned_import(provider, name, version, binding="GLOBAL"):
    return {
        "provider": provider,
        "name": name,
        "version": version,
        "binding": binding,
    }


def unversioned_import(name, binding="GLOBAL"):
    return {"name": name, "binding": binding}


class PythonAbiAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = abi_contract.load_baseline(
            REPOSITORY / "abi/el8/x86_64.json",
            "x86_64",
            "x86_64-unknown-linux-gnu",
        )

    def fixture(self):
        catalog = {
            soname: elf_record(soname)
            for soname in self.baseline["providers"]
        }
        catalog["libc.so.6"] = elf_record(
            "libc.so.6",
            versioned_exports=[
                versioned_export("puts", "GLIBC_2.2.5")
            ],
            unversioned_exports=["transitive_api"],
        )
        catalog["libexternal.so.1"] = elf_record(
            "libexternal.so.1",
            needed=["libc.so.6"],
            versioned_exports=[versioned_export("ext_api", "EXT_1")],
            unversioned_exports=["ext_plain"],
        )
        python = elf_record(
            "python-executable",
            needed=["libc.so.6"],
            unversioned_exports=["PyLong_FromLong"],
            executable=True,
        )
        artifact = elf_record(
            "lib-dynload/example.so",
            needed=["libexternal.so.1", "libc.so.6"],
            versioned_imports=[
                versioned_import("libc.so.6", "puts", "GLIBC_2.2.5"),
                versioned_import("libexternal.so.1", "ext_api", "EXT_1"),
            ],
            unversioned_imports=[
                unversioned_import("PyLong_FromLong"),
                unversioned_import("ext_plain"),
                unversioned_import("_ITM_registerTMCloneTable", "WEAK"),
            ],
            executable=True,
        )
        return catalog, python, artifact

    def audit(self, catalog, python, artifact):
        return AUDIT["audit_python_elf"](
            self.baseline,
            EXTERNAL,
            catalog,
            python,
            artifact,
        )

    def test_core_external_strong_and_optional_weak_are_explicit(self):
        catalog, python, artifact = self.fixture()
        result = self.audit(catalog, python, artifact)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["core_versioned"],
            [versioned_import("libc.so.6", "puts", "GLIBC_2.2.5")],
        )
        self.assertEqual(
            result["external_versioned"],
            [versioned_import("libexternal.so.1", "ext_api", "EXT_1")],
        )
        self.assertEqual(
            result["strong_unversioned"],
            [
                {"name": "PyLong_FromLong", "owner": "python-executable"},
                {"name": "ext_plain", "owner": "libexternal.so.1"},
            ],
        )
        self.assertEqual(
            result["optional_weak"],
            [
                {
                    "name": "_ITM_registerTMCloneTable",
                    "resolution": "optional-unresolved-weak",
                    "owners": [],
                }
            ],
        )

    def test_unknown_or_spoofed_versioned_provider_is_rejected(self):
        catalog, python, artifact = self.fixture()
        artifact["needed"].append("libunknown.so.1")
        artifact["versioned_imports"].append(
            versioned_import("libunknown.so.1", "bad", "VENDOR_1")
        )
        artifact["versioned_imports"].sort(
            key=lambda item: (
                item["provider"], item["name"], item["version"], item["binding"]
            )
        )
        with self.assertRaisesRegex(PythonAbiAuditError, "not owned"):
            self.audit(catalog, python, artifact)

        catalog, python, artifact = self.fixture()
        artifact["versioned_imports"][1]["name"] = "forged_api"
        with self.assertRaisesRegex(
            PythonAbiAuditError, "absent from the locked provider DSO"
        ):
            self.audit(catalog, python, artifact)

    def test_private_or_unfrozen_core_import_is_rejected(self):
        catalog, python, artifact = self.fixture()
        artifact["versioned_imports"][0]["version"] = "GLIBC_PRIVATE"
        catalog["libc.so.6"] = elf_record(
            "libc.so.6",
            versioned_exports=[versioned_export("puts", "GLIBC_PRIVATE")],
            unversioned_exports=["transitive_api"],
        )
        with self.assertRaisesRegex(PythonAbiAuditError, "private version node"):
            self.audit(catalog, python, artifact)

        catalog, python, artifact = self.fixture()
        artifact["versioned_imports"][0]["name"] = "new_api"
        catalog["libc.so.6"]["versioned_exports"].append(
            versioned_export("new_api", "GLIBC_2.2.5")
        )
        catalog["libc.so.6"]["versioned_exports"].sort(
            key=lambda item: (item["name"], item["version"], item["default"])
        )
        catalog["libc.so.6"]["default_exports"].append("new_api")
        catalog["libc.so.6"]["default_exports"].sort()
        with self.assertRaisesRegex(PythonAbiAuditError, "not in the frozen baseline"):
            self.audit(catalog, python, artifact)

    def test_nondefault_export_cannot_satisfy_an_unversioned_import(self):
        catalog, python, artifact = self.fixture()
        catalog["libexternal.so.1"] = elf_record(
            "libexternal.so.1",
            needed=["libc.so.6"],
            versioned_exports=[
                versioned_export("ext_api", "EXT_1"),
                versioned_export("legacy_only", "EXT_0", default=False),
            ],
            unversioned_exports=["ext_plain"],
        )
        artifact["unversioned_imports"].append(
            unversioned_import("legacy_only")
        )
        artifact["unversioned_imports"].sort(
            key=lambda item: (item["name"], item["binding"])
        )
        with self.assertRaisesRegex(PythonAbiAuditError, "has no owner"):
            self.audit(catalog, python, artifact)

    def test_strong_unversioned_import_requires_exactly_one_owner(self):
        catalog, python, artifact = self.fixture()
        artifact["unversioned_imports"].append(unversioned_import("missing"))
        artifact["unversioned_imports"].sort(
            key=lambda item: (item["name"], item["binding"])
        )
        with self.assertRaisesRegex(PythonAbiAuditError, "has no owner"):
            self.audit(catalog, python, artifact)

        catalog, python, artifact = self.fixture()
        python["unversioned_exports"].append("ext_plain")
        python["unversioned_exports"].sort()
        python["default_exports"].append("ext_plain")
        python["default_exports"].sort()
        with self.assertRaisesRegex(PythonAbiAuditError, "multiple owners"):
            self.audit(catalog, python, artifact)

    def test_transitive_closure_and_cycle_detection_are_strict(self):
        catalog, python, artifact = self.fixture()
        artifact["unversioned_imports"].append(
            unversioned_import("transitive_api")
        )
        artifact["unversioned_imports"].sort(
            key=lambda item: (item["name"], item["binding"])
        )
        result = self.audit(catalog, python, artifact)
        self.assertIn(
            {"name": "transitive_api", "owner": "libc.so.6"},
            result["strong_unversioned"],
        )

        catalog["libc.so.6"]["needed"] = ["libexternal.so.1"]
        with self.assertRaisesRegex(PythonAbiAuditError, "contains a cycle"):
            self.audit(catalog, python, artifact)

    def test_catalog_membership_and_dependencies_are_closed(self):
        catalog, python, artifact = self.fixture()
        del catalog["libexternal.so.1"]
        with self.assertRaisesRegex(PythonAbiAuditError, "membership differs"):
            self.audit(catalog, python, artifact)

        catalog, python, artifact = self.fixture()
        catalog["libexternal.so.1"]["needed"].append("libescape.so.1")
        with self.assertRaisesRegex(PythonAbiAuditError, "outside the owned universe"):
            self.audit(catalog, python, artifact)

    def test_readelf_parser_preserves_default_version_and_import_provider(self):
        dynamic_symbols = """
Symbol table '.dynsym' contains 7 entries:
   Num: Value Size Type Bind Vis Ndx Name
     1: 0000000000001000 8 FUNC GLOBAL DEFAULT 12 current@@EXT_2 (2)
     2: 0000000000001010 8 FUNC GLOBAL DEFAULT 12 legacy@EXT_1 (3)
     3: 0000000000001020 8 FUNC GLOBAL DEFAULT 12 plain
     4: 0000000000000000 0 FUNC GLOBAL DEFAULT UND need@EXT_2 (4)
     5: 0000000000000000 0 NOTYPE WEAK DEFAULT UND weak_hook
     6: 0000000000000000 0 OBJECT GLOBAL DEFAULT ABS EXT_2
"""
        version_info = """
Version needs section '.gnu.version_r' contains 1 entry:
  000000: Version: 1  File: libdependency.so.1  Cnt: 1
  0x0010:   Name: EXT_2  Flags: none  Version: 4
"""
        dynamic = """
 0x0000000000000001 (NEEDED) Shared library: [libdependency.so.1]
 0x000000000000000e (SONAME) Library soname: [libexternal.so.1]
"""
        record = AUDIT["elf_record_from_readelf"](
            "libexternal.so.1",
            dynamic_symbols,
            version_info,
            dynamic,
            expected_soname="libexternal.so.1",
        )
        self.assertEqual(
            record["versioned_exports"],
            [
                versioned_export("current", "EXT_2", default=True),
                versioned_export("legacy", "EXT_1", default=False),
            ],
        )
        self.assertEqual(record["unversioned_exports"], ["plain"])
        self.assertEqual(record["default_exports"], ["current", "plain"])
        self.assertEqual(
            record["versioned_imports"],
            [versioned_import("libdependency.so.1", "need", "EXT_2")],
        )
        self.assertEqual(
            record["unversioned_imports"],
            [unversioned_import("weak_hook", "WEAK")],
        )

    def test_record_cannot_forge_default_or_import_order(self):
        catalog, python, artifact = self.fixture()
        forged = copy.deepcopy(catalog["libexternal.so.1"])
        forged["default_exports"].append("not_defined")
        with self.assertRaisesRegex(PythonAbiAuditError, "default exports differ"):
            AUDIT["validate_elf_record"](forged, "libexternal.so.1")

        artifact["unversioned_imports"].reverse()
        with self.assertRaisesRegex(PythonAbiAuditError, "not sorted"):
            self.audit(catalog, python, artifact)

    def test_module_remains_python36_syntax_compatible(self):
        path = REPOSITORY / "scripts/python_abi_audit.py"
        ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
