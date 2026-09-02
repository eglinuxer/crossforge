import ast
import copy
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
PROVIDERS = runpy.run_path(
    str(REPOSITORY / "scripts/python_runtime_providers.py")
)
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
PolicyError = PROVIDERS["RuntimeProviderPolicyError"]
POLICY_PATH = REPOSITORY / "config/python-runtime-providers.json"
SCHEMA_PATH = (
    REPOSITORY / "config/schemas/python-runtime-providers.schema.json"
)
POLICY = PROVIDERS["load_json"](POLICY_PATH)
SCHEMA = PROVIDERS["load_json"](SCHEMA_PATH)
LOCKS = {
    arch: PROVIDERS["load_json"](
        REPOSITORY / PROVIDERS["TARGETS"][arch]["lock_file"]
    )
    for arch in PROVIDERS["TARGET_ORDER"]
}


def provider(document, arch, soname):
    target = next(item for item in document["targets"] if item["arch"] == arch)
    return next(item for item in target["providers"] if item["soname"] == soname)


def owner(document, arch, name):
    target = next(item for item in document["targets"] if item["arch"] == arch)
    return next(item for item in target["owners"] if item["name"] == name)


def locked_package(locks, arch, name):
    return next(
        item
        for item in locks[arch]["packages"]
        if item["header"]["name"] == name
    )


class PythonRuntimeProviderPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = copy.deepcopy(POLICY)
        self.locks = copy.deepcopy(LOCKS)

    def validate(self):
        return PROVIDERS["validate_policy_against_locks"](
            self.policy, self.locks
        )

    def test_repository_policy_schema_locks_and_full_transactions_pass(self):
        STRICT["validate_schema_subset"](SCHEMA)
        STRICT["validate"](POLICY, SCHEMA, SCHEMA, "$")
        report = PROVIDERS["validate_repository"]()
        self.assertRegex(report["policy_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            report["runtime_contract"], PROVIDERS["RUNTIME_CONTRACT"]
        )
        self.assertEqual(set(report["targets"]), {"aarch64", "x86_64"})
        for target in report["targets"].values():
            self.assertEqual(target["provider_count"], 8)
            self.assertEqual(target["rpm_owner_count"], 7)

    def test_exact_eight_sonames_seven_owners_and_no_core_or_zstd(self):
        expected = list(PROVIDERS["EXPECTED_PROVIDERS"])
        core = PROVIDERS["CORE_PROVIDERS"]
        for target in self.policy["targets"]:
            observed = [
                (item["soname"], item["owner"])
                for item in target["providers"]
            ]
            self.assertEqual(observed, expected)
            self.assertEqual(
                [item["name"] for item in target["owners"]],
                list(PROVIDERS["EXPECTED_OWNERS"]),
            )
            self.assertEqual(len(target["owners"]), 7)
            self.assertFalse({soname for soname, _owner in observed} & core)
            self.assertNotIn("libzstd.so.1", {soname for soname, _ in observed})
        self.validate()

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            for text, pattern in (
                ('{"kind":"a","kind":"b"}', "duplicate JSON key"),
                ('{"value":NaN}', "non-finite JSON number"),
                ('{"value":1e9999}', "non-finite JSON number"),
            ):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(PolicyError, pattern):
                        PROVIDERS["load_json"](path)

    def test_unknown_and_missing_fields_fail_at_every_policy_level(self):
        mutations = (
            lambda value: value.update({"unknown": True}),
            lambda value: value["targets"][0].update({"unknown": True}),
            lambda value: value["targets"][0]["sysroot_lock"].update(
                {"unknown": True}
            ),
            lambda value: value["targets"][0]["providers"][0].update(
                {"unknown": True}
            ),
            lambda value: value["targets"][0]["owners"][0].update(
                {"unknown": True}
            ),
            lambda value: value.pop("runtime_contract"),
            lambda value: value["targets"][0]["providers"][0].pop(
                "dso_sha256"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                document = copy.deepcopy(self.policy)
                mutate(document)
                with self.assertRaisesRegex(PolicyError, "fields differ"):
                    PROVIDERS["validate_policy"](document)

    def test_target_and_provider_order_are_contractual(self):
        self.policy["targets"].reverse()
        with self.assertRaisesRegex(PolicyError, "targets are not sorted"):
            PROVIDERS["validate_policy"](self.policy)

        self.policy = copy.deepcopy(POLICY)
        self.policy["targets"][0]["providers"].reverse()
        with self.assertRaisesRegex(PolicyError, "fixed sorted set"):
            PROVIDERS["validate_policy"](self.policy)

        self.policy = copy.deepcopy(POLICY)
        self.policy["targets"][0]["providers"].pop()
        with self.assertRaisesRegex(PolicyError, "exactly eight"):
            PROVIDERS["validate_policy"](self.policy)

    def test_arch_triple_and_lock_path_cannot_be_swapped(self):
        target = self.policy["targets"][0]
        for field, value, pattern in (
            ("arch", "x86_64", "not sorted"),
            ("triple", "x86_64-unknown-linux-gnu", "triple differ"),
            (
                "sysroot_lock",
                {
                    "file": "locks/sysroot-el8-x86_64.json",
                    "canonical_sha256": "0" * 64,
                },
                "path differs",
            ),
        ):
            with self.subTest(field=field):
                document = copy.deepcopy(self.policy)
                document["targets"][0][field] = value
                with self.assertRaisesRegex(PolicyError, pattern):
                    PROVIDERS["validate_policy"](document)

    def test_provider_paths_are_exact_absolute_logical_soname_paths(self):
        for value in (
            "usr/lib64/libbz2.so.1",
            "/usr/lib64/../lib64/libbz2.so.1",
            "/usr/lib64//libbz2.so.1",
            "/usr/lib64/libcrypto.so.1.1",
            "/tmp/libbz2.so.1",
        ):
            with self.subTest(path=value):
                document = copy.deepcopy(self.policy)
                document["targets"][0]["providers"][0]["path"] = value
                with self.assertRaisesRegex(PolicyError, "exact /usr/lib64"):
                    PROVIDERS["validate_policy"](document)

    def test_core_zstd_unknown_and_duplicate_providers_are_rejected(self):
        for soname in ("libc.so.6", "libzstd.so.1", "libevil.so.1"):
            with self.subTest(soname=soname):
                document = copy.deepcopy(self.policy)
                item = document["targets"][0]["providers"][0]
                item["soname"] = soname
                item["path"] = "/usr/lib64/" + soname
                with self.assertRaisesRegex(PolicyError, "fixed sorted set"):
                    PROVIDERS["validate_policy"](document)

        document = copy.deepcopy(self.policy)
        document["targets"][0]["providers"][1] = copy.deepcopy(
            document["targets"][0]["providers"][0]
        )
        with self.assertRaisesRegex(PolicyError, "fixed sorted set"):
            PROVIDERS["validate_policy"](document)

    def test_dso_hash_is_policy_authority_and_must_be_lowercase(self):
        item = self.policy["targets"][0]["providers"][0]
        item["dso_sha256"] = "0" * 64
        PROVIDERS["validate_policy"](self.policy)

        self.policy = copy.deepcopy(POLICY)
        self.policy["targets"][0]["providers"][0]["dso_sha256"] = "A" * 64
        with self.assertRaisesRegex(PolicyError, "lowercase hexadecimal"):
            PROVIDERS["validate_policy"](self.policy)

    def test_each_provider_is_bound_to_the_canonical_target_lock(self):
        self.policy["targets"][0]["sysroot_lock"][
            "canonical_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(PolicyError, "target sysroot lock SHA256 differs"):
            self.validate()

        self.policy = copy.deepcopy(POLICY)
        self.policy["targets"][0]["sysroot_lock"]["canonical_sha256"] = "A" * 64
        with self.assertRaisesRegex(PolicyError, "lowercase hexadecimal"):
            PROVIDERS["validate_policy"](self.policy)

    def test_single_target_lock_binding_matches_matrix_validation(self):
        matrix = self.validate()
        for arch in PROVIDERS["TARGET_ORDER"]:
            self.assertEqual(
                PROVIDERS["validate_policy_target_against_lock"](
                    self.policy, arch, self.locks[arch]
                ),
                matrix[arch],
            )

    def test_rpm_owner_name_nevra_and_received_hash_are_exact(self):
        mutations = (
            ("name", "zlib", "fixed ownership"),
            (
                "nevra",
                "bzip2-libs-0:1.0.6-99.el8.aarch64",
                "owner .* differs from locked RPM",
            ),
            ("received_sha256", "0" * 64, "owner .* differs from locked RPM"),
        )
        for field, value, pattern in mutations:
            with self.subTest(field=field):
                document = copy.deepcopy(self.policy)
                document["targets"][0]["owners"][0][field] = value
                with self.assertRaisesRegex(PolicyError, pattern):
                    PROVIDERS["validate_policy_against_locks"](
                        document, self.locks
                    )

    def test_lock_package_order_arch_and_owner_identity_are_checked(self):
        self.locks["aarch64"]["packages"].reverse()
        with self.assertRaisesRegex(PolicyError, "sorted and unique"):
            self.validate()

        self.locks = copy.deepcopy(LOCKS)
        package = locked_package(self.locks, "aarch64", "bzip2-libs")
        package["header"]["arch"] = "x86_64"
        with self.assertRaisesRegex(PolicyError, "wrong architecture"):
            self.validate()

        self.locks = copy.deepcopy(LOCKS)
        package = locked_package(self.locks, "aarch64", "bzip2-libs")
        package["received_sha256"] = "0" * 64
        with self.assertRaisesRegex(PolicyError, "target sysroot lock SHA256 differs"):
            self.validate()
        mutated_lock_sha256 = PROVIDERS["canonical_sha256"](
            self.locks["aarch64"]
        )
        self.policy["targets"][0]["sysroot_lock"][
            "canonical_sha256"
        ] = mutated_lock_sha256
        with self.assertRaisesRegex(PolicyError, "owner .* differs from locked RPM"):
            self.validate()

    def test_owner_records_are_sorted_unique_and_provider_references_are_exact(self):
        self.policy["targets"][0]["owners"].reverse()
        with self.assertRaisesRegex(PolicyError, "fixed ownership"):
            PROVIDERS["validate_policy"](self.policy)

        self.policy = copy.deepcopy(POLICY)
        self.policy["targets"][0]["owners"].pop()
        with self.assertRaisesRegex(PolicyError, "exactly seven"):
            PROVIDERS["validate_policy"](self.policy)

        self.policy = copy.deepcopy(POLICY)
        provider(self.policy, "aarch64", "libssl.so.1.1")["owner"] = "zlib"
        with self.assertRaisesRegex(PolicyError, "fixed ownership"):
            PROVIDERS["validate_policy"](self.policy)

    def test_provider_file_hash_follows_only_a_contained_final_symlink(self):
        item = copy.deepcopy(self.policy["targets"][0]["providers"][0])
        payload = b"synthetic provider bytes"
        item["dso_sha256"] = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            directory = root / "usr/lib64"
            directory.mkdir(parents=True)
            implementation = directory / "libbz2.so.1.0.6"
            implementation.write_bytes(payload)
            os.symlink(implementation.name, str(directory / "libbz2.so.1"))
            self.assertEqual(
                PROVIDERS["provider_file_sha256"](root, item, "synthetic"),
                item["dso_sha256"],
            )
            implementation.write_bytes(payload + b"tampered")
            with self.assertRaisesRegex(PolicyError, "DSO SHA256 differs"):
                PROVIDERS["provider_file_sha256"](root, item, "synthetic")

    def test_provider_file_rejects_symlink_escape_and_symlink_parent(self):
        item = copy.deepcopy(self.policy["targets"][0]["providers"][0])
        payload = b"outside provider"
        item["dso_sha256"] = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            directory = root / "usr/lib64"
            directory.mkdir(parents=True)
            outside = base / "outside.so"
            outside.write_bytes(payload)
            os.symlink(str(outside), str(directory / "libbz2.so.1"))
            with self.assertRaisesRegex(PolicyError, "escapes its root"):
                PROVIDERS["provider_file_sha256"](root, item, "synthetic")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            outside = base / "outside"
            outside.mkdir()
            root.mkdir()
            os.symlink(str(outside), str(root / "usr"))
            with self.assertRaisesRegex(PolicyError, "parent is a symlink"):
                PROVIDERS["provider_file_sha256"](root, item, "synthetic")

    def test_cli_validates_repository_contract(self):
        process = subprocess.run(
            [
                sys.executable,
                str(REPOSITORY / "scripts/validate-python-runtime-providers.py"),
            ],
            cwd=str(REPOSITORY),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn("8 providers, 7 RPM owners", process.stdout)
        self.assertNotIn(str(REPOSITORY), process.stdout)

    def test_runtime_evidence_is_the_exact_target_policy_projection(self):
        evidence = PROVIDERS["runtime_provider_evidence"](
            self.policy, "x86_64"
        )
        target = PROVIDERS["policy_target"](self.policy, "x86_64")
        owners = {item["name"]: item for item in target["owners"]}
        self.assertEqual(
            evidence,
            {
                "policy_sha256": PROVIDERS["canonical_sha256"](self.policy),
                "target": {
                    "arch": "x86_64",
                    "triple": "x86_64-unknown-linux-gnu",
                },
                "sysroot_lock_sha256": target["sysroot_lock"][
                    "canonical_sha256"
                ],
                "provider_catalog_sha256": target[
                    "provider_catalog_sha256"
                ],
                "providers": [
                    {
                        "soname": item["soname"],
                        "path": item["path"],
                        "owner": owners[item["owner"]],
                        "dso_sha256": item["dso_sha256"],
                    }
                    for item in target["providers"]
                ],
            },
        )

    def test_validator_sources_parse_with_python_3_6_grammar(self):
        for relative in (
            "scripts/python_runtime_providers.py",
            "scripts/validate-python-runtime-providers.py",
        ):
            with self.subTest(path=relative):
                path = REPOSITORY / relative
                ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                    feature_version=(3, 6),
                )

    def test_policy_canonical_digest_is_stable_under_key_order(self):
        reordered = json.loads(json.dumps(self.policy, sort_keys=True))
        self.assertEqual(
            PROVIDERS["canonical_sha256"](self.policy),
            PROVIDERS["canonical_sha256"](reordered),
        )


if __name__ == "__main__":
    unittest.main()
