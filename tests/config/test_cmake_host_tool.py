import ast
import json
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
INSTALLER_PATH = REPOSITORY / "scripts/install-qualify-cmake-tool.py"
INSTALLER = runpy.run_path(str(INSTALLER_PATH))
BAKE = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


class CmakeHostToolTests(unittest.TestCase):
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
        cls.digests = {
            item["component"]: item["canonical_sha256"]
            for item in cls.binding["components"]
        }
        cls.source = json.loads(
            (
                REPOSITORY / "config/generated/components/sources/cmake.json"
            ).read_text(encoding="utf-8")
        )
        cls.policy = json.loads(
            (
                REPOSITORY
                / "config/generated/components/implementation/cmake-host-tool.json"
            ).read_text(encoding="utf-8")
        )
        cls.tool = json.loads(
            (
                REPOSITORY / "config/generated/components/host-tools/cmake.json"
            ).read_text(encoding="utf-8")
        )

    def test_release_locks_vcpkg_selected_cmake_and_payloads(self):
        cmake = self.release["host_tools"]["cmake"]
        self.assertEqual(cmake["version"], "4.4.0")
        self.assertEqual(
            cmake["binary"]["sha512"],
            "3df4aaa128a438ed48dcac7065fd355ff538eed8f394491298d0db63a891d671da247c8fa262e4fa6bf99429d630abab317d5a0248168fe203d1ca4978dab4da",
        )
        self.assertEqual(
            [item["path"] for item in cmake["payloads"]],
            ["bin/cmake", "bin/cpack", "bin/ctest"],
        )
        identity = INSTALLER["load_identity"](self.source)
        self.assertEqual(
            identity["binary"],
            {
                key: value
                for key, value in cmake["binary"].items()
                if key != "status"
            },
        )
        self.assertEqual(identity["payloads"], cmake["payloads"])

    def test_component_closure_binds_runtime_ninja_source_and_policy(self):
        self.assertEqual(self.source["dependencies"], [])
        self.assertTrue(
            all(
                item["path"].startswith("/host_tools/cmake/")
                for item in self.source["materials"]
            )
        )
        self.assertEqual(
            INSTALLER["load_policy"](self.policy)["install_prefix"],
            "/opt/crossforge/host-tools/cmake",
        )
        self.assertEqual(
            {item["component"] for item in self.tool["dependencies"]},
            {
                "host-tools/ninja",
                "implementation/cmake-host-tool",
                "rpm/host-runtime",
                "sources/cmake",
            },
        )

    def test_bake_target_is_an_offline_overlay_after_network_fetch(self):
        rendered = json.loads(BAKE["render"](REPOSITORY))
        target = rendered["target"]["cmake-host-tool"]
        self.assertEqual(target["target"], "cmake-host-tool-export")
        self.assertEqual(
            target["contexts"],
            {
                "crossforge_host_runtime": "target:host-runtime-qualified",
                "crossforge_ninja_host_tool": "target:ninja-host-tool",
            },
        )
        self.assertEqual(
            target["args"]["CMAKE_SOURCE_COMPONENT_SHA256"],
            self.digests["sources/cmake"],
        )
        self.assertEqual(
            target["args"]["CMAKE_TOOL_COMPONENT_SHA256"],
            self.digests["host-tools/cmake"],
        )
        self.assertIn(
            "cmake-host-tool",
            rendered["group"]["phase13-host-tools"]["targets"],
        )

    def test_docker_stage_preserves_system_cmake_and_qualifies_consumers(self):
        dockerfile = (REPOSITORY / "docker/host-tools.Dockerfile").read_text(
            encoding="utf-8"
        )
        block = dockerfile.split(
            "FROM crossforge_host_runtime AS cmake-host-tool", 1
        )[1].split("FROM scratch AS cmake-host-tool-export", 1)[0]
        self.assertIn("RUN --network=none", block)
        self.assertIn("install-qualify-cmake-tool.py", block)
        self.assertIn("crossforge_ninja_host_tool", block)
        installer = INSTALLER_PATH.read_text(encoding="utf-8")
        for required in ("cmake", "ctest", "cpack", "CMAKE_MAKE_PROGRAM"):
            self.assertIn(required, installer)
        vcpkg = (REPOSITORY / "docker/vcpkg.Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("crossforge_cmake_host_tool", vcpkg)
        self.assertIn("CROSSFORGE_CMAKE_ROOT", vcpkg)

    def test_installer_is_python36_syntax_compatible(self):
        ast.parse(
            INSTALLER_PATH.read_text(encoding="utf-8"),
            filename=str(INSTALLER_PATH),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
