import ast
import tempfile
import unittest
from pathlib import Path
import runpy


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/qualify-qt-host-configure.py"
QUALIFY = runpy.run_path(str(SCRIPT))


class QualifyQtHostConfigureTests(unittest.TestCase):
    def test_cache_parser_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "CMakeCache.txt"
            path.write_text("FEATURE:BOOL=ON\nFEATURE:INTERNAL=OFF\n", encoding="utf-8")
            with self.assertRaises(QUALIFY["QualificationError"]):
                QUALIFY["parse_cache"](path)

    def test_cache_parser_preserves_quoted_target_names_with_colons(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "CMakeCache.txt"
            path.write_text(
                '"QT_QMAKE_LIB_OF_TARGET_FFmpeg::avcodec":INTERNAL=avcodec\n'
                '"QT_QMAKE_LIB_OF_TARGET_FFmpeg::avformat":INTERNAL=avformat\n',
                encoding="utf-8",
            )
            self.assertEqual(
                QUALIFY["parse_cache"](path),
                {
                    '"QT_QMAKE_LIB_OF_TARGET_FFmpeg::avcodec"': "avcodec",
                    '"QT_QMAKE_LIB_OF_TARGET_FFmpeg::avformat"': "avformat",
                },
            )

    def test_required_runtime_features_cover_the_observed_gaps(self):
        enabled = set(QUALIFY["ENABLED_FEATURES"])
        self.assertTrue(
            {"ffmpeg", "gbm", "libudev", "libinput", "pulseaudio"}.issubset(
                enabled
            )
        )
        self.assertTrue(
            {"gstreamer", "qdoc", "clangcpp"}.issubset(
                set(QUALIFY["DISABLED_REVIEWED"])
            )
        )

    def test_critical_degradations_are_forbidden(self):
        self.assertIn("No media backend found", QUALIFY["FORBIDDEN_LOG"])
        self.assertIn("System GBM is disabled", QUALIFY["FORBIDDEN_LOG"])
        self.assertEqual(
            QUALIFY["REVIEWED_WARNINGS"],
            sorted(
                [
                    "documentation-qdoc-disabled-without-clang",
                    "clang-lupdate-parser-disabled",
                ]
            ),
        )

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
