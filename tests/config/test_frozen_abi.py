import ast
import copy
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = REPOSITORY / "scripts/validate-frozen-abi.py"
sys.path.insert(0, str(REPOSITORY / "scripts"))
import abi_contract  # noqa: E402


VALIDATOR = runpy.run_path(str(VALIDATOR_PATH))


class FrozenAbiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = VALIDATOR["load_repository_documents"](REPOSITORY)

    @classmethod
    def mutated_documents(cls):
        return copy.deepcopy(cls.documents)

    @staticmethod
    def validate(documents):
        return VALIDATOR["validate_documents"](*documents)

    @staticmethod
    def inventory(documents, arch, variant):
        return documents[3][arch][variant]

    @staticmethod
    def evidence(documents, arch, variant):
        return documents[4][arch][variant]

    @staticmethod
    def copied_repository(directory):
        repository = directory / "repository"
        for relative in (
            "scripts/validate-release.py",
            "config/release.json",
            "config/abi-providers.json",
            "config/schemas/release.schema.json",
        ):
            source = REPOSITORY / relative
            destination = repository / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(destination))
        shutil.copytree(str(REPOSITORY / "abi"), str(repository / "abi"))
        shutil.copytree(
            str(REPOSITORY / "evidence/abi"), str(repository / "evidence/abi")
        )
        return repository

    def test_real_repository_and_cli_pass_with_compact_summary(self):
        summary = VALIDATOR["validate_repository"](REPOSITORY)
        self.assertEqual(summary["baseline"], "el8")
        self.assertEqual(
            [
                (
                    target["arch"],
                    target["provider_count"],
                    target["public_export_count"],
                )
                for target in summary["targets"]
            ],
            [("x86_64", 15, 9285), ("aarch64", 14, 9109)],
        )

        process = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH)],
            cwd=str(REPOSITORY),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(
            process.stdout.strip(),
            "frozen ABI el8 valid: x86_64=15 providers/9285 exports, "
            "aarch64=14 providers/9109 exports; sysroot extras=0",
        )

    def test_release_source_identities_are_mandatory_for_both_sources(self):
        for variant in ("clean", "sysroot"):
            with self.subTest(variant=variant):
                documents = self.mutated_documents()
                self.inventory(documents, "aarch64", variant)["source"][
                    "identity_sha256"
                ] = "0" * 64
                with self.assertRaisesRegex(
                    abi_contract.AbiContractError,
                    "source identity or manifest binding differs from release",
                ):
                    self.validate(documents)

    def test_manifest_digest_binds_inventory_and_extraction_evidence(self):
        documents = self.mutated_documents()
        self.inventory(documents, "x86_64", "clean")["source"][
            "provider_manifest_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(
            abi_contract.AbiContractError, "provider manifest SHA256 differs"
        ):
            self.validate(documents)

        documents = self.mutated_documents()
        self.evidence(documents, "x86_64", "clean")["provider_manifest"][
            "canonical_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(
            abi_contract.AbiContractError, "provider manifest binding differs"
        ):
            self.validate(documents)

    def test_baseline_must_exactly_equal_clean_public_exports(self):
        documents = self.mutated_documents()
        clean = self.inventory(documents, "x86_64", "clean")
        clean["providers"]["libc.so.6"]["exports"].append(
            {"name": "zz_crossforge_unreviewed", "version": "GLIBC_2.2.5"}
        )
        clean["providers"]["libc.so.6"]["exports"].sort(
            key=lambda record: (record["name"], record["version"])
        )
        documents[2]["x86_64"]["review"][
            "source_inventory_sha256"
        ] = abi_contract.canonical_sha256(clean)
        with self.assertRaisesRegex(
            abi_contract.AbiContractError, "unreviewed public export"
        ):
            self.validate(documents)

    def test_sysroot_must_remain_an_exact_public_export_superset(self):
        documents = self.mutated_documents()
        sysroot = self.inventory(documents, "x86_64", "sysroot")
        sysroot["providers"]["libc.so.6"]["exports"].append(
            {"name": "zz_crossforge_errata", "version": "GLIBC_2.2.5"}
        )
        sysroot["providers"]["libc.so.6"]["exports"].sort(
            key=lambda record: (record["name"], record["version"])
        )
        with self.assertRaisesRegex(
            abi_contract.AbiContractError, "sysroot inventory has unreviewed ABI extras"
        ):
            self.validate(documents)

        documents = self.mutated_documents()
        self.inventory(documents, "aarch64", "sysroot")["providers"][
            "libc.so.6"
        ]["exports"].pop()
        with self.assertRaisesRegex(
            abi_contract.AbiContractError, "missing a baseline export"
        ):
            self.validate(documents)

    def test_provider_maps_and_evidence_lists_reject_wrong_order_and_duplicates(self):
        documents = self.mutated_documents()
        clean = self.inventory(documents, "x86_64", "clean")
        clean["providers"] = dict(reversed(list(clean["providers"].items())))
        with self.assertRaisesRegex(
            abi_contract.AbiContractError, "provider order differs"
        ):
            self.validate(documents)

        documents = self.mutated_documents()
        providers = self.evidence(documents, "aarch64", "sysroot")["providers"]
        providers.insert(0, copy.deepcopy(providers[0]))
        with self.assertRaisesRegex(
            abi_contract.AbiContractError,
            "provider path, SHA256, count, membership, or order differs",
        ):
            self.validate(documents)

        documents = self.mutated_documents()
        commands = self.evidence(documents, "x86_64", "clean")["commands"]
        commands[0], commands[1] = commands[1], commands[0]
        with self.assertRaisesRegex(
            abi_contract.AbiContractError,
            "readelf operations, logical paths, membership, or order differ",
        ):
            self.validate(documents)

    def test_extraction_provider_path_sha_and_counts_match_inventory(self):
        for field, value in (
            ("path", "/usr/lib64/forged.so"),
            ("sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                documents = self.mutated_documents()
                self.evidence(documents, "aarch64", "clean")["providers"][0][
                    field
                ] = value
                with self.assertRaisesRegex(
                    abi_contract.AbiContractError,
                    "provider path, SHA256, count, membership, or order differs",
                ):
                    self.validate(documents)

        documents = self.mutated_documents()
        self.evidence(documents, "aarch64", "clean")["providers"][0]["counts"][
            "public_versioned_exports"
        ] += 1
        with self.assertRaisesRegex(
            abi_contract.AbiContractError,
            "provider path, SHA256, count, membership, or order differs",
        ):
            self.validate(documents)

    def test_extraction_unknown_fields_and_inventory_binding_are_rejected(self):
        documents = self.mutated_documents()
        self.evidence(documents, "x86_64", "sysroot")["tool"]["unexpected"] = True
        with self.assertRaisesRegex(abi_contract.AbiContractError, "tool fields differ"):
            self.validate(documents)

        documents = self.mutated_documents()
        self.evidence(documents, "aarch64", "clean")["inventory"][
            "provider_count"
        ] += 1
        with self.assertRaisesRegex(abi_contract.AbiContractError, "provider_count differs"):
            self.validate(documents)

        documents = self.mutated_documents()
        self.evidence(documents, "x86_64", "sysroot")["inventory"][
            "canonical_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(
            abi_contract.AbiContractError, "canonical inventory digest differs"
        ):
            self.validate(documents)

        documents = self.mutated_documents()
        self.evidence(documents, "aarch64", "clean")["tool"][
            "version"
        ] = "GNU readelf (forged provenance) 999"
        with self.assertRaisesRegex(
            abi_contract.AbiContractError, "readelf tool provenance differs"
        ):
            self.validate(documents)

    def test_strict_repository_loader_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.copied_repository(Path(temporary))

            path = repository / "evidence/abi/el8-x86_64-clean.extraction.json"
            payload = path.read_text(encoding="utf-8")
            self.assertTrue(payload.startswith('{"commands":'))
            path.write_text(
                '{"commands":[],"commands":' + payload[len('{"commands":') :],
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                abi_contract.AbiContractError, "duplicate JSON key: 'commands'"
            ):
                VALIDATOR["validate_repository"](repository)

    def test_frozen_files_must_use_canonical_json_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.copied_repository(Path(temporary))

            path = repository / "abi/el8/aarch64.json"
            path.write_text(
                "\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                abi_contract.AbiContractError, "is not canonical JSON"
            ):
                VALIDATOR["validate_repository"](repository)

    def test_tool_remains_python36_syntax_compatible(self):
        ast.parse(
            VALIDATOR_PATH.read_text(encoding="utf-8"),
            filename=str(VALIDATOR_PATH),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
