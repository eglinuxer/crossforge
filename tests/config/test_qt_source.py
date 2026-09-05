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
        cls.plan = json.loads(
            (REPOSITORY / "config/qt-qualification.json").read_text(
                encoding="utf-8"
            )
        )
        cls.bake = json.loads(RENDERER["render"](REPOSITORY))
        cls.dockerfile = (REPOSITORY / "docker/qt.Dockerfile").read_text(
            encoding="utf-8"
        )
        cls.runtime_dockerfile = (
            REPOSITORY / "docker/qt-runtime.Dockerfile"
        ).read_text(encoding="utf-8")

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
        build = self.dockerfile.split(
            " AS xcb-util-cursor-target-build", 1
        )[1].split("\nFROM ", 1)[0]
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
        build = self.dockerfile.split(
            " AS ffmpeg-target-build", 1
        )[1].split("\nFROM ", 1)[0]
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
        configure_stage = self.dockerfile.split(
            " AS qt-host-configure", 1
        )[1].split("\nFROM ", 1)[0]
        qualified_stage = self.dockerfile.split(
            " AS qt-host-configure-qualified", 1
        )[1].split("\nFROM ", 1)[0]
        self.assertIn("RUN --network=none", configure_stage)
        self.assertIn("configure-qt-host.sh", configure_stage)
        self.assertIn("/deps/host/ffmpeg", configure_stage)
        self.assertNotIn("FUTURE_QT_QUALIFICATION", configure_stage)
        self.assertIn("qualify-qt-host-configure.py", qualified_stage)

    def test_host_build_uses_configured_tree_without_policy_cache_coupling(self):
        target = self.bake["target"]["qt-host-build"]
        self.assertEqual(target["target"], "qt-host-install-checked")
        self.assertEqual(
            self.bake["group"]["qt-host-built"]["targets"],
            ["qt-host-build"],
        )
        self.assertEqual(
            target["contexts"],
            self.bake["target"]["qt-host-configure-qualified"]["contexts"],
        )
        stage = self.dockerfile.split(" AS qt-host-build", 1)[1].split(
            "\nFROM ", 1
        )[0]
        self.assertIn("FROM qt-host-configure AS qt-host-build", self.dockerfile)
        self.assertIn("RUN --network=none", stage)
        script = (REPOSITORY / "scripts/build-qt-host.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("ulimit -Sn", script)
        self.assertIn("-print-file-name=libatomic.so", script)
        self.assertNotIn("qt-host-configure.json", script)
        self.assertIn("CMakeCache.txt", script)
        self.assertIn("config.summary", script)
        self.assertIn("print-build-log-diagnostics.py", script)
        self.assertNotIn("| tee", script)
        self.assertNotIn("libQt6Core", script)
        check = (REPOSITORY / "scripts/check-qt-host-install.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("lib/libQt6Core.so.6.8.4", check)
        self.assertNotIn("lib64/libQt6Core", check)
        checked_stage = self.dockerfile.split(
            " AS qt-host-install-checked", 1
        )[1]
        self.assertIn("FROM qt-host-build", self.dockerfile)
        self.assertIn("RUN --network=none", checked_stage)

    def test_host_rpm_plan_supplies_chromium_atomic_link_support(self):
        host_plan = json.loads(
            (
                REPOSITORY
                / "config/rpm/host-qt-build-el8-x86_64.plan.json"
            ).read_text(encoding="utf-8")
        )
        host_roots = {item["name"] for item in host_plan["roots"]}
        self.assertIn("gcc-toolset-15-libatomic-devel", host_roots)
        for arch in ("x86_64", "aarch64"):
            target_plan = json.loads(
                (
                    REPOSITORY
                    / ("config/rpm/qt-target-el8-%s.plan.json" % arch)
                ).read_text(encoding="utf-8")
            )
            target_roots = {item["name"] for item in target_plan["roots"]}
            self.assertNotIn("gcc-toolset-15-libatomic-devel", target_roots)

    def test_host_build_qualification_is_downstream_and_offline(self):
        qualified = self.bake["target"]["qt-host-qualified"]
        evidence = self.bake["target"]["qt-host-qualification-evidence"]
        self.assertEqual(qualified["target"], "qt-host-qualified")
        self.assertEqual(evidence["target"], "qt-host-qualification-evidence")
        self.assertEqual(qualified["contexts"], evidence["contexts"])
        self.assertEqual(
            self.bake["group"]["qt-host-qualified"]["targets"],
            ["qt-host-qualification-evidence"],
        )
        stage = self.dockerfile.split(" AS qt-host-qualified", 1)[1]
        self.assertIn("FROM qt-host-install-checked", self.dockerfile)
        self.assertIn("RUN --network=none", stage)
        self.assertIn("qualify-qt-host-build.py", stage)
        self.assertIn("qt-host-build.schema.json", stage)
        self.assertIn("--from=qt-host-configure-qualified", stage)
        self.assertIn("/work/evidence/qt-host-configure.json", stage)
        self.assertIn("--ffmpeg-prefix", stage)
        self.assertIn("--xcb-prefix", stage)
        self.assertIn("--diagnostics", stage)
        schema = json.loads(
            (
                REPOSITORY / "config/schemas/qt-host-build.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(schema["properties"]["builders"]["minItems"], 4)
        self.assertEqual(schema["properties"]["builders"]["maxItems"], 4)

    def test_target_configure_uses_qualified_host_and_never_executes_target_code(self):
        expected = []
        for arch, triple in (
            ("x86_64", "x86_64-unknown-linux-gnu"),
            ("aarch64", "aarch64-unknown-linux-gnu"),
        ):
            name = "qt-%s-configure-observation" % arch
            expected.append(name)
            target = self.bake["target"][name]
            self.assertEqual(target["target"], "qt-target-configure-observation")
            self.assertEqual(target["args"]["QT_TARGET_TRIPLE"], triple)
            self.assertEqual(
                target["args"]["QT_XNNPACK_PATCH_SHA256"],
                self.plan["patches"][0]["sha256"],
            )
            self.assertEqual(
                target["contexts"]["crossforge_qt_host"],
                "target:qt-host-qualified",
            )
            self.assertEqual(
                target["contexts"]["crossforge_xcb_target"],
                "target:xcb-util-cursor-%s-build" % arch,
            )
            self.assertEqual(
                target["contexts"]["crossforge_ffmpeg_target"],
                "target:ffmpeg-%s-build" % arch,
            )
        self.assertEqual(
            self.bake["group"]["qt-target-configure-observed"]["targets"],
            expected,
        )
        self.assertEqual(
            self.bake["group"]["qt-target-configure-qualified"]["targets"],
            [
                "qt-x86_64-configure-qualified",
                "qt-aarch64-configure-qualified",
            ],
        )
        for arch in ("x86_64", "aarch64"):
            qualified = self.bake["target"][
                "qt-%s-configure-qualified" % arch
            ]
            observed = self.bake["target"][
                "qt-%s-configure-observation" % arch
            ]
            self.assertEqual(qualified["target"], "qt-target-configure-evidence")
            self.assertEqual(qualified["args"], observed["args"])
            self.assertEqual(qualified["contexts"], observed["contexts"])
        stage = self.dockerfile.split(" AS qt-target-configure", 1)[1]
        self.assertIn("RUN --network=none", stage)
        self.assertIn("configure-qt-target.sh", stage)
        script = (REPOSITORY / "scripts/configure-qt-target.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("-qt-host-path", script)
        self.assertIn("-DCMAKE_TOOLCHAIN_FILE=", script)
        self.assertIn(
            "-DPKG_CONFIG_HOST_EXECUTABLE=/usr/bin/pkg-config", script
        )
        self.assertIn("sha256sum --check --status", script)
        self.assertIn("patch --batch --forward --fuzz=0", script)
        self.assertIn("vld1q_dup_u16(&params->neon.scale)", script)
        self.assertIn("Qt target XNNPACK incompatible load remains", script)
        patch = REPOSITORY / self.plan["patches"][0]["file"]
        self.assertTrue(patch.is_file())
        self.assertIn(
            self.plan["patches"][0]["upstream"]["commit"],
            patch.read_text(encoding="utf-8"),
        )
        self.assertIn("CMAKE_CROSSCOMPILING_EMULATOR", script)
        self.assertNotIn("qemu", script.lower())
        qualified_stage = self.dockerfile.split(
            " AS qt-target-configure-qualified", 1
        )[1]
        self.assertIn("qualify-qt-target-configure.py", qualified_stage)
        self.assertIn("qt-target-configure.schema.json", qualified_stage)
        self.assertIn("FROM qt-target-configure AS", self.dockerfile)
        self.assertIn("FROM qt-target-configure AS qt-target-webengine-build", self.dockerfile)

    def test_target_builds_are_split_from_install_checks_and_forbid_execution(self):
        self.assertEqual(
            self.bake["group"]["qt-target-webengine-built"]["targets"],
            ["qt-x86_64-webengine-build", "qt-aarch64-webengine-build"],
        )
        self.assertEqual(
            self.bake["group"]["qt-target-built"]["targets"],
            ["qt-x86_64-build", "qt-aarch64-build"],
        )
        self.assertEqual(
            self.bake["group"]["qt-target-build-qualified"]["targets"],
            ["qt-x86_64-qualified", "qt-aarch64-qualified"],
        )
        for arch in ("x86_64", "aarch64"):
            webengine = self.bake["target"][
                "qt-%s-webengine-build" % arch
            ]
            build = self.bake["target"]["qt-%s-build" % arch]
            configure = self.bake["target"][
                "qt-%s-configure-observation" % arch
            ]
            self.assertEqual(webengine["target"], "qt-target-webengine-build")
            self.assertEqual(webengine["args"], configure["args"])
            self.assertEqual(webengine["contexts"], configure["contexts"])
            self.assertEqual(build["target"], "qt-target-build-observation")
            self.assertEqual(build["args"], configure["args"])
            self.assertEqual(build["contexts"], configure["contexts"])
            qualified = self.bake["target"]["qt-%s-qualified" % arch]
            self.assertEqual(
                qualified["target"], "qt-target-qualification-evidence"
            )
            self.assertEqual(qualified["args"], configure["args"])
            self.assertEqual(qualified["contexts"], configure["contexts"])
            qualified_root = self.bake["target"][
                "qt-%s-build-qualified-root" % arch
            ]
            self.assertEqual(
                qualified_root["target"], "qt-target-build-qualified"
            )
            self.assertEqual(qualified_root["args"], configure["args"])
            self.assertEqual(qualified_root["contexts"], configure["contexts"])
            self.assertEqual(qualified_root["output"], ["type=cacheonly"])
        build_script = (REPOSITORY / "scripts/build-qt-target.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("ulimit -Sn", build_script)
        self.assertIn("CMAKE_CROSSCOMPILING_EMULATOR", build_script)
        self.assertIn("--target WebEngineCore", build_script)
        self.assertIn("print-build-log-diagnostics.py", build_script)
        self.assertNotIn("| tee", build_script)
        self.assertNotIn("libQt6Core", build_script)
        self.assertNotIn("qemu", build_script.lower())
        check_script = (
            REPOSITORY / "scripts/check-qt-target-install.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("lib/libQt6Core.so.6.8.4", check_script)
        stage = self.dockerfile.split(" AS qt-target-webengine-build", 1)[1]
        self.assertIn("FROM qt-target-configure", self.dockerfile)
        self.assertIn(
            "FROM qt-target-webengine-build AS qt-target-build", stage
        )
        self.assertIn("FROM qt-target-build AS qt-target-install-checked", stage)
        self.assertIn(
            "FROM qt-target-install-checked AS qt-target-build-qualified",
            stage,
        )
        self.assertIn("qualify-qt-target-build.py", stage)
        self.assertIn("qt-target-build.schema.json", stage)
        self.assertIn("FROM scratch AS qt-target-qualification-evidence", stage)
        self.assertIn("RUN --network=none", stage)

    def test_runtime_overlay_is_isolated_from_the_qt_build_identity(self):
        self.assertEqual(
            self.bake["group"]["qt-runtime-overlay-qualified"]["targets"],
            [
                "qt-x86_64-runtime-overlay-qualified",
                "qt-aarch64-runtime-overlay-qualified",
            ],
        )
        self.assertEqual(
            self.bake["group"]["qt-target-runtime-qualified"]["targets"],
            [
                "qt-x86_64-runtime-qualified",
                "qt-aarch64-runtime-qualified",
            ],
        )
        runtime_argument = (
            "CROSSFORGE_COMPONENT_FUTURE_QT_RUNTIME_QUALIFICATION_SHA256"
        )
        build_argument = "CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256"
        base = self.release["base_image"]
        for arch, oci_arch in (("x86_64", "amd64"), ("aarch64", "arm64")):
            runtime = self.bake["target"][
                "qt-%s-runtime-overlay-qualified" % arch
            ]
            build = self.bake["target"]["qt-%s-qualified" % arch]
            self.assertEqual(runtime["target"], "qt-runtime-overlay-evidence")
            self.assertEqual(
                runtime["dockerfile"], "docker/qt-runtime.Dockerfile"
            )
            self.assertIn(runtime_argument, runtime["args"])
            self.assertNotIn(runtime_argument, build["args"])
            self.assertNotIn(build_argument, runtime["args"])
            self.assertNotIn("QT_XNNPACK_PATCH_SHA256", runtime["args"])
            self.assertEqual(
                set(runtime["contexts"]),
                {
                    "crossforge_host_qt",
                    "crossforge_qt_runtime_rpms",
                    "crossforge_rocky_target",
                },
            )
            self.assertEqual(
                runtime["contexts"]["crossforge_qt_runtime_rpms"],
                "target:qt-runtime-rpms-%s" % arch,
            )
            self.assertEqual(
                runtime["contexts"]["crossforge_rocky_target"],
                "docker-image://%s:%s@%s"
                % (
                    base["repository"],
                    base["tag"],
                    base["manifests"][oci_arch],
                ),
            )
            qualified = self.bake["target"][
                "qt-%s-runtime-qualified" % arch
            ]
            self.assertEqual(
                qualified["target"],
                "qt-target-runtime-%s-evidence" % arch,
            )
            self.assertEqual(
                qualified["dockerfile"], "docker/qt-runtime.Dockerfile"
            )
            self.assertEqual(
                qualified["contexts"]["crossforge_qt_target"],
                "target:qt-%s-build-qualified-root" % arch,
            )
            if arch == "aarch64":
                self.assertEqual(
                    qualified["contexts"]["crossforge_qemu_validated"],
                    "target:qemu-aarch64-validated",
                )
            else:
                self.assertNotIn(
                    "crossforge_qemu_validated", qualified["contexts"]
                )
            for expanded_context in (
                "crossforge_cmake",
                "crossforge_ffmpeg_target",
                "crossforge_ninja",
                "crossforge_qt_host",
                "crossforge_qt_source",
                "crossforge_toolchain",
                "crossforge_xcb_target",
            ):
                self.assertNotIn(expanded_context, qualified["contexts"])
        stage = self.runtime_dockerfile.split(
            " AS qt-runtime-overlay-qualified", 1
        )[1].split("\nFROM ", 1)[0]
        self.assertIn(
            "FROM crossforge_host_qt AS qt-runtime-overlay-qualified",
            self.runtime_dockerfile,
        )
        self.assertIn("COPY --from=crossforge_rocky_target", stage)
        self.assertIn("COPY --from=crossforge_qt_runtime_rpms", stage)
        self.assertIn("assemble-qt-runtime.py", stage)
        self.assertIn("config/rpm/qt-runtime-el8-x86_64.plan.json", stage)
        self.assertIn("--qualification-component-sha256", stage)
        self.assertIn("RUN --network=none", stage)
        staged = self.runtime_dockerfile.split(
            " AS qt-runtime-artifacts-staged", 1
        )[1].split("\nFROM ", 1)[0]
        self.assertIn(
            "FROM crossforge_qt_target AS qt-runtime-artifacts-staged",
            self.runtime_dockerfile,
        )
        self.assertIn("--from=qt-runtime-overlay-qualified", staged)
        self.assertIn("/work/config/release.json", staged)
        self.assertIn("/work/config/schemas/release.schema.json", staged)
        self.assertIn("/work/config/schemas/qt-runtime-overlay.schema.json", staged)
        self.assertIn(
            '"$sysroot"/usr/lib64/libavcodec.so*', staged
        )
        self.assertIn("scripts/qt-plugin-probe.c", staged)
        self.assertIn("$QT_TARGET_TRIPLE-gcc", staged)
        self.assertIn("-Wall -Wextra -Werror", staged)
        self.assertIn("qt-plugin-probe", staged)
        self.assertNotIn(
            '"$sysroot"/usr/lib/libavcodec.so*', staged
        )
        runtime_stage = self.runtime_dockerfile.split(
            " AS qt-target-runtime-aarch64-qualified", 1
        )[1].split("\nFROM ", 1)[0]
        self.assertIn("run-qt-target-runtime.py", runtime_stage)
        self.assertIn("--mount=type=bind,from=crossforge_qemu_validated", runtime_stage)
        x86_stage = self.runtime_dockerfile.split(
            " AS qt-target-runtime-x86_64-qualified", 1
        )[1].split("\nFROM ", 1)[0]
        self.assertIn("run-qt-target-runtime.py", x86_stage)
        self.assertNotIn("qemu", x86_stage.lower())
        native = self.bake["target"]["qt-aarch64-native-runtime-root"]
        self.assertEqual(native["target"], "qt-native-runtime-root")
        self.assertEqual(
            native["dockerfile"], "docker/qt-runtime.Dockerfile"
        )
        self.assertNotIn("crossforge_qemu_validated", native["contexts"])
        self.assertFalse(any(key.startswith("QEMU_") for key in native["args"]))
        self.assertEqual(
            self.bake["group"]["qt-native-runtime-root"]["targets"],
            ["qt-aarch64-native-runtime-root"],
        )
        native_stage = self.runtime_dockerfile.split(
            " AS qt-native-runtime-root", 1
        )[1].split("\nFROM ", 1)[0]
        self.assertIn("FROM scratch", self.runtime_dockerfile)
        self.assertIn("--from=qt-runtime-artifacts-staged", native_stage)
        self.assertNotIn("qemu", native_stage.lower())

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
