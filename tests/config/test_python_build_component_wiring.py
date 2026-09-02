import json
import re
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


def stage_blocks(dockerfile):
    matches = list(
        re.finditer(
            r"^FROM\s+[^\n]+\s+AS\s+([a-z0-9_-]+)\s*$",
            dockerfile,
            re.MULTILINE,
        )
    )
    result = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(
            dockerfile
        )
        result[match.group(1)] = dockerfile[match.start():end]
    return result


class PythonBuildComponentWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        cls.binding = json.loads(
            (REPOSITORY / "config/generated/release-binding.json").read_text(
                encoding="utf-8"
            )
        )
        cls.records = {
            record["component"]: record
            for record in cls.binding["components"]
        }
        cls.bake = json.loads(RENDERER["render"](REPOSITORY))
        cls.targets = cls.bake["target"]
        cls.dockerfile = (REPOSITORY / "docker/python.Dockerfile").read_text(
            encoding="utf-8"
        )
        cls.stages = stage_blocks(cls.dockerfile)

    def test_build_host_is_separate_from_the_full_release_host(self):
        build_host = self.stages["python-build-host"]
        self.assertIn(
            "FROM crossforge_host_python AS python-build-host", build_host
        )
        self.assertNotIn("COPY ", build_host)
        for forbidden in (
            "release.json",
            "release.schema.json",
            "finalize",
            "render-release-components.py",
            "python_source_release_binding.py",
        ):
            self.assertNotIn(forbidden, build_host)
        full_host = self.stages["python-host"]
        self.assertIn("FROM python-build-host AS python-host", full_host)
        self.assertIn("config/release.json", full_host)
        self.assertIn("config/schemas/release.schema.json", full_host)
        self.assertIn("python_source_release_binding.py", full_host)
        self.assertIn("render-release-components.py", full_host)
        self.assertIn("python_zstd_evidence.py", full_host)

    def test_prepared_native_and_cross_are_component_only_build_stages(self):
        expected_parents = {
            "cpython-prepared": "FROM python-build-host AS cpython-prepared",
            "cpython-build": "FROM crossforge_cpython_prepared AS cpython-build",
            "cpython-cross": "FROM python-build-host AS cpython-cross",
        }
        for stage, parent in expected_parents.items():
            with self.subTest(stage=stage):
                block = self.stages[stage]
                self.assertIn(parent, block)
                for forbidden in (
                    "config/release.json",
                    "release.schema.json",
                    "--release ",
                    "finalize-cpython-qualification.py",
                    "finalize-python-row.py",
                    "python_source_release_binding.py",
                    "render-release-components.py",
                    "python_row_contract.py",
                ):
                    self.assertNotIn(forbidden, block)
                self.assertIn("--source-component", block)
                self.assertIn("--source-component-sha256", block)
                self.assertIn("--policy-component", block)
                self.assertIn("--policy-component-sha256", block)
                self.assertIn("--manifest /work/source/source-manifest.json", block)
        prepared = self.stages["cpython-prepared"]
        self.assertIn(
            "COPY --from=crossforge_cpython_source \\\n  /work/config/python-source-component.json",
            prepared,
        )
        self.assertIn(
            "config/generated/components/implementation/"
            "python-${CPYTHON_ROW}-build-policy.json",
            prepared,
        )
        self.assertGreaterEqual(prepared.count("verify-python-row.py \\"), 2)
        self.assertIn("prepare-cpython-source.py", prepared)

    def test_each_row_carries_exact_source_and_build_policy_identities(self):
        versions = {
            entry["version"].rsplit(".", 1)[0]: entry
            for entry in self.release["python"]["versions"]
        }
        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            row = contract["row"]
            source = "python/%s-source" % row
            policy = "implementation/python-%s-build-policy" % row
            expected = {
                "CPYTHON_SOURCE_COMPONENT": source,
                "CPYTHON_SOURCE_COMPONENT_SHA256": self.records[source][
                    "canonical_sha256"
                ],
                "CPYTHON_BUILD_POLICY_COMPONENT": policy,
                "CPYTHON_BUILD_POLICY_COMPONENT_SHA256": self.records[policy][
                    "canonical_sha256"
                ],
            }
            entry = versions[contract["minor"]]
            for name, target in self.targets.items():
                arguments = target.get("args", {})
                if arguments.get("CPYTHON_ROW") != row:
                    continue
                with self.subTest(row=row, target=name):
                    for key, value in expected.items():
                        self.assertEqual(arguments[key], value)
                    self.assertEqual(arguments["CPYTHON_MINOR"], contract["minor"])
            self.assertEqual(
                bool(entry["patches"]),
                row in ("cp39", "cp310", "cp311", "cp312"),
            )

    def test_patch_named_contexts_are_minor_scoped_or_controlled_empty(self):
        expected = {
            "cp39": "target:cpython-patches-cp39",
            "cp310": "target:cpython-patches-cp310",
            "cp311": "target:cpython-patches-cp311",
            "cp312": "target:cpython-patches-cp312",
            "cp313": "target:cpython-empty-patches",
            "cp314": "target:cpython-empty-patches",
        }
        for row, context in expected.items():
            prepared = self.targets["cpython-prepared-%s" % row]
            self.assertEqual(
                prepared["contexts"]["crossforge_cpython_patches"],
                context,
            )
        for row, minor in (
            ("cp39", "3.9"),
            ("cp310", "3.10"),
            ("cp311", "3.11"),
            ("cp312", "3.12"),
        ):
            target = self.targets["cpython-patches-%s" % row]
            self.assertEqual(target["target"], "cpython-patch-context")
            self.assertEqual(
                target["contexts"],
                {"crossforge_cpython_patch_files": "patches/cpython/%s" % minor},
            )
        empty = self.targets["cpython-empty-patches"]
        self.assertEqual(empty["target"], "cpython-empty-patches")
        self.assertIn("crossforge_rocky_amd64", empty["contexts"])
        prepared_block = self.stages["cpython-prepared"]
        self.assertNotIn("COPY patches/", prepared_block)
        self.assertIn(
            "COPY --from=crossforge_cpython_patches /row-patches/",
            prepared_block,
        )

    def test_zstd_inputs_are_versioned_and_only_cp314_uses_real_slices(self):
        empty = self.stages["zstd-empty"]
        self.assertIn("FROM scratch AS zstd-empty", empty)
        build = self.stages["cpython-build"]
        cross = self.stages["cpython-cross"]
        self.assertIn(
            "/opt/crossforge/deps/zstd/${CPYTHON_ZSTD_VERSION}/host/",
            build,
        )
        self.assertIn(
            "/opt/crossforge/deps/zstd/${CPYTHON_ZSTD_VERSION}/${CROSSFORGE_TARGET_TRIPLE}/",
            cross,
        )
        for block in (build, cross):
            self.assertEqual(block.count("COPY --from=crossforge_zstd"), 1)
            self.assertIn("/work/deps/zstd", block)
            self.assertNotIn("/opt/crossforge/deps/zstd/ /work", block)

        for contract in RENDERER["IMPLEMENTED_ROWS"]:
            row = contract["row"]
            native = self.targets["cpython-build-%s" % row]
            expected_native = (
                "target:zstd-host-build" if contract["zstd"] else "target:zstd-empty"
            )
            self.assertEqual(
                native["contexts"]["crossforge_zstd"], expected_native
            )
            for arch in RENDERER["PYTHON_TARGETS"]:
                cross_target = self.targets[
                    "cpython-cross-%s-%s" % (row, arch)
                ]
                expected_cross = (
                    "target:zstd-%s-build" % arch
                    if contract["zstd"]
                    else "target:zstd-empty"
                )
                self.assertEqual(
                    cross_target["contexts"]["crossforge_zstd"], expected_cross
                )

    def test_full_release_bridge_enters_only_at_late_boundaries(self):
        stages_with_release_copy = {
            name
            for name, block in self.stages.items()
            if "COPY config/release.json" in block
        }
        self.assertEqual(
            stages_with_release_copy,
            {"python-host", "cpython-qualify-build", "python-sdk-append"},
        )
        qualify = self.stages["cpython-qualify-build"]
        self.assertIn("config/schemas/release.schema.json", qualify)
        self.assertIn("python_source_release_binding.py", qualify)
        self.assertIn("render-release-components.py", qualify)
        self.assertIn("python_row_contract.py", qualify)
        self.assertIn("python_zstd_evidence.py", qualify)
        self.assertIn("validate-release.py", qualify)
        self.assertIn("--manifest /work/source/source-manifest.json", qualify)
        self.assertLess(
            qualify.index(
                "RUN /usr/libexec/platform-python /work/scripts/validate-release.py"
            ),
            qualify.index(
                "&& /usr/libexec/platform-python /work/scripts/verify-python-row.py"
            ),
        )
        append = self.stages["python-sdk-append"]
        self.assertIn("config/schemas/release.schema.json", append)
        self.assertIn("python_source_release_binding.py", append)
        self.assertIn("render-release-components.py", append)
        self.assertIn("/work/scripts/validate-release.py", append)
        for stage in (
            "cpython-runtime-input",
            "cpython-row-assemble",
            "sdk-toolchains-dev",
        ):
            self.assertIn("FROM python-host AS %s" % stage, self.stages[stage])

    def test_static_qualifier_binds_both_qualification_components(self):
        qualify = self.stages["cpython-qualify-build"]
        arguments = {
            "CROSSFORGE_COMPONENT_IMPLEMENTATION_PYTHON_QUALIFICATION_POLICY_SHA256": (
                "--qualification-policy-component-sha256"
            ),
            "CROSSFORGE_COMPONENT_PYTHON_QUALIFICATION_SHA256": (
                "--qualification-component-sha256"
            ),
        }
        for argument, option in arguments.items():
            with self.subTest(argument=argument):
                self.assertEqual(qualify.count("ARG " + argument), 1)
                self.assertEqual(qualify.count(option), 1)
                self.assertIn('"$%s"' % argument, qualify)

        for stage in (
            "cpython-source",
            "cpython-prepared",
            "cpython-build",
            "cpython-cross",
        ):
            with self.subTest(stage=stage):
                block = self.stages[stage]
                for argument in arguments:
                    self.assertNotIn(argument, block)

    def test_cross_stage_copies_the_legacy_sysconfig_preflight(self):
        cross = self.stages["cpython-cross"]
        self.assertIn("scripts/verify-python-build-sysconfig.py", cross)
        self.assertIn("scripts/build-cpython-cross.sh", cross)


if __name__ == "__main__":
    unittest.main()
