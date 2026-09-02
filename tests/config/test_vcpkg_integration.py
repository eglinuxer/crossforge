import ast
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPOSITORY / "scripts/render-vcpkg-integration.py"
GENERATOR = runpy.run_path(str(GENERATOR_PATH))
BAKE = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))
QUALIFIER_PATH = REPOSITORY / "scripts/qualify-vcpkg-sdk.py"
QUALIFIER = runpy.run_path(str(QUALIFIER_PATH))
COMPONENTS = runpy.run_path(
    str(REPOSITORY / "scripts/render-release-components.py")
)


class VcpkgIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outputs = GENERATOR["render"](REPOSITORY)
        release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        cls.components = COMPONENTS["render_component_documents"](release)

    def test_generated_file_set_and_content_are_current(self):
        expected = {
            "integration/cmake/host-gts15.cmake",
            "integration/cmake/x86_64-unknown-linux-gnu.cmake",
            "integration/cmake/aarch64-unknown-linux-gnu.cmake",
            "integration/vcpkg/triplets/crossforge-host-x64-el8.cmake",
            "integration/vcpkg/triplets/crossforge-x64-el8.cmake",
            "integration/vcpkg/triplets/crossforge-x64-el8-dynamic.cmake",
            "integration/vcpkg/triplets/crossforge-arm64-el8.cmake",
            "integration/vcpkg/triplets/crossforge-arm64-el8-dynamic.cmake",
            "integration/vcpkg/manifest.json",
        }
        self.assertEqual(
            {path.as_posix() for path in self.outputs}, expected
        )
        self.assertEqual(GENERATOR["output_drift"](REPOSITORY, self.outputs), [])

    def test_component_graph_is_isolated_and_complete(self):
        policy = self.components["implementation/vcpkg-integration"]
        self.assertEqual(policy["scope"], "build")
        self.assertEqual(policy["dependencies"], [])
        self.assertTrue(
            all(
                material["path"].startswith("/@implementation/vcpkg/")
                for material in policy["materials"]
            )
        )

    def test_integration_manifest_binds_all_generated_files(self):
        manifest = json.loads(
            self.outputs[GENERATOR["MANIFEST_PATH"]]
        )
        self.assertEqual(manifest["kind"], "crossforge-vcpkg-integration")
        self.assertEqual(len(manifest["files"]), 8)
        self.assertEqual(
            QUALIFIER["flatten_policy"](manifest["policy"]),
            QUALIFIER["material_map"](
                self.components["implementation/vcpkg-integration"]
            ),
        )
        sdk = self.components["vcpkg/sdk-build"]
        self.assertEqual(sdk["scope"], "build")
        self.assertEqual(
            {item["component"] for item in sdk["dependencies"]},
            {
                "rpm/host-runtime",
                "sources/vcpkg",
                "host-tools/ninja",
                "implementation/vcpkg-integration",
                "toolchain/x86_64-build",
                "toolchain/aarch64-build",
            },
        )

    def test_generator_rejects_every_untracked_integration_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            for relative, content in self.outputs.items():
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            unexpected = repository / GENERATOR["TOOLCHAIN_ROOT"] / ".leak"
            unexpected.write_text("untracked\n", encoding="utf-8")
            self.assertEqual(
                GENERATOR["output_drift"](repository, self.outputs),
                ["unexpected integration/cmake/.leak"],
            )

    def test_target_toolchains_are_absolute_true_cross_and_emulator_free(self):
        for arch, triple, processor in (
            ("x86_64", "x86_64-unknown-linux-gnu", "x86_64"),
            ("aarch64", "aarch64-unknown-linux-gnu", "aarch64"),
        ):
            source = self.outputs[
                GENERATOR["TOOLCHAIN_ROOT"] / (triple + ".cmake")
            ]
            self.assertIn('set(CMAKE_SYSTEM_NAME "Linux"', source)
            self.assertNotIn("set(CMAKE_CROSSCOMPILING", source)
            self.assertIn(
                'set(CMAKE_SYSTEM_PROCESSOR "%s"' % processor, source
            )
            self.assertIn(
                "/opt/crossforge/sysroots/el8/%s" % arch, source
            )
            self.assertIn(
                "/opt/crossforge/targets/%s/bin/%s-gcc"
                % (triple, triple),
                source,
            )
            self.assertIn("CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER", source)
            self.assertIn("CMAKE_CROSSCOMPILING_EMULATOR", source)
            self.assertIn("HOSTRUNNER", source)
            self.assertNotIn("qemu", source.lower())

    def test_host_toolchain_does_not_force_cmake_into_cross_mode(self):
        source = self.outputs[
            GENERATOR["TOOLCHAIN_ROOT"] / "host-gts15.cmake"
        ]
        self.assertNotIn("CMAKE_SYSTEM_NAME", source)
        self.assertNotIn("CMAKE_SYSTEM_PROCESSOR", source)
        self.assertNotIn("CMAKE_CROSSCOMPILING", source)
        self.assertIn(
            "/opt/rh/gcc-toolset-15/root/usr/bin/gcc", source
        )

    def test_host_and_target_triplets_are_distinct_and_linkage_is_explicit(self):
        triplets = {
            path.stem: content
            for path, content in self.outputs.items()
            if path.parent == GENERATOR["TRIPLET_ROOT"]
            and path.suffix == ".cmake"
        }
        self.assertEqual(
            set(triplets),
            {
                "crossforge-host-x64-el8",
                "crossforge-x64-el8",
                "crossforge-x64-el8-dynamic",
                "crossforge-arm64-el8",
                "crossforge-arm64-el8-dynamic",
            },
        )
        self.assertIn(
            "set(VCPKG_LIBRARY_LINKAGE static)",
            triplets["crossforge-host-x64-el8"],
        )
        for name, content in triplets.items():
            expected = "dynamic" if name.endswith("-dynamic") else "static"
            self.assertIn(
                "set(VCPKG_LIBRARY_LINKAGE %s)" % expected, content
            )
            self.assertIn('set(VCPKG_C_FLAGS "-fPIC")', content)
            self.assertIn("vcpkg/sdk-build", content)
        self.assertNotEqual(
            "crossforge-host-x64-el8", "crossforge-x64-el8"
        )

    def test_generator_is_python36_syntax_compatible(self):
        for path in (GENERATOR_PATH, QUALIFIER_PATH):
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 6),
            )

    def test_sdk_bake_target_assembles_without_changing_phase10(self):
        rendered = json.loads(BAKE["render"](REPOSITORY))
        target = rendered["target"]["sdk-phase13-base"]
        self.assertEqual(target["inherits"], ["_vcpkg_common"])
        self.assertEqual(target["target"], "vcpkg-sdk-base")
        self.assertEqual(
            target["contexts"],
            {
                "crossforge_ninja_host_tool": "target:ninja-host-tool",
                "crossforge_sdk_base": "target:python-phase10-dev",
                "crossforge_vcpkg_source": "target:vcpkg-source",
            },
        )
        self.assertEqual(target["output"], ["type=cacheonly"])
        self.assertNotIn(
            "crossforge_vcpkg_source",
            rendered["target"]["python-phase10-dev"]["contexts"],
        )

    def test_sdk_stage_is_offline_and_has_no_default_target_or_ports(self):
        dockerfile = (REPOSITORY / "docker/vcpkg.Dockerfile").read_text(
            encoding="utf-8"
        )
        block = dockerfile.split(
            "FROM crossforge_sdk_base AS vcpkg-sdk-base", 1
        )[1]
        self.assertIn("RUN --network=none", block)
        self.assertIn("VCPKG_DEFAULT_HOST_TRIPLET", block)
        self.assertNotIn("VCPKG_DEFAULT_TRIPLET=", block)
        self.assertIn("qualify-vcpkg-sdk.py", block)
        for name in (
            "downloads",
            "buildtrees",
            "packages",
            "installed",
            "vcpkg_installed",
        ):
            self.assertIn("root/%s" % name, block)

    def test_qualifier_does_not_register_the_environment_overlay_twice(self):
        source = QUALIFIER_PATH.read_text(encoding="utf-8")
        self.assertIn('"VCPKG_OVERLAY_TRIPLETS": str(triplet_root)', source)
        self.assertNotIn('"--overlay-triplets=" + str(triplet_root)', source)


if __name__ == "__main__":
    unittest.main()
