#!/usr/bin/env python3
"""Build reproducible crosspack qualification packages for both targets."""

import argparse
import copy
import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


TARGETS = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
SCRIPT_FIELDS = ("pre_install", "post_install", "pre_remove", "post_remove")


class QualificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise QualificationError(message)


def run(arguments):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "command failed (%s):\n%s%s"
        % (
            " ".join(str(argument) for argument in arguments),
            process.stdout,
            process.stderr,
        ),
    )


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_json(path, document):
    Path(path).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def populate_staging(root, arch):
    triple = TARGETS[arch]
    compiler = Path("/opt/crossforge/targets") / triple / "bin" / (
        triple + "-gcc"
    )
    sysroot = Path("/opt/crossforge/sysroots/el8") / arch
    require(compiler.is_file() and sysroot.is_dir(), "target SDK is missing")
    (root / "usr/bin").mkdir(parents=True)
    (root / "etc").mkdir(parents=True)
    (root / "usr/include/crossforge").mkdir(parents=True)
    (root / "usr/lib64").mkdir(parents=True)
    (root / "usr/share/crossforge").mkdir(parents=True)
    (root / "var/lib/crossforge-demo").mkdir(parents=True)
    source = root / "probe.c"
    source.write_text(
        "#include <stdio.h>\n"
        "int crossforge_package_probe(void) { return puts(\"crossforge\"); }\n",
        encoding="utf-8",
    )
    library = root / "usr/lib64/libcrossforge-demo.so.1"
    run(
        [
            compiler,
            "--sysroot=" + str(sysroot),
            "-shared",
            "-fPIC",
            "-g",
            "-O2",
            "-ffile-prefix-map=%s=/usr/src/debug/crossforge-demo" % root.parent,
            "-fdebug-prefix-map=%s=/usr/src/debug/crossforge-demo" % root.parent,
            "-Wl,-soname,libcrossforge-demo.so.1",
            "-o",
            library,
            source,
        ]
    )
    source.unlink()
    (root / "usr/lib64/libcrossforge-demo.so").symlink_to(
        "libcrossforge-demo.so.1"
    )
    (root / "usr/include/crossforge/demo.h").write_text(
        "int crossforge_package_probe(void);\n", encoding="utf-8"
    )
    tool = root / "usr/bin/crossforge-demo"
    tool.write_text("#!/bin/sh\nprintf 'crossforge-package-probe\\n'\n", encoding="utf-8")
    tool.chmod(0o755)
    (root / "etc/crossforge-demo.conf").write_text(
        "mode=qualified\n", encoding="utf-8"
    )
    (root / "usr/share/crossforge/README").write_text(
        "crossforge package qualification\n", encoding="utf-8"
    )


def package_config(template, arch, crosspack):
    config = copy.deepcopy(template)
    config["target"] = arch
    for component in config["components"]:
        component["relations"]["deb"]["depends"] = []
        component["relations"]["rpm"]["requires"] = []
    config["debug_symbols"] = {"component": "debug"}
    config["components"].append(
        {
            "name": "debug",
            "package_names": {
                "deb": "crossforge-demo-dbgsym",
                "rpm": "crossforge-demo-debuginfo",
            },
            "description": "Crosspack detached debug symbols",
            "files": [],
            "relations": {
                "components": ["runtime"],
                "deb": {
                    field: [] for field in crosspack["DEB_RELATION_FIELDS"]
                },
                "rpm": {
                    field: [] for field in crosspack["RPM_RELATION_FIELDS"]
                },
            },
        }
    )
    return config


def write_scriptlets(root, config):
    runtime = next(
        component
        for component in config["components"]
        if component["name"] == "runtime"
    )
    runtime["scripts"] = {"deb": {}, "rpm": {}}
    for packager in ("deb", "rpm"):
        for field in SCRIPT_FIELDS:
            relative = Path("scripts") / packager / (field + ".sh")
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "#!/bin/sh\n"
                "printf '%%s\\n' 'crossforge-%s-%s' >> /crossforge-scriptlets.log\n"
                % (packager, field.replace("_", "-")),
                encoding="utf-8",
            )
            runtime["scripts"][packager][field] = relative.as_posix()


def compare_outputs(first, second, result):
    for artifact in result["artifacts"]:
        relative = Path(artifact["path"])
        require(
            (first / relative).read_bytes() == (second / relative).read_bytes(),
            "nFPM output is not byte reproducible: %s" % relative,
        )


def write_install_contract(root, plan):
    hashes = []
    symlinks = []
    directories = []
    for package in plan["packages"]:
        for content in package["contents"]:
            if content["type"] == "file":
                hashes.append(
                    "%s  .%s" % (content["sha256"], content["destination"])
                )
            elif content["type"] == "symlink":
                symlinks.append(
                    {
                        "path": content["destination"],
                        "target": content["link_target"],
                    }
                )
            else:
                directories.append(content["destination"])
    hashes.sort()
    (root / "installed.sha256").write_text(
        "\n".join(hashes) + "\n", encoding="utf-8"
    )
    write_json(root / "installed-links.json", sorted(symlinks, key=lambda item: item["path"]))
    write_json(root / "installed-directories.json", sorted(directories))


def validate_plan(plan, arch):
    require(plan.get("target") == arch, "crosspack plan target differs")
    audit = plan.get("elf_audit")
    require(
        isinstance(audit, dict)
        and audit.get("elf_count") == 2
        and audit.get("providers_count", 0) > 0,
        "crosspack ELF audit coverage differs",
    )
    debug = plan.get("debug_symbols")
    require(
        isinstance(debug, dict)
        and debug.get("component") == "debug"
        and debug.get("generated_count") == 1
        and len(debug.get("files", [])) == 1,
        "crosspack debug-symbol report differs",
    )
    packages = {item["component"]: item for item in plan.get("packages", [])}
    require(
        set(packages) == {"runtime", "development", "tools", "debug"}
        and packages["debug"]["relations"]["components"] == ["runtime"],
        "crosspack split-package set differs",
    )
    require(
        packages["tools"]["architecture"] == "independent"
        and packages["tools"]["architecture_qualification"]
        == "declared-independent"
        and packages["tools"]["architectures"]
        == {"deb": "all", "rpm": "noarch"}
        and all(
            item.get("elf") is None
            for item in packages["tools"]["contents"]
        ),
        "crosspack independent package differs",
    )
    runtime_scripts = packages["runtime"]["scripts"]
    require(
        all(
            runtime_scripts[packager][field].get("interpreter") == "/bin/sh"
            and len(runtime_scripts[packager][field].get("sha256", "")) == 64
            for packager in ("deb", "rpm")
            for field in SCRIPT_FIELDS
        )
        and runtime_scripts["deb"] != runtime_scripts["rpm"],
        "crosspack lifecycle-script plan differs",
    )
    runtime = next(
        content
        for content in packages["runtime"]["contents"]
        if content["destination"] == "/usr/lib64/libcrossforge-demo.so.1"
    )
    require(
        runtime["elf"]["machine"] == arch
        and runtime["elf"]["soname"] == "libcrossforge-demo.so.1"
        and runtime["elf"]["needed"] == ["libc.so.6"]
        and runtime["elf"]["runpath"] == []
        and runtime["elf"]["runpath_resolution"] == []
        and len(runtime["elf"]["needed_providers"]) == 1
        and runtime["elf"]["needed_providers"][0]["soname"] == "libc.so.6"
        and runtime["elf"]["needed_providers"][0]["kind"] == "sysroot"
        and runtime["elf"]["exports_count"] >= 1,
        "crosspack runtime ELF audit differs",
    )


def build(
    template_path,
    release_path,
    crosspack_path,
    crossforge_cli,
    output_root,
):
    crosspack = runpy.run_path(str(crosspack_path))
    template = crosspack["load_json"](template_path)
    release = crosspack["load_json"](release_path)
    nfpm = release["nfpm"]
    output_root = Path(output_root)
    require(
        not output_root.exists() and not output_root.is_symlink(),
        "qualification output already exists",
    )
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = Path(
        tempfile.mkdtemp(prefix=".crosspack-qualification.", dir=str(output_root.parent))
    )
    reports = {}
    independent_observations = {}
    try:
        with tempfile.TemporaryDirectory(
            prefix="crossforge-crosspack-build-"
        ) as temporary_text:
            temporary = Path(temporary_text)
            for arch in ("x86_64", "aarch64"):
                staging = temporary / arch / "staging"
                populate_staging(staging, arch)
                config = package_config(template, arch, crosspack)
                write_scriptlets(temporary / arch, config)
                config_path = temporary / arch / "crosspack.json"
                write_json(config_path, config)
                first = temporary / arch / "first"
                second = temporary / arch / "second"
                for output in (first, second):
                    run(
                        [
                            crossforge_cli,
                            "package",
                            "build",
                            "--config",
                            config_path,
                            "--staging-root",
                            staging,
                            "--output-directory",
                            output,
                        ]
                    )
                result = crosspack["load_json"](
                    first / "crosspack-result.json"
                )
                repeated = crosspack["load_json"](
                    second / "crosspack-result.json"
                )
                require(result == repeated, "repeated crosspack result differs")
                compare_outputs(first, second, result)
                plan = crosspack["load_json"](first / "crosspack-plan.json")
                validate_plan(plan, arch)
                upgrade_staging = temporary / arch / "upgrade-staging"
                shutil.copytree(str(staging), str(upgrade_staging), symlinks=True)
                (upgrade_staging / "etc/crossforge-demo.conf").write_text(
                    "mode=upgrade-default\n", encoding="utf-8"
                )
                upgrade_config = copy.deepcopy(config)
                upgrade_config["project"]["release"] = {
                    "deb": "5",
                    "rpm": "5",
                }
                upgrade_config_path = temporary / arch / "crosspack-upgrade.json"
                write_json(upgrade_config_path, upgrade_config)
                upgrade = temporary / arch / "upgrade"
                run(
                    [
                        crossforge_cli,
                        "package",
                        "build",
                        "--config",
                        upgrade_config_path,
                        "--staging-root",
                        upgrade_staging,
                        "--output-directory",
                        upgrade,
                    ]
                )
                upgrade_result = crosspack["load_json"](
                    upgrade / "crosspack-result.json"
                )
                for package in plan["packages"]:
                    if package["architecture"] != "independent":
                        continue
                    component = package["component"]
                    observation = {
                        "package": package,
                        "artifacts": sorted(
                            (
                                item
                                for item in result["artifacts"]
                                if item["component"] == component
                            ),
                            key=lambda item: item["format"],
                        ),
                        "upgrade_artifacts": sorted(
                            (
                                item
                                for item in upgrade_result["artifacts"]
                                if item["component"] == component
                            ),
                            key=lambda item: item["format"],
                        ),
                    }
                    if component in independent_observations:
                        require(
                            independent_observations[component] == observation,
                            "independent component differs across targets: %s"
                            % component,
                        )
                    else:
                        independent_observations[component] = observation
                destination = temporary_output / arch
                shutil.copytree(str(first), str(destination))
                shutil.copytree(str(upgrade), str(destination / "upgrade"))
                write_install_contract(destination, plan)
                reports[arch] = {
                    "target": arch,
                    "config_sha256": canonical_sha256(config),
                    "plan_sha256": result["plan_sha256"],
                    "artifacts": result["artifacts"],
                    "elf_audit": plan["elf_audit"],
                    "debug_symbols": plan["debug_symbols"],
                    "upgrade": {
                        "release": "5",
                        "artifacts": upgrade_result["artifacts"],
                        "plan_sha256": upgrade_result["plan_sha256"],
                    },
                }
        require(
            set(independent_observations) == {"tools"},
            "independent qualification coverage differs",
        )
        independent_components = {}
        for component, observation in independent_observations.items():
            independent_components[component] = {
                "status": "verified-independent",
                "package_sha256": canonical_sha256(observation["package"]),
                "artifacts": {
                    item["format"]: item["sha256"]
                    for item in observation["artifacts"]
                },
                "upgrade_artifacts": {
                    item["format"]: item["sha256"]
                    for item in observation["upgrade_artifacts"]
                },
            }
        report = {
            "schema_version": 1,
            "kind": "crossforge-crosspack-package-qualification",
            "status": "passed",
            "package_contract": {
                "format_specific_relations": "passed",
                "configuration_file_semantics": "passed",
                "configuration_upgrade_preserves_user_changes": "passed",
                "installed_file_attributes": "passed",
                "lifecycle_scripts": "passed",
                "verified_independent_components": "passed",
            },
            "nfpm": {
                "version": nfpm["version"],
                "sha256": nfpm["binary"]["extracted_sha256"],
            },
            "independent_components": independent_components,
            "targets": reports,
        }
        write_json(temporary_output / "qualification.json", report)
        temporary_output.rename(output_root)
    except BaseException:
        shutil.rmtree(str(temporary_output), ignore_errors=True)
        raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--crosspack", type=Path, required=True)
    parser.add_argument("--crossforge", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    build(
        arguments.template,
        arguments.release,
        arguments.crosspack,
        arguments.crossforge,
        arguments.output_root,
    )
    print("built crosspack qualification packages: %s" % arguments.output_root)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, QualificationError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
