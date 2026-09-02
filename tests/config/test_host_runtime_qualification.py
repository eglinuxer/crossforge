import ast
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/qualify-host-runtime.py"
QUALIFIER = runpy.run_path(str(SCRIPT))


class HostRuntimeQualificationTests(unittest.TestCase):
    def fixture(self, directory):
        packages = ["base-0:1-1.x86_64", "tool-0:1-1.x86_64"]
        transaction = {
            "manifests": {
                "result": {
                    "packages": packages,
                    "canonical_sha256": QUALIFIER["canonical_sha256"](
                        packages
                    ),
                }
            }
        }
        transaction_path = directory / "transaction.json"
        transaction_path.write_text(
            json.dumps(transaction, sort_keys=True), encoding="utf-8"
        )
        lock = {"kind": "rpm-lock"}
        lock_path = directory / "lock.json"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        component = {
            "kind": "crossforge-release-component",
            "component": "rpm/host-runtime",
            "scope": "build",
        }
        component_path = directory / "component.json"
        component_path.write_text(json.dumps(component), encoding="utf-8")
        component_sha256 = QUALIFIER["canonical_sha256"](component)
        marker_directory = directory / "markers"
        marker_directory.mkdir()
        marker_path = marker_directory / "host-runtime.json"
        marker_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "kind": "host-rpm-install-marker",
                    "role": "host-runtime",
                    "lock_sha256": QUALIFIER["canonical_sha256"](lock),
                    "transaction_sha256": QUALIFIER["canonical_sha256"](
                        transaction
                    ),
                    "result_sha256": transaction["manifests"]["result"][
                        "canonical_sha256"
                    ],
                    "result_item_count": 2,
                    "release_binding": {
                        "kind": "release-component",
                        "component": "rpm/host-runtime",
                        "scope": "build",
                        "canonical_sha256": component_sha256,
                    },
                }
            ),
            encoding="utf-8",
        )
        return (
            packages,
            lock_path,
            transaction_path,
            marker_path,
            component_path,
            component_sha256,
        )

    def test_exact_rpm_marker_tools_and_smokes_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                packages,
                lock,
                transaction,
                marker,
                component,
                component_sha256,
            ) = self.fixture(Path(temporary))
            function = QUALIFIER["qualify"]
            with mock.patch.dict(
                function.__globals__,
                {
                    "rpm_inventory": lambda: list(packages),
                    "tool_evidence": lambda: {"gcc": {"path": "gcc"}},
                    "smoke_builds": lambda _work: {"native_c": "0" * 64},
                },
            ), mock.patch.dict(
                QUALIFIER["os"].environ,
                {
                    "PATH": "/opt/rh/gcc-toolset-15/root/usr/bin:/usr/bin"
                },
            ):
                report = function(
                    lock,
                    transaction,
                    marker,
                    component,
                    component_sha256,
                )
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["rpm"]["result_item_count"], 2)
            self.assertEqual(report["tools"], {"gcc": {"path": "gcc"}})
            self.assertEqual(report["smoke"], {"native_c": "0" * 64})

    def test_rpm_or_marker_inventory_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                packages,
                lock,
                transaction,
                marker,
                component,
                component_sha256,
            ) = self.fixture(Path(temporary))
            function = QUALIFIER["qualify"]
            with mock.patch.dict(
                function.__globals__,
                {
                    "rpm_inventory": lambda: packages[:-1],
                    "tool_evidence": lambda: {},
                    "smoke_builds": lambda _work: {},
                },
            ):
                with self.assertRaises(QUALIFIER["QualificationError"]):
                    function(
                        lock,
                        transaction,
                        marker,
                        component,
                        component_sha256,
                    )

            (marker.parent / "host-build-common.json").write_text(
                "{}", encoding="utf-8"
            )
            with mock.patch.dict(
                function.__globals__,
                {
                    "rpm_inventory": lambda: list(packages),
                    "tool_evidence": lambda: {},
                    "smoke_builds": lambda _work: {},
                },
            ):
                with self.assertRaisesRegex(
                    QUALIFIER["QualificationError"], "build-only host lock"
                ):
                    function(
                        lock,
                        transaction,
                        marker,
                        component,
                        component_sha256,
                    )

    def test_script_is_python36_syntax_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )

    def test_docker_qualification_is_offline_and_cache_only(self):
        dockerfile = (REPOSITORY / "docker/Dockerfile").read_text(
            encoding="utf-8"
        )
        block = dockerfile.split(
            "FROM host-runtime-locked AS host-runtime-qualified", 1
        )[1].split("\nFROM ", 1)[0]
        self.assertIn("RUN --network=none", block)
        self.assertIn("scripts/qualify-host-runtime.py", block)
        self.assertIn(
            "/opt/crossforge/qualification/host-runtime.json", block
        )
        bake = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        self.assertIn('target "host-runtime-qualified"', bake)
        self.assertIn('target   = "host-runtime-qualified"', bake)

        workflow = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(workflow.count("host-runtime-qualified"), 2)
        self.assertNotIn("phase4 host-runtime-locked", workflow)


if __name__ == "__main__":
    unittest.main()
