import ast
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
        cls.binding = json.loads(
            (REPOSITORY / "config/generated/release-binding.json").read_text(
                encoding="utf-8"
            )
        )
        cls.binding_records = {
            record["component"]: record
            for record in cls.binding["components"]
        }
        cls.component_arguments = {
            key: value
            for key, value in cls.targets["_common"]["args"].items()
            if key.startswith("CROSSFORGE_COMPONENT_")
        }
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
        row = "cp" + minor.replace(".", "")
        source_component = "python/%s-source" % row
        policy_component = "implementation/python-%s-build-policy" % row
        return {
            "CPYTHON_ROW": row,
            "CPYTHON_MINOR": minor,
            "CPYTHON_VERSION": entry["version"],
            "CPYTHON_ADAPTER": entry["adapter"],
            "CPYTHON_SOURCE_COMPONENT": source_component,
            "CPYTHON_SOURCE_COMPONENT_SHA256": self.binding_records[
                source_component
            ]["canonical_sha256"],
            "CPYTHON_BUILD_POLICY_COMPONENT": policy_component,
            "CPYTHON_BUILD_POLICY_COMPONENT_SHA256": self.binding_records[
                policy_component
            ]["canonical_sha256"],
        }

    def test_component_arguments_cover_the_complete_release_binding(self):
        records = self.binding["components"]
        self.assertEqual(len(records), 58)
        expected = {
            RENDERER["component_argument_name"](record["component"]): record[
                "canonical_sha256"
            ]
            for record in records
        }
        self.assertEqual(len(expected), len(records))
        self.assertEqual(self.component_arguments, expected)

    def test_component_argument_names_are_stable_unique_and_content_locked(self):
        self.assertEqual(
            RENDERER["component_argument_name"](
                "toolchain/x86_64-qualification"
            ),
            "CROSSFORGE_COMPONENT_TOOLCHAIN_X86_64_QUALIFICATION_SHA256",
        )
        names = list(self.component_arguments)
        self.assertEqual(len(names), len(set(names)))
        for name, digest in self.component_arguments.items():
            with self.subTest(name=name):
                self.assertRegex(
                    name,
                    r"^CROSSFORGE_COMPONENT_[A-Z0-9_]+_SHA256$",
                )
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_python_qualification_identities_enter_only_static_qualifiers(self):
        expected = {
            RENDERER["component_argument_name"](
                "implementation/python-qualification-policy"
            ): self.binding_records[
                "implementation/python-qualification-policy"
            ]["canonical_sha256"],
            RENDERER["component_argument_name"](
                "python/qualification"
            ): self.binding_records["python/qualification"][
                "canonical_sha256"
            ],
        }
        qualifier_names = set()
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            row = contract["row"]
            for arch in RENDERER["PYTHON_TARGETS"]:
                name = "cpython-%s-%s-qualify-build" % (row, arch)
                qualifier_names.add(name)
                target = self.targets[name]
                self.assertEqual(target["target"], "cpython-qualify-build")
                self.assertEqual(
                    {
                        key: target["args"][key]
                        for key in expected
                    },
                    expected,
                )

        for name, target in self.targets.items():
            if target.get("inherits") != ["_python_common"]:
                continue
            present = set(target.get("args", {})) & set(expected)
            with self.subTest(target=name):
                self.assertEqual(
                    present,
                    set(expected) if name in qualifier_names else set(),
                )

    def test_python_qualification_identity_arguments_fail_closed(self):
        for component in (
            "implementation/python-qualification-policy",
            "python/qualification",
        ):
            arguments = copy.deepcopy(self.component_arguments)
            del arguments[RENDERER["component_argument_name"](component)]
            with self.subTest(component=component):
                with self.assertRaisesRegex(
                    ValueError,
                    "missing Python qualification component digest",
                ):
                    RENDERER["render_python_graph"](
                        copy.deepcopy(self.release), {}, arguments
                    )

    def test_future_components_are_bound_without_a_global_release_argument(self):
        future = {
            record["component"]
            for record in self.binding["components"]
            if record["scope"] == "future"
        }
        self.assertTrue(future)
        for component in future:
            self.assertIn(
                RENDERER["component_argument_name"](component),
                self.component_arguments,
            )
        self.assertNotIn(
            "CROSSFORGE_RELEASE_SHA256",
            self.targets["_common"]["args"],
        )

    def test_python_only_change_preserves_rpm_and_toolchain_component_args(self):
        before = RENDERER["component_digest_arguments"](
            REPOSITORY,
            self.release,
            require_tracked=False,
        )
        release = copy.deepcopy(self.release)
        entry = next(
            item
            for item in release["python"]["versions"]
            if item["version"] == "3.12.14"
        )
        original = entry["source"]["sha256"]
        entry["source"]["sha256"] = (
            ("0" if original[0] != "0" else "1") + original[1:]
        )
        after = RENDERER["component_digest_arguments"](
            REPOSITORY,
            release,
            require_tracked=False,
        )
        protected_components = {
            record["component"]
            for record in self.binding["components"]
            if record["component"].startswith(
                ("rpm/", "sources/", "toolchain/")
            )
        }
        for component in protected_components:
            name = RENDERER["component_argument_name"](component)
            self.assertEqual(before[name], after[name], component)
        self.assertNotEqual(
            before[
                RENDERER["component_argument_name"]("python/cp312-source")
            ],
            after[
                RENDERER["component_argument_name"]("python/cp312-source")
            ],
        )

    def test_enabled_rows_take_exact_metadata_from_release(self):
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            minor = contract["minor"]
            expected = self.expected_row(minor)
            row = expected["CPYTHON_ROW"]
            for name in (
                "cpython-source-%s" % row,
                "cpython-prepared-%s" % row,
                "python-row-%s" % row,
                "python-%s-dev" % row,
            ):
                self.assertEqual(self.targets[name]["args"], expected)
            build_expected = dict(expected)
            build_expected["CPYTHON_ZSTD_VERSION"] = (
                self.release["python"]["zstd"]["version"]
                if contract["zstd"]
                else "none"
            )
            self.assertEqual(
                self.targets["cpython-build-%s" % row]["args"], build_expected
            )

    def test_python_zstd_contexts_are_row_and_arch_scoped(self):
        empty = self.targets["zstd-empty"]
        self.assertEqual(empty["target"], "zstd-empty")
        self.assertEqual(empty["inherits"], ["_python_common"])
        self.assertEqual(empty["output"], ["type=cacheonly"])
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            row = contract["row"]
            version = (
                self.release["python"]["zstd"]["version"]
                if contract["zstd"]
                else "none"
            )
            native = self.targets["cpython-build-%s" % row]
            self.assertEqual(native["args"]["CPYTHON_ZSTD_VERSION"], version)
            self.assertEqual(
                native["contexts"]["crossforge_zstd"],
                "target:zstd-host-build" if contract["zstd"] else "target:zstd-empty",
            )
            for arch in RENDERER["PYTHON_TARGETS"]:
                cross = self.targets["cpython-cross-%s-%s" % (row, arch)]
                self.assertEqual(cross["args"]["CPYTHON_ZSTD_VERSION"], version)
                self.assertEqual(
                    cross["contexts"]["crossforge_zstd"],
                    (
                        "target:zstd-%s-build" % arch
                        if contract["zstd"]
                        else "target:zstd-empty"
                    ),
                )

    def test_zstd_version_change_preserves_pre314_python_build_targets(self):
        before = {}
        RENDERER["render_python_graph"](
            copy.deepcopy(self.release), before, self.component_arguments
        )
        changed_release = copy.deepcopy(self.release)
        changed_release["python"]["zstd"]["version"] = "1.5.8"
        after = {}
        RENDERER["render_python_graph"](
            changed_release, after, self.component_arguments
        )
        for row in ("cp313", "cp311", "cp312", "cp310", "cp39"):
            self.assertEqual(
                before["cpython-build-%s" % row],
                after["cpython-build-%s" % row],
            )
            for arch in RENDERER["PYTHON_TARGETS"]:
                self.assertEqual(
                    before["cpython-cross-%s-%s" % (row, arch)],
                    after["cpython-cross-%s-%s" % (row, arch)],
                )

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

    def test_phase_snapshots_and_native_groups_have_fixed_membership(self):
        expected = {
            5: ("cp313",),
            6: ("cp313", "cp311"),
            7: ("cp313", "cp311", "cp312"),
            8: ("cp313", "cp311", "cp312", "cp314"),
            9: ("cp313", "cp311", "cp312", "cp314", "cp310"),
            10: (
                "cp313",
                "cp311",
                "cp312",
                "cp314",
                "cp310",
                "cp39",
            ),
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
                        "target:toolchain-%s-build-export" % arch,
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
        base = self.release["base_image"]
        self.assertEqual(
            source["contexts"],
            {
                "crossforge_rocky_amd64": "docker-image://%s:%s@%s"
                % (
                    base["repository"],
                    base["tag"],
                    base["manifests"]["amd64"],
                )
            },
        )
        prepared = self.targets["cpython-prepared-cp311"]
        self.assertEqual(
            prepared["contexts"],
            {
                "crossforge_host_python": "target:host-python-build-locked",
                "crossforge_cpython_source": "target:cpython-source-cp311",
                "crossforge_cpython_patches": "target:cpython-patches-cp311",
            },
        )
        source_block = self.python_dockerfile.split(
            "FROM crossforge_rocky_amd64 AS cpython-source", 1
        )[1].split(
            "FROM crossforge_rocky_amd64 AS cpython-empty-patches-build", 1
        )[0]
        self.assertNotIn("prepare-cpython-source.py", source_block)
        prepared_block = self.python_dockerfile.split(
            "FROM python-build-host AS cpython-prepared", 1
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

    def test_qemu_edge_exists_only_on_runtime_and_final_sdk_boundaries(self):
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
            }
            | {"python-dev"}
            | {
                "python-phase%d-dev" % phase
                for phase in range(5, RENDERER["LATEST_PHASE"] + 1)
            },
        )
        for name, target in self.targets.items():
            if target.get("target") == "cpython-cross":
                self.assertFalse(
                    any("qemu" in key for key in target.get("contexts", {})),
                    name,
                )
        cross_block = self.python_dockerfile.split(
            "FROM python-build-host AS cpython-cross", 1
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
        self.assertEqual(
            self.targets["python-dev"]["contexts"][
                "crossforge_qemu_validated"
            ],
            "target:qemu-aarch64-validated",
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
            RENDERER["render_python_graph"](config, {}, {})

    def test_renderer_is_python36_syntax_compatible(self):
        path = REPOSITORY / "scripts/render-bake.py"
        ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
