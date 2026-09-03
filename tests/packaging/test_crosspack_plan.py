import ast
import copy
import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
CROSSPACK_PATH = REPOSITORY / "tools/crossforge/crosspack.py"
CROSSPACK = runpy.run_path(str(CROSSPACK_PATH))
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
FIXTURE = REPOSITORY / "tests/packaging/fixtures/basic/crosspack.json"
CONFIG_SCHEMA = REPOSITORY / "config/schemas/crosspack.schema.json"
PLAN_SCHEMA = REPOSITORY / "config/schemas/crosspack-plan.schema.json"


def write_elf(path, machine):
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[6] = 1
    header[16:18] = (3).to_bytes(2, byteorder="little")
    header[18:20] = machine.to_bytes(2, byteorder="little")
    path.write_bytes(header)


class CrosspackPlanTests(unittest.TestCase):
    def setUp(self):
        self.config = CROSSPACK["load_json"](FIXTURE)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "staging"
        (self.root / "usr/bin").mkdir(parents=True)
        (self.root / "usr/include/crossforge").mkdir(parents=True)
        (self.root / "usr/lib64").mkdir(parents=True)
        (self.root / "usr/share/crossforge").mkdir(parents=True)
        executable = self.root / "usr/bin/crossforge-demo"
        executable.write_text("#!/bin/sh\necho crossforge\n", encoding="utf-8")
        executable.chmod(0o755)
        (self.root / "usr/include/crossforge/demo.h").write_text(
            "#define CROSSFORGE_DEMO 1\n", encoding="utf-8"
        )
        write_elf(self.root / "usr/lib64/libcrossforge-demo.so.1", 62)
        (self.root / "usr/lib64/libcrossforge-demo.so").symlink_to(
            "libcrossforge-demo.so.1"
        )
        (self.root / "usr/share/crossforge/README").write_text(
            "fixture\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_manifest_and_generated_plan_validate_against_strict_schemas(self):
        config_schema = VALIDATOR["load_json"](CONFIG_SCHEMA)
        plan_schema = VALIDATOR["load_json"](PLAN_SCHEMA)
        for schema in (config_schema, plan_schema):
            VALIDATOR["validate_schema_subset"](schema)
        VALIDATOR["validate"](
            self.config, config_schema, config_schema, "$"
        )
        plan = CROSSPACK["build_plan"](self.config, self.root)
        VALIDATOR["validate"](plan, plan_schema, plan_schema, "$")
        plan["project"]["source_date_epoch"] = 253402300800
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate"](plan, plan_schema, plan_schema, "$")

    def test_plan_has_complete_split_ownership_and_exact_dependencies(self):
        plan = CROSSPACK["build_plan"](self.config, self.root)
        self.assertEqual(
            plan["architectures"], {"deb": "amd64", "rpm": "x86_64"}
        )
        self.assertEqual(
            [item["component"] for item in plan["packages"]],
            ["development", "runtime", "tools"],
        )
        packages = {item["component"]: item for item in plan["packages"]}
        self.assertEqual(
            packages["development"]["dependencies"],
            {
                "components": ["runtime"],
                "deb": ["crossforge-demo (= 1.2.3-4)"],
                "rpm": ["crossforge-demo = 1.2.3-4"],
            },
        )
        self.assertEqual(
            packages["runtime"]["dependencies"]["deb"],
            ["libc6 (>= 2.28)"],
        )
        contents = [
            item
            for package in plan["packages"]
            for item in package["contents"]
        ]
        self.assertEqual(len(contents), 5)
        self.assertEqual(len({item["source"] for item in contents}), 5)
        self.assertEqual(len({item["destination"] for item in contents}), 5)
        library = next(item for item in contents if item.get("elf"))
        self.assertEqual(
            library["elf"],
            {"class": 64, "endianness": "little", "machine": "x86_64"},
        )

    def test_plan_identity_ignores_mtime_and_absolute_staging_path(self):
        first = CROSSPACK["build_plan"](self.config, self.root)
        os.utime(str(self.root / "usr/bin/crossforge-demo"), (1, 1))
        second = CROSSPACK["build_plan"](self.config, self.root)
        self.assertEqual(first, second)
        self.assertNotIn(str(self.root), json.dumps(first, sort_keys=True))

    def test_nfpm_configs_are_explicit_format_specific_and_non_globbing(self):
        plan = CROSSPACK["build_plan"](self.config, self.root)
        rendered = CROSSPACK["render_nfpm_configs"](plan, self.root)
        development = rendered["deb"]["development"]
        runtime_rpm = rendered["rpm"]["runtime"]
        self.assertTrue(development["disable_globbing"])
        self.assertEqual(development["mtime"], "2023-11-14T22:13:20Z")
        self.assertEqual(development["deb"], {"arch": "amd64", "compression": "gzip"})
        self.assertEqual(
            runtime_rpm["rpm"],
            {
                "arch": "x86_64",
                "buildhost": "crossforge.invalid",
                "compression": "gzip",
                "packager": self.config["project"]["maintainer"],
            },
        )
        link = next(
            item for item in development["contents"] if item.get("type") == "symlink"
        )
        self.assertEqual(link["src"], "libcrossforge-demo.so.1")
        self.assertEqual(link["dst"], "/usr/lib64/libcrossforge-demo.so")
        for packager in rendered.values():
            for package in packager.values():
                for content in package["contents"]:
                    self.assertEqual(content["file_info"]["owner"], "root")
                    self.assertEqual(content["file_info"]["group"], "root")
                    self.assertEqual(
                        content["file_info"]["mtime"], "2023-11-14T22:13:20Z"
                    )
        packages = {item["component"]: item for item in plan["packages"]}
        self.assertEqual(
            CROSSPACK["package_filename"](
                plan, packages["development"], "deb"
            ),
            "crossforge-demo-dev_1.2.3-4_amd64.deb",
        )
        self.assertEqual(
            CROSSPACK["package_filename"](plan, packages["runtime"], "rpm"),
            "crossforge-demo-1.2.3-4.x86_64.rpm",
        )

    def test_target_mapping_and_elf_machine_are_exact(self):
        self.config["target"] = "aarch64"
        write_elf(self.root / "usr/lib64/libcrossforge-demo.so.1", 183)
        plan = CROSSPACK["build_plan"](self.config, self.root)
        self.assertEqual(
            plan["architectures"], {"deb": "arm64", "rpm": "aarch64"}
        )
        write_elf(self.root / "usr/lib64/libcrossforge-demo.so.1", 62)
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](self.config, self.root)

    def test_unassigned_overlap_and_destination_collisions_fail_closed(self):
        (self.root / "unassigned").write_text("x", encoding="utf-8")
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](self.config, self.root)
        (self.root / "unassigned").unlink()

        overlap = copy.deepcopy(self.config)
        overlap["components"][0]["files"].append(
            {
                "source": "usr/include/crossforge/demo.h",
                "destination": "/opt/demo.h",
            }
        )
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](overlap, self.root)

        collision = copy.deepcopy(self.config)
        collision["components"][1]["files"][0] = {
            "source": "usr/include/crossforge/demo.h",
            "destination": "/usr/bin/crossforge-demo",
        }
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](collision, self.root)

        tree_collision = copy.deepcopy(self.config)
        tree_collision["components"][2]["files"][0] = {
            "source": "usr/share/crossforge/README",
            "destination": "/usr",
        }
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](tree_collision, self.root)

    def test_symlinks_require_owned_targets_and_declared_component_edges(self):
        missing_edge = copy.deepcopy(self.config)
        missing_edge["components"][1]["dependencies"]["components"] = []
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](missing_edge, self.root)
        (self.root / "usr/lib64/libcrossforge-demo.so").unlink()
        (self.root / "usr/lib64/libcrossforge-demo.so").symlink_to("missing.so")
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](self.config, self.root)

    def test_cycles_unsafe_paths_and_unknown_fields_are_rejected(self):
        cycle = copy.deepcopy(self.config)
        cycle["components"][0]["dependencies"]["components"] = ["tools"]
        cycle["components"][2]["dependencies"]["components"] = ["development"]
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](cycle, self.root)
        for field, value in (
            ("source", "../escape"),
            ("source", "/absolute"),
            ("destination", "relative/path"),
            ("destination", "/usr/../etc/passwd"),
        ):
            candidate = copy.deepcopy(self.config)
            candidate["components"][0]["files"][0][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(CROSSPACK["CrosspackError"]):
                    CROSSPACK["validate_config"](candidate)
        unknown = copy.deepcopy(self.config)
        unknown["project"]["unknown"] = True
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["validate_config"](unknown)
        invalid_package = copy.deepcopy(self.config)
        invalid_package["components"][0]["package_names"]["deb"] = "bad_name"
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["validate_config"](invalid_package)
        invalid_epoch = copy.deepcopy(self.config)
        invalid_epoch["project"]["source_date_epoch"] = True
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["validate_config"](invalid_epoch)

    def test_duplicate_json_keys_are_rejected(self):
        path = Path(self.temporary.name) / "duplicate.json"
        path.write_text(
            '{"$schema":"x","$schema":"y"}\n', encoding="utf-8"
        )
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["load_json"](path)

    def test_special_privileged_and_world_writable_files_are_rejected(self):
        fifo = self.root / "usr/share/crossforge/fifo"
        os.mkfifo(str(fifo))
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](self.config, self.root)
        fifo.unlink()
        executable = self.root / "usr/bin/crossforge-demo"
        for mode in (0o4755, 0o777):
            executable.chmod(mode)
            with self.subTest(mode=oct(mode)):
                with self.assertRaises(CROSSPACK["CrosspackError"]):
                    CROSSPACK["build_plan"](self.config, self.root)
        executable.chmod(0o755)

    def test_empty_directories_are_owned_explicitly(self):
        config = copy.deepcopy(self.config)
        config["components"] = [
            {
                "name": "runtime",
                "package_names": {"deb": "empty", "rpm": "empty"},
                "description": "Empty directory fixture",
                "files": [{"source": "var/lib/empty", "destination": "/var/lib/empty"}],
                "dependencies": {"components": [], "deb": [], "rpm": []},
            }
        ]
        empty_root = Path(self.temporary.name) / "empty-staging"
        (empty_root / "var/lib/empty").mkdir(parents=True)
        plan = CROSSPACK["build_plan"](config, empty_root)
        self.assertEqual(
            plan["packages"][0]["contents"][0]["type"], "directory"
        )
        nfpm = CROSSPACK["render_nfpm_configs"](plan, empty_root)
        directory = nfpm["rpm"]["runtime"]["contents"][0]
        self.assertEqual(directory["type"], "dir")
        self.assertNotIn("src", directory)

    def test_mapping_cannot_traverse_a_staged_symlink(self):
        (self.root / "alias").symlink_to("usr")
        config = copy.deepcopy(self.config)
        config["components"][0]["files"][0]["source"] = "alias/bin"
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](config, self.root)

    def test_cli_writes_the_same_canonical_plan(self):
        output = Path(self.temporary.name) / "plan.json"
        process = subprocess.run(
            [
                sys.executable,
                str(CROSSPACK_PATH),
                "plan",
                "--config",
                str(FIXTURE),
                "--staging-root",
                str(self.root),
                "--output",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")),
            CROSSPACK["build_plan"](self.config, self.root),
        )

    @unittest.skipUnless(
        os.environ.get("CROSSFORGE_NFPM")
        or shutil.which("nfpm"),
        "nFPM is not available",
    )
    def test_real_nfpm_build_is_atomic_and_reproducible(self):
        nfpm = os.environ.get("CROSSFORGE_NFPM") or shutil.which("nfpm")
        nfpm_sha256 = CROSSPACK["sha256_file"](nfpm)
        output_one = Path(self.temporary.name) / "packages-one"
        output_two = Path(self.temporary.name) / "packages-two"
        result_one = CROSSPACK["package"](
            FIXTURE, self.root, output_one, nfpm, "2.47.0", nfpm_sha256
        )
        result_two = CROSSPACK["package"](
            FIXTURE, self.root, output_two, nfpm, "2.47.0", nfpm_sha256
        )
        result_schema = VALIDATOR["load_json"](
            REPOSITORY / "config/schemas/crosspack-result.schema.json"
        )
        VALIDATOR["validate_schema_subset"](result_schema)
        VALIDATOR["validate"](
            result_one, result_schema, result_schema, "$"
        )
        self.assertEqual(result_one, result_two)
        self.assertEqual(len(result_one["artifacts"]), 6)
        for artifact in result_one["artifacts"]:
            first = output_one / artifact["path"]
            second = output_two / artifact["path"]
            self.assertEqual(first.read_bytes(), second.read_bytes())
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["package"](
                FIXTURE,
                self.root,
                Path(self.temporary.name) / "wrong-nfpm",
                nfpm,
                "2.47.0",
                "0" * 64,
            )
        artifacts = {
            (item["component"], item["format"]): output_one / item["path"]
            for item in result_one["artifacts"]
        }
        if shutil.which("dpkg-deb"):
            metadata = subprocess.check_output(
                [
                    "dpkg-deb",
                    "--field",
                    str(artifacts[("development", "deb")]),
                    "Package",
                    "Version",
                    "Architecture",
                    "Depends",
                ],
                universal_newlines=True,
            )
            self.assertIn("Package: crossforge-demo-dev", metadata)
            self.assertIn("Version: 1.2.3-4", metadata)
            self.assertIn("Architecture: amd64", metadata)
            self.assertIn("Depends: crossforge-demo (= 1.2.3-4)", metadata)
        if shutil.which("rpm"):
            rpmdb = Path(self.temporary.name) / "rpmdb"
            rpmdb.mkdir()
            metadata = subprocess.check_output(
                [
                    "rpm",
                    "--dbpath",
                    str(rpmdb),
                    "-qp",
                    "--qf",
                    "%{NAME} %{VERSION}-%{RELEASE} %{ARCH} [%{REQUIRENAME}]\n",
                    str(artifacts[("development", "rpm")]),
                ],
                universal_newlines=True,
            )
            self.assertIn(
                "crossforge-demo-devel 1.2.3-4 x86_64", metadata
            )
            self.assertIn("crossforge-demo", metadata)
        self.assertTrue((output_one / "crosspack-plan.json").is_file())
        self.assertTrue((output_one / "crosspack-result.json").is_file())
        self.assertFalse((output_one / "configs").exists())

    def test_crosspack_source_is_python36_syntax_compatible(self):
        ast.parse(
            CROSSPACK_PATH.read_text(encoding="utf-8"),
            filename=str(CROSSPACK_PATH),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
