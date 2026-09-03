import ast
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY / "tests/vcpkg/contract"
QUALIFIER_PATH = REPOSITORY / "scripts/qualify-vcpkg-contract.py"
QUALIFIER = runpy.run_path(str(QUALIFIER_PATH))
BAKE = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))
COMMON_PATH = REPOSITORY / "scripts/vcpkg_qualification.py"
COMMON = runpy.run_path(str(COMMON_PATH))


class VcpkgContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binding = json.loads(
            (REPOSITORY / "config/generated/release-binding.json").read_text(
                encoding="utf-8"
            )
        )
        cls.digests = {
            item["component"]: item["canonical_sha256"]
            for item in cls.binding["components"]
        }
        cls.policy = json.loads(
            (
                REPOSITORY
                / "config/generated/components/implementation/vcpkg-contract-qualification.json"
            ).read_text(encoding="utf-8")
        )
        cls.qualification = json.loads(
            (
                REPOSITORY
                / "config/generated/components/vcpkg/contract-qualification.json"
            ).read_text(encoding="utf-8")
        )

    def test_fixture_tree_is_exactly_bound_by_the_policy_component(self):
        records = QUALIFIER["policy_files"](self.policy, FIXTURE_ROOT)
        self.assertEqual(len(records), 12)
        self.assertEqual(
            {record["path"] for record in records},
            {
                path.relative_to(FIXTURE_ROOT).as_posix()
                for path in FIXTURE_ROOT.rglob("*")
                if path.is_file()
            },
        )

    def test_qualification_component_binds_sdk_and_both_runtime_gates(self):
        self.assertEqual(self.qualification["scope"], "qualification")
        self.assertEqual(
            {item["component"] for item in self.qualification["dependencies"]},
            {
                "implementation/vcpkg-contract-qualification",
                "toolchain/x86_64-qualification",
                "toolchain/aarch64-qualification",
                "vcpkg/sdk-build",
            },
        )

    def test_contract_covers_host_and_both_target_linkages(self):
        self.assertEqual(
            set(QUALIFIER["TRIPLETS"]),
            {
                "crossforge-host-x64-el8",
                "crossforge-x64-el8",
                "crossforge-x64-el8-dynamic",
                "crossforge-arm64-el8",
                "crossforge-arm64-el8-dynamic",
            },
        )
        self.assertFalse(
            QUALIFIER["TRIPLETS"]["crossforge-host-x64-el8"]["cross"]
        )
        for name, profile in QUALIFIER["TRIPLETS"].items():
            expected = "dynamic" if name.endswith("-dynamic") else "static"
            self.assertEqual(profile["linkage"], expected)

    def test_overlay_port_requires_and_executes_a_host_dependency(self):
        target_manifest = json.loads(
            (
                FIXTURE_ROOT / "ports/crossforge-target-probe/vcpkg.json"
            ).read_text(encoding="utf-8")
        )
        dependencies = {
            item["name"]: item.get("host", False)
            for item in target_manifest["dependencies"]
        }
        self.assertEqual(
            dependencies,
            {"crossforge-host-probe": True, "vcpkg-cmake": True},
        )
        cmake = (
            FIXTURE_ROOT / "ports/crossforge-target-probe/CMakeLists.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("execute_process(", cmake)
        self.assertIn("CROSSFORGE_HOST_PROBE", cmake)
        self.assertIn("silently became native", cmake)
        self.assertNotIn("qemu", cmake.lower())

    def test_bake_target_is_offline_qualification_over_the_sdk(self):
        rendered = json.loads(BAKE["render"](REPOSITORY))
        assets = rendered["target"]["vcpkg-contract-assets"]
        self.assertEqual(assets["target"], "vcpkg-contract-assets-export")
        self.assertEqual(assets["output"], ["type=cacheonly"])
        self.assertEqual(
            assets["args"]["VCPKG_PATCHELF_SHA512"],
            QUALIFIER["PATCHELF_ASSET"]["sha512"],
        )
        target = rendered["target"]["vcpkg-contract-qualified"]
        self.assertEqual(target["inherits"], ["_vcpkg_common"])
        self.assertEqual(target["target"], "vcpkg-contract-qualified")
        self.assertEqual(
            target["contexts"],
            {
                "crossforge_vcpkg_contract_assets": "target:vcpkg-contract-assets",
                "crossforge_vcpkg_sdk": "target:sdk-phase13-base",
            },
        )
        self.assertEqual(target["output"], ["type=cacheonly"])
        self.assertEqual(
            target["args"]["VCPKG_CONTRACT_QUALIFICATION_COMPONENT_SHA256"],
            self.digests["vcpkg/contract-qualification"],
        )
        self.assertEqual(
            rendered["group"]["phase13-contract"]["targets"],
            ["vcpkg-contract-qualified"],
        )

    def test_docker_stage_and_vcpkg_invocation_forbid_downloads(self):
        dockerfile = (REPOSITORY / "docker/vcpkg.Dockerfile").read_text(
            encoding="utf-8"
        )
        block = dockerfile.split(
            "FROM crossforge_vcpkg_sdk AS vcpkg-contract-qualified", 1
        )[1]
        self.assertIn("RUN --network=none", block)
        self.assertIn("qualify-vcpkg-contract.py", block)
        self.assertIn("--patchelf-archive", block)
        source = QUALIFIER_PATH.read_text(encoding="utf-8")
        source += (REPOSITORY / "scripts/vcpkg_qualification.py").read_text(
            encoding="utf-8"
        )
        for required in (
            '"--no-downloads"',
            '"--binarysource=clear"',
            '"--x-buildtrees-root="',
            '"--x-packages-root="',
            '"X_VCPKG_ASSET_SOURCES": "clear;x-block-origin"',
        ):
            self.assertIn(required, source)
        workflow = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("vcpkg-upstream-tier3-qualified", workflow)
        self.assertIn("phase13-contract", workflow)

    def test_qualifier_remains_python36_syntax_compatible(self):
        for path in (QUALIFIER_PATH, COMMON_PATH):
            with self.subTest(path=path.name):
                ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                    feature_version=(3, 6),
                )

    def test_failed_install_prioritizes_referenced_vcpkg_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            buildtrees = root / "buildtrees"
            buildtrees.mkdir()
            for index in range(20):
                (buildtrees / ("early-%02d-err.log" % index)).write_text(
                    "unrelated\n", encoding="utf-8"
                )
            important = buildtrees / "z-important-out.log"
            important.write_text("important failure\n", encoding="utf-8")
            command = root / "fail.sh"
            command.write_text(
                "#!/bin/sh\nprintf '%s\\n' '%s'\nexit 1\n"
                % ("%s", important),
                encoding="utf-8",
            )
            command.chmod(0o755)
            with self.assertRaises(COMMON["QualificationError"]) as context:
                COMMON["run"](
                    [command, "--x-buildtrees-root=" + str(buildtrees)]
                )
            self.assertIn("important failure", str(context.exception))

    def test_isolated_install_can_reuse_only_an_in_work_installed_seed(self):
        function = COMMON["isolated_install"]
        original_run = function.__globals__["run"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            seed = work / "seed"
            (seed / "vcpkg").mkdir(parents=True)
            (seed / "vcpkg/status").write_text("seed\n", encoding="utf-8")
            manifest = root / "manifest"
            manifest.mkdir()
            vcpkg_root = root / "vcpkg"
            vcpkg_root.mkdir()
            (vcpkg_root / "vcpkg").write_text("", encoding="utf-8")
            asset = root / "asset.tar.gz"
            asset.write_bytes(b"asset")
            calls = []

            def fake_run(arguments, cwd=None, env=None):
                calls.append(arguments)
                return "", ""

            function.__globals__["run"] = fake_run
            try:
                roots = function(
                    vcpkg_root,
                    manifest,
                    "test-triplet",
                    (asset,),
                    work,
                    seed_installed=seed,
                )
                outside = root / "outside"
                outside.mkdir()
                with self.assertRaises(COMMON["QualificationError"]):
                    function(
                        vcpkg_root,
                        manifest,
                        "unsafe-triplet",
                        (asset,),
                        work,
                        seed_installed=outside,
                    )
            finally:
                function.__globals__["run"] = original_run
            self.assertEqual(
                (roots["installed"] / "vcpkg/status").read_text(
                    encoding="utf-8"
                ),
                "seed\n",
            )
            self.assertIn("--no-downloads", calls[0])

    def test_shared_library_accepts_only_the_relocatable_vcpkg_runpath(self):
        dynamic = (
            " 0x000000000000001d (RUNPATH) "
            "Library runpath: [$ORIGIN]\n"
        )
        self.assertEqual(
            QUALIFIER["validate_shared_library_dynamic"](dynamic, "test"),
            "$ORIGIN",
        )
        for unsafe in (
            " 0x0 (RPATH) Library rpath: [$ORIGIN]\n",
            " 0x0 (RUNPATH) Library runpath: [/opt/crossforge]\n",
            " 0x0 (RUNPATH) Library runpath: [$ORIGIN/../lib]\n",
            dynamic + " 0x0 (TEXTREL) 0x0\n",
            "",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(QUALIFIER["QualificationError"]):
                    QUALIFIER["validate_shared_library_dynamic"](unsafe, "test")


if __name__ == "__main__":
    unittest.main()
