import ast
import copy
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
PREPARER_PATH = REPOSITORY / "scripts/prepare-ninja-tool.py"
INSTALLER_PATH = REPOSITORY / "scripts/install-qualify-ninja-tool.py"
PREPARER = runpy.run_path(str(PREPARER_PATH))
INSTALLER = runpy.run_path(str(INSTALLER_PATH))
BAKE = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))
COMPONENT_READER = runpy.run_path(
    str(REPOSITORY / "scripts/release_component.py")
)
EVIDENCE = runpy.run_path(
    str(REPOSITORY / "scripts/validate-supply-chain-evidence.py")
)


class NinjaHostToolTests(unittest.TestCase):
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
            record["component"]: record["canonical_sha256"]
            for record in cls.binding["components"]
        }
        cls.components = {
            name: json.loads(
                (
                    REPOSITORY
                    / "config/generated/components"
                    / (name + ".json")
                ).read_text(encoding="utf-8")
            )
            for name in (
                "sources/ninja",
                "implementation/ninja-host-tool",
                "host-tools/ninja",
            )
        }

    def test_release_evidence_and_locked_materials_are_exact(self):
        result = EVIDENCE["validate_evidence"](self.release, REPOSITORY)
        ninja = self.release["host_tools"]["ninja"]
        self.assertEqual(
            result["ninja_commit"],
            "3441b633c2fe2c494e958780ba0f4227b1327634",
        )
        self.assertEqual(result["ninja_binary_sha256"], ninja["binary"]["sha256"])
        self.assertEqual(ninja["version"], "1.13.2")
        self.assertFalse(ninja["release"]["immutable"])

    def test_source_and_tool_components_have_exact_dependency_boundaries(self):
        source = self.components["sources/ninja"]
        self.assertEqual(source["scope"], "build")
        self.assertEqual(source["dependencies"], [])
        self.assertTrue(
            all(
                item["path"].startswith("/host_tools/ninja/")
                for item in source["materials"]
            )
        )
        policy = self.components["implementation/ninja-host-tool"]
        INSTALLER["load_policy"](policy)
        tool = self.components["host-tools/ninja"]
        self.assertEqual(
            {item["component"] for item in tool["dependencies"]},
            {
                "rpm/host-runtime",
                "sources/ninja",
                "implementation/ninja-host-tool",
            },
        )

    def test_source_component_tampering_fails_closed(self):
        component = copy.deepcopy(self.components["sources/ninja"])
        component["materials"].pop()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "component.json"
            path.write_text(json.dumps(component), encoding="utf-8")
            digest = COMPONENT_READER["canonical_sha256"](component)
            with self.assertRaises(PREPARER["PreparationError"]):
                PREPARER["load_identity"](path, digest)

    def test_bake_graph_is_component_bound_and_cache_only(self):
        rendered = json.loads(BAKE["render"](REPOSITORY))
        source = rendered["target"]["ninja-source"]
        self.assertEqual(source["inherits"], ["_host_tools_common"])
        self.assertEqual(source["target"], "ninja-source-export")
        self.assertEqual(source["output"], ["type=cacheonly"])
        self.assertEqual(
            source["args"]["NINJA_SOURCE_COMPONENT_SHA256"],
            self.digests["sources/ninja"],
        )
        tool = rendered["target"]["ninja-host-tool"]
        self.assertEqual(tool["target"], "ninja-host-tool-export")
        self.assertEqual(tool["output"], ["type=cacheonly"])
        self.assertEqual(
            tool["contexts"],
            {
                "crossforge_host_runtime": "target:host-runtime-qualified",
                "crossforge_ninja_source": "target:ninja-source",
            },
        )
        self.assertEqual(
            rendered["group"]["phase13-host-tools"]["targets"],
            [
                "validate",
                "host-runtime-qualified",
                "ninja-source",
                "ninja-host-tool",
            ],
        )

    def test_docker_network_and_export_boundaries_are_explicit(self):
        dockerfile = (
            REPOSITORY / "docker/host-tools.Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertEqual(dockerfile.count("curl --fail --location --retry 3"), 2)
        for stage in ("ninja-source", "ninja-host-tool"):
            block = dockerfile.split(" AS %s" % stage, 1)[1]
            block = block.split("\nFROM ", 1)[0]
            self.assertIn("RUN --network=none", block)
            self.assertNotIn("curl ", block)
        self.assertIn("FROM scratch AS ninja-source-export", dockerfile)
        self.assertIn("FROM scratch AS ninja-host-tool-export", dockerfile)
        self.assertIn("install-qualify-ninja-tool.py", dockerfile)
        hcl = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        self.assertEqual(hcl.count('target "_host_tools_common"'), 1)
        self.assertIn('dockerfile = "docker/host-tools.Dockerfile"', hcl)

    def test_scripts_remain_python36_syntax_compatible(self):
        for path in (PREPARER_PATH, INSTALLER_PATH):
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 6),
            )


if __name__ == "__main__":
    unittest.main()
