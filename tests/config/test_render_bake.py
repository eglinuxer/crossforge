import copy
import json
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


class RenderBakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        cls.document = json.loads(RENDERER["render"](REPOSITORY))
        cls.targets = cls.document["target"]
        cls.python_dockerfile = (
            REPOSITORY / "docker/python.Dockerfile"
        ).read_text(encoding="utf-8")

    def expected_row(self, minor):
        matches = [
            item
            for item in self.release["python"]["versions"]
            if item["version"].rsplit(".", 1)[0] == minor
        ]
        self.assertEqual(len(matches), 1)
        entry = matches[0]
        return {
            "CPYTHON_ROW": "cp" + minor.replace(".", ""),
            "CPYTHON_VERSION": entry["version"],
            "CPYTHON_ADAPTER": entry["adapter"],
        }

    def test_enabled_rows_take_exact_metadata_from_release(self):
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            minor = contract["minor"]
            expected = self.expected_row(minor)
            row = expected["CPYTHON_ROW"]
            for name in (
                "cpython-source-%s" % row,
                "cpython-prepared-%s" % row,
                "cpython-build-%s" % row,
                "python-row-%s" % row,
                "python-%s-dev" % row,
            ):
                self.assertEqual(self.targets[name]["args"], expected)

    def test_cp313_phase5_target_names_remain_available(self):
        expected = {
            "cpython-source-cp313",
            "cpython-prepared-cp313",
            "cpython-build-cp313",
            "cpython-cross-cp313-x86_64",
            "cpython-cross-cp313-aarch64",
            "cpython-cp313-x86_64-qualify-build",
            "cpython-cp313-aarch64-qualify-build",
            "cpython-cp313-x86_64-qualify",
            "cpython-cp313-aarch64-qualify",
            "python-cp313-dev",
        }
        self.assertTrue(expected.issubset(self.targets))
        phase5 = self.document["group"]["phase5"]["targets"]
        for name in (
            "cpython-build-cp313",
            "cpython-cp313-x86_64-qualify",
            "cpython-cp313-aarch64-qualify",
            "python-phase5-dev",
        ):
            self.assertIn(name, phase5)

    def test_phase_snapshots_and_native_groups_are_frozen_by_contract(self):
        expected = {
            5: ("cp313",),
            6: ("cp313", "cp311"),
            7: ("cp313", "cp311", "cp312"),
        }
        for phase, rows in expected.items():
            with self.subTest(phase=phase):
                self.assertEqual(RENDERER["rows_for_phase"](phase), rows)
                native = self.document["group"][
                    "python-native-phase%d" % phase
                ]["targets"]
                self.assertEqual(
                    native,
                    ["cpython-build-%s" % row for row in rows],
                )
                snapshot = self.targets["python-phase%d-dev" % phase]
                self.assertEqual(snapshot["target"], "python-sdk-final")
                self.assertEqual(
                    snapshot["args"]["CROSSFORGE_PYTHON_ROWS"],
                    " ".join(rows),
                )
                self.assertEqual(
                    snapshot["contexts"]["crossforge_sdk_base"],
                    "target:python-dev-append-%s" % rows[-1],
                )
                phase_targets = self.document["group"]["phase%d" % phase][
                    "targets"
                ]
                for row in rows:
                    for arch in RENDERER["PYTHON_TARGETS"]:
                        self.assertIn(
                            "cpython-%s-%s-qualify" % (row, arch),
                            phase_targets,
                        )
                self.assertIn("python-phase%d-dev" % phase, phase_targets)

        latest_phase = max(
            row["introduced_phase"] for row in RENDERER["IMPLEMENTED_ROWS"]
        )
        latest_rows = tuple(
            row["row"] for row in RENDERER["IMPLEMENTED_ROWS"]
        )
        self.assertEqual(RENDERER["LATEST_PHASE"], latest_phase)
        self.assertEqual(
            self.document["group"]["python-native-latest"],
            {
                "targets": [
                    "cpython-build-%s" % row for row in latest_rows
                ]
            },
        )
        self.assertEqual(
            self.document["group"]["python-matrix"],
            {"targets": ["python-dev"]},
        )
        self.assertEqual(
            self.targets["python-dev"]["args"]["CROSSFORGE_PYTHON_ROWS"],
            " ".join(latest_rows),
        )
        self.assertEqual(
            [row["introduced_phase"] for row in RENDERER["IMPLEMENTED_ROWS"]],
            sorted(
                row["introduced_phase"]
                for row in RENDERER["IMPLEMENTED_ROWS"]
            ),
        )

    def test_all_rows_have_independent_cross_and_qualification_edges(self):
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            row = contract["row"]
            for arch, triple in RENDERER["PYTHON_TARGETS"].items():
                with self.subTest(row=row, arch=arch):
                    cross = self.targets["cpython-cross-%s-%s" % (row, arch)]
                    self.assertEqual(cross["target"], "cpython-cross")
                    self.assertEqual(
                        cross["contexts"]["crossforge_toolchain"],
                        "target:toolchain-%s-dev" % arch,
                    )
                    self.assertEqual(
                        cross["contexts"]["crossforge_cpython_prepared"],
                        "target:cpython-prepared-%s" % row,
                    )
                    self.assertEqual(
                        cross["contexts"]["crossforge_cpython_build"],
                        "target:cpython-build-%s" % row,
                    )
                    self.assertEqual(
                        cross["args"]["CROSSFORGE_TARGET_TRIPLE"], triple
                    )
                    self.assertEqual(
                        cross["args"]["CPYTHON_ADAPTER"], contract["adapter"]
                    )
                    qualify = self.targets[
                        "cpython-%s-%s-qualify" % (row, arch)
                    ]
                    self.assertEqual(
                        qualify["target"], "cpython-qualify-%s" % arch
                    )
                    self.assertEqual(
                        qualify["contexts"]["crossforge_cpython_qualify_build"],
                        "target:cpython-%s-%s-qualify-build" % (row, arch),
                    )

    def test_source_fetch_is_independent_and_prepare_uses_locked_host(self):
        source = self.targets["cpython-source-cp311"]
        self.assertEqual(source["contexts"], {"crossforge_config": "target:validate"})
        prepared = self.targets["cpython-prepared-cp311"]
        self.assertEqual(
            prepared["contexts"],
            {
                "crossforge_host_python": "target:host-python-build-locked",
                "crossforge_cpython_source": "target:cpython-source-cp311",
            },
        )
        source_block = self.python_dockerfile.split(
            "FROM crossforge_config AS cpython-source", 1
        )[1].split("FROM crossforge_host_python AS python-host", 1)[0]
        self.assertNotIn("prepare-cpython-source.py", source_block)
        prepared_block = self.python_dockerfile.split(
            "FROM python-host AS cpython-prepared", 1
        )[1].split("FROM crossforge_cpython_prepared AS cpython-build", 1)[0]
        self.assertIn("command -v patch", prepared_block)
        self.assertIn("--network=none", prepared_block)

    def test_all_generated_python_outputs_are_cache_only(self):
        python_names = {
            name
            for name, value in self.targets.items()
            if value.get("inherits") == ["_python_common"]
        }
        self.assertIn("python-dev", python_names)
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            self.assertIn("python-%s-dev" % contract["row"], python_names)
        for name in python_names:
            self.assertEqual(
                self.targets[name].get("output"),
                ["type=cacheonly"],
                name,
            )

    def test_qemu_edge_exists_only_on_aarch64_runtime_qualification(self):
        self.assertNotIn(
            "crossforge_qemu", self.targets["_common"].get("contexts", {})
        )
        self.assertEqual(
            {
                name
                for name, target in self.targets.items()
                if "crossforge_qemu" in target.get("contexts", {})
            },
            {
                "qemu-aarch64-validated",
                "runtime-smoke-aarch64",
                "toolchain-aarch64-dev",
            },
        )
        consumers = []
        for name, target in self.targets.items():
            contexts = target.get("contexts", {})
            if "crossforge_qemu_validated" in contexts:
                consumers.append(name)
        self.assertEqual(
            set(consumers),
            {
                "cpython-%s-aarch64-qualify" % contract["row"]
                for contract in RENDERER["IMPLEMENTED_ROWS"]
            },
        )
        for name, target in self.targets.items():
            if target.get("target") == "cpython-cross":
                self.assertFalse(
                    any("qemu" in key for key in target.get("contexts", {})),
                    name,
                )
        cross_block = self.python_dockerfile.split(
            "FROM python-host AS cpython-cross", 1
        )[1].split("FROM crossforge_cpython_cross AS cpython-qualify-build", 1)[0]
        self.assertNotIn("crossforge_qemu", cross_block)
        self.assertNotIn("qemu-aarch64", cross_block)
        self.assertNotIn("HOSTRUNNER", cross_block)
        self.assertIn("from=crossforge_cpython_prepared", cross_block)

    def test_runtime_qualification_mounts_real_shm(self):
        mount = "--mount=type=tmpfs,target=/runtime-%s/dev/shm"
        for tier in ("locked", "clean"):
            self.assertEqual(self.python_dockerfile.count(mount % tier), 2)

    def test_row_exports_are_scratch_and_aggregate_is_append_only(self):
        self.assertIn("FROM scratch AS cpython-row-export", self.python_dockerfile)
        rows = tuple(
            contract["row"] for contract in RENDERER["IMPLEMENTED_ROWS"]
        )
        for row in rows:
            target = self.targets["python-row-%s" % row]
            self.assertEqual(target["target"], "cpython-row-export")
            self.assertEqual(
                set(target["contexts"]),
                {
                    "crossforge_host_python",
                    "crossforge_cpython_build",
                    "crossforge_cpython_x86_64",
                    "crossforge_cpython_aarch64",
                },
            )
        previous = "sdk-toolchains-dev"
        for row in rows:
            self.assertEqual(
                self.targets["python-dev-append-%s" % row]["contexts"][
                    "crossforge_sdk_base"
                ],
                "target:%s" % previous,
            )
            previous = "python-dev-append-%s" % row
        self.assertEqual(
            self.targets["python-dev"]["contexts"]["crossforge_sdk_base"],
            "target:%s" % previous,
        )

    def test_generated_target_context_graph_is_acyclic(self):
        edges = {}
        for name, target in self.targets.items():
            edges[name] = {
                value.removeprefix("target:")
                for value in target.get("contexts", {}).values()
                if value.startswith("target:") and value.removeprefix("target:") in self.targets
            }
        visiting = set()
        visited = set()

        def visit(name):
            self.assertNotIn(name, visiting, "Bake target-context cycle at %s" % name)
            if name in visited:
                return
            visiting.add(name)
            for dependency in edges[name]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in edges:
            visit(name)

    def test_renderer_rejects_a_duplicate_enabled_minor(self):
        config = copy.deepcopy(self.release)
        duplicate_minor = RENDERER["IMPLEMENTED_ROWS"][0]["minor"]
        duplicate = next(
            item
            for item in config["python"]["versions"]
            if item["version"].rsplit(".", 1)[0] == duplicate_minor
        )
        config["python"]["versions"].append(copy.deepcopy(duplicate))
        with self.assertRaises(ValueError):
            RENDERER["render_python_graph"](config, {})


if __name__ == "__main__":
    unittest.main()
