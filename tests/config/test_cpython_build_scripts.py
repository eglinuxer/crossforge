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

    def test_build_scripts_do_not_reopen_global_row_contract(self):
        for name, script in (("native", self.native), ("cross", self.cross)):
            with self.subTest(script=name):
                self.assertNotIn("python_row_contract.py", script)
                self.assertNotIn("contract_checker", script)
                self.assertNotIn("platform_python", script)
                self.assertNotIn("ADAPTER", script)
                self.assertNotIn("adapter=", script)

    def test_cross_build_locates_all_guard_sources_from_its_script_directory(self):
        self.assertIn(
            'script_directory=$(cd "$(dirname "$0")" && pwd)',
            self.cross,
        )
        for source in (
            "deny-target-exec.c",
            "target-artifact-canary.c",
            "target-exec-canary.c",
        ):
                self.assertIn('"$script_directory/%s"' % source, self.cross)

    def test_cp314_uses_exact_private_static_zstd_flags(self):
        expected_cflags = 'LIBZSTD_CFLAGS="-I$zstd_directory/include"'
        expected_libs = (
            'LIBZSTD_LIBS="$zstd_archive -pthread '
            '-Wl,--exclude-libs,libzstd.a"'
        )
        for name, script in (("native", self.native), ("cross", self.cross)):
            with self.subTest(script=name):
                self.assertIn('if [[ "$minor" == 3.14 ]]', script)
                self.assertIn(expected_cflags, script)
                self.assertIn(expected_libs, script)
                self.assertIn("MODULE__ZSTD_STATE=yes", script)
                self.assertIn("MODULE__ZSTD_CFLAGS=", script)
                self.assertIn("MODULE__ZSTD_LDFLAGS=", script)
                self.assertIn("fell back to dynamic -lzstd", script)
                self.assertIn("pre-3.14 CPython", script)

    def test_zstd_extension_and_manifest_are_fail_closed(self):
        for name, script in (("native", self.native), ("cross", self.cross)):
            with self.subTest(script=name):
                self.assertIn("'_zstd.*.so'", script)
                self.assertIn("NEEDED.*libzstd", script)
                self.assertIn("TEXTREL|RPATH|RUNPATH", script)
                self.assertIn("ZSTD_|ZDICT_|FSE_|HUF_|XXH_", script)
                self.assertIn(".crossforge/zstd-build.json", script)
                self.assertIn(
                    'install -m 0644 "$zstd_manifest"', script
                )
                self.assertNotIn(
                    'install -m 0644 "$zstd_archive" "$prefix', script
                )
        self.assertIn("zstd.zstd_version_info != (1, 5, 7)", self.native)
        self.assertIn(
            "CompressionParameter.nb_workers.bounds()[1] < 1", self.native
        )
        self.assertNotIn("\nassert ", self.native)


if __name__ == "__main__":
    unittest.main()
