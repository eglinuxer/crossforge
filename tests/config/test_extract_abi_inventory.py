import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
EXTRACTOR = REPOSITORY / "scripts/extract-abi-inventory.py"


class ExtractAbiInventoryTests(unittest.TestCase):
    @staticmethod
    def provider_records():
        manifest = json.loads(
            (REPOSITORY / "config/abi-providers.json").read_text(encoding="utf-8")
        )
        return manifest["targets"][1]["providers"]

    @classmethod
    def populate_root(cls, directory, omit=None):
        root = directory / "root"
        for provider in cls.provider_records():
            if provider["soname"] == omit:
                continue
            path = root.joinpath(*provider["path"].split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((provider["soname"] + "\n").encode("ascii"))
        return root

    @staticmethod
    def fake_readelf(directory):
        path = directory / "fake-readelf.py"
        path.write_text(
            "#!%s\n" % sys.executable
            + "import os, sys\n"
            + "if '--version' in sys.argv:\n"
            + " print('GNU readelf (Crossforge fixture) 2.30'); raise SystemExit(0)\n"
            + "soname=os.path.basename(sys.argv[-1])\n"
            + "if '-h' in sys.argv:\n"
            + " print('  Type: DYN (Shared object file)')\n"
            + " print('  Machine: Advanced Micro Devices X86-64')\n"
            + "elif '-d' in sys.argv:\n"
            + " print(' 0x000000000000000e (SONAME) Library soname: [%s]' % soname)\n"
            + "elif '--dyn-syms' in sys.argv:\n"
            + " print(\"Symbol table '.dynsym' contains 2 entries:\")\n"
            + " print(' Num: Value Size Type Bind Vis Ndx Name')\n"
            + " print(' 1: 0000000000001000 8 FUNC GLOBAL DEFAULT 12 fixture@@GLIBC_2.2.5 (2)')\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def test_candidate_and_compact_evidence_are_the_only_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.populate_root(directory)
            readelf = self.fake_readelf(directory)
            candidate = directory / "candidate.json"
            evidence = directory / "evidence.json"
            command = [
                sys.executable,
                str(EXTRACTOR),
                "--arch",
                "x86_64",
                "--triple",
                "x86_64-unknown-linux-gnu",
                "--source-kind",
                "clean-rocky-oci",
                "--root",
                str(root),
                "--release",
                str(REPOSITORY / "config/release.json"),
                "--candidate",
                str(candidate),
                "--evidence",
                str(evidence),
                "--readelf",
                str(readelf),
            ]
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)

            inventory = json.loads(candidate.read_text(encoding="utf-8"))
            extraction = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(inventory["source"]["kind"], "clean-rocky-oci")
            self.assertEqual(
                inventory["source"]["identity_sha256"],
                "f5529992e67440c1a4ae7788244d4381c6909159a88eacd95b7523ae47ced82e",
            )
            self.assertEqual(set(inventory["providers"]), {record["soname"] for record in self.provider_records()})
            self.assertEqual(extraction["inventory"]["provider_count"], 15)
            self.assertEqual(len(extraction["commands"]), 45)
            self.assertNotIn("Symbol table", evidence.read_text(encoding="utf-8"))
            self.assertEqual(
                {path.name for path in directory.iterdir()},
                {
                    "candidate.json",
                    "evidence.json",
                    "fake-readelf.py",
                    "root",
                },
            )

    def test_reviewed_abi_tree_is_never_an_output_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "evidence.json"
            process = subprocess.run(
                [
                    sys.executable,
                    str(EXTRACTOR),
                    "--arch",
                    "x86_64",
                    "--triple",
                    "x86_64-unknown-linux-gnu",
                    "--source-kind",
                    "clean-rocky-oci",
                    "--root",
                    temporary,
                    "--release",
                    str(REPOSITORY / "config/release.json"),
                    "--candidate",
                    str(REPOSITORY / "abi/forbidden.json"),
                    "--evidence",
                    str(evidence),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("never write into the reviewed abi/ tree", process.stderr)
            self.assertFalse(evidence.exists())

    def test_missing_and_escaping_manifest_providers_are_rejected(self):
        first = self.provider_records()[0]
        for mode, pattern in (
            ("missing", "fixed ABI provider is missing"),
            ("escape", "escapes the resolved root"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                root = self.populate_root(
                    directory, omit=first["soname"] if mode == "missing" else None
                )
                if mode == "escape":
                    provider = root.joinpath(*first["path"].split("/"))
                    provider.unlink()
                    outside = directory / "outside-provider"
                    outside.write_bytes(b"outside\n")
                    provider.symlink_to(outside)
                process = subprocess.run(
                    [
                        sys.executable,
                        str(EXTRACTOR),
                        "--arch",
                        "x86_64",
                        "--triple",
                        "x86_64-unknown-linux-gnu",
                        "--source-kind",
                        "clean-rocky-oci",
                        "--root",
                        str(root),
                        "--release",
                        str(REPOSITORY / "config/release.json"),
                        "--candidate",
                        str(directory / "candidate.json"),
                        "--evidence",
                        str(directory / "evidence.json"),
                        "--readelf",
                        str(self.fake_readelf(directory)),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(pattern, process.stderr)
                self.assertFalse((directory / "candidate.json").exists())
                self.assertFalse((directory / "evidence.json").exists())

    def test_root_boundary_rejects_symlinks_and_the_host_root(self):
        for mode, pattern in (
            ("symlink", "not a real directory"),
            ("host-root", "must not be the host root"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                real_root = self.populate_root(directory)
                if mode == "symlink":
                    root = directory / "root-link"
                    root.symlink_to(real_root, target_is_directory=True)
                else:
                    root = Path("/")
                process = subprocess.run(
                    [
                        sys.executable,
                        str(EXTRACTOR),
                        "--arch",
                        "x86_64",
                        "--triple",
                        "x86_64-unknown-linux-gnu",
                        "--source-kind",
                        "clean-rocky-oci",
                        "--root",
                        str(root),
                        "--release",
                        str(REPOSITORY / "config/release.json"),
                        "--candidate",
                        str(directory / "candidate.json"),
                        "--evidence",
                        str(directory / "evidence.json"),
                        "--readelf",
                        str(self.fake_readelf(directory)),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(pattern, process.stderr)


if __name__ == "__main__":
    unittest.main()
