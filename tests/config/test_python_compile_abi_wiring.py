import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
QUALIFIER = runpy.run_path(str(REPOSITORY / "scripts/qualify-cpython.py"))
RUNTIME_PROVIDERS = QUALIFIER["python_runtime_providers"]

class PythonCompileAbiWiringTests(unittest.TestCase):
    def bind_repository_target(self, arch, triple, **paths):
        lock = RUNTIME_PROVIDERS.load_json(
            REPOSITORY / ("locks/sysroot-el8-%s.json" % arch)
        )
        return QUALIFIER["bind_abi_inputs"](
            paths.get("baseline", REPOSITORY / ("abi/el8/%s.json" % arch)),
            paths.get(
                "manifest", REPOSITORY / "config/abi-providers.json"
            ),
            paths.get(
                "inventory",
                REPOSITORY
                / ("evidence/abi/el8-%s-sysroot.json" % arch),
            ),
            paths.get(
                "policy",
                REPOSITORY / "config/python-runtime-providers.json",
            ),
            paths.get(
                "catalog",
                REPOSITORY
                / (
                    "evidence/abi/el8-%s-python-provider-catalog.json"
                    % arch
                ),
            ),
            lock,
            RUNTIME_PROVIDERS.canonical_sha256(lock),
            arch,
            triple,
        )

    def test_repository_inputs_bind_to_each_target_and_logical_paths(self):
        targets = (
            ("aarch64", "aarch64-unknown-linux-gnu", 22),
            ("x86_64", "x86_64-unknown-linux-gnu", 23),
        )
        for arch, triple, provider_count in targets:
            with self.subTest(arch=arch):
                bound = self.bind_repository_target(arch, triple)
                identities = bound["identities"]
                self.assertEqual(
                    identities["baseline"]["file"],
                    "abi/el8/%s.json" % arch,
                )
                self.assertEqual(
                    identities["sysroot_inventory"]["file"],
                    "evidence/abi/el8-%s-sysroot.json" % arch,
                )
                self.assertEqual(
                    identities["provider_manifest"]["file"],
                    "config/abi-providers.json",
                )
                self.assertEqual(
                    identities["runtime_provider_policy"]["file"],
                    "config/python-runtime-providers.json",
                )
                self.assertEqual(
                    identities["sysroot_inventory"]["source"][
                        "identity_sha256"
                    ],
                    identities["runtime_provider_policy"][
                        "sysroot_lock_sha256"
                    ],
                )
                self.assertEqual(
                    len(bound["baseline"]["providers"])
                    + len(bound["runtime_target"]["providers"]),
                    provider_count,
                )
                release = RUNTIME_PROVIDERS.load_json(
                    REPOSITORY / "config/release.json"
                )
                self.assertEqual(
                    QUALIFIER["validate_release_abi_inputs"](
                        release, bound
                    ),
                    QUALIFIER["abi_contract"].release_abi_inputs(
                        release, arch
                    ),
                )

    def test_release_abi_pin_cannot_diverge_from_compile_inputs(self):
        bound = self.bind_repository_target(
            "x86_64", "x86_64-unknown-linux-gnu"
        )
        release = RUNTIME_PROVIDERS.load_json(
            REPOSITORY / "config/release.json"
        )
        release["abi"]["targets"]["x86_64"]["baseline"][
            "canonical_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(
            QUALIFIER["QualificationError"],
            "ABI inputs differ from release.json",
        ):
            QUALIFIER["validate_release_abi_inputs"](release, bound)

    def test_inventory_and_policy_must_name_the_embedded_sysroot_lock(self):
        arch = "x86_64"
        triple = "x86_64-unknown-linux-gnu"
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            inventory = RUNTIME_PROVIDERS.load_json(
                REPOSITORY / "evidence/abi/el8-x86_64-sysroot.json"
            )
            inventory["source"]["identity_sha256"] = "0" * 64
            inventory_path = temporary / "inventory.json"
            inventory_path.write_text(
                json.dumps(inventory, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                QUALIFIER["QualificationError"],
                "inventory source differs",
            ):
                self.bind_repository_target(
                    arch, triple, inventory=inventory_path
                )

            policy = RUNTIME_PROVIDERS.load_json(
                REPOSITORY / "config/python-runtime-providers.json"
            )
            next(
                item for item in policy["targets"] if item["arch"] == arch
            )["sysroot_lock"]["canonical_sha256"] = "0" * 64
            policy_path = temporary / "policy.json"
            policy_path.write_text(
                json.dumps(policy, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                QUALIFIER["QualificationError"],
                "policy differs from the embedded sysroot lock",
            ):
                self.bind_repository_target(arch, triple, policy=policy_path)

    def test_reviewed_provider_catalog_bytes_are_explicitly_bound(self):
        arch = "x86_64"
        triple = "x86_64-unknown-linux-gnu"
        with tempfile.TemporaryDirectory() as temporary:
            catalog = RUNTIME_PROVIDERS.load_json(
                REPOSITORY
                / "evidence/abi/el8-x86_64-python-provider-catalog.json"
            )
            catalog["libc.so.6"]["unversioned_exports"].append(
                "forged_catalog_export"
            )
            catalog["libc.so.6"]["unversioned_exports"].sort()
            catalog_path = Path(temporary) / "catalog.json"
            catalog_path.write_bytes(
                RUNTIME_PROVIDERS.canonical_bytes(catalog) + b"\n"
            )
            with self.assertRaises(QUALIFIER["QualificationError"]):
                self.bind_repository_target(
                    arch, triple, catalog=catalog_path
                )

    def test_core_provider_hash_follows_only_a_contained_final_symlink(self):
        payload = b"reviewed core provider"
        expected = QUALIFIER["hashlib"].sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            library = root / "usr/lib64"
            library.mkdir(parents=True)
            implementation = library / "libc-2.28.so"
            implementation.write_bytes(payload)
            os.symlink(implementation.name, str(library / "libc.so.6"))
            self.assertEqual(
                QUALIFIER["contained_file_sha256"](
                    root, "/usr/lib64/libc.so.6", expected, "libc"
                ),
                expected,
            )
            implementation.write_bytes(payload + b"tampered")
            with self.assertRaisesRegex(
                QUALIFIER["QualificationError"], "SHA256 differs"
            ):
                QUALIFIER["contained_file_sha256"](
                    root, "/usr/lib64/libc.so.6", expected, "libc"
                )

    def test_catalog_is_exact_core_plus_eight_runtime_providers(self):
        for arch, triple, expected_count in (
            ("aarch64", "aarch64-unknown-linux-gnu", 22),
            ("x86_64", "x86_64-unknown-linux-gnu", 23),
        ):
            with self.subTest(arch=arch):
                bound = self.bind_repository_target(arch, triple)
                runtime_evidence = RUNTIME_PROVIDERS.runtime_provider_evidence(
                    bound["runtime_policy"], arch
                )
                reviewed_catalog = RUNTIME_PROVIDERS.load_json(
                    REPOSITORY
                    / (
                        "evidence/abi/el8-%s-python-provider-catalog.json"
                        % arch
                    )
                )

                def fake_elf_record(_readelf, _path, identity, expected_soname=None):
                    self.assertEqual(identity, expected_soname)
                    return {}, reviewed_catalog[identity]

                globals_ = QUALIFIER["build_provider_catalog"].__globals__
                with mock.patch.object(
                    RUNTIME_PROVIDERS,
                    "runtime_provider_evidence",
                    return_value=runtime_evidence,
                ), mock.patch.dict(
                    globals_,
                    {
                        "contained_file_sha256": lambda *args: args[2],
                        "elf_record": fake_elf_record,
                    },
                ):
                    catalog, external, report = QUALIFIER[
                        "build_provider_catalog"
                    ](Path("readelf"), Path("/sysroot"), bound)
                self.assertEqual(len(catalog), expected_count)
                self.assertEqual(len(external), 8)
                self.assertEqual(report["provider_count"], expected_count)
                self.assertEqual(
                    report["file"],
                    "evidence/abi/el8-%s-python-provider-catalog.json"
                    % arch,
                )
                self.assertEqual(
                    [item["soname"] for item in report["providers"]],
                    sorted(catalog),
                )
                external_records = [
                    item
                    for item in report["providers"]
                    if item["source"] == "python-runtime"
                ]
                self.assertEqual(len(external_records), 8)
                self.assertTrue(
                    all(item["rpm_owner"] for item in external_records)
                )

    def test_elf_audit_v4_has_only_policy_and_ownership_authorities(self):
        policy = {"artifact": "bin/python3.13", "profile": "crossforge-qualified-v1"}
        ownership = {"artifact": "bin/python3.13", "status": "passed"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "python"
            path.write_bytes(b"target ELF")
            globals_ = QUALIFIER["audit_python_artifact"].__globals__
            with mock.patch.object(
                QUALIFIER["abi_contract"],
                "audit_elf_policy",
                return_value=policy,
            ), mock.patch.object(
                QUALIFIER["python_abi_audit"],
                "audit_python_elf",
                return_value=ownership,
            ):
                audit = QUALIFIER["audit_python_artifact"](
                    {},
                    [],
                    {},
                    {},
                    "bin/python3.13",
                    {
                        "dynamic_section": "",
                        "program_headers": "",
                        "elf_header": "",
                    },
                    {"needed": ["libc.so.6"]},
                    path,
                )
        self.assertEqual(
            set(audit),
            {
                "needed",
                "sha256",
                "elf_record_sha256",
                "elf_record",
                "elf_policy",
                "ownership",
            },
        )
        self.assertNotIn("required_versions", audit)

    def test_docker_wires_only_the_selected_target_abi_inputs(self):
        dockerfile = (REPOSITORY / "docker/python.Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "COPY abi/el8/${CROSSFORGE_TARGET_ARCH}.json "
            "/work/config/abi-baseline.json",
            dockerfile,
        )
        self.assertIn(
            "COPY evidence/abi/el8-${CROSSFORGE_TARGET_ARCH}-sysroot.json",
            dockerfile,
        )
        self.assertIn(
            "COPY evidence/abi/el8-${CROSSFORGE_TARGET_ARCH}-python-provider-catalog.json",
            dockerfile,
        )
        for option in (
            "--abi-baseline",
            "--abi-provider-manifest",
            "--sysroot-abi-inventory",
            "--runtime-provider-policy",
            "--python-provider-catalog",
        ):
            self.assertIn(option, dockerfile)
        qualifier = (REPOSITORY / "scripts/qualify-cpython.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"qualification_schema_version": 4', qualifier)
        self.assertNotIn("required_versions", qualifier)


if __name__ == "__main__":
    unittest.main()
