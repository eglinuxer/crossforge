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


def empty_relations(components=()):
    return {
        "components": list(components),
        "deb": {field: [] for field in CROSSPACK["DEB_RELATION_FIELDS"]},
        "rpm": {field: [] for field in CROSSPACK["RPM_RELATION_FIELDS"]},
    }


class CrosspackPlanTests(unittest.TestCase):
    def setUp(self):
        self.config = CROSSPACK["load_json"](FIXTURE)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "staging"
        (self.root / "usr/bin").mkdir(parents=True)
        (self.root / "etc").mkdir(parents=True)
        (self.root / "usr/include/crossforge").mkdir(parents=True)
        (self.root / "usr/lib64").mkdir(parents=True)
        (self.root / "usr/share/crossforge").mkdir(parents=True)
        executable = self.root / "usr/bin/crossforge-demo"
        executable.write_text("#!/bin/sh\necho crossforge\n", encoding="utf-8")
        executable.chmod(0o755)
        (self.root / "etc/crossforge-demo.conf").write_text(
            "mode=fixture\n", encoding="utf-8"
        )
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

    def debug_config(self):
        config = copy.deepcopy(self.config)
        config["debug_symbols"] = {"component": "debug"}
        config["components"].append(
            {
                "name": "debug",
                "package_names": {
                    "deb": "crossforge-demo-dbgsym",
                    "rpm": "crossforge-demo-debuginfo",
                },
                "description": "Detached debug symbols",
                "files": [],
                "relations": empty_relations(["runtime"]),
            }
        )
        return config

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

    def test_plan_has_complete_split_ownership_and_exact_relations(self):
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
            packages["development"]["relations"]["components"], ["runtime"]
        )
        self.assertEqual(
            packages["development"]["relations"]["deb"]["depends"],
            ["crossforge-demo (= 1.2.3-4)"],
        )
        self.assertEqual(
            packages["development"]["relations"]["rpm"]["requires"],
            ["crossforge-demo = 1.2.3-4"],
        )
        self.assertEqual(
            packages["runtime"]["relations"]["deb"]["depends"],
            ["libc6 (>= 2.28)"],
        )
        contents = [
            item
            for package in plan["packages"]
            for item in package["contents"]
        ]
        self.assertEqual(len(contents), 6)
        self.assertEqual(len({item["source"] for item in contents}), 6)
        self.assertEqual(len({item["destination"] for item in contents}), 6)
        config = next(
            item
            for item in contents
            if item["destination"] == "/etc/crossforge-demo.conf"
        )
        self.assertEqual(config["mode"], 0o640)
        self.assertEqual(config["owner"], "root")
        self.assertEqual(config["group"], "root")
        self.assertEqual(config["config"], "noreplace")
        library = next(item for item in contents if item.get("elf"))
        self.assertEqual(
            library["elf"],
            {
                "class": 64,
                "endianness": "little",
                "machine": "x86_64",
                "type": "dynamic",
            },
        )

    def test_plan_identity_ignores_mtime_and_absolute_staging_path(self):
        first = CROSSPACK["build_plan"](self.config, self.root)
        os.utime(str(self.root / "usr/bin/crossforge-demo"), (1, 1))
        second = CROSSPACK["build_plan"](self.config, self.root)
        self.assertEqual(first, second)
        plan_schema = VALIDATOR["load_json"](PLAN_SCHEMA)
        VALIDATOR["validate"](first, plan_schema, plan_schema, "$")
        self.assertNotIn(str(self.root), json.dumps(first, sort_keys=True))

    def test_nfpm_configs_are_explicit_format_specific_and_non_globbing(self):
        plan = CROSSPACK["build_plan"](self.config, self.root)
        rendered = CROSSPACK["render_nfpm_configs"](plan, self.root)
        development = rendered["deb"]["development"]
        runtime_rpm = rendered["rpm"]["runtime"]
        self.assertTrue(development["disable_globbing"])
        self.assertEqual(development["mtime"], "2023-11-14T22:13:20Z")
        self.assertEqual(
            development["deb"],
            {
                "arch": "amd64",
                "compression": "gzip",
                "predepends": [],
                "breaks": [],
            },
        )
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
        runtime_deb = rendered["deb"]["runtime"]
        runtime_config = next(
            item
            for item in runtime_deb["contents"]
            if item["dst"] == "/etc/crossforge-demo.conf"
        )
        self.assertEqual(runtime_config["type"], "config|noreplace")
        self.assertEqual(runtime_config["file_info"]["mode"], 0o640)
        self.assertEqual(runtime_deb["provides"], ["crossforge-demo-virtual"])
        self.assertEqual(runtime_rpm["provides"], ["crossforge-demo-virtual"])
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

    def test_all_format_specific_relations_map_to_nfpm(self):
        plan = CROSSPACK["build_plan"](self.config, self.root)
        runtime = next(
            item for item in plan["packages"] if item["component"] == "runtime"
        )
        runtime["relations"]["deb"] = {
            "depends": ["deb-dep"],
            "pre_depends": ["deb-pre"],
            "recommends": ["deb-rec"],
            "suggests": ["deb-suggest"],
            "conflicts": ["deb-conflict"],
            "provides": ["deb-provide"],
            "replaces": ["deb-replace"],
            "breaks": ["deb-break"],
        }
        runtime["relations"]["rpm"] = {
            "requires": ["rpm-require"],
            "recommends": ["rpm-rec"],
            "suggests": ["rpm-suggest"],
            "conflicts": ["rpm-conflict"],
            "provides": ["rpm-provide"],
            "obsoletes": ["rpm-obsolete"],
        }
        deb = CROSSPACK["nfpm_config"](plan, runtime, "deb", self.root)
        rpm = CROSSPACK["nfpm_config"](plan, runtime, "rpm", self.root)
        self.assertEqual(deb["depends"], ["deb-dep"])
        self.assertEqual(deb["recommends"], ["deb-rec"])
        self.assertEqual(deb["suggests"], ["deb-suggest"])
        self.assertEqual(deb["conflicts"], ["deb-conflict"])
        self.assertEqual(deb["provides"], ["deb-provide"])
        self.assertEqual(deb["replaces"], ["deb-replace"])
        self.assertEqual(deb["deb"]["predepends"], ["deb-pre"])
        self.assertEqual(deb["deb"]["breaks"], ["deb-break"])
        self.assertEqual(rpm["depends"], ["rpm-require"])
        self.assertEqual(rpm["recommends"], ["rpm-rec"])
        self.assertEqual(rpm["suggests"], ["rpm-suggest"])
        self.assertEqual(rpm["conflicts"], ["rpm-conflict"])
        self.assertEqual(rpm["provides"], ["rpm-provide"])
        self.assertEqual(rpm["replaces"], ["rpm-obsolete"])

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

    def test_target_readelf_audits_needed_runpath_and_exports(self):
        readelf = shutil.which("readelf")
        if readelf is None or not Path("/bin/true").is_file():
            self.skipTest("host ELF audit fixture is unavailable")
        shutil.copy2(
            "/bin/true", self.root / "usr/lib64/libcrossforge-demo.so.1"
        )
        plan = CROSSPACK["build_plan"](
            self.config, self.root, readelf, Path("/")
        )
        self.assertEqual(plan["elf_audit"]["elf_count"], 1)
        self.assertGreater(plan["elf_audit"]["providers_count"], 0)
        runtime = next(
            content
            for package in plan["packages"]
            for content in package["contents"]
            if content["destination"]
            == "/usr/lib64/libcrossforge-demo.so.1"
        )
        self.assertEqual(runtime["elf"]["machine"], "x86_64")
        self.assertEqual(runtime["elf"]["runpath"], [])
        self.assertEqual(runtime["elf"]["runpath_resolution"], [])
        self.assertEqual(
            [item["soname"] for item in runtime["elf"]["needed_providers"]],
            runtime["elf"]["needed"],
        )
        self.assertTrue(
            all(
                item["kind"] == "sysroot"
                for item in runtime["elf"]["needed_providers"]
            )
        )
        self.assertRegex(runtime["elf"]["exports_sha256"], r"^[0-9a-f]{64}$")
        self.assertGreaterEqual(runtime["elf"]["exports_count"], 0)
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](
                self.config, self.root, "/bin/true", Path("/")
            )
        with self.assertRaises(CROSSPACK["ElfError"]):
            CROSSPACK["ELF"]["dynamic_identity"](
                "0x (RPATH) Library rpath: [/tmp/host]\n"
            )
        identity = CROSSPACK["ELF"]["dynamic_identity"](
            "0x (RUNPATH) Library runpath: [$ORIGIN/../lib]\n"
        )
        self.assertEqual(identity["runpath"], ["$ORIGIN/../lib"])
        for runpath in (
            "$ORIGIN/lib/../alt",
            "$ORIGIN/${LIB}",
            "$ORIGIN//lib",
        ):
            with self.subTest(runpath=runpath):
                with self.assertRaises(CROSSPACK["ElfError"]):
                    CROSSPACK["ELF"]["dynamic_identity"](
                        "0x (RUNPATH) Library runpath: [%s]\n" % runpath
                    )
        compiler = shutil.which("gcc")
        if compiler is not None:
            missing_source = Path(self.temporary.name) / "missing.c"
            consumer_source = Path(self.temporary.name) / "consumer.c"
            missing_library = Path(self.temporary.name) / "libmissing-crossforge.so.1"
            missing_source.write_text(
                "int missing_crossforge(void) { return 1; }\n",
                encoding="utf-8",
            )
            consumer_source.write_text(
                "extern int missing_crossforge(void);\n"
                "int consumer(void) { return missing_crossforge(); }\n",
                encoding="utf-8",
            )
            for command in (
                [
                    compiler,
                    "-shared",
                    "-fPIC",
                    "-Wl,-soname,libmissing-crossforge.so.1",
                    "-o",
                    str(missing_library),
                    str(missing_source),
                ],
                [
                    compiler,
                    "-shared",
                    "-fPIC",
                    "-Wl,-soname,libcrossforge-demo.so.1",
                    "-o",
                    str(self.root / "usr/lib64/libcrossforge-demo.so.1"),
                    str(consumer_source),
                    "-L" + self.temporary.name,
                    "-Wl,--no-as-needed",
                    "-l:libmissing-crossforge.so.1",
                ],
            ):
                process = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
            (self.root / "usr/share/crossforge/libmissing-crossforge.so.1").write_text(
                "not an ELF provider\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                CROSSPACK["CrosspackError"], "unresolved DT_NEEDED"
            ):
                CROSSPACK["build_plan"](
                    self.config, self.root, readelf, Path("/")
                )

    def test_loader_resolution_enforces_private_prefix_and_component_edges(self):
        provider = {
            "type": "file",
            "destination": "/opt/acme/demo/lib/libprivate.so.1",
            "sha256": "1" * 64,
            "elf": {
                "type": "dynamic",
                "soname": "libprivate.so.1",
            },
        }
        packages = {"app": [], "runtime": [provider]}
        index = CROSSPACK["ELF"]["destination_index"](packages)
        resolved = CROSSPACK["ELF"]["resolve_needed_provider"](
            "app",
            "/opt/acme/demo/bin/app",
            "libprivate.so.1",
            ["$ORIGIN/../lib"],
            index,
            {"app": ["runtime"]},
            self.root,
        )
        self.assertEqual(resolved["kind"], "package")
        self.assertEqual(resolved["component"], "runtime")
        self.assertEqual(resolved["destination"], provider["destination"])

        with self.assertRaisesRegex(
            CROSSPACK["ElfError"], "undeclared component dependency"
        ):
            CROSSPACK["ELF"]["resolve_needed_provider"](
                "app",
                "/opt/acme/demo/bin/app",
                "libprivate.so.1",
                ["$ORIGIN/../lib"],
                index,
                {"app": []},
                self.root,
            )
        with self.assertRaisesRegex(
            CROSSPACK["ElfError"], "unresolved DT_NEEDED provider"
        ):
            CROSSPACK["ELF"]["resolve_needed_provider"](
                "app",
                "/opt/acme/demo/bin/app",
                "libprivate.so.1",
                ["$ORIGIN"],
                index,
                {"app": ["runtime"]},
                self.root,
            )

    def test_loader_resolution_rejects_parent_escape_and_ambiguous_providers(self):
        self.assertEqual(
            CROSSPACK["ELF"]["resolve_runpath"](
                "/opt/acme/demo/bin/app", ["$ORIGIN/../lib"]
            ),
            [
                {
                    "entry": "$ORIGIN/../lib",
                    "directory": "/opt/acme/demo/lib",
                }
            ],
        )
        for destination, runpath in (
            ("/usr/bin/app", "$ORIGIN/../lib"),
            ("/opt/acme/demo/bin/app", "$ORIGIN/../../escape"),
        ):
            with self.subTest(destination=destination, runpath=runpath):
                with self.assertRaisesRegex(
                    CROSSPACK["ElfError"], "private install prefix"
                ):
                    CROSSPACK["ELF"]["resolve_runpath"](
                        destination, [runpath]
                    )

        packages = {"app": [], "runtime": []}
        for directory, digest in (("lib", "2"), ("alt", "3")):
            packages["runtime"].append(
                {
                    "type": "file",
                    "destination": "/opt/acme/demo/%s/libprivate.so.1"
                    % directory,
                    "sha256": digest * 64,
                    "elf": {
                        "type": "dynamic",
                        "soname": "libprivate.so.1",
                    },
                }
            )
        with self.assertRaisesRegex(
            CROSSPACK["ElfError"], "ambiguous DT_NEEDED provider"
        ):
            CROSSPACK["ELF"]["resolve_needed_provider"](
                "app",
                "/opt/acme/demo/bin/app",
                "libprivate.so.1",
                ["$ORIGIN/../lib", "$ORIGIN/../alt"],
                CROSSPACK["ELF"]["destination_index"](packages),
                {"app": ["runtime"]},
                self.root,
            )

    def test_elf_audit_allows_a_pure_script_package(self):
        readelf = shutil.which("readelf")
        if readelf is None:
            self.skipTest("host readelf is unavailable")
        root = Path(self.temporary.name) / "script-staging"
        (root / "usr/bin").mkdir(parents=True)
        script = root / "usr/bin/crossforge-script"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        config = {
            "$schema": self.config["$schema"],
            "schema_version": 1,
            "project": copy.deepcopy(self.config["project"]),
            "target": "x86_64",
            "debug_symbols": None,
            "components": [
                {
                    "name": "tools",
                    "package_names": {
                        "deb": "crossforge-script",
                        "rpm": "crossforge-script",
                    },
                    "description": "Pure script package",
                    "files": [
                        {"source": "usr/bin", "destination": "/usr/bin"}
                    ],
                    "relations": empty_relations(),
                }
            ],
        }
        plan = CROSSPACK["build_plan"](config, root, readelf, Path("/"))
        self.assertEqual(plan["elf_audit"]["elf_count"], 0)

    def test_debug_symbols_are_split_with_target_objcopy_reproducibly(self):
        compiler = shutil.which("gcc")
        readelf = shutil.which("readelf")
        objcopy = shutil.which("objcopy")
        if None in (compiler, readelf, objcopy):
            self.skipTest("host ELF build tools are unavailable")
        source = Path(self.temporary.name) / "debug-probe.c"
        source.write_text(
            "int crossforge_debug_probe(void) { return 42; }\n",
            encoding="utf-8",
        )
        library = self.root / "usr/lib64/libcrossforge-demo.so.1"
        process = subprocess.run(
            [
                compiler,
                "-shared",
                "-fPIC",
                "-g",
                "-Wl,-soname,libcrossforge-demo.so.1",
                "-o",
                str(library),
                str(source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        original_sha256 = CROSSPACK["sha256_file"](library)
        config = self.debug_config()
        config_path = Path(self.temporary.name) / "debug-crosspack.json"
        config_path.write_text(
            json.dumps(config, sort_keys=True), encoding="utf-8"
        )
        first = CROSSPACK["plan"](
            config_path, self.root, readelf, Path("/"), objcopy
        )
        second = CROSSPACK["plan"](
            config_path, self.root, readelf, Path("/"), objcopy
        )
        self.assertEqual(first, second)
        plan_schema = VALIDATOR["load_json"](PLAN_SCHEMA)
        VALIDATOR["validate"](first, plan_schema, plan_schema, "$")
        with tempfile.TemporaryDirectory() as prepared_text:
            _effective, prepared, _debug = CROSSPACK[
                "prepare_debug_staging"
            ](config, self.root, objcopy, Path(prepared_text))
            stripped = prepared / "usr/lib64/libcrossforge-demo.so.1"
            detached = (
                prepared
                / ".crossforge-debug-symbols/usr/lib/debug/usr/lib64"
                / "libcrossforge-demo.so.1.debug"
            )
            stripped_sections = subprocess.check_output(
                [readelf, "--sections", str(stripped)],
                universal_newlines=True,
            )
            debug_sections = subprocess.check_output(
                [readelf, "--sections", str(detached)],
                universal_newlines=True,
            )
            debuglink = subprocess.check_output(
                [readelf, "--string-dump=.gnu_debuglink", str(stripped)]
            )
            self.assertNotIn(".debug_info", stripped_sections)
            self.assertIn(".gnu_debuglink", stripped_sections)
            self.assertIn(".debug_info", debug_sections)
            self.assertIn(b"libcrossforge-demo.so.1.debug", debuglink)
        self.assertEqual(CROSSPACK["sha256_file"](library), original_sha256)
        self.assertEqual(first["debug_symbols"]["generated_count"], 1)
        record = first["debug_symbols"]["files"][0]
        self.assertEqual(
            record["debug_destination"],
            "/usr/lib/debug/usr/lib64/libcrossforge-demo.so.1.debug",
        )
        self.assertNotEqual(record["runtime_sha256"], original_sha256)
        packages = {item["component"]: item for item in first["packages"]}
        self.assertEqual(
            packages["debug"]["relations"]["components"], ["runtime"]
        )
        self.assertEqual(len(packages["debug"]["contents"]), 1)
        self.assertEqual(first["elf_audit"]["elf_count"], 2)

        invalid = self.debug_config()
        invalid["components"][-1]["relations"]["components"] = []
        invalid_path = Path(self.temporary.name) / "invalid-debug.json"
        invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["plan"](
                invalid_path, self.root, readelf, Path("/"), objcopy
            )

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
        missing_edge["components"][1]["relations"]["components"] = []
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](missing_edge, self.root)
        (self.root / "usr/lib64/libcrossforge-demo.so").unlink()
        (self.root / "usr/lib64/libcrossforge-demo.so").symlink_to("missing.so")
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["build_plan"](self.config, self.root)

    def test_cycles_unsafe_paths_and_unknown_fields_are_rejected(self):
        cycle = copy.deepcopy(self.config)
        cycle["components"][0]["relations"]["components"] = ["tools"]
        cycle["components"][2]["relations"]["components"] = ["development"]
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

        for attributes in (
            {},
            {"mode": "0777"},
            {"mode": "755"},
            {"owner": "Unsafe Owner"},
            {"group": "root", "unknown": True},
            {"config": "replace-always"},
        ):
            with self.subTest(attributes=attributes):
                candidate = copy.deepcopy(self.config)
                candidate["components"][0]["files"][0][
                    "attributes"
                ] = attributes
                with self.assertRaises(CROSSPACK["CrosspackError"]):
                    CROSSPACK["validate_config"](candidate)

        recursive_attributes = copy.deepcopy(self.config)
        recursive_attributes["components"][0]["files"][0]["attributes"] = {
            "owner": "root"
        }
        with self.assertRaisesRegex(
            CROSSPACK["CrosspackError"], "cannot be applied recursively"
        ):
            CROSSPACK["build_plan"](recursive_attributes, self.root)

        malformed_relations = copy.deepcopy(self.config)
        malformed_relations["components"][0]["relations"]["deb"][
            "unknown"
        ] = []
        with self.assertRaises(CROSSPACK["CrosspackError"]):
            CROSSPACK["validate_config"](malformed_relations)

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
                "relations": empty_relations(),
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
        readelf = shutil.which("readelf")
        objcopy = shutil.which("objcopy")
        if readelf is None or objcopy is None or not Path("/bin/true").is_file():
            self.skipTest("host ELF tool fixture is unavailable")
        shutil.copy2(
            "/bin/true", self.root / "usr/lib64/libcrossforge-demo.so.1"
        )
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
                "--readelf",
                readelf,
                "--sysroot",
                "/",
                "--objcopy",
                objcopy,
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
            CROSSPACK["build_plan"](
                self.config, self.root, readelf, Path("/")
            ),
        )

    @unittest.skipUnless(
        os.environ.get("CROSSFORGE_NFPM")
        or shutil.which("nfpm"),
        "nFPM is not available",
    )
    def test_real_nfpm_build_is_atomic_and_reproducible(self):
        nfpm = os.environ.get("CROSSFORGE_NFPM") or shutil.which("nfpm")
        readelf = shutil.which("readelf")
        objcopy = shutil.which("objcopy")
        if readelf is None or objcopy is None or not Path("/bin/true").is_file():
            self.skipTest("host ELF tool fixture is unavailable")
        shutil.copy2(
            "/bin/true", self.root / "usr/lib64/libcrossforge-demo.so.1"
        )
        nfpm_sha256 = CROSSPACK["sha256_file"](nfpm)
        output_one = Path(self.temporary.name) / "packages-one"
        output_two = Path(self.temporary.name) / "packages-two"
        result_one = CROSSPACK["package"](
            FIXTURE,
            self.root,
            output_one,
            nfpm,
            "2.47.0",
            nfpm_sha256,
            readelf,
            Path("/"),
            objcopy,
        )
        result_two = CROSSPACK["package"](
            FIXTURE,
            self.root,
            output_two,
            nfpm,
            "2.47.0",
            nfpm_sha256,
            readelf,
            Path("/"),
            objcopy,
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
                readelf,
                Path("/"),
                objcopy,
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
        for path in (CROSSPACK_PATH, REPOSITORY / "tools/crossforge/elf.py"):
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 6),
            )


if __name__ == "__main__":
    unittest.main()
