import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
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

    def test_native_bytecode_normalization_preserves_optimization_and_file_set(self):
        block = self.native.split('PYTHONHASHSEED=0 "$python"', 1)[1]
        block = 'PYTHONHASHSEED=0 "$python"' + block.split(
            "\nPYTHON_BYTECODE", 1
        )[0] + "\nPYTHON_BYTECODE\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "fixture.with.dots.py"
            source.write_text('"module docstring"\nvalues = {"one", "two", "three"}\n')
            import py_compile
            import marshal
            caches = []
            for level in range(3):
                caches.append(Path(py_compile.compile(
                    str(source), optimize=level,
                    invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
                )))
            paths = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            environment = dict(os.environ, python=sys.executable, prefix=str(root),
                               build_directory=str(root / "build"),
                               PYTHONHASHSEED="random")
            subprocess.run(["bash", "-Eeuo", "pipefail", "-c", block],
                           env=environment, check=True)
            first = [cache.read_bytes() for cache in caches]
            self.assertEqual(paths, sorted(str(path.relative_to(root))
                                          for path in root.rglob("*")))
            for level, data in enumerate(first):
                self.assertEqual(int.from_bytes(data[4:8], "little"), 3)
                code = marshal.loads(data[16:])
                self.assertEqual(code.co_filename, str(source))
                self.assertEqual("module docstring" in code.co_consts, level < 2)
            os.utime(source, (1234567890, 1234567890))
            subprocess.run(["bash", "-Eeuo", "pipefail", "-c", block],
                           env=environment, check=True)
            self.assertEqual(first, [cache.read_bytes() for cache in caches])

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("cc", "ar", "ranlib")),
                         "native archive tools are unavailable")
    def test_native_archive_normalization_removes_symbol_index_timestamp(self):
        command = next(line for line in self.native.splitlines()
                       if line.startswith('find "$prefix" -type f -name'))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            obj = root / "fixture.o"
            subprocess.run(["cc", "-xc", "-c", "-o", str(obj), "-"],
                           input="int fixture(void) { return 1; }\n", text=True,
                           check=True)
            archive = root / "first.a"
            subprocess.run(["ar", "rcD", str(archive), str(obj)], check=True)
            original = archive.read_bytes()
            self.assertEqual(original[8:24].strip(), b"/")
            for name, timestamp in (("first.a", b"123"), ("second.a", b"456")):
                (root / name).write_bytes(original[:24] + timestamp.ljust(12)
                                         + original[36:])
            subprocess.run(["bash", "-Eeuo", "pipefail", "-c", command],
                           env=dict(os.environ, prefix=str(root),
                                    RANLIB=shutil.which("ranlib")), check=True)
            self.assertEqual((root / "first.a").read_bytes(), original)
            self.assertEqual((root / "second.a").read_bytes(), original)

    def test_cross_build_has_no_runner_and_suppresses_bytecode_writes(self):
        self.assertIn("unset HOSTRUNNER", self.cross)
        self.assertIn("export PYTHONDONTWRITEBYTECODE=1", self.cross)
        self.assertNotIn("qemu", self.cross.lower())

    def test_isolated_guard_probes_do_not_modify_imported_python_tree(self):
        probes = re.findall(r'if "\$build_python" ([^\n]+?) -c', self.cross)
        self.assertEqual(len(probes), 2)
        for flags in probes:
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                (directory / "guard_fixture.py").write_text("value = 1\n")
                subprocess.run(
                    [sys.executable, *shlex.split(flags), "-c",
                     "import sys; sys.path.insert(0, sys.argv[1]); import guard_fixture",
                     str(directory)],
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                    check=True,
                )
                self.assertFalse((directory / "__pycache__").exists())

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

    def test_cp39_and_cp310_use_the_explicit_legacy_setup_build_boundary(self):
        for name, script in (("native", self.native), ("cross", self.cross)):
            with self.subTest(script=name):
                self.assertIn(
                    'if [[ "$minor" == 3.9 || "$minor" == 3.10 ]]',
                    script,
                )
                self.assertIn('if [[ "$legacy_setup_build" -eq 0 ]]', script)
                self.assertIn("configure_arguments+=(", script)
                self.assertIn("--with-pkg-config=yes", script)
                self.assertIn("export PYTHONSTRICTEXTENSIONBUILD=1", script)
                self.assertIn(
                    "'$(PYTHON_FOR_BUILD) $(srcdir)/setup.py $$quiet build'",
                    script,
                )
                self.assertIn(
                    "'$(PYTHON_FOR_BUILD) $(srcdir)/setup.py install'",
                    script,
                )

        self.assertIn("--with-build-python=\"$build_python\"", self.cross)
        self.assertIn(
            "unset HOSTRUNNER MAKEFLAGS MFLAGS PYTHON_FOR_BUILD PYTHON_FOR_REGEN",
            self.cross,
        )
        self.assertIn('export PYTHON_FOR_REGEN="$build_python"', self.cross)
        self.assertIn(
            'grep -Fqx "PYTHON_FOR_REGEN?=$build_python" Makefile',
            self.cross,
        )
        for fragment in (
            "legacy_python_for_build='_PYTHON_PROJECT_BASE=$(abs_builddir)'",
            "legacy_python_for_build+=' _PYTHON_HOST_PLATFORM=$(_PYTHON_HOST_PLATFORM)'",
            "legacy_python_for_build+=' PYTHONPATH=$(srcdir)/Lib'",
            "legacy_python_for_build+=' _PYTHON_SYSCONFIGDATA_NAME="
            "_sysconfigdata_$(ABIFLAGS)_$(MACHDEP)_$(MULTIARCH)'",
            "legacy_python_for_build+=' _PYTHON_SYSCONFIGDATA_PATH=$(shell test -f "
            "pybuilddir.txt && echo $(abs_builddir)/`cat pybuilddir.txt`)'",
            'legacy_python_for_build+=" $build_python"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.cross)
        self.assertIn(
            '[[ "$python_for_build_line" == "PYTHON_FOR_BUILD=$legacy_python_for_build" ]]',
            self.cross,
        )

    def test_legacy_and_newer_rows_have_distinct_hostrunner_contracts(self):
        self.assertIn("legacy CPython unexpectedly defines HOSTRUNNER", self.cross)
        self.assertIn("grep -Eq '^HOSTRUNNER=' Makefile", self.cross)
        self.assertIn("grep -Eq '^HOSTRUNNER=[[:space:]]*$' Makefile", self.cross)
        self.assertIn('export PATH="$toolchain/bin:/usr/bin:/bin"', self.cross)
        self.assertNotIn('PATH="$build_python', self.cross)
        self.assertNotIn('command -v "python$minor"', self.cross)

    def test_only_cp39_omits_disable_test_modules(self):
        for name, script in (("native", self.native), ("cross", self.cross)):
            with self.subTest(script=name):
                self.assertIn("disable_test_modules=1", script)
                self.assertIn('if [[ "$minor" == 3.9 ]]', script)
                self.assertIn("disable_test_modules=0", script)
                self.assertIn(
                    'if [[ "$disable_test_modules" -eq 1 ]]', script
                )
                self.assertEqual(script.count("--disable-test-modules"), 1)

    def test_legacy_cross_build_preflights_both_sysconfig_implementations(self):
        for required in (
            'make -j"$jobs" pybuilddir.txt',
            "legacy target sysconfigdata is not unique",
            "_PYTHON_SYSCONFIGDATA_PATH=\"$target_sysconfig_directory\"",
            'PYTHONPATH="$source_directory/Lib"',
            '"$script_directory/verify-python-build-sysconfig.py"',
            '--cc "$CC"',
            '--ar "$AR"',
            '--ldshared "$CC -shared $LDFLAGS $LDFLAGS"',
            '--multiarch "$target_multiarch"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.cross)
        self.assertLess(
            self.cross.index('make -j"$jobs" pybuilddir.txt'),
            self.cross.index('make -j"$jobs"\n'),
        )

    def test_configure_rejects_every_unrecognized_option_warning(self):
        expected = "grep -Fq 'unrecognized options:' config.log"
        for name, script in (("native", self.native), ("cross", self.cross)):
            with self.subTest(script=name):
                self.assertEqual(script.count(expected), 1)
                self.assertIn(
                    "CPython configure accepted an unsupported option", script
                )

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
