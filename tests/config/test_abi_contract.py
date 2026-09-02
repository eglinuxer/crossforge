import ast
import copy
import json
import re
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
ABI = runpy.run_path(str(REPOSITORY / "scripts/abi_contract.py"))
AbiContractError = ABI["AbiContractError"]
PROVIDER_MANIFEST = json.loads(
    (REPOSITORY / "config/abi-providers.json").read_text(encoding="utf-8")
)
PROVIDER_MANIFEST_SHA256 = ABI["canonical_sha256"](PROVIDER_MANIFEST)


def abi_export(name, version):
    return {"name": name, "version": version}


def profile(interpreter, bind_now):
    return {
        "textrel": "forbid",
        "relr": "forbid",
        "rpath": "forbid",
        "runpath": "forbid",
        "gnu_stack": "require-non-executable",
        "writable_executable_segments": "forbid",
        "interpreter": {"decision": "require", "expected": interpreter},
        "relro": "require",
        "bind_now": bind_now,
    }


def inventory_provider(soname, path, exports):
    return {
        "path": "/" + path,
        "soname": soname,
        "sha256": "1" * 64,
        "exports": copy.deepcopy(exports),
        "unversioned_exports": ["provider_unversioned"],
        "nonpublic_versioned_exports": [],
    }


def inventory(source_kind="clean-rocky-oci", arch="x86_64"):
    triple = ABI["TARGETS"][arch]["triple"]
    providers = {}
    for soname, path in ABI["EXPECTED_PROVIDERS"][arch]:
        symbol = "symbol_" + soname.replace(".", "_").replace("+", "x").replace("-", "_")
        exports = [abi_export(symbol, "GLIBC_2.2.5")]
        if soname == "libc.so.6":
            exports = [
                abi_export("clock_gettime", "GLIBC_2.17"),
                abi_export("memcpy", "GLIBC_2.14"),
                abi_export("puts", "GLIBC_2.2.5"),
            ]
        elif soname == "libm.so.6":
            exports = [abi_export("cos", "GLIBC_2.2.5")]
        elif soname == "libstdc++.so.6":
            exports = [
                abi_export("debug_message", "GLIBCXX_DEBUG_MESSAGE_LENGTH")
            ]
        providers[soname] = inventory_provider(soname, path, exports)
    return {
        "$schema": "https://crossforge.dev/schemas/abi-inventory.schema.json",
        "schema_version": 1,
        "kind": "crossforge-abi-inventory",
        "target": {
            "arch": arch,
            "triple": triple,
        },
        "source": {
            "kind": source_kind,
            "identity_sha256": "2" * 64,
            "provider_manifest_sha256": PROVIDER_MANIFEST_SHA256,
        },
        "providers": providers,
    }


def baseline(clean_inventory=None):
    if clean_inventory is None:
        clean_inventory = inventory()
    providers = {
        soname: copy.deepcopy(details["exports"])
        for soname, details in clean_inventory["providers"].items()
    }
    return {
        "$schema": "https://crossforge.dev/schemas/abi-baseline.schema.json",
        "schema_version": 1,
        "kind": "crossforge-abi-baseline",
        "baseline": "el8",
        "target": {
            "arch": "x86_64",
            "triple": "x86_64-unknown-linux-gnu",
        },
        "review": {
            "status": "reviewed",
            "source_inventory": "evidence/abi/el8-x86_64-clean.json",
            "source_inventory_sha256": ABI["canonical_sha256"](clean_inventory),
        },
        "providers": providers,
        "elf_policy": {
            "profiles": {
                "compiler-default-observation": profile(
                    "/lib64/ld-linux-x86-64.so.2", "require-absent"
                ),
                "crossforge-qualified-v1": profile(
                    "/lib64/ld-linux-x86-64.so.2", "require"
                ),
            },
            "artifact_exceptions": [
                {
                    "artifact": "toolchain/catch",
                    "profile": "crossforge-qualified-v1",
                    "exceptions": [
                        {
                            "check": "runpath",
                            "allowed_values": ["$ORIGIN"],
                            "reason": "test-only cross-DSO lookup",
                        }
                    ],
                },
                {
                    "artifact": "toolchain/compiler-default-canary",
                    "profile": "compiler-default-observation",
                    "exceptions": [],
                },
            ],
        },
    }


DYNSYMS = """
Symbol table '.dynsym' contains 4 entries:
   Num:    Value          Size Type    Bind   Vis      Ndx Name
     1: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND puts@GLIBC_2.2.5 (2)
     2: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND cos@GLIBC_2.2.5 (3)
     3: 0000000000000000     0 NOTYPE  WEAK   DEFAULT  UND optional_hook
"""

VERSION_INFO = """
Version needs section '.gnu.version_r' contains 2 entries:
 Addr: 0x0000000000000520  Offset: 0x000520  Link: 7 (.dynstr)
  000000: Version: 1  File: libc.so.6  Cnt: 1
  0x0010:   Name: GLIBC_2.2.5  Flags: none  Version: 2
  0x0020: Version: 1  File: libm.so.6  Cnt: 1
  0x0030:   Name: GLIBC_2.2.5  Flags: none  Version: 3
"""

ELF_HEADER_DYN = """
ELF Header:
  Class:                             ELF64
  Data:                              2's complement, little endian
  Type:                              DYN (Position-Independent Executable file)
  Machine:                           Advanced Micro Devices X86-64
"""

PROGRAM_HEADERS = """
Elf file type is DYN
Program Headers:
  Type           Offset   VirtAddr           PhysAddr           FileSiz  MemSiz   Flg Align
  INTERP         0x000238 0x0000000000000238 0x0000000000000238 0x00001c 0x00001c R   0x1
      [Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]
  LOAD           0x001000 0x0000000000001000 0x0000000000001000 0x000100 0x000100 R E 0x1000
  GNU_STACK      0x000000 0x0000000000000000 0x0000000000000000 0x000000 0x000000 RW  0x10
  GNU_RELRO      0x002000 0x0000000000003000 0x0000000000003000 0x000100 0x000100 R   0x1
"""

DYNAMIC_NOW = """
Dynamic section at offset 0x2000 contains 2 entries:
 0x000000000000001e (FLAGS)              BIND_NOW
 0x000000006ffffffb (FLAGS_1)            Flags: NOW PIE
"""


class AbiDocumentTests(unittest.TestCase):
    def test_valid_documents_and_canonical_review_binding(self):
        clean = inventory()
        reviewed = baseline(clean)
        ABI["validate_inventory"](clean, "x86_64", "x86_64-unknown-linux-gnu")
        ABI["validate_baseline"](reviewed, "x86_64", "x86_64-unknown-linux-gnu")
        self.assertEqual(
            ABI["validate_baseline_against_inventory"](reviewed, clean),
            {
                "missing_providers": [],
                "extra_providers": [],
                "missing_exports": {},
                "extra_exports": {},
            },
        )

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "abi.json"
            for text, pattern in (
                ('{"kind":"a","kind":"b"}', "duplicate JSON key"),
                ('{"value":NaN}', "non-finite JSON number"),
                ('{"value":1e9999}', "non-finite JSON number"),
            ):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(AbiContractError, pattern):
                        ABI["load_json"](path)

    def test_architecture_and_triple_cannot_be_swapped(self):
        document = inventory()
        document["target"]["arch"] = "aarch64"
        with self.assertRaisesRegex(AbiContractError, "architecture and triple"):
            ABI["validate_inventory"](document)

        document = inventory()
        with self.assertRaisesRegex(AbiContractError, "differs from expected"):
            ABI["validate_inventory"](document, "aarch64")

    def test_review_source_path_digest_and_root_provenance_are_mandatory(self):
        clean = inventory()
        reviewed = baseline(clean)
        reviewed["review"]["source_inventory"] = "evidence/abi/other.json"
        with self.assertRaisesRegex(AbiContractError, "path differs"):
            ABI["validate_baseline"](reviewed)

        reviewed = baseline(clean)
        forged = copy.deepcopy(clean)
        forged["source"]["identity_sha256"] = "3" * 64
        with self.assertRaisesRegex(AbiContractError, "review digest differs"):
            ABI["validate_baseline_against_inventory"](reviewed, forged)

        missing_source = inventory()
        del missing_source["source"]
        with self.assertRaisesRegex(AbiContractError, "fields differ"):
            ABI["validate_inventory"](missing_source)

    def test_exports_and_policy_overrides_must_be_sorted_and_exact(self):
        document = baseline()
        document["providers"]["libc.so.6"].reverse()
        with self.assertRaisesRegex(AbiContractError, "not sorted"):
            ABI["validate_baseline"](document)

        for mutate in (
            lambda value: value["elf_policy"]["artifact_exceptions"].reverse(),
            lambda value: value["elf_policy"]["artifact_exceptions"][0]["exceptions"][0].update(
                {"check": "rpath"}
            ),
            lambda value: value["elf_policy"]["artifact_exceptions"][0]["exceptions"][0]["allowed_values"].append(
                "/tmp"
            ),
            lambda value: value["elf_policy"]["artifact_exceptions"][1]["exceptions"].append(
                {"check": "textrel", "reason": "forged"}
            ),
        ):
            document = baseline()
            mutate(document)
            with self.assertRaisesRegex(AbiContractError, "two fixed v1 records"):
                ABI["validate_baseline"](document)

        candidate = inventory()
        candidate["providers"]["libc.so.6"]["unversioned_exports"].extend(
            ["z", "a"]
        )
        with self.assertRaisesRegex(AbiContractError, "not sorted"):
            ABI["validate_inventory"](candidate)

    def test_private_unknown_and_glibc_abi_nodes_cannot_enter_baseline(self):
        for version, classification in (
            ("GLIBC_PRIVATE", "private"),
            ("GLIBC_ABI_DT_RELR", "abi-internal"),
            ("OPENSSL_3.0.0", "unknown-namespace"),
        ):
            with self.subTest(version=version):
                document = baseline()
                document["providers"]["libc.so.6"][0]["version"] = version
                with self.assertRaisesRegex(AbiContractError, classification):
                    ABI["validate_baseline"](document)

    def test_nonnumeric_public_nodes_are_preserved(self):
        document = baseline()
        ABI["validate_baseline"](document)
        self.assertEqual(
            document["providers"]["libstdc++.so.6"],
            [abi_export("debug_message", "GLIBCXX_DEBUG_MESSAGE_LENGTH")],
        )

    def test_xcrypt_is_public_only_for_the_libcrypt_provider(self):
        document = baseline()
        document["providers"]["libcrypt.so.1"] = [
            abi_export("crypt_r", "XCRYPT_2.0")
        ]
        ABI["validate_baseline"](document)

        document = baseline()
        document["providers"]["libc.so.6"][0]["version"] = "XCRYPT_2.0"
        with self.assertRaisesRegex(AbiContractError, "unknown-namespace"):
            ABI["validate_baseline"](document)

    def test_profiles_are_fixed_and_no_policy_is_silently_selected(self):
        document = baseline()
        del document["elf_policy"]["profiles"]["compiler-default-observation"]
        with self.assertRaisesRegex(AbiContractError, "both fixed profiles"):
            ABI["validate_baseline"](document)

        document = baseline()
        document["elf_policy"]["profiles"]["compiler-default-observation"][
            "bind_now"
        ] = "require"
        with self.assertRaisesRegex(AbiContractError, "must set bind_now"):
            ABI["validate_baseline"](document)


class AbiInventoryComparisonTests(unittest.TestCase):
    def test_same_version_new_symbol_is_explicit_extra_not_approved(self):
        clean = inventory()
        reviewed = baseline(clean)
        sysroot = inventory("locked-sysroot")
        sysroot["providers"]["libc.so.6"]["exports"].insert(
            2, abi_export("new_api", "GLIBC_2.2.5")
        )
        difference = ABI["validate_inventory_superset"](sysroot, reviewed)
        self.assertEqual(
            difference["extra_exports"],
            {"libc.so.6": [abi_export("new_api", "GLIBC_2.2.5")]},
        )
        clean_with_extra = copy.deepcopy(sysroot)
        clean_with_extra["source"]["kind"] = "clean-rocky-oci"
        reviewed["review"]["source_inventory_sha256"] = ABI["canonical_sha256"](
            clean_with_extra
        )
        with self.assertRaisesRegex(AbiContractError, "unreviewed public export"):
            ABI["validate_baseline_against_inventory"](
                reviewed, clean_with_extra, require_exact=True
            )

    def test_missing_baseline_symbol_is_never_a_superset(self):
        reviewed = baseline()
        sysroot = inventory("locked-sysroot")
        sysroot["providers"]["libc.so.6"]["exports"].pop()
        with self.assertRaisesRegex(AbiContractError, "missing a baseline export"):
            ABI["validate_inventory_superset"](sysroot, reviewed)

    def test_cross_arch_inventory_cannot_validate_a_baseline(self):
        reviewed = baseline()
        sysroot = inventory("locked-sysroot", arch="aarch64")
        with self.assertRaisesRegex(AbiContractError, "targets differ"):
            ABI["validate_inventory_superset"](sysroot, reviewed)


class AbiProviderManifestTests(unittest.TestCase):
    def test_fixed_manifest_has_complete_non_nss_provider_sets(self):
        ABI["validate_provider_manifest"](PROVIDER_MANIFEST)
        targets = {
            target["arch"]: target for target in PROVIDER_MANIFEST["targets"]
        }
        self.assertEqual(len(targets["x86_64"]["providers"]), 15)
        self.assertEqual(len(targets["aarch64"]["providers"]), 14)
        self.assertFalse(
            any(
                provider["soname"].startswith("libnss_")
                for target in targets.values()
                for provider in target["providers"]
            )
        )

    def test_manifest_rejects_omissions_nss_and_unsafe_paths(self):
        mutations = []
        omitted = copy.deepcopy(PROVIDER_MANIFEST)
        omitted["targets"][1]["providers"].pop()
        mutations.append((omitted, "fixed public provider set"))
        nss = copy.deepcopy(PROVIDER_MANIFEST)
        nss["targets"][1]["providers"].append(
            {"soname": "libnss_files.so.2", "path": "usr/lib64/libnss_files.so.2"}
        )
        mutations.append((nss, "fixed public provider set"))
        escaped = copy.deepcopy(PROVIDER_MANIFEST)
        escaped["targets"][1]["providers"][0]["path"] = "usr/lib64/../escape.so"
        mutations.append((escaped, "canonical safe relative path"))
        doubled = copy.deepcopy(PROVIDER_MANIFEST)
        doubled["targets"][1]["providers"][0]["path"] = "usr//lib64/loader.so"
        mutations.append((doubled, "empty path component"))
        for document, pattern in mutations:
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(AbiContractError, pattern):
                    ABI["validate_provider_manifest"](document)

    def test_inventory_is_bound_to_manifest_canonical_sha(self):
        document = inventory()
        ABI["validate_inventory_provider_manifest"](
            document, PROVIDER_MANIFEST
        )
        document["source"]["provider_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(AbiContractError, "manifest SHA256 differs"):
            ABI["validate_inventory_provider_manifest"](
                document, PROVIDER_MANIFEST
            )

        missing = inventory()
        missing["providers"].pop("libc.so.6")
        replaced_path = inventory()
        replaced_path["providers"]["libc.so.6"]["path"] = "/usr/lib64/not-libc.so.6"
        extra = inventory()
        extra["providers"]["libfake.so.1"] = inventory_provider(
            "libfake.so.1",
            "usr/lib64/libfake.so.1",
            [abi_export("fake", "GLIBC_2.2.5")],
        )
        for changed in (missing, replaced_path, extra):
            with self.subTest(providers=sorted(changed["providers"])):
                with self.assertRaisesRegex(
                    AbiContractError,
                    "providers differ from the fixed provider manifest",
                ):
                    ABI["validate_inventory_provider_manifest"](
                        changed, PROVIDER_MANIFEST
                    )


class AbiReadelfTests(unittest.TestCase):
    def test_version_index_not_version_text_selects_provider(self):
        result = ABI["audit_symbol_requirements"](
            baseline(), DYNSYMS, VERSION_INFO
        )
        self.assertEqual(
            result["versioned_imports"],
            [
                {
                    "provider": "libc.so.6",
                    "name": "puts",
                    "version": "GLIBC_2.2.5",
                },
                {
                    "provider": "libm.so.6",
                    "name": "cos",
                    "version": "GLIBC_2.2.5",
                },
            ],
        )
        self.assertEqual(
            result["unversioned_imports"],
            {
                "disposition": "recorded-not-allowlisted",
                "symbols": ["optional_hook"],
            },
        )

    def test_same_version_new_import_is_rejected_by_exact_symbol_name(self):
        changed = DYNSYMS.replace("puts@GLIBC_2.2.5", "new_api@GLIBC_2.2.5")
        with self.assertRaisesRegex(AbiContractError, "not allowlisted"):
            ABI["audit_symbol_requirements"](baseline(), changed, VERSION_INFO)

    def test_private_unknown_and_internal_imports_are_rejected(self):
        for version in ("GLIBC_PRIVATE", "GLIBC_ABI_DT_RELR", "VENDOR_1"):
            symbols = DYNSYMS.replace("puts@GLIBC_2.2.5", "puts@" + version)
            needs = VERSION_INFO.replace(
                "Name: GLIBC_2.2.5  Flags: none  Version: 2",
                "Name: %s  Flags: none  Version: 2" % version,
                1,
            )
            with self.subTest(version=version):
                with self.assertRaisesRegex(AbiContractError, "version node"):
                    ABI["audit_symbol_requirements"](baseline(), symbols, needs)

    def test_unique_binding_is_a_public_provider_export(self):
        dynamic_symbols = """
Symbol table '.dynsym' contains 3 entries:
   Num: Value Size Type Bind Vis Ndx Name
     1: 0000000000001000 8 OBJECT UNIQUE DEFAULT 12 unique_object@@GLIBCXX_3.4 (2)
     2: 0000000000001010 8 OBJECT GLOBAL DEFAULT 12 private_object@@GLIBC_PRIVATE (3)
"""
        record = ABI["provider_inventory_from_readelf"](
            "/usr/lib64/libstdc++.so.6",
            "libstdc++.so.6",
            "a" * 64,
            dynamic_symbols,
        )
        self.assertEqual(
            record["exports"],
            [abi_export("unique_object", "GLIBCXX_3.4")],
        )
        self.assertEqual(
            record["nonpublic_versioned_exports"],
            [
                {
                    "name": "private_object",
                    "version": "GLIBC_PRIVATE",
                    "classification": "private",
                }
            ],
        )

    def test_xcrypt_export_and_import_are_bound_to_libcrypt(self):
        dynamic_symbols = """
Symbol table '.dynsym' contains 2 entries:
   Num: Value Size Type Bind Vis Ndx Name
     1: 0000000000001000 8 FUNC GLOBAL DEFAULT 12 crypt_r@@XCRYPT_2.0 (2)
"""
        record = ABI["provider_inventory_from_readelf"](
            "/usr/lib64/libcrypt.so.1",
            "libcrypt.so.1",
            "a" * 64,
            dynamic_symbols,
        )
        self.assertEqual(
            record["exports"],
            [abi_export("crypt_r", "XCRYPT_2.0")],
        )

        wrong_provider = ABI["provider_inventory_from_readelf"](
            "/usr/lib64/libc.so.6",
            "libc.so.6",
            "b" * 64,
            dynamic_symbols,
        )
        self.assertEqual(wrong_provider["exports"], [])
        self.assertEqual(
            wrong_provider["nonpublic_versioned_exports"],
            [
                {
                    "name": "crypt_r",
                    "version": "XCRYPT_2.0",
                    "classification": "unknown-namespace",
                }
            ],
        )

        reviewed = baseline()
        reviewed["providers"]["libcrypt.so.1"] = [
            abi_export("crypt_r", "XCRYPT_2.0")
        ]
        imports = """
Symbol table '.dynsym' contains 2 entries:
   Num: Value Size Type Bind Vis Ndx Name
     1: 0000000000000000 0 FUNC GLOBAL DEFAULT UND crypt_r@XCRYPT_2.0 (2)
"""
        needs = """
Version needs section '.gnu.version_r' contains 1 entry:
  000000: Version: 1  File: libcrypt.so.1  Cnt: 1
  0x0010:   Name: XCRYPT_2.0  Flags: none  Version: 2
"""
        self.assertEqual(
            ABI["audit_symbol_requirements"](reviewed, imports, needs)[
                "versioned_imports"
            ],
            [
                {
                    "provider": "libcrypt.so.1",
                    "name": "crypt_r",
                    "version": "XCRYPT_2.0",
                }
            ],
        )
        wrong_needs = needs.replace("File: libcrypt.so.1", "File: libc.so.6")
        with self.assertRaisesRegex(AbiContractError, "unknown-namespace"):
            ABI["audit_symbol_requirements"](reviewed, imports, wrong_needs)

    def test_qualified_profile_and_exact_catch_runpath(self):
        ordinary = ABI["audit_elf_policy"](
            baseline(),
            "python/cp314/bin/python3.14",
            DYNAMIC_NOW,
            PROGRAM_HEADERS,
            ELF_HEADER_DYN,
        )
        self.assertEqual(ordinary["profile"], "crossforge-qualified-v1")
        self.assertEqual(ordinary["used_exceptions"], [])

        catch_dynamic = DYNAMIC_NOW + "\n 0x000000000000001d (RUNPATH) Library runpath: [$ORIGIN]\n"
        catch = ABI["audit_elf_policy"](
            baseline(),
            "toolchain/catch",
            catch_dynamic,
            PROGRAM_HEADERS,
            ELF_HEADER_DYN,
        )
        self.assertEqual(catch["used_exceptions"], ["runpath"])
        with self.assertRaisesRegex(AbiContractError, "exact exception"):
            ABI["audit_elf_policy"](
                baseline(),
                "toolchain/catch",
                catch_dynamic.replace("$ORIGIN", "$ORIGIN:/tmp"),
                PROGRAM_HEADERS,
                ELF_HEADER_DYN,
            )

    def test_dynamic_search_paths_preserve_order_and_reject_ambiguity(self):
        first = ABI["_dynamic_properties"](
            " 0x1 (RUNPATH) Library runpath: [/first:/second]\n"
        )
        second = ABI["_dynamic_properties"](
            " 0x1 (RUNPATH) Library runpath: [/second:/first]\n"
        )
        self.assertEqual(first["runpath"], ["/first", "/second"])
        self.assertEqual(
            first["path_tags"],
            [
                {
                    "tag": "RUNPATH",
                    "components": ["/first", "/second"],
                }
            ],
        )
        self.assertNotEqual(first["runpath"], second["runpath"])
        for value, pattern in (
            ("$ORIGIN:", "empty path component"),
            ("$ORIGIN:$ORIGIN", "duplicate path component"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(AbiContractError, pattern):
                    ABI["_dynamic_properties"](
                        " 0x1 (RUNPATH) Library runpath: [%s]\n" % value
                    )
        with self.assertRaisesRegex(AbiContractError, "multiple RUNPATH tags"):
            ABI["_dynamic_properties"](
                " 0x1 (RUNPATH) Library runpath: [$ORIGIN]\n"
                " 0x2 (RUNPATH) Library runpath: [/tmp]\n"
            )

    def test_loader_injection_and_symbolic_tags_are_always_forbidden(self):
        for dynamic in (
            " 0x1 (AUDIT) Audit library: [/tmp/libevil.so]\n",
            " 0x1 (DEPAUDIT) Dependency audit library: [libevil.so]\n",
            " 0x1 (FILTER) Filter library: [libevil.so]\n",
            " 0x1 (AUXILIARY) Auxiliary library: [libevil.so]\n",
            " 0x1 (SYMBOLIC) 0x0\n",
            " 0x1 (FLAGS) SYMBOLIC BIND_NOW\n",
        ):
            with self.subTest(dynamic=dynamic):
                with self.assertRaisesRegex(
                    AbiContractError, "load-affecting dynamic tags"
                ):
                    ABI["_dynamic_properties"](dynamic)

    def test_elf_class_and_data_encoding_are_target_bound(self):
        for changed in (
            ELF_HEADER_DYN.replace("ELF64", "ELF32"),
            ELF_HEADER_DYN.replace("little endian", "big endian"),
        ):
            with self.subTest(header=changed):
                with self.assertRaisesRegex(
                    AbiContractError, "class or data encoding"
                ):
                    ABI["audit_elf_policy"](
                        baseline(),
                        "python/cp314/bin/python3.14",
                        DYNAMIC_NOW,
                        PROGRAM_HEADERS,
                        changed,
                    )

    def test_shared_objects_reject_the_pie_dynamic_flag(self):
        shared_headers = PROGRAM_HEADERS.replace(
            "  INTERP         0x000238 0x0000000000000238 0x0000000000000238 0x00001c 0x00001c R   0x1\n"
            "      [Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]\n",
            "",
        )
        dynamic = DYNAMIC_NOW + "\n 0x1 (FLAGS_1) Flags: NOW PIE\n"
        self.assertTrue(ABI["_dynamic_properties"](dynamic)["pie"])
        with self.assertRaisesRegex(AbiContractError, "PIE dynamic flag"):
            ABI["audit_elf_policy"](
                baseline(),
                "lib/python3.14/lib-dynload/_ssl.so",
                dynamic,
                shared_headers,
                ELF_HEADER_DYN,
            )

    def test_compiler_default_observes_now_absent(self):
        result = ABI["audit_elf_policy"](
            baseline(),
            "toolchain/compiler-default-canary",
            "Dynamic section at offset 0x2000 contains 1 entry:\n",
            PROGRAM_HEADERS,
            ELF_HEADER_DYN,
            profile_name="compiler-default-observation",
        )
        self.assertFalse(result["properties"]["bind_now"])
        with self.assertRaisesRegex(AbiContractError, "bind_now decision differs"):
            ABI["audit_elf_policy"](
                baseline(),
                "toolchain/compiler-default-canary",
                DYNAMIC_NOW,
                PROGRAM_HEADERS,
                ELF_HEADER_DYN,
                profile_name="compiler-default-observation",
            )
        with self.assertRaisesRegex(AbiContractError, "exact artifact override"):
            ABI["audit_elf_policy"](
                baseline(),
                "python/cp39/bin/python3.9",
                "Dynamic section at offset 0x2000 contains 1 entry:\n",
                PROGRAM_HEADERS,
                ELF_HEADER_DYN,
                profile_name="compiler-default-observation",
            )

    def test_rel_and_static_mark_dynamic_hardening_not_applicable(self):
        rel = ABI["audit_elf_policy"](
            baseline(),
            "objects/helper.o",
            "There is no dynamic section in this file.\n",
            "There are no program headers in this file.\n",
            "  Class: ELF64\n  Data: 2's complement, little endian\n"
            "  Type: REL (Relocatable file)\n  Machine: Advanced Micro Devices X86-64\n",
        )
        self.assertEqual(
            rel["not_applicable"],
            ["bind_now", "gnu_stack", "interpreter", "relro"],
        )

        static_headers = PROGRAM_HEADERS.replace(
            "  INTERP         0x000238 0x0000000000000238 0x0000000000000238 0x00001c 0x00001c R   0x1\n"
            "      [Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]\n",
            "",
        ).replace("  GNU_RELRO", "  NOTE     ")
        static = ABI["audit_elf_policy"](
            baseline(),
            "bin/static-probe",
            "There is no dynamic section in this file.\n",
            static_headers,
            "  Class: ELF64\n  Data: 2's complement, little endian\n"
            "  Type: EXEC (Executable file)\n  Machine: Advanced Micro Devices X86-64\n",
        )
        self.assertEqual(
            static["not_applicable"], ["bind_now", "interpreter", "relro"]
        )

    def test_textrel_wx_wrong_machine_and_missing_now_are_rejected(self):
        mutations = (
            (DYNAMIC_NOW + "\n 0x0 (TEXTREL) 0x0\n", PROGRAM_HEADERS, ELF_HEADER_DYN, "textrel"),
            (
                DYNAMIC_NOW,
                PROGRAM_HEADERS.replace("R E 0x1000", "RWE 0x1000"),
                ELF_HEADER_DYN,
                "writable_executable_segments",
            ),
            (
                DYNAMIC_NOW,
                PROGRAM_HEADERS,
                ELF_HEADER_DYN.replace("Advanced Micro Devices X86-64", "AArch64"),
                "machine differs",
            ),
            (
                "Dynamic section at offset 0x2000 contains 1 entry:\n",
                PROGRAM_HEADERS,
                ELF_HEADER_DYN,
                "bind_now decision differs",
            ),
        )
        for dynamic, headers, elf_header, pattern in mutations:
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(AbiContractError, pattern):
                    ABI["audit_elf_policy"](
                        baseline(),
                        "python/cp314/bin/python3.14",
                        dynamic,
                        headers,
                        elf_header,
                    )


class AbiExportBakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hcl = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        matches = re.findall(
            r'^target "abi-export"\s*\{\n(?P<body>.*?)^\}\n',
            hcl,
            re.MULTILINE | re.DOTALL,
        )
        if len(matches) != 1:
            raise AssertionError("docker-bake.hcl must define abi-export exactly once")
        cls.body = matches[0]

    def test_target_is_fixed_cache_only_and_uses_only_build_targets(self):
        self.assertEqual(
            re.findall(r'^\s*context\s*=\s*"([^"]+)"$', self.body, re.MULTILINE),
            ["."],
        )
        self.assertEqual(
            re.findall(r"^\s*platforms\s*=\s*(\[[^\n]+\])$", self.body, re.MULTILINE),
            ['["linux/amd64"]'],
        )
        self.assertEqual(
            re.findall(r"^\s*output\s*=\s*(\[[^\n]+\])$", self.body, re.MULTILINE),
            ['["type=cacheonly"]'],
        )
        contexts_match = re.search(
            r"^\s*contexts\s*=\s*\{\n(?P<contexts>.*?)^\s*\}\n",
            self.body,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(contexts_match)
        context_pairs = re.findall(
            r'^\s*([a-z0-9_]+)\s*=\s*"([^"]+)"\s*$',
            contexts_match.group("contexts"),
            re.MULTILINE,
        )
        self.assertEqual(
            dict(context_pairs),
            {
                "clean_x86_64": "target:python-runtime-clean-x86_64",
                "clean_aarch64": "target:python-runtime-clean-aarch64",
                "sysroot_x86_64": "target:sysroot-x86_64",
                "sysroot_aarch64": "target:sysroot-aarch64",
            },
        )
        self.assertEqual(len(context_pairs), 4)
        self.assertTrue(all(value.startswith("target:") for _, value in context_pairs))
        self.assertNotIn("/tmp", self.body)

    def test_inline_scratch_export_copies_only_the_four_library_roots(self):
        dockerfile_match = re.search(
            r"^\s*dockerfile-inline\s*=\s*<<EOF\n(?P<dockerfile>.*?)^EOF$",
            self.body,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(dockerfile_match)
        dockerfile = dockerfile_match.group("dockerfile")
        self.assertEqual(
            dockerfile.splitlines(),
            [
                "# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32",
                "FROM scratch",
                "COPY --from=clean_x86_64 /runtime-root/usr/lib64/ /clean/x86_64/usr/lib64/",
                "COPY --from=clean_aarch64 /runtime-root/usr/lib64/ /clean/aarch64/usr/lib64/",
                "COPY --from=sysroot_x86_64 /opt/crossforge/sysroots/el8/x86_64/usr/lib64/ /sysroot/x86_64/usr/lib64/",
                "COPY --from=sysroot_aarch64 /opt/crossforge/sysroots/el8/aarch64/usr/lib64/ /sysroot/aarch64/usr/lib64/",
            ],
        )
        copies = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]
        self.assertEqual(len(copies), 4)
        self.assertTrue(all(line.startswith("COPY --from=") for line in copies))
        for forbidden in (
            "RUN ",
            "ADD ",
            "http://",
            "https://",
            "docker-image://",
            "local://",
            "/tmp",
        ):
            self.assertNotIn(forbidden, dockerfile)


class AbiSchemaTests(unittest.TestCase):
    def test_abi_tools_remain_python36_syntax_compatible(self):
        for relative in (
            "scripts/abi_contract.py",
            "scripts/extract-abi-inventory.py",
        ):
            path = REPOSITORY / relative
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 6),
            )

    def test_schemas_are_strict_and_describe_unversioned_separation(self):
        baseline_schema = json.loads(
            (REPOSITORY / "config/schemas/abi-baseline.schema.json").read_text(
                encoding="utf-8"
            )
        )
        inventory_schema = json.loads(
            (REPOSITORY / "config/schemas/abi-inventory.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(baseline_schema["additionalProperties"])
        self.assertFalse(inventory_schema["additionalProperties"])
        baseline_providers = baseline_schema["properties"]["providers"]
        self.assertEqual(
            baseline_providers["additionalProperties"]["$ref"],
            "#/$defs/exports",
        )
        self.assertEqual(
            baseline_providers["properties"]["libcrypt.so.1"]["$ref"],
            "#/$defs/libcrypt_exports",
        )
        self.assertEqual(
            set(baseline_schema["$defs"]["export"]["required"]),
            {"name", "version"},
        )
        self.assertNotIn("unversioned_exports", baseline_schema["$defs"]["exports"])
        inventory_provider = inventory_schema["$defs"]["provider_shape"]
        self.assertIn("unversioned_exports", inventory_provider["required"])
        self.assertIn("source", inventory_schema["required"])

        inventory_providers = inventory_schema["properties"]["providers"]
        self.assertEqual(
            inventory_providers["additionalProperties"]["$ref"],
            "#/$defs/provider",
        )
        self.assertEqual(
            inventory_providers["properties"]["libcrypt.so.1"]["$ref"],
            "#/$defs/libcrypt_provider",
        )

    def test_schema_public_version_patterns_are_provider_scoped(self):
        for relative in (
            "config/schemas/abi-baseline.schema.json",
            "config/schemas/abi-inventory.schema.json",
        ):
            with self.subTest(schema=relative):
                schema = json.loads((REPOSITORY / relative).read_text(encoding="utf-8"))
                core_pattern = re.compile(
                    schema["$defs"]["export"]["properties"]["version"]["pattern"]
                )
                libcrypt_pattern = re.compile(
                    schema["$defs"]["libcrypt_export"]["properties"]["version"][
                        "pattern"
                    ]
                )
                for version in (
                    "GLIBC_2.28",
                    "GLIBCXX_DEBUG_MESSAGE_LENGTH",
                    "CXXABI_1.3",
                    "GCC_3.0",
                ):
                    self.assertIsNotNone(core_pattern.fullmatch(version))
                    self.assertIsNotNone(libcrypt_pattern.fullmatch(version))
                for version in (
                    "XCRYPT_2.0",
                    "GLIBC_PRIVATE",
                    "GLIBC_PRIVATE_FUTURE",
                    "GLIBC_ABI_DT_RELR",
                    "OPENSSL_1_1_0",
                ):
                    self.assertIsNone(core_pattern.fullmatch(version))
                self.assertIsNotNone(libcrypt_pattern.fullmatch("XCRYPT_2.0"))
                for version in (
                    "XCRYPT_PRIVATE",
                    "XCRYPT_PRIVATE_FUTURE",
                    "GLIBC_PRIVATE",
                    "GLIBC_ABI_DT_RELR",
                    "OPENSSL_1_1_0",
                ):
                    self.assertIsNone(libcrypt_pattern.fullmatch(version))


if __name__ == "__main__":
    unittest.main()
