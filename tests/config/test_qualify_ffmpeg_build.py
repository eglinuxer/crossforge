import ast
import tempfile
import unittest
from pathlib import Path
import runpy


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/qualify-ffmpeg-build.py"
QUALIFY = runpy.run_path(str(SCRIPT))


class QualifyFFmpegBuildTests(unittest.TestCase):
    def configuration(self, root, override=None):
        values = {
            macro: enabled
            for name, macro in QUALIFY["CONFIG_MACROS"].items()
            for enabled in [QUALIFY["CONFIGURATION"][name]]
        }
        values.update(
            {
                "CONFIG_FFMPEG": False,
                "CONFIG_FFPLAY": False,
                "CONFIG_FFPROBE": False,
            }
        )
        if override:
            values.update(override)
        path = root / "config.h"
        path.write_text(
            "".join(
                "#define %s %d\n" % (name, value)
                for name, value in sorted(values.items())
            ),
            encoding="utf-8",
        )
        return path

    def test_configuration_accepts_only_the_lgpl_shared_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.configuration(Path(temporary))
            self.assertEqual(
                QUALIFY["parse_configuration"](path),
                QUALIFY["CONFIGURATION"],
            )

    def test_configuration_rejects_gpl_or_programs(self):
        for override in (
            {"CONFIG_GPL": True},
            {"CONFIG_FFMPEG": True},
            {"CONFIG_AVFILTER": True},
        ):
            with self.subTest(override=override):
                with tempfile.TemporaryDirectory() as temporary:
                    path = self.configuration(Path(temporary), override)
                    with self.assertRaises(QUALIFY["QualificationError"]):
                        QUALIFY["parse_configuration"](path)

    def test_all_identities_bind_a_lock_and_forbid_cross_execution(self):
        identities = QUALIFY["IDENTITIES"]
        self.assertEqual(identities["host"]["tier"], "host-direct")
        self.assertEqual(
            {
                identities["x86_64-unknown-linux-gnu"]["lock_id"],
                identities["aarch64-unknown-linux-gnu"]["lock_id"],
            },
            {"qt-target-x86_64", "qt-target-aarch64"},
        )
        self.assertTrue(
            all(
                identity["tier"] == "cross-no-exec"
                for name, identity in identities.items()
                if name != "host"
            )
        )

    def test_build_profile_disables_unneeded_libraries_and_restricted_code(self):
        script = (REPOSITORY / "scripts/build-ffmpeg.sh").read_text(
            encoding="utf-8"
        )
        for option in (
            "--disable-avdevice",
            "--disable-avfilter",
            "--disable-gpl",
            "--disable-version3",
            "--disable-nonfree",
            "--disable-autodetect",
            "--enable-openssl",
            "--enable-zlib",
        ):
            self.assertIn(option, script)

    def test_script_remains_python36_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
