import io
import json
import runpy
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
INSTALLER = runpy.run_path(str(REPOSITORY / "scripts/install-host-rpm-lock.py"))


class InstallHostRPMLockTests(unittest.TestCase):
    def test_install_marker_binds_release_component_digest(self):
        base = ["base-0:1-1.x86_64"]
        result = ["base-0:1-1.x86_64", "extra-0:1-1.x86_64"]
        normalized = {
            "role": "host-python-build",
            "base": base,
            "result": result,
            "packages": [{"path": Path("/bundle/extra.rpm")}],
        }
        lock = {"kind": "rpm-lock"}
        transaction = {"kind": "rpm-transaction"}
        binding = {
            "kind": "release-component",
            "component": "rpm/host-python-build",
            "scope": "build",
            "canonical_sha256": "a" * 64,
        }
        globals_ = INSTALLER["install"].__globals__
        original_inventory = globals_["rpm_inventory"]
        original_command = globals_["run_command"]
        inventories = iter((base, base, result))
        calls = []
        globals_["rpm_inventory"] = lambda: next(inventories)
        globals_["run_command"] = lambda arguments, label: (
            calls.append((arguments, label)) or ("", "")
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / "marker.json"
                with redirect_stdout(io.StringIO()):
                    INSTALLER["install"](
                        normalized, lock, transaction, marker, binding
                    )
                value = json.loads(marker.read_text(encoding="utf-8"))
        finally:
            globals_["rpm_inventory"] = original_inventory
            globals_["run_command"] = original_command
        self.assertEqual(value["schema_version"], 2)
        self.assertEqual(value["kind"], "host-rpm-install-marker")
        self.assertEqual(value["release_binding"], binding)
        self.assertNotIn("release_sha256", value)
        self.assertEqual(len(calls), 2)

    def test_invalid_component_binding_fails_before_rpm_or_marker_mutation(self):
        normalized = {
            "role": "host-python-build",
            "base": [],
            "result": [],
            "packages": [],
        }
        invalid_values = (
            {
                "kind": "release-component",
                "component": "rpm/host-python-build",
                "scope": "build",
                "canonical_sha256": "invalid",
            },
            {
                "kind": "release-component",
                "component": "rpm/host-gcc-build",
                "scope": "build",
                "canonical_sha256": "a" * 64,
            },
            {
                "kind": "release-component",
                "component": "rpm/host-python-build",
                "scope": "build",
                "canonical_sha256": "a" * 64 + "\n",
            },
        )
        globals_ = INSTALLER["install"].__globals__
        original_inventory = globals_["rpm_inventory"]

        def forbidden_inventory():
            raise AssertionError("RPM inventory must not run")

        globals_["rpm_inventory"] = forbidden_inventory
        try:
            with tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / "marker.json"
                for invalid in invalid_values:
                    with self.subTest(component=invalid["component"]):
                        with self.assertRaises(INSTALLER["ValidationError"]):
                            INSTALLER["install"](
                                normalized,
                                {"kind": "rpm-lock"},
                                {"kind": "rpm-transaction"},
                                marker,
                                invalid,
                            )
                self.assertFalse(marker.exists())
        finally:
            globals_["rpm_inventory"] = original_inventory


if __name__ == "__main__":
    unittest.main()
