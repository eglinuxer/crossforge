import ast
import copy
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY / "tests/vcpkg/upstream-tier1"
QUALIFIER_PATH = REPOSITORY / "scripts/qualify-vcpkg-upstream.py"
QUALIFIER = runpy.run_path(str(QUALIFIER_PATH))
ASSETS = runpy.run_path(str(REPOSITORY / "scripts/fetch-vcpkg-assets.py"))
BAKE = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


class VcpkgUpstreamTests(unittest.TestCase):
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
                / "config/generated/components/implementation/"
                "vcpkg-upstream-tier1-qualification.json"
            ).read_text(encoding="utf-8")
        )
        cls.qualification = json.loads(
            (
                REPOSITORY
                / "config/generated/components/vcpkg/"
                "upstream-tier1-qualification.json"
            ).read_text(encoding="utf-8")
        )

    def test_policy_binds_exact_ports_assets_triplets_and_fixtures(self):
        context = QUALIFIER["policy_context"](self.policy, FIXTURE_ROOT)
        self.assertEqual(tuple(context["ports"]), QUALIFIER["EXPECTED_PORTS"])
        self.assertEqual(
            [asset["filename"] for asset in context["assets"]],
            [
                "fmt-backport-4813.patch",
                "fmtlib-fmt-12.2.0.tar.gz",
                "madler-zlib-v1.3.2.tar.gz",
            ],
        )
        self.assertEqual(
            {record["path"] for record in context["files"]},
            {"consumer.cpp", "manifest/vcpkg.json"},
        )

    def test_asset_policy_rejects_unsafe_or_ambiguous_filenames(self):
        for filename in ("../asset", "/asset", "asset/name", ""):
            policy = copy.deepcopy(self.policy)
            material = next(
                item
                for item in policy["materials"]
                if item["path"].endswith("assets/0/filename")
            )
            material["value"] = filename
            with self.subTest(filename=filename):
                with self.assertRaises(ASSETS["AssetError"]):
                    ASSETS["policy_assets"](policy)

    def test_asset_root_rejects_missing_extra_and_symlinked_entries(self):
        assets = ASSETS["policy_assets"](self.policy)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ASSETS["AssetError"]):
                ASSETS["verify_asset_root"](root, assets)
            (root / assets[0]["filename"]).write_bytes(b"wrong")
            (root / "extra").write_bytes(b"extra")
            with self.assertRaises(ASSETS["AssetError"]):
                ASSETS["verify_asset_root"](root, assets)
            (root / "extra").unlink()
            (root / assets[0]["filename"]).unlink()
            (root / assets[0]["filename"]).symlink_to("missing")
            with self.assertRaises(ASSETS["AssetError"]):
                ASSETS["verify_asset_root"](root, assets)

    def test_qualification_depends_on_the_synthetic_contract(self):
        self.assertEqual(
            {item["component"] for item in self.qualification["dependencies"]},
            {
                "implementation/vcpkg-upstream-tier1-qualification",
                "vcpkg/contract-qualification",
            },
        )

    def test_bake_graph_prefetches_then_qualifies_offline(self):
        rendered = json.loads(BAKE["render"](REPOSITORY))
        assets = rendered["target"]["vcpkg-upstream-tier1-assets"]
        self.assertEqual(assets["target"], "vcpkg-upstream-tier1-assets-export")
        self.assertEqual(
            assets["args"]["VCPKG_UPSTREAM_TIER1_POLICY_COMPONENT_SHA256"],
            self.digests["implementation/vcpkg-upstream-tier1-qualification"],
        )
        target = rendered["target"]["vcpkg-upstream-tier1-qualified"]
        self.assertEqual(target["target"], "vcpkg-upstream-tier1-qualified")
        self.assertEqual(
            target["contexts"],
            {
                "crossforge_vcpkg_contract": "target:vcpkg-contract-qualified",
                "crossforge_vcpkg_contract_assets": "target:vcpkg-contract-assets",
                "crossforge_vcpkg_upstream_tier1_assets": (
                    "target:vcpkg-upstream-tier1-assets"
                ),
            },
        )
        self.assertEqual(
            rendered["group"]["phase13-ports"]["targets"],
            ["vcpkg-upstream-tier1-qualified"],
        )

    def test_qualifier_uses_shared_isolated_install_without_downloads(self):
        qualifier = QUALIFIER_PATH.read_text(encoding="utf-8")
        common = (REPOSITORY / "scripts/vcpkg_qualification.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("isolated_install(", qualifier)
        for required in (
            '"--no-downloads"',
            '"--binarysource=clear"',
            '"X_VCPKG_ASSET_SOURCES": "clear;x-block-origin"',
        ):
            self.assertIn(required, common)
        dockerfile = (REPOSITORY / "docker/vcpkg.Dockerfile").read_text(
            encoding="utf-8"
        )
        block = dockerfile.split(
            "FROM crossforge_vcpkg_contract AS vcpkg-upstream-tier1-qualified",
            1,
        )[1]
        self.assertIn("RUN --network=none", block)
        self.assertIn("qualify-vcpkg-upstream.py", block)
        workflow = (REPOSITORY / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("vcpkg-upstream-tier1-qualified", workflow)
        self.assertIn("phase13-ports", workflow)

    def test_new_scripts_remain_python36_syntax_compatible(self):
        for path in (
            QUALIFIER_PATH,
            REPOSITORY / "scripts/fetch-vcpkg-assets.py",
            REPOSITORY / "scripts/vcpkg_qualification.py",
        ):
            with self.subTest(path=path.name):
                ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                    feature_version=(3, 6),
                )


if __name__ == "__main__":
    unittest.main()
