import ast
import hashlib
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/qualify-final-sdk.py"
QUALIFIER = runpy.run_path(str(SCRIPT))
BAKE = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


class FinalSdkQualificationTests(unittest.TestCase):
    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_only_exact_historical_phase_rows_are_accepted(self):
        contract = QUALIFIER["ROW_CONTRACT"]
        for phase in range(5, contract["LATEST_PHASE"] + 1):
            rows = list(contract["rows_for_phase"](phase))
            self.assertEqual(QUALIFIER["phase_for_rows"](rows), phase)
        for invalid in (
            ["cp313", "cp312"],
            ["cp313", "cp313"],
            list(reversed(contract["LATEST_ROWS"])),
            ["cp999"],
        ):
            with self.subTest(rows=invalid):
                with self.assertRaises(QUALIFIER["QualificationError"]):
                    QUALIFIER["phase_for_rows"](invalid)

    def test_final_sdk_uses_qualified_runtime_and_copy_only_qemu(self):
        release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        rendered = json.loads(BAKE["render"](REPOSITORY))
        sdk = rendered["target"]["sdk-toolchains-dev"]["contexts"]
        self.assertEqual(
            sdk["crossforge_host_runtime"],
            "target:host-runtime-qualified",
        )
        self.assertNotIn("crossforge_host_python", sdk)
        finals = ["python-dev"] + [
            "python-phase%d-dev" % phase
            for phase in range(5, 11)
        ]
        for name in finals:
            self.assertEqual(
                rendered["target"][name]["contexts"][
                    "crossforge_qemu_validated"
                ],
                "target:qemu-aarch64-validated",
            )
        self.assertEqual(release["qemu"]["version"], "10.2.3")

    def test_final_stage_qualifies_then_removes_every_staging_root(self):
        dockerfile = (REPOSITORY / "docker/python.Dockerfile").read_text(
            encoding="utf-8"
        )
        block = dockerfile.split(
            "FROM crossforge_sdk_base AS python-sdk-final", 1
        )[1]
        self.assertIn("COPY --from=crossforge_qemu_validated", block)
        self.assertIn("/opt/crossforge/qualification/toolchain", dockerfile)
        self.assertIn("scripts/qualify-final-sdk.py", block)
        self.assertIn("scripts/release-components-core.py", block)
        self.assertNotIn("scripts/render-release-components.py", block)
        self.assertIn("/opt/crossforge/qualification/final-sdk.json", block)
        self.assertIn("RUN --network=none", block)
        self.assertLess(
            block.index("/work/scripts/qualify-final-sdk.py"),
            block.index("rm -rf /src /work"),
        )
        for path in (
            "/src",
            "/work",
            "/sources",
            "/out",
            "/resolved",
            "/row-export",
            "/runtime-root",
            "/runtime-locked",
            "/runtime-clean",
            "/sysroot",
            "/rpm-bundle",
            "/plans",
        ):
            self.assertIn(path, block)
        self.assertIn("WORKDIR /workspace", block)

    def test_script_is_python36_syntax_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--qemu-sha256", source)
        self.assertNotIn("--qemu-version", source)
        self.assertIn('executor = release["qemu"]["executor"]', source)

    def test_dynamic_elf_policy_rejects_paths_and_search_tags(self):
        function = QUALIFIER["audit_dynamic_elf"]

        def invoke(dynamic, headers=""):
            outputs = iter(((dynamic, ""), (headers, "")))
            with mock.patch.dict(
                function.__globals__, {"run": lambda _arguments: next(outputs)}
            ):
                return function("readelf", Path("python"), None)

        self.assertEqual(
            invoke(" 0x1 (NEEDED) Shared library: [libc.so.6]\n"),
            {"interpreter": None, "needed": ["libc.so.6"]},
        )
        for dynamic in (
            " 0x1 (RPATH) Library rpath: [/work/lib]\n",
            " 0x1 (RUNPATH) Library runpath: [/src/lib]\n",
            " 0x1 (TEXTREL) 0x0\n",
            " 0x1 (NEEDED) Shared library: [/work/libbad.so]\n",
        ):
            with self.subTest(dynamic=dynamic):
                with self.assertRaises(QUALIFIER["QualificationError"]):
                    invoke(dynamic)

    def test_target_python_trees_and_reports_are_manifest_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row_prefix = root / "python/cp313"
            reports = root / "qualification/cp313"
            release_sha256 = "a" * 64
            qualifications = {}
            target_paths = []
            for arch, profile in QUALIFIER["TARGETS"].items():
                prefix = row_prefix / "targets" / profile["triple"]
                python = prefix / "bin/python3.13"
                python.parent.mkdir(parents=True)
                python.write_bytes(("python-" + arch).encode("ascii"))
                target_paths.append(python)
                tree = QUALIFIER["SDK_IDENTITY"]["sdk_tree_identity"](prefix)
                python_sha256 = hashlib.sha256(python.read_bytes()).hexdigest()
                report = {
                    "qualification_schema_version": 4,
                    "report_kind": "crossforge-cpython-qualification",
                    "status": "passed",
                    "target": profile["triple"],
                    "version": "3.13.15",
                    "release_sha256": release_sha256,
                    "python_sha256": python_sha256,
                    "compile": {"sdk_tree": tree},
                }
                report_path = reports / (arch + ".json")
                self.write_json(report_path, report)
                qualifications[arch] = {
                    "target": profile["triple"],
                    "report_sha256": QUALIFIER["sha256_file"](report_path),
                    "python_sha256": python_sha256,
                    "sdk_tree": tree,
                }
            manifest = {"qualifications": qualifications}
            function = QUALIFIER["qualify_target_pythons"]
            result = function(
                row_prefix,
                reports,
                "cp313",
                "3.13.15",
                manifest,
                release_sha256,
            )
            self.assertEqual([item["arch"] for item in result], ["x86_64", "aarch64"])

            target_paths[0].write_bytes(b"tampered")
            with self.assertRaisesRegex(
                QUALIFIER["QualificationError"], "target SDK differs"
            ):
                function(
                    row_prefix,
                    reports,
                    "cp313",
                    "3.13.15",
                    manifest,
                    release_sha256,
                )

            target_paths[0].write_bytes(b"python-x86_64")
            report_path = reports / "x86_64.json"
            report_path.write_text(
                report_path.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                QUALIFIER["QualificationError"], "target SDK differs"
            ):
                function(
                    row_prefix,
                    reports,
                    "cp313",
                    "3.13.15",
                    manifest,
                    release_sha256,
                )

    def test_release_target_map_is_exact(self):
        release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        target_map = QUALIFIER["release_target_map"](release)
        self.assertEqual(set(target_map), {"x86_64", "aarch64"})
        release["targets"].reverse()
        with self.assertRaises(QUALIFIER["QualificationError"]):
            QUALIFIER["release_target_map"](release)

    def test_host_runtime_report_rebinds_rpmdb_marker_and_release(self):
        release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        components = QUALIFIER["RELEASE_COMPONENTS"][
            "render_component_documents"
        ](release)
        binding = {
            "kind": "release-component",
            "component": "rpm/host-runtime",
            "scope": "build",
            "canonical_sha256": QUALIFIER["canonical_sha256"](
                components["rpm/host-runtime"]
            ),
        }
        packages = ["base-0:1-1.x86_64"]
        result_sha256 = QUALIFIER["canonical_sha256"](packages)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            marker = directory / "markers/host-runtime.json"
            marker.parent.mkdir()
            marker_value = {
                "schema_version": 2,
                "kind": "host-rpm-install-marker",
                "role": "host-runtime",
                "lock_sha256": release["host_locks"]["host-runtime"][
                    "canonical_sha256"
                ],
                "transaction_sha256": "b" * 64,
                "result_sha256": result_sha256,
                "result_item_count": 1,
                "release_binding": binding,
            }
            self.write_json(marker, marker_value)
            report = directory / "report.json"
            self.write_json(
                report,
                {
                    "kind": "crossforge-host-runtime-qualification",
                    "status": "passed",
                    "rpm": {
                        "lock_sha256": marker_value["lock_sha256"],
                        "transaction_sha256": marker_value[
                            "transaction_sha256"
                        ],
                        "result_sha256": result_sha256,
                        "result_item_count": 1,
                        "marker_sha256": QUALIFIER["sha256_file"](marker),
                        "release_binding": binding,
                    },
                },
            )
            function = QUALIFIER["qualify_host_runtime"]
            with mock.patch.dict(
                function.__globals__,
                {"rpm_inventory": lambda: list(packages)},
            ):
                evidence = function(release, report, marker)
            self.assertEqual(evidence["release_binding"], binding)

            marker_value["release_binding"]["canonical_sha256"] = "0" * 64
            self.write_json(marker, marker_value)
            with mock.patch.dict(
                function.__globals__,
                {"rpm_inventory": lambda: list(packages)},
            ):
                with self.assertRaises(QUALIFIER["QualificationError"]):
                    function(release, report, marker)


if __name__ == "__main__":
    unittest.main()
