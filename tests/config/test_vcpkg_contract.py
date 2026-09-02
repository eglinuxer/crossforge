import ast
import json
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY / "tests/vcpkg/contract"
QUALIFIER_PATH = REPOSITORY / "scripts/qualify-vcpkg-contract.py"
QUALIFIER = runpy.run_path(str(QUALIFIER_PATH))
BAKE = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


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
        self.assertIn("vcpkg-contract-qualified", workflow)
        self.assertIn("phase13-contract", workflow)

    def test_qualifier_remains_python36_syntax_compatible(self):
        ast.parse(
            QUALIFIER_PATH.read_text(encoding="utf-8"),
            filename=str(QUALIFIER_PATH),
            feature_version=(3, 6),
        )

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
