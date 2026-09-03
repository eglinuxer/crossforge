import copy
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER = runpy.run_path(
    str(REPOSITORY / "scripts/render-release-components.py")
)
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))


def changed(before, after):
    return {
        name
        for name in set(before) & set(after)
        if RENDERER["canonical_sha256"](before[name])
        != RENDERER["canonical_sha256"](after[name])
    }


class CrosspackComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = VALIDATOR["load_json"](
            REPOSITORY / "config/release.json"
        )
        cls.components = RENDERER["render_component_documents"](
            cls.release
        )

    def test_nfpm_and_crosspack_have_separate_build_identities(self):
        nfpm = self.components["sources/nfpm"]
        implementation = self.components["implementation/crosspack"]
        self.assertEqual(nfpm["scope"], "build")
        self.assertEqual(implementation["scope"], "build")
        self.assertTrue(
            all(item["path"].startswith("/nfpm/") for item in nfpm["materials"])
        )
        self.assertTrue(
            all(
                item["path"].startswith("/@implementation/crosspack/")
                for item in implementation["materials"]
            )
        )
        self.assertEqual(nfpm["dependencies"], [])
        self.assertEqual(implementation["dependencies"], [])

    def test_nfpm_change_does_not_invalidate_unrelated_components(self):
        release = copy.deepcopy(self.release)
        release["nfpm"]["binary"]["extracted_sha256"] = "0" * 64
        after = RENDERER["render_component_documents"](release)
        self.assertEqual(changed(self.components, after), {"sources/nfpm"})

    def test_crosspack_policy_matches_the_implemented_v1_boundary(self):
        policy = RENDERER["CROSSPACK_POLICY"]
        self.assertEqual(policy["schema_version"], 1)
        self.assertEqual(policy["formats"], ["deb", "rpm"])
        self.assertEqual(
            set(policy["targets"]), {"x86_64", "aarch64"}
        )
        self.assertEqual(
            policy["ownership"], "complete-exclusive-staged-tree"
        )
        self.assertEqual(
            policy["external_dependencies"], "explicit-per-format"
        )


if __name__ == "__main__":
    unittest.main()
