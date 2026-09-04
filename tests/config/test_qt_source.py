import json
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
RENDERER = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))


class QtSourceGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        cls.bake = json.loads(RENDERER["render"](REPOSITORY))
        cls.dockerfile = (REPOSITORY / "docker/qt.Dockerfile").read_text(
            encoding="utf-8"
        )

    def test_source_target_is_cache_only_and_release_component_bound(self):
        target = self.bake["target"]["qt-source"]
        self.assertEqual(target["inherits"], ["_qt_common"])
        self.assertEqual(target["target"], "qt-source-export")
        self.assertEqual(target["output"], ["type=cacheonly"])
        self.assertEqual(target["args"]["QT_VERSION"], "6.8.4")
        self.assertEqual(
            target["args"]["QT_SOURCE_URL"],
            self.release["qt"]["source"]["url"],
        )
        self.assertRegex(
            target["args"]["CROSSFORGE_COMPONENT_SOURCES_QT_SHA256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            self.bake["group"]["qt-source-qualified"]["targets"],
            ["qt-source", "xcb-util-cursor-source"],
        )

    def test_xcb_cursor_source_is_signed_offline_and_cache_only(self):
        target = self.bake["target"]["xcb-util-cursor-source"]
        self.assertEqual(target["inherits"], ["_qt_common"])
        self.assertEqual(target["target"], "xcb-util-cursor-source-export")
        self.assertEqual(target["output"], ["type=cacheonly"])
        self.assertEqual(target["args"]["XCB_UTIL_CURSOR_VERSION"], "0.1.6")
        self.assertRegex(
            target["args"][
                "CROSSFORGE_COMPONENT_SOURCES_XCB_UTIL_CURSOR_SHA256"
            ],
            r"^[0-9a-f]{64}$",
        )
        fetch = self.dockerfile.split(" AS xcb-util-cursor-fetch", 1)[1].split(
            "\nFROM ", 1
        )[0]
        source = self.dockerfile.split(" AS xcb-util-cursor-source", 1)[1].split(
            "\nFROM ", 1
        )[0]
        self.assertIn("curl --fail --location --retry 3", fetch)
        self.assertIn("RUN --network=none", source)
        self.assertIn("prepare-xcb-util-cursor-source.py", source)
        self.assertIn("XCB-UTIL-CURSOR-RELEASE-KEY.asc", source)
        self.assertIn("FROM scratch AS xcb-util-cursor-source-export", self.dockerfile)

    def test_fetch_is_networked_but_all_source_acceptance_is_offline(self):
        fetch = self.dockerfile.split(" AS qt-fetch", 1)[1].split(
            "\nFROM ", 1
        )[0]
        source = self.dockerfile.split(" AS qt-source", 1)[1].split(
            "\nFROM ", 1
        )[0]
        self.assertIn("curl --fail --location --retry 3", fetch)
        self.assertNotIn("fetch-release-source.py", fetch)
        self.assertNotIn("sources-qt.json", fetch)
        self.assertNotIn("RUN --network=none", fetch)
        self.assertIn("RUN --network=none", source)
        self.assertIn("prepare-qt-source.py", source)
        self.assertIn("qt-source-manifest.schema.json", source)
        self.assertIn(
            "qt-everywhere-opensource-src-6.8.4.tar.xz.sha256.b64",
            source,
        )
        self.assertIn("FROM scratch AS qt-source-export", self.dockerfile)

    def test_qt_source_cannot_enter_the_sdk_or_candidate_ancestry(self):
        for path in (
            REPOSITORY / "docker/Dockerfile",
            REPOSITORY / "docker/python.Dockerfile",
            REPOSITORY / "docker/vcpkg.Dockerfile",
            REPOSITORY / "docker/packaging.Dockerfile",
        ):
            with self.subTest(path=path.name):
                self.assertNotIn(
                    "crossforge_qt", path.read_text(encoding="utf-8")
                )
        candidate = self.bake["target"]["sdk-candidate"]
        self.assertFalse(
            any("qt-source" in value for value in candidate["contexts"].values())
        )
        self.assertFalse(
            any(
                "xcb-util-cursor" in value
                for value in candidate["contexts"].values()
            )
        )


if __name__ == "__main__":
    unittest.main()
