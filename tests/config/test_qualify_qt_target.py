import ast
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
CONFIGURE_SCRIPT = REPOSITORY / "scripts/qualify-qt-target-configure.py"
CONFIGURE = runpy.run_path(str(CONFIGURE_SCRIPT))
BUILD_SCRIPT = REPOSITORY / "scripts/qualify-qt-target-build.py"
BUILD = runpy.run_path(str(BUILD_SCRIPT))


class QualifyQtTargetConfigureTests(unittest.TestCase):
    def test_target_contract_distinguishes_cross_features_and_warnings(self):
        self.assertIn("cross_compile", CONFIGURE["ENABLED_FEATURES"])
        self.assertIn("webengine_build_gn", CONFIGURE["DISABLED_REVIEWED"])
        self.assertEqual(
            CONFIGURE["reviewed_warnings"]("x86_64"),
            [
                "clang-lupdate-parser-disabled",
                "documentation-qdoc-disabled-without-clang",
            ],
        )
        self.assertIn(
            "qtwebengine-thumb-check-matches-arm64",
            CONFIGURE["reviewed_warnings"]("aarch64"),
        )

    def test_cache_parser_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CMakeCache.txt"
            path.write_text("A:BOOL=ON\nA:STRING=ON\n", encoding="utf-8")
            with self.assertRaises(CONFIGURE["QualificationError"]):
                CONFIGURE["parse_cache"](path)

    def test_target_scripts_remain_python36_compatible(self):
        for script in (CONFIGURE_SCRIPT, BUILD_SCRIPT):
            ast.parse(
                script.read_text(encoding="utf-8"),
                filename=str(script),
                feature_version=(3, 6),
            )

    def test_build_contract_covers_desktop_elf_and_manifest_drift(self):
        self.assertEqual(len(BUILD["COMPONENTS"]), 13)
        self.assertEqual(len(BUILD["LIBRARIES"]), 13)
        self.assertEqual(
            {name for name, _path in BUILD["PLUGINS"]},
            {"offscreen", "xcb"},
        )
        self.assertEqual(
            BUILD["TARGETS"]["aarch64"]["manifest_duplicate_headers"],
            [
                "usr/include/QtGui/6.8.4/QtGui/private/"
                "qdrawhelper_neon_p.h"
            ],
        )
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Qt target downstream CMake build", source)
        self.assertIn("CMAKE_SKIP_BUILD_RPATH=TRUE", source)
        self.assertIn('LINKER:-rpath,/usr/lib', source)
        self.assertIn('evidence_path="consumer/qt-target-consumer"', source)
        self.assertIn("CMAKE_CROSSCOMPILING_EMULATOR", source)
        self.assertNotIn("qemu", source.lower())

    def test_build_contract_rejects_staging_runpath(self):
        header = "Machine: Advanced Micro Devices X86-64\n"
        dynamic = (
            "(RUNPATH) Library runpath: "
            "[/opt/crossforge/qualification/qt/6.8.4/targets/"
            "x86_64-unknown-linux-gnu/usr/lib]\n"
        )
        with self.assertRaisesRegex(
            BUILD["QualificationError"], "unsafe RPATH"
        ):
            BUILD["parse_elf"](
                header, dynamic, "target-consumer", require_soname=False
            )


if __name__ == "__main__":
    unittest.main()
