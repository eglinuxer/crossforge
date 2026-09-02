import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
FREEZER = REPOSITORY / "scripts/freeze-abi-baseline.py"
sys.path.insert(0, str(REPOSITORY / "scripts"))
import abi_contract  # noqa: E402


class FreezeAbiBaselineTests(unittest.TestCase):
    def repository_fixture(self, directory, arch="x86_64", source_kind="clean-rocky-oci"):
        repository = directory / "repository"
        (repository / "scripts").mkdir(parents=True)
        (repository / "config").mkdir()
        (repository / "evidence/abi").mkdir(parents=True)
        shutil.copy2(FREEZER, repository / "scripts/freeze-abi-baseline.py")
        shutil.copy2(
            REPOSITORY / "scripts/abi_contract.py",
            repository / "scripts/abi_contract.py",
        )
        shutil.copy2(
            REPOSITORY / "config/abi-providers.json",
            repository / "config/abi-providers.json",
        )
        manifest = json.loads(
            (repository / "config/abi-providers.json").read_text(encoding="utf-8")
        )
        target = next(item for item in manifest["targets"] if item["arch"] == arch)
        providers = {}
        for record in target["providers"]:
            soname = record["soname"]
            symbol = "fixture_" + soname.replace(".", "_").replace("-", "_")
            version = "XCRYPT_2.0" if soname == "libcrypt.so.1" else "GLIBC_2.2.5"
            providers[soname] = {
                "path": "/" + record["path"],
                "soname": soname,
                "sha256": "1" * 64,
                "exports": [{"name": symbol, "version": version}],
                "unversioned_exports": ["unversioned_" + symbol],
                "nonpublic_versioned_exports": [],
            }
        inventory = {
            "$schema": abi_contract.INVENTORY_SCHEMA_ID,
            "schema_version": 1,
            "kind": abi_contract.INVENTORY_KIND,
            "target": {"arch": arch, "triple": abi_contract.TARGETS[arch]["triple"]},
            "source": {
                "kind": source_kind,
                "identity_sha256": "2" * 64,
                "provider_manifest_sha256": abi_contract.canonical_sha256(manifest),
            },
            "providers": providers,
        }
        inventory_path = repository / (
            "evidence/abi/el8-%s-clean.json" % arch
        )
        inventory_path.write_text(
            json.dumps(inventory, sort_keys=True) + "\n", encoding="utf-8"
        )
        return repository, inventory

    @staticmethod
    def run_freezer(repository, arch, digest=None, extra=()):
        command = [
            sys.executable,
            str(repository / "scripts/freeze-abi-baseline.py"),
            "--arch",
            arch,
        ]
        if digest is not None:
            command.extend(["--accept-inventory-sha256", digest])
        command.extend(extra)
        return subprocess.run(
            command,
            cwd=str(repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

    def test_both_targets_promote_exact_public_exports_and_fixed_policy(self):
        for arch in ("x86_64", "aarch64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                repository, inventory = self.repository_fixture(Path(temporary), arch)
                digest = abi_contract.canonical_sha256(inventory)
                process = self.run_freezer(repository, arch, digest)
                self.assertEqual(process.returncode, 0, process.stderr)
                output = repository / ("abi/el8/%s.json" % arch)
                self.assertTrue(output.is_file())
                baseline = abi_contract.load_baseline(
                    output,
                    arch,
                    abi_contract.TARGETS[arch]["triple"],
                )
                self.assertEqual(
                    baseline["providers"],
                    {
                        soname: provider["exports"]
                        for soname, provider in inventory["providers"].items()
                    },
                )
                self.assertEqual(
                    baseline["review"],
                    {
                        "status": "reviewed",
                        "source_inventory": "evidence/abi/el8-%s-clean.json" % arch,
                        "source_inventory_sha256": digest,
                    },
                )
                self.assertEqual(
                    {
                        profile["interpreter"]["expected"]
                        for profile in baseline["elf_policy"]["profiles"].values()
                    },
                    {abi_contract.TARGETS[arch]["interpreter"]},
                )
                self.assertEqual(
                    baseline["elf_policy"]["artifact_exceptions"],
                    abi_contract.EXPECTED_ARTIFACT_EXCEPTIONS,
                )
                self.assertEqual(
                    baseline["providers"]["libcrypt.so.1"][0]["version"],
                    "XCRYPT_2.0",
                )
                self.assertEqual(
                    abi_contract.validate_baseline_against_inventory(
                        baseline, inventory, require_exact=True
                    ),
                    {
                        "missing_providers": [],
                        "extra_providers": [],
                        "missing_exports": {},
                        "extra_exports": {},
                    },
                )

    def test_digest_is_mandatory_and_must_match_canonical_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, inventory = self.repository_fixture(Path(temporary))
            missing = self.run_freezer(repository, "x86_64")
            self.assertEqual(missing.returncode, 2)
            self.assertIn("--accept-inventory-sha256", missing.stderr)

            mismatch = self.run_freezer(repository, "x86_64", "0" * 64)
            self.assertEqual(mismatch.returncode, 1)
            self.assertIn("differs from the reviewed clean inventory", mismatch.stderr)
            self.assertFalse((repository / "abi").exists())

            # A hash of the file bytes is not the reviewed canonical JSON digest.
            byte_digest = hashlib.sha256(
                (repository / "evidence/abi/el8-x86_64-clean.json").read_bytes()
            ).hexdigest()
            self.assertNotEqual(byte_digest, abi_contract.canonical_sha256(inventory))
            mismatch = self.run_freezer(repository, "x86_64", byte_digest)
            self.assertEqual(mismatch.returncode, 1)
            self.assertFalse((repository / "abi").exists())

    def test_inventory_and_output_paths_cannot_be_selected_by_the_caller(self):
        for option in ("--inventory", "--output"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as temporary:
                repository, inventory = self.repository_fixture(Path(temporary))
                process = self.run_freezer(
                    repository,
                    "x86_64",
                    abi_contract.canonical_sha256(inventory),
                    (option, str(Path(temporary) / "attacker.json")),
                )
                self.assertEqual(process.returncode, 2)
                self.assertFalse((repository / "abi").exists())

    def test_only_exact_clean_evidence_path_is_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, inventory = self.repository_fixture(Path(temporary))
            exact = repository / "evidence/abi/el8-x86_64-clean.json"
            alternate = repository / "evidence/abi/reviewed.json"
            exact.rename(alternate)
            process = self.run_freezer(
                repository, "x86_64", abi_contract.canonical_sha256(inventory)
            )
            self.assertEqual(process.returncode, 1)
            self.assertIn("reviewed clean ABI inventory is unavailable", process.stderr)
            self.assertFalse((repository / "abi").exists())

    def test_fixed_provider_manifest_binding_precedes_promotion(self):
        for mutation, pattern in (
            ("manifest-digest", "provider manifest SHA256 differs"),
            ("provider-path", "providers differ from the fixed provider manifest"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                repository, inventory = self.repository_fixture(Path(temporary))
                if mutation == "manifest-digest":
                    inventory["source"]["provider_manifest_sha256"] = "3" * 64
                else:
                    first = sorted(inventory["providers"])[0]
                    inventory["providers"][first]["path"] = "/usr/lib64/forged.so"
                (repository / "evidence/abi/el8-x86_64-clean.json").write_text(
                    json.dumps(inventory, sort_keys=True) + "\n", encoding="utf-8"
                )
                process = self.run_freezer(
                    repository, "x86_64", abi_contract.canonical_sha256(inventory)
                )
                self.assertEqual(process.returncode, 1)
                self.assertIn(pattern, process.stderr)
                self.assertFalse((repository / "abi").exists())

    def test_locked_sysroot_inventory_cannot_be_promoted_as_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, inventory = self.repository_fixture(
                Path(temporary), source_kind="locked-sysroot"
            )
            process = self.run_freezer(
                repository, "x86_64", abi_contract.canonical_sha256(inventory)
            )
            self.assertEqual(process.returncode, 1)
            self.assertIn("not a clean Rocky OCI inventory", process.stderr)
            self.assertFalse((repository / "abi").exists())

    def test_input_and_output_symlinks_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            repository, inventory = self.repository_fixture(directory)
            digest = abi_contract.canonical_sha256(inventory)
            outside = directory / "outside"
            outside.mkdir()
            shutil.move(str(repository / "evidence/abi"), str(outside / "abi"))
            (repository / "evidence").rmdir()
            (repository / "evidence").symlink_to(outside, target_is_directory=True)
            process = self.run_freezer(repository, "x86_64", digest)
            self.assertEqual(process.returncode, 1)
            self.assertIn("must not be a symbolic link", process.stderr)
            self.assertFalse((repository / "abi").exists())

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            repository, inventory = self.repository_fixture(directory)
            outside = directory / "outside"
            outside.mkdir()
            (repository / "abi").symlink_to(outside, target_is_directory=True)
            process = self.run_freezer(
                repository, "x86_64", abi_contract.canonical_sha256(inventory)
            )
            self.assertEqual(process.returncode, 1)
            self.assertIn("must not be a symbolic link", process.stderr)
            self.assertFalse((outside / "el8/x86_64.json").exists())

    def test_existing_regular_or_symlink_baseline_is_never_replaced(self):
        for mode in ("regular", "symlink"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                repository, inventory = self.repository_fixture(directory)
                destination = repository / "abi/el8/x86_64.json"
                destination.parent.mkdir(parents=True)
                if mode == "regular":
                    destination.write_text("keep\n", encoding="utf-8")
                    protected = destination
                else:
                    protected = directory / "outside-baseline.json"
                    protected.write_text("keep\n", encoding="utf-8")
                    destination.symlink_to(protected)
                process = self.run_freezer(
                    repository, "x86_64", abi_contract.canonical_sha256(inventory)
                )
                self.assertEqual(process.returncode, 1)
                self.assertIn("refusing to replace existing ABI baseline", process.stderr)
                self.assertEqual(protected.read_text(encoding="utf-8"), "keep\n")
                self.assertEqual(
                    sorted(path.name for path in destination.parent.iterdir()),
                    ["x86_64.json"],
                )

    def test_invalid_empty_public_export_set_fails_before_any_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, inventory = self.repository_fixture(Path(temporary))
            first = sorted(inventory["providers"])[0]
            inventory["providers"][first]["exports"] = []
            (repository / "evidence/abi/el8-x86_64-clean.json").write_text(
                json.dumps(inventory, sort_keys=True) + "\n", encoding="utf-8"
            )
            process = self.run_freezer(
                repository, "x86_64", abi_contract.canonical_sha256(inventory)
            )
            self.assertEqual(process.returncode, 1)
            self.assertIn("must not be empty", process.stderr)
            self.assertFalse((repository / "abi").exists())

    def test_tool_remains_python36_syntax_compatible(self):
        ast.parse(
            FREEZER.read_text(encoding="utf-8"),
            filename=str(FREEZER),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
