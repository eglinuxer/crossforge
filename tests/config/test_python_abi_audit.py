import ast
import copy
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
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
                versioned_export("malloc", "GLIBC_2.2.5"),
                versioned_export("puts", "GLIBC_2.2.5"),
            ],
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
            unversioned_import("malloc")
        )
        artifact["unversioned_imports"].sort(
            key=lambda item: (item["name"], item["binding"])
        )
        result = self.audit(catalog, python, artifact)
        self.assertIn(
            {"name": "malloc", "owner": "libc.so.6"},
            result["strong_unversioned"],
        )
        catalog["libc.so.6"]["needed"] = ["libexternal.so.1"]
        with self.assertRaisesRegex(PythonAbiAuditError, "contains a cycle"):
            self.audit(catalog, python, artifact)

    def test_private_default_core_export_cannot_own_unversioned_import(self):
        catalog, python, artifact = self.fixture()
        catalog["libc.so.6"]["versioned_exports"].append(
            versioned_export("__libc_dlopen_mode", "GLIBC_PRIVATE")
        )
        catalog["libc.so.6"]["versioned_exports"].sort(
            key=lambda item: (item["name"], item["version"], item["default"])
        )
        catalog["libc.so.6"]["default_exports"].append(
            "__libc_dlopen_mode"
        )
        catalog["libc.so.6"]["default_exports"].sort()
        artifact["unversioned_imports"].append(
            unversioned_import("__libc_dlopen_mode")
        )
        artifact["unversioned_imports"].sort(
            key=lambda item: (item["name"], item["binding"])
        )
        with self.assertRaisesRegex(PythonAbiAuditError, "has no owner"):
            self.audit(catalog, python, artifact)

    def test_catalog_membership_and_dependencies_are_closed(self):
        catalog, python, artifact = self.fixture()
        del catalog["libexternal.so.1"]
        with self.assertRaisesRegex(PythonAbiAuditError, "membership differs"):
            self.audit(catalog, python, artifact)

        catalog, python, artifact = self.fixture()
        artifact["soname"] = "libexternal.so.1"
        with self.assertRaisesRegex(PythonAbiAuditError, "must not define DT_SONAME"):
            self.audit(catalog, python, artifact)

        catalog, python, artifact = self.fixture()
        catalog["libexternal.so.1"]["needed"].append("libescape.so.1")
        with self.assertRaisesRegex(PythonAbiAuditError, "outside the owned universe"):
            self.audit(catalog, python, artifact)

    def test_pinned_provider_may_use_symbolic_but_artifact_may_not(self):
        dynamic = (
            " 0x1 (SONAME) Library soname: [libexternal.so.1]\n"
            " 0x2 (SYMBOLIC) 0x0\n"
        )
        self.assertEqual(
            AUDIT["parse_dynamic_identities"](
                dynamic, expected_soname="libexternal.so.1"
            ),
            ([], "libexternal.so.1"),
        )
        with self.assertRaisesRegex(
            PythonAbiAuditError, "load-affecting dynamic tags"
        ):
            AUDIT["parse_dynamic_identities"](dynamic)

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
     7: 0000000000002000 8 OBJECT GLOBAL DEFAULT 14 copied@EXT_2 (4)
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
            "0000 0000 R_X86_64_COPY 0000 copied@EXT_2 + 0\n",
            expected_soname="libexternal.so.1",
        )
        self.assertEqual(
            record["versioned_exports"],
            [
                versioned_export("copied", "EXT_2", default=False),
                versioned_export("current", "EXT_2", default=True),
                versioned_export("legacy", "EXT_1", default=False),
            ],
        )
        self.assertEqual(record["unversioned_exports"], ["plain"])
        self.assertEqual(record["default_exports"], ["current", "plain"])
        self.assertEqual(
            record["versioned_imports"],
            [
                versioned_import("libdependency.so.1", "copied", "EXT_2"),
                versioned_import("libdependency.so.1", "need", "EXT_2"),
            ],
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

    def test_report_revalidation_rejects_forged_ownership(self):
        catalog, python, artifact = self.fixture()
        result = self.audit(catalog, python, artifact)

        forged = copy.deepcopy(result)
        forged["core_versioned"][0]["name"] = "not_frozen"
        with self.assertRaisesRegex(PythonAbiAuditError, "frozen baseline"):
            AUDIT["validate_audit_result"](
                forged, self.baseline, EXTERNAL, python["identity"]
            )

        forged = copy.deepcopy(result)
        forged["strong_unversioned"][0]["owner"] = "libunknown.so.1"
        with self.assertRaisesRegex(PythonAbiAuditError, "outside the loader scope"):
            AUDIT["validate_audit_result"](
                forged, self.baseline, EXTERNAL, python["identity"]
            )

        forged = copy.deepcopy(result)
        forged["optional_weak"][0]["resolution"] = "resolved"
        with self.assertRaisesRegex(PythonAbiAuditError, "differs from its owners"):
            AUDIT["validate_audit_result"](
                forged, self.baseline, EXTERNAL, python["identity"]
            )

    def test_module_remains_python36_syntax_compatible(self):
        path = REPOSITORY / "scripts/python_abi_audit.py"
        ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
            feature_version=(3, 6),
        )

    @unittest.skipUnless(
        shutil.which("gcc") and shutil.which("readelf"),
        "host GCC/readelf are required",
    )
    def test_real_dt_audit_binary_is_rejected_by_both_parsers(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "audit.c"
            library = directory / "libaudit-canary.so"
            source.write_text("void crossforge_audit_canary(void) {}\n", encoding="utf-8")
            subprocess.run(
                [
                    "gcc",
                    "-shared",
                    "-fPIC",
                    "-nostdlib",
                    "-Wl,-z,relro,-z,now",
                    "-Wl,--audit,/tmp/libevil.so",
                    str(source),
                    "-o",
                    str(library),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            dynamic = subprocess.run(
                ["readelf", "--wide", "-d", str(library)],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
        self.assertIn("(AUDIT)", dynamic)
        for function in (
            abi_contract._dynamic_properties,
            AUDIT["parse_dynamic_identities"],
        ):
            with self.subTest(function=function.__name__):
                with self.assertRaisesRegex(
                    (abi_contract.AbiContractError, PythonAbiAuditError),
                    "load-affecting dynamic tags",
                ):
                    function(dynamic)

    @unittest.skipUnless(
        shutil.which("gcc") and shutil.which("readelf"),
        "host GCC/readelf are required",
    )
    def test_real_copy_relocation_is_recorded_as_a_versioned_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "copy.c"
            executable = directory / "copy-canary"
            source.write_text(
                "extern char **environ; int main(void) { return environ == 0; }\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["gcc", "-no-pie", str(source), "-o", str(executable)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            evidence = AUDIT["readelf_evidence"]("readelf", executable)
            self.assertRegex(
                evidence["relocations"], r"R_X86_64_COPY|R_AARCH64_COPY"
            )
            record = AUDIT["elf_record_from_readelf"](
                "copy-canary",
                evidence["dynamic_symbols"],
                evidence["version_info"],
                evidence["dynamic_section"],
                evidence["relocations"],
            )
        copy_names = {
            match.group(1).split("@", 1)[0]
            for match in re.finditer(
                r"\bR_(?:X86_64|AARCH64)_COPY\b\s+\S+\s+(\S+)",
                evidence["relocations"],
            )
        }
        imported = {
            item["name"] for item in record["versioned_imports"]
        }
        self.assertTrue(copy_names)
        self.assertTrue(copy_names.issubset(imported))


if __name__ == "__main__":
    unittest.main()
