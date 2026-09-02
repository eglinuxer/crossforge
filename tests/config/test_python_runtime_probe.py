import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
PROBE = runpy.run_path(str(REPOSITORY / "tests/python/runtime_probe.py"))


class PythonRuntimeProbeContractTests(unittest.TestCase):
    def test_absent_and_zero_gil_policies_are_distinct(self):
        PROBE["validate_gil_policy"]({}, "3.11", "absent")
        PROBE["validate_gil_policy"]({}, "3.12", "absent")
        PROBE["validate_gil_policy"](
            {"Py_GIL_DISABLED": 0}, "3.13", "zero"
        )

        for variables, minor, policy in (
            ({"Py_GIL_DISABLED": 0}, "3.11", "absent"),
            ({"Py_GIL_DISABLED": 0}, "3.12", "absent"),
            ({}, "3.13", "zero"),
            ({"Py_GIL_DISABLED": 1}, "3.13", "zero"),
            ({"Py_GIL_DISABLED": False}, "3.13", "zero"),
            ({"Py_GIL_DISABLED": 0.0}, "3.13", "zero"),
            ({}, "3.13", "unsupported"),
        ):
            with self.subTest(variables=variables, minor=minor, policy=policy):
                with self.assertRaises(PROBE["ProbeError"]):
                    PROBE["validate_gil_policy"](variables, minor, policy)

    def test_abi_integer_predicate_rejects_bool_and_float(self):
        self.assertTrue(PROBE["is_exact_integer"](0, 0))
        self.assertFalse(PROBE["is_exact_integer"](False, 0))
        self.assertFalse(PROBE["is_exact_integer"](0.0, 0))


if __name__ == "__main__":
    unittest.main()
