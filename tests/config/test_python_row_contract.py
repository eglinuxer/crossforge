import ast
import copy
import json
import runpy
import subprocess
import sys
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPOSITORY / "scripts/python_row_contract.py"
CONTRACT = runpy.run_path(str(CONTRACT_PATH))


class PythonRowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )

    def test_implemented_records_are_ordered_and_exact(self):
        self.assertEqual(
            CONTRACT["IMPLEMENTED_ROWS"],
            (
                {
                    "minor": "3.13",
                    "row": "cp313",
                    "adapter": "modern",
                    "gil_policy": "zero",
                    "sysconfig_isolation": True,
                    "zstd": False,
                    "hash_algorithm": "siphash13",
                    "introduced_phase": 5,
                },
                {
                    "minor": "3.11",
                    "row": "cp311",
                    "adapter": "transition",
                    "gil_policy": "absent",
                    "sysconfig_isolation": True,
                    "zstd": False,
                    "hash_algorithm": "siphash13",
                    "introduced_phase": 6,
                },
                {
                    "minor": "3.12",
                    "row": "cp312",
                    "adapter": "modern",
                    "gil_policy": "absent",
                    "sysconfig_isolation": True,
                    "zstd": False,
                    "hash_algorithm": "siphash13",
                    "introduced_phase": 7,
                },
                {
                    "minor": "3.14",
                    "row": "cp314",
                    "adapter": "modern",
                    "gil_policy": "zero",
                    "sysconfig_isolation": True,
                    "zstd": True,
                    "hash_algorithm": "siphash13",
                    "introduced_phase": 8,
                },
                {
                    "minor": "3.10",
                    "row": "cp310",
                    "adapter": "legacy",
                    "gil_policy": "absent",
                    "sysconfig_isolation": True,
                    "zstd": False,
                    "hash_algorithm": "siphash24",
                    "introduced_phase": 9,
                },
                {
                    "minor": "3.9",
                    "row": "cp39",
                    "adapter": "legacy",
                    "gil_policy": "absent",
                    "sysconfig_isolation": True,
                    "zstd": False,
                    "hash_algorithm": "siphash24",
                    "introduced_phase": 10,
                },
            ),
        )
        self.assertEqual(CONTRACT["LATEST_PHASE"], 10)
        self.assertEqual(
            CONTRACT["LATEST_ROWS"],
            ("cp313", "cp311", "cp312", "cp314", "cp310", "cp39"),
        )

    def test_version_and_row_lookups_return_independent_records(self):
        by_version = CONTRACT["contract_for_version"]("3.13.15")
        by_row = CONTRACT["contract_for_row"]("cp313")
        self.assertEqual(by_version, by_row)
        by_version["adapter"] = "tampered"
        self.assertEqual(CONTRACT["contract_for_row"]("cp313")["adapter"], "modern")

    def test_release_binding_is_exact_for_version_and_row(self):
        cases = (
            ("3.9.25", "cp39", "legacy", "absent", False, "siphash24"),
            ("3.10.21", "cp310", "legacy", "absent", False, "siphash24"),
            ("3.11.16", "cp311", "transition", "absent", False, "siphash13"),
            ("3.12.14", "cp312", "modern", "absent", False, "siphash13"),
            ("3.13.15", "cp313", "modern", "zero", False, "siphash13"),
            ("3.14.7", "cp314", "modern", "zero", True, "siphash13"),
        )
        for version, row, adapter, gil_policy, zstd, hash_algorithm in cases:
            with self.subTest(version=version):
                version_binding = CONTRACT["bind_release"](
                    self.release, version=version, adapter=adapter
                )
                row_binding = CONTRACT["bind_release"](self.release, row=row)
                self.assertEqual(version_binding, row_binding)
                self.assertEqual(version_binding["entry"]["version"], version)
                self.assertEqual(
                    version_binding["contract"]["gil_policy"], gil_policy
                )
                self.assertIs(version_binding["contract"]["zstd"], zstd)
                self.assertEqual(
                    version_binding["contract"]["hash_algorithm"],
                    hash_algorithm,
                )

    def test_unimplemented_or_mismatched_contract_is_rejected(self):
        for operation in (
            lambda: CONTRACT["contract_for_version"]("3.8.20"),
            lambda: CONTRACT["contract_for_row"]("cp38"),
            lambda: CONTRACT["bind_release"](
                self.release, version="3.13.15", adapter="transition"
            ),
            lambda: CONTRACT["bind_release"](
                self.release, version="3.13.15", row="cp313"
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(CONTRACT["ContractError"]):
                    operation()

    def test_release_binding_rejects_duplicate_or_unlocked_entry(self):
        duplicate = copy.deepcopy(self.release)
        cp313 = next(
            entry
            for entry in duplicate["python"]["versions"]
            if entry["version"] == "3.13.15"
        )
        duplicate["python"]["versions"].append(
            copy.deepcopy(cp313)
        )
        with self.assertRaises(CONTRACT["ContractError"]):
            CONTRACT["bind_release"](duplicate, version="3.13.15")

        duplicate_minor = copy.deepcopy(self.release)
        other_patch = copy.deepcopy(cp313)
        other_patch["version"] = "3.13.99"
        duplicate_minor["python"]["versions"].append(other_patch)
        for selector in ({"version": "3.13.15"}, {"row": "cp313"}):
            with self.subTest(selector=selector):
                with self.assertRaises(CONTRACT["ContractError"]):
                    CONTRACT["bind_release"](duplicate_minor, **selector)

        unlocked = copy.deepcopy(self.release)
        next(
            entry
            for entry in unlocked["python"]["versions"]
            if entry["version"] == "3.13.15"
        )["source"]["status"] = "pending"
        with self.assertRaises(CONTRACT["ContractError"]):
            CONTRACT["bind_release"](unlocked, version="3.13.15")

    def test_phase_rows_preserve_introduction_order(self):
        self.assertEqual(CONTRACT["rows_for_phase"](4), ())
        self.assertEqual(CONTRACT["rows_for_phase"](5), ("cp313",))
        self.assertEqual(CONTRACT["rows_for_phase"](6), ("cp313", "cp311"))
        self.assertEqual(
            CONTRACT["rows_for_phase"](7), ("cp313", "cp311", "cp312")
        )
        self.assertEqual(
            CONTRACT["rows_for_phase"](8),
            ("cp313", "cp311", "cp312", "cp314"),
        )
        self.assertEqual(
            CONTRACT["rows_for_phase"](9),
            ("cp313", "cp311", "cp312", "cp314", "cp310"),
        )
        self.assertEqual(
            CONTRACT["rows_for_phase"](10),
            ("cp313", "cp311", "cp312", "cp314", "cp310", "cp39"),
        )
        with self.assertRaises(CONTRACT["ContractError"]):
            CONTRACT["rows_for_phase"](0)

    def test_python36_compatible_check_cli_contract(self):
        ast.parse(
            CONTRACT_PATH.read_text(encoding="utf-8"),
            filename=str(CONTRACT_PATH),
            feature_version=(3, 6),
        )
        for version, adapter, row in (
            ("3.9.25", "legacy", "cp39"),
            ("3.10.21", "legacy", "cp310"),
            ("3.11.16", "transition", "cp311"),
            ("3.12.14", "modern", "cp312"),
            ("3.13.15", "modern", "cp313"),
            ("3.14.7", "modern", "cp314"),
        ):
            with self.subTest(version=version):
                valid = subprocess.run(
                    [sys.executable, str(CONTRACT_PATH), "check", version, adapter],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                self.assertEqual(valid.returncode, 0, valid.stderr)
                self.assertIn(
                    "valid: %s %s %s" % (row, version, adapter), valid.stdout
                )

        invalid = subprocess.run(
            [
                sys.executable,
                str(CONTRACT_PATH),
                "check",
                "3.13.15",
                "transition",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertNotEqual(invalid.returncode, 0)


if __name__ == "__main__":
    unittest.main()
