import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]


class CPythonBuildScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native = (REPOSITORY / "scripts/build-cpython-native.sh").read_text(
            encoding="utf-8"
        )
        cls.cross = (REPOSITORY / "scripts/build-cpython-cross.sh").read_text(
            encoding="utf-8"
        )

    def test_builds_clear_compiler_and_python_search_injection(self):
        required = (
            "CPATH",
            "C_INCLUDE_PATH",
            "CPLUS_INCLUDE_PATH",
            "LIBRARY_PATH",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
            "CPPFLAGS",
            "LIBS",
            "PKG_CONFIG_LIBDIR",
            "PYTHON*|_PYTHON*|ac_cv_*",
        )
        for name, script in (("native", self.native), ("cross", self.cross)):
            for value in required:
                with self.subTest(script=name, value=value):
                    self.assertIn(value, script)

    def test_cross_build_has_no_runner_and_suppresses_bytecode_writes(self):
        self.assertIn("unset HOSTRUNNER", self.cross)
        self.assertIn("export PYTHONDONTWRITEBYTECODE=1", self.cross)
        self.assertNotIn("qemu", self.cross.lower())

    def test_build_outputs_must_start_absent(self):
        for script in (self.native, self.cross):
            self.assertIn('[[ ! -e "$build_directory"', script)
            self.assertIn('[[ ! -e "$prefix"', script)


if __name__ == "__main__":
    unittest.main()
