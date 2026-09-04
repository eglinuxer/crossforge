import ast
import json
import os
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
        self.assertTrue(Path(result["VCPKG_DOWNLOADS"]).is_dir())
        self.assertTrue(Path(result["VCPKG_DEFAULT_BINARY_CACHE"]).is_dir())

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

    def test_runtime_paths_fall_back_for_missing_home(self):
        result = environment.build_environment(
            self.release,
            root=self.root,
            target="x86_64",
            base={
                "PATH": "/usr/bin",
                "HOME": "/missing-home",
                "PKG_CONFIG_PATH": "/host/pkgconfig",
            },
        )
        self.assertNotEqual(result["HOME"], "/missing-home")
        self.assertTrue(Path(result["HOME"]).is_dir())
        self.assertTrue(Path(result["CROSSFORGE_CACHE_ROOT"]).is_dir())
        self.assertNotIn("PKG_CONFIG_PATH", result)

    def test_explicit_cache_must_be_absolute_and_not_a_symlink(self):
        with self.assertRaises(environment.EnvironmentError):
            environment.build_environment(
                self.release,
                root=self.root,
                target="x86_64",
                base={
                    "PATH": "/usr/bin",
                    "CROSSFORGE_CACHE_ROOT": "relative/cache",
                },
            )
        cache_target = self.root / "cache-target"
        cache_target.mkdir()
        cache_link = self.root / "cache-link"
        cache_link.symlink_to(cache_target.name)
        with self.assertRaises(environment.EnvironmentError):
            environment.build_environment(
                self.release,
                root=self.root,
                target="x86_64",
                base={
                    "PATH": "/usr/bin",
                    "CROSSFORGE_CACHE_ROOT": str(cache_link),
                },
            )

    def test_python_selection_removes_inherited_pythonpath(self):
        result = environment.build_environment(
            self.release,
            root=self.root,
            target="aarch64",
            python="3.14",
            base={"PATH": "/usr/bin", "PYTHONPATH": "/host/python"},
        )
        self.assertNotIn("PYTHONPATH", result)

    def test_info_reports_installed_state_without_guessing(self):
        nfpm = self.release["nfpm"]
        self.executable(
            "opt/crossforge/host-tools/nfpm/%s/bin/nfpm" % nfpm["version"]
        )
        document = cli.info_document(self.release, self.root)
        self.assertEqual(document["name"], "crossforge")
        self.assertEqual(document["version"], "0.1.0")
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
                "build",
                "--config",
                str(REPOSITORY / "tests/packaging/fixtures/basic/crosspack.json"),
                "--staging-root",
                "/staging",
                "--staging-manifest",
                "/staging.json",
                "--output-directory",
                "/output",
                "--format",
                "rpm",
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
            Path("/opt/crossforge/targets/x86_64-unknown-linux-gnu/bin/x86_64-unknown-linux-gnu-readelf"),
            Path("/opt/crossforge/sysroots/el8/x86_64"),
            Path("/opt/crossforge/targets/x86_64-unknown-linux-gnu/bin/x86_64-unknown-linux-gnu-objcopy"),
            "rpm",
            Path("/staging.json"),
        )

    def test_package_seal_does_not_invoke_build_tools(self):
        arguments = cli.parser().parse_args(
            [
                "package",
                "seal",
                "--config",
                str(REPOSITORY / "tests/packaging/fixtures/basic/crosspack.json"),
                "--staging-root",
                "/staging",
                "--variant-id",
                "1" * 64,
                "--resolution-sha256",
                "2" * 64,
                "--output",
                "/output/staging.json",
            ]
        )
        manifest = {"kind": "crossforge-sealed-staging"}
        with mock.patch.object(
            cli.crosspack,
            "create_staging_manifest",
            return_value=manifest,
        ) as create, mock.patch.object(
            cli.crosspack, "write_staging_manifest"
        ) as write, mock.patch.object(
            cli, "package_tools"
        ) as tools, mock.patch("builtins.print"):
            self.assertEqual(cli.package_command(self.release, arguments), 0)
        create.assert_called_once_with(
            arguments.config,
            arguments.staging_root,
            "1" * 64,
            "2" * 64,
        )
        write.assert_called_once_with(
            manifest, arguments.staging_root, arguments.output
        )
        tools.assert_not_called()

    def test_package_plan_does_not_invoke_nfpm(self):
        arguments = cli.parser().parse_args(
            [
                "package",
                "plan",
                "--config",
                str(REPOSITORY / "tests/packaging/fixtures/basic/crosspack.json"),
                "--staging-root",
                "/staging",
                "--staging-manifest",
                "/staging.json",
                "--output",
                "/output/plan.json",
                "--format",
                "deb",
            ]
        )
        planned = {"schema_version": 1, "kind": "test-plan"}
        with mock.patch.object(
            cli.crosspack, "plan", return_value=planned
        ) as plan, mock.patch.object(cli.crosspack, "write_json") as write, mock.patch.object(
            cli.crosspack, "package"
        ) as package:
            self.assertEqual(cli.package_command(self.release, arguments), 0)
        plan.assert_called_once_with(
            arguments.config,
            arguments.staging_root,
            Path("/opt/crossforge/targets/x86_64-unknown-linux-gnu/bin/x86_64-unknown-linux-gnu-readelf"),
            Path("/opt/crossforge/sysroots/el8/x86_64"),
            Path("/opt/crossforge/targets/x86_64-unknown-linux-gnu/bin/x86_64-unknown-linux-gnu-objcopy"),
            "deb",
            Path("/staging.json"),
        )
        write.assert_called_once_with(planned, arguments.output)
        package.assert_not_called()

    def test_env_json_is_versioned_and_never_discloses_inherited_secrets(self):
        arguments = cli.parser().parse_args(
            ["env", "--target", "aarch64", "--vcpkg", "--json"]
        )
        document = cli.environment_document(
            self.release,
            arguments,
            root=self.root,
            base={"PATH": "/usr/bin", "SECRET_TOKEN": "do-not-print"},
        )
        self.assertEqual(document["kind"], "crossforge-environment")
        self.assertEqual(document["selection"]["target"], "aarch64")
        self.assertTrue(document["selection"]["vcpkg"])
        self.assertNotIn("SECRET_TOKEN", document["environment"])
        self.assertIn("VCPKG_DOWNLOADS", document["environment"])

    def test_env_shell_quotes_values_and_run_replaces_the_launcher(self):
        arguments = cli.parser().parse_args(["env", "--target", "x86_64"])
        with mock.patch.object(
            cli, "environment_document"
        ) as document, mock.patch("builtins.print") as output:
            document.return_value = {
                "environment": {"CC": "/tool path/gcc --sysroot=/sdk"}
            }
            self.assertEqual(cli.environment_command(self.release, arguments), 0)
        output.assert_called_once_with(
            "export CC='/tool path/gcc --sysroot=/sdk'"
        )

        run = cli.parser().parse_args(["run", "--", "/usr/bin/true"])
        selected = {"PATH": "/usr/bin"}
        with mock.patch.object(
            cli, "selected_environment", return_value=selected
        ), mock.patch.object(os, "execvpe") as execute:
            cli.run_command(self.release, run)
        execute.assert_called_once_with(
            "/usr/bin/true", ["/usr/bin/true"], selected
        )

    def test_version_uses_the_canonical_release_version(self):
        with mock.patch.object(
            cli, "load_release", return_value=self.release
        ), mock.patch("builtins.print") as output:
            self.assertEqual(cli.main(["--version"]), 0)
        output.assert_called_once_with("crossforge 0.1.0")

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
