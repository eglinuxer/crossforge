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
            ["qt-source", "ffmpeg-source", "xcb-util-cursor-source"],
        )

    def test_ffmpeg_source_is_signed_offline_and_cache_only(self):
        target = self.bake["target"]["ffmpeg-source"]
        self.assertEqual(target["inherits"], ["_qt_common"])
        self.assertEqual(target["target"], "ffmpeg-source-export")
        self.assertEqual(target["output"], ["type=cacheonly"])
        self.assertEqual(target["args"]["FFMPEG_VERSION"], "7.1.1")
        self.assertRegex(
            target["args"]["CROSSFORGE_COMPONENT_SOURCES_FFMPEG_SHA256"],
            r"^[0-9a-f]{64}$",
        )
        fetch = self.dockerfile.split(" AS ffmpeg-fetch", 1)[1].split(
            "\nFROM ", 1
        )[0]
        source = self.dockerfile.split(" AS ffmpeg-source", 1)[1].split(
            "\nFROM ", 1
        )[0]
        self.assertIn("curl --fail --location --retry 3", fetch)
        self.assertIn("RUN --network=none", source)
        self.assertIn("prepare-ffmpeg-source.py", source)
        self.assertIn("FFMPEG-RELEASE-KEY.asc", source)
        self.assertIn("FROM scratch AS ffmpeg-source-export", self.dockerfile)

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

    def test_xcb_cursor_builds_cover_host_and_both_targets(self):
        self.assertEqual(
            self.bake["group"]["xcb-util-cursor-qualified"]["targets"],
            [
                "xcb-util-cursor-host-build",
                "xcb-util-cursor-x86_64-build",
                "xcb-util-cursor-aarch64-build",
            ],
        )
        host = self.bake["target"]["xcb-util-cursor-host-build"]
        self.assertEqual(host["contexts"]["crossforge_host_qt"], "target:host-qt-build-locked")
        for arch, triple in (
            ("x86_64", "x86_64-unknown-linux-gnu"),
            ("aarch64", "aarch64-unknown-linux-gnu"),
        ):
            target = self.bake["target"]["xcb-util-cursor-%s-build" % arch]
            self.assertEqual(target["args"]["XCB_UTIL_CURSOR_TARGET_TRIPLE"], triple)
            self.assertEqual(
                target["contexts"]["crossforge_qt_target"],
                "target:qt-target-%s-locked" % arch,
            )
            self.assertEqual(
                target["contexts"]["crossforge_toolchain"],
                "target:toolchain-%s-dev" % arch,
            )
        build = self.dockerfile.split(" AS xcb-util-cursor-target-build", 1)[1]
        self.assertIn("RUN --network=none", build)
        self.assertNotIn("HOSTRUNNER", build)
        self.assertNotIn("qemu", build.lower())

    def test_ffmpeg_builds_cover_host_and_both_targets_without_target_execution(self):
        self.assertEqual(
            self.bake["group"]["ffmpeg-qualified"]["targets"],
            ["ffmpeg-host-build", "ffmpeg-x86_64-build", "ffmpeg-aarch64-build"],
        )
        host = self.bake["target"]["ffmpeg-host-build"]
        self.assertEqual(
            host["contexts"]["crossforge_xcb_host"],
            "target:xcb-util-cursor-host-build",
        )
        for arch, triple in (
            ("x86_64", "x86_64-unknown-linux-gnu"),
            ("aarch64", "aarch64-unknown-linux-gnu"),
        ):
            target = self.bake["target"]["ffmpeg-%s-build" % arch]
            self.assertEqual(target["args"]["FFMPEG_TARGET_TRIPLE"], triple)
            self.assertEqual(
                target["args"]["FFMPEG_RPM_LOCK"],
                "locks/qt-target-el8-%s.json" % arch,
            )
            self.assertEqual(
                target["contexts"]["crossforge_qt_target"],
                "target:qt-target-%s-locked" % arch,
            )
            self.assertEqual(
                target["contexts"]["crossforge_toolchain"],
                "target:toolchain-%s-dev" % arch,
            )
        build = self.dockerfile.split(" AS ffmpeg-target-build", 1)[1]
        self.assertIn("RUN --network=none", build)
        self.assertIn("qualify-ffmpeg-build.py", build)
        self.assertNotIn("HOSTRUNNER", build)
        self.assertNotIn("qemu", build.lower())

    def test_host_configure_qualification_uses_locked_tools_and_inputs_offline(self):
        target = self.bake["target"]["qt-host-configure-qualified"]
        self.assertEqual(target["target"], "qt-host-configure-qualified")
        self.assertEqual(
            target["contexts"],
            {
                "crossforge_cmake": "target:cmake-host-tool",
                "crossforge_ffmpeg_host": "target:ffmpeg-host-build",
                "crossforge_ninja": "target:ninja-host-tool",
                "crossforge_qt_source": "target:qt-source",
            },
        )
        self.assertEqual(
            self.bake["group"]["qt-host-configure-qualified"]["targets"],
            ["qt-host-configure-evidence"],
        )
        report = self.bake["target"]["qt-host-configure-evidence"]
        self.assertEqual(report["target"], "qt-host-configure-evidence")
        self.assertEqual(report["contexts"], target["contexts"])
        stage = self.dockerfile.split(" AS qt-host-configure-qualified", 1)[1]
        self.assertIn("RUN --network=none", stage)
        self.assertIn("configure-qt-host.sh", stage)
        self.assertIn("/deps/host/ffmpeg", stage)
        self.assertIn("qualify-qt-host-configure.py", stage)

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
