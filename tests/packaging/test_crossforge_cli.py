import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "tools"))

from crossforge import cli, environment


class CrossforgeCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        self.make_tool_tree()

    def tearDown(self):
        self.temporary.cleanup()

    def executable(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def make_tool_tree(self):
        suffixes = (
            "gcc",
            "g++",
            "ar",
            "as",
            "ld",
            "nm",
            "objcopy",
            "objdump",
            "ranlib",
            "readelf",
            "strip",
        )
        for arch, record in environment.TARGETS.items():
            triple = record["triple"]
            for suffix in suffixes:
                self.executable(
                    "opt/crossforge/targets/%s/bin/%s-%s"
                    % (triple, triple, suffix)
                )
            (self.root / "opt/crossforge/sysroots/el8" / arch).mkdir(
                parents=True
            )
            for relative in (
                "opt/crossforge/cmake/%s" % record["cmake"],
                "opt/crossforge/meson/%s.ini" % triple,
            ):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
        for suffix in ("gcc", "g++"):
            self.executable("opt/rh/gcc-toolset-15/root/usr/bin/" + suffix)
        for suffix in suffixes[2:]:
            self.executable("usr/bin/" + suffix)
        self.executable("opt/crossforge/vcpkg/root/vcpkg")
        vcpkg_toolchain = self.root / "opt/crossforge/vcpkg/root/scripts/buildsystems/vcpkg.cmake"
        vcpkg_toolchain.parent.mkdir(parents=True)
        vcpkg_toolchain.write_text("fixture\n", encoding="utf-8")
        (self.root / "opt/crossforge/vcpkg/triplets").mkdir(parents=True)
        build = self.executable(
            "opt/crossforge/python/cp314/build/bin/python3.14"
        )
        self.assertTrue(build.is_file())
        sysconfig = (
            self.root
            / "opt/crossforge/python/cp314/targets/aarch64-unknown-linux-gnu"
            / "lib/python3.14/_sysconfigdata_crossforge.py"
        )
        sysconfig.parent.mkdir(parents=True)
        sysconfig.write_text("build_time_vars = {}\n", encoding="utf-8")

    def test_target_python_and_vcpkg_environment_is_explicit_and_isolated(self):
        base = {"PATH": "/usr/bin", "PRESERVE": "yes"}
        result = environment.build_environment(
            self.release,
            root=self.root,
            target="aarch64",
            python="3.14",
            vcpkg=True,
            linkage="dynamic",
            base=base,
        )
        self.assertEqual(base, {"PATH": "/usr/bin", "PRESERVE": "yes"})
        self.assertEqual(result["CROSSFORGE_TARGET"], "aarch64")
        self.assertIn("aarch64-unknown-linux-gnu-gcc --sysroot=", result["CC"])
        self.assertEqual(
            result["VCPKG_DEFAULT_TRIPLET"], "crossforge-arm64-el8-dynamic"
        )
        self.assertTrue(
            result["CMAKE_TOOLCHAIN_FILE"].endswith(
                "/vcpkg/root/scripts/buildsystems/vcpkg.cmake"
            )
        )
        self.assertTrue(
            result["MESON_CROSS_FILE"].endswith(
                "/meson/aarch64-unknown-linux-gnu.ini"
            )
        )
        self.assertEqual(
            result["_PYTHON_SYSCONFIGDATA_NAME"],
            "_sysconfigdata_crossforge",
        )
        self.assertNotIn("PYTHONPATH", result)

    def test_cross_target_cmake_and_native_host_are_distinct(self):
        target = environment.build_environment(
            self.release,
            root=self.root,
            target="x86_64",
            base={"PATH": "/usr/bin"},
        )
        host_ld = self.root / "usr/bin/ld"
        host_ld.rename(self.root / "usr/bin/ld.bfd")
        host_ld.symlink_to("ld.bfd")
        host = environment.build_environment(
            self.release, root=self.root, base={"PATH": "/usr/bin"}
        )
        self.assertIn("x86_64-unknown-linux-gnu.cmake", target["CMAKE_TOOLCHAIN_FILE"])
        self.assertEqual(target["CROSSFORGE_TARGET"], "x86_64")
        self.assertEqual(host["CROSSFORGE_TARGET"], "host")
        self.assertTrue(host["LD"].endswith("/usr/bin/ld"))
        self.assertNotIn("CROSSFORGE_SYSROOT", host)
        with self.assertRaises(environment.EnvironmentError):
            environment.build_environment(
                self.release,
                root=self.root,
                python="3.14",
                base={"PATH": "/usr/bin"},
            )

    def test_info_reports_installed_state_without_guessing(self):
        nfpm = self.release["nfpm"]
        self.executable(
            "opt/crossforge/host-tools/nfpm/%s/bin/nfpm" % nfpm["version"]
        )
        document = cli.info_document(self.release, self.root)
        self.assertEqual(document["targets"], ["aarch64", "x86_64"])
        self.assertTrue(document["vcpkg"]["installed"])
        self.assertTrue(document["nfpm"]["installed"])
        installed_python = {
            item["minor"]: item["installed"] for item in document["python"]
        }
        self.assertTrue(installed_python["3.14"])
        self.assertFalse(installed_python["3.13"])

    def test_package_command_hides_the_locked_nfpm_backend(self):
        parser = cli.parser()
        arguments = parser.parse_args(
            [
                "package",
                "--config",
                str(REPOSITORY / "tests/packaging/fixtures/basic/crosspack.json"),
                "--staging-root",
                "/staging",
                "--output-directory",
                "/output",
            ]
        )
        self.assertFalse(hasattr(arguments, "nfpm"))
        with mock.patch.object(cli.crosspack, "package") as package:
            with mock.patch("builtins.print"):
                cli.package_command(self.release, arguments)
        nfpm = self.release["nfpm"]
        package.assert_called_once_with(
            arguments.config,
            arguments.staging_root,
            arguments.output_directory,
            Path("/opt/crossforge/host-tools/nfpm")
            / nfpm["version"]
            / "bin/nfpm",
            nfpm["version"],
            nfpm["binary"]["extracted_sha256"],
        )

    def test_launcher_sources_remain_python36_compatible(self):
        for path in (
            REPOSITORY / "tools/crossforge/environment.py",
            REPOSITORY / "tools/crossforge/cli.py",
            REPOSITORY / "tools/crossforge/__main__.py",
            REPOSITORY / "tools/crossforge/launcher",
        ):
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 6),
            )


if __name__ == "__main__":
    unittest.main()
