import ast
import unittest
from pathlib import Path
import runpy


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/qualify-qt-host-build.py"
QUALIFY = runpy.run_path(str(SCRIPT))


class QualifyQtHostBuildTests(unittest.TestCase):
    def test_downstream_component_contract_covers_desktop_modules(self):
        self.assertEqual(len(QUALIFY["COMPONENTS"]), 13)
        self.assertTrue(
            {
                "Multimedia",
                "Quick3D",
                "WaylandClient",
                "WebEngineCore",
                "WebEngineWidgets",
            }.issubset(set(QUALIFY["COMPONENTS"]))
        )
        self.assertEqual(len(QUALIFY["LIBRARIES"]), 13)
        self.assertEqual(QUALIFY["MACHINE"], "Advanced Micro Devices X86-64")

    def test_elf_parser_accepts_relative_runpath_and_rejects_unsafe_elf(self):
        header = "  Machine:                           Advanced Micro Devices X86-64\n"
        valid = " 0x000000000000000e (SONAME) Library soname: [libQt6Core.so.6]\n"
        self.assertEqual(
            QUALIFY["parse_elf"](
                header,
                valid
                + " 0x000000000000001d (RUNPATH) Library runpath: [$ORIGIN]\n",
                "Core",
            ),
            (
                "Advanced Micro Devices X86-64",
                "libQt6Core.so.6",
                [],
                ["$ORIGIN"],
            ),
        )
        self.assertEqual(
            QUALIFY["parse_elf"](
                header,
                valid
                + " 0x0 (RUNPATH) Library runpath: "
                "[/opt/crossforge/qualification/qt/6.8.4/host/lib]\n",
                "Core",
            )[3],
            ["/opt/crossforge/qualification/qt/6.8.4/host/lib"],
        )
        for diagnostic in (
            " 0x0 (RPATH) Library rpath: [/work/build]\n",
            " 0x0 (RUNPATH) Library runpath: [/usr/local/lib]\n",
            " 0x0 (TEXTREL) ignored\n",
        ):
            with self.subTest(diagnostic=diagnostic):
                with self.assertRaises(QUALIFY["QualificationError"]):
                    QUALIFY["parse_elf"](header, valid + diagnostic, "Core")

    def test_plugin_elf_may_omit_a_soname(self):
        header = "  Machine:                           Advanced Micro Devices X86-64\n"
        self.assertEqual(
            QUALIFY["parse_elf"](
                header,
                " 0x0000000000000001 (NEEDED) Shared library: [libQt6Gui.so.6]\n",
                "xcb",
                require_soname=False,
            ),
            ("Advanced Micro Devices X86-64", None, ["libQt6Gui.so.6"], []),
        )

    def test_webengine_process_resources_and_platform_plugins_are_required(self):
        self.assertIn("libexec/QtWebEngineProcess", QUALIFY["HOST_TOOLS"])
        self.assertEqual(
            QUALIFY["EXECUTABLES"],
            [("QtWebEngineProcess", "libexec/QtWebEngineProcess")],
        )
        self.assertIn("resources/qtwebengine_resources.pak", QUALIFY["RESOURCES"])
        self.assertEqual(
            {name for name, _path in QUALIFY["PLUGINS"]},
            {"offscreen", "xcb"},
        )
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('Path("/usr/bin/ldd")', source)
        self.assertIn('"not found" not in command', source)

    def test_qttools_are_part_of_the_host_contract(self):
        self.assertEqual(
            QUALIFY["HOST_TOOLS"],
            [
                "bin/assistant",
                "bin/designer",
                "bin/lrelease",
                "bin/lupdate",
                "bin/qmake6",
                "bin/qt-cmake",
                "bin/qt-configure-module",
                "bin/qtpaths6",
                "libexec/QtWebEngineProcess",
            ],
        )
        self.assertIn(
            'arguments.prefix / "bin/qt-cmake"',
            SCRIPT.read_text(encoding="utf-8"),
        )

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
