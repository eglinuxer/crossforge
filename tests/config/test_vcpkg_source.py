import ast
import copy
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
PREPARER_PATH = REPOSITORY / "scripts/prepare-vcpkg-source.py"
PREPARER = runpy.run_path(str(PREPARER_PATH))
RENDERER = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))
COMPONENT_READER = runpy.run_path(
    str(REPOSITORY / "scripts/release_component.py")
)


class VcpkgSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        cls.component_path = (
            REPOSITORY / "config/generated/components/sources/vcpkg.json"
        )
        cls.component = json.loads(
            cls.component_path.read_text(encoding="utf-8")
        )
        cls.component_sha256 = COMPONENT_READER["canonical_sha256"](
            cls.component
        )

    def test_locked_component_has_exact_material_contract(self):
        identity = PREPARER["load_identity"](
            self.component_path, self.component_sha256
        )
        self.assertEqual(
            identity["/vcpkg/release/commit"],
            "9e593bb18ea69cc5095e012465dcd675a822ed0d",
        )
        self.assertEqual(
            identity["/vcpkg/tool/commit"],
            "98d7cb0cf1f4686a3e43aa5672b6230c1d56bce8",
        )
        self.assertEqual(identity["/vcpkg/tool/size"], 8548168)

    def test_component_tampering_and_incomplete_materials_fail_closed(self):
        for mutate in (
            lambda value: value["materials"].pop(),
            lambda value: value["materials"][0].__setitem__(
                "value", "GPL-3.0-only"
            ),
        ):
            component = copy.deepcopy(self.component)
            mutate(component)
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "component.json"
                path.write_text(json.dumps(component), encoding="utf-8")
                digest = COMPONENT_READER["canonical_sha256"](component)
                with self.assertRaises(PREPARER["PreparationError"]):
                    PREPARER["load_identity"](path, digest)

    def test_bake_source_target_is_release_bound_and_cache_only(self):
        rendered = json.loads(RENDERER["render"](REPOSITORY))
        target = rendered["target"]["vcpkg-source"]
        self.assertEqual(target["inherits"], ["_vcpkg_common"])
        self.assertEqual(target["target"], "vcpkg-source-export")
        self.assertEqual(target["output"], ["type=cacheonly"])
        self.assertEqual(
            target["contexts"],
            {"crossforge_host_runtime": "target:host-runtime-qualified"},
        )
        self.assertEqual(
            target["args"]["VCPKG_RELEASE_TAG"],
            self.release["vcpkg"]["release"]["tag"],
        )
        self.assertEqual(
            target["args"]["VCPKG_SOURCE_COMPONENT_SHA256"],
            self.component_sha256,
        )
        self.assertEqual(
            rendered["group"]["phase13-source"]["targets"],
            ["validate", "host-runtime-qualified", "vcpkg-source"],
        )

    def test_docker_fetches_full_history_then_prepares_offline(self):
        dockerfile = (REPOSITORY / "docker/vcpkg.Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("--single-branch --no-checkout", dockerfile)
        self.assertIn("--no-tags", dockerfile)
        self.assertNotIn("--depth", dockerfile)
        self.assertNotIn("bootstrap-vcpkg", dockerfile)
        self.assertIn("RUN --network=none", dockerfile)
        self.assertIn("prepare-vcpkg-source.py", dockerfile)
        self.assertIn("fetch-vcpkg-history.py", dockerfile)
        self.assertIn("FROM scratch AS vcpkg-source-export", dockerfile)
        hcl = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        self.assertIn('target "_vcpkg_common"', hcl)
        self.assertIn('dockerfile = "docker/vcpkg.Dockerfile"', hcl)
        workflow = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(workflow.count("vcpkg-source"), 1)
        self.assertIn("phase13-source", workflow)

    def test_version_database_check_is_fail_closed_and_batched(self):
        source = PREPARER_PATH.read_text(encoding="utf-8")
        history = (
            REPOSITORY / "scripts/fetch-vcpkg-history.py"
        ).read_text(encoding="utf-8")
        self.assertIn('glob("*/*.json")', history)
        self.assertIn('"git", "cat-file", "--batch-check=', history)
        self.assertIn('line == tree + " tree"', history)
        self.assertIn('"git", "fetch", "--no-tags", "origin"', history)
        self.assertIn(
            "e593f4bc1905ff51ddc990ffee7a04ed81ae7472ed300d8884f1ba506e94363e",
            history,
        )

    def test_preparer_is_python36_syntax_compatible(self):
        for path in (
            PREPARER_PATH,
            REPOSITORY / "scripts/fetch-vcpkg-history.py",
        ):
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 6),
            )


if __name__ == "__main__":
    unittest.main()
