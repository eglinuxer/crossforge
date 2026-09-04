"""Single user-facing command line for the Crossforge SDK."""

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from . import crosspack
from .environment import EnvironmentError, TARGETS, build_environment


RELEASE_PATH = Path("/opt/crossforge/release.json")


class CliError(ValueError):
    pass


MANAGED_ENVIRONMENT_KEYS = {
    "AR",
    "AS",
    "CC",
    "CMAKE_TOOLCHAIN_FILE",
    "CROSSFORGE_CACHE_ROOT",
    "CROSSFORGE_HOME",
    "CROSSFORGE_PYTHON",
    "CROSSFORGE_PYTHON_BUILD",
    "CROSSFORGE_PYTHON_PREFIX",
    "CROSSFORGE_SYSROOT",
    "CROSSFORGE_TARGET",
    "CROSSFORGE_TARGET_TRIPLE",
    "CXX",
    "HOME",
    "LD",
    "MESON_CROSS_FILE",
    "NM",
    "OBJCOPY",
    "OBJDUMP",
    "PATH",
    "PKG_CONFIG_LIBDIR",
    "PKG_CONFIG_SYSROOT_DIR",
    "PYTHON_FOR_BUILD",
    "RANLIB",
    "READELF",
    "STRIP",
    "VCPKG_DEFAULT_BINARY_CACHE",
    "VCPKG_DEFAULT_HOST_TRIPLET",
    "VCPKG_DEFAULT_TRIPLET",
    "VCPKG_DISABLE_METRICS",
    "VCPKG_DOWNLOADS",
    "VCPKG_FORCE_SYSTEM_BINARIES",
    "VCPKG_OVERLAY_TRIPLETS",
    "VCPKG_ROOT",
    "XDG_CACHE_HOME",
    "_PYTHON_SYSCONFIGDATA_NAME",
    "_PYTHON_SYSCONFIGDATA_PATH",
}


def require(condition, message):
    if not condition:
        raise CliError(message)


def load_release(path=RELEASE_PATH):
    release = crosspack.load_json(path)
    require(release.get("schema_version") == 1, "unsupported release schema")
    return release


def selection_arguments(parser):
    parser.add_argument("--target", choices=sorted(TARGETS))
    parser.add_argument("--python")
    parser.add_argument("--vcpkg", action="store_true")
    parser.add_argument("--linkage", choices=("static", "dynamic"), default="static")


def info_document(release, root=Path("/")):
    python_rows = []
    entries = sorted(
        release["python"]["versions"],
        key=lambda item: tuple(
            int(part) for part in item["version"].split(".")
        ),
    )
    for entry in entries:
        minor = entry["version"].rsplit(".", 1)[0]
        row = "cp" + minor.replace(".", "")
        python_rows.append(
            {
                "minor": minor,
                "version": entry["version"],
                "installed": (root / "opt/crossforge/python" / row).is_dir(),
            }
        )
    nfpm = release["nfpm"]
    return {
        "schema_version": 1,
        "kind": "crossforge-info",
        "name": release["product"]["name"],
        "version": release["product"]["version"],
        "baseline": release["baseline"],
        "host": "x86_64",
        "targets": sorted(TARGETS),
        "python": python_rows,
        "vcpkg": {
            "version": release["vcpkg"]["release"]["tag"],
            "installed": (root / "opt/crossforge/vcpkg/root/vcpkg").is_file(),
        },
        "nfpm": {
            "version": nfpm["version"],
            "installed": (
                root
                / "opt/crossforge/host-tools/nfpm"
                / nfpm["version"]
                / "bin/nfpm"
            ).is_file(),
        },
    }


def print_info(release, as_json):
    document = info_document(release)
    if as_json:
        print(json.dumps(document, indent=2, sort_keys=True))
        return
    print("Crossforge %s EL8 SDK" % document["version"])
    print("host: x86_64")
    print("targets: x86_64, aarch64")
    print(
        "Python: "
        + ", ".join(
            "%s%s" % (item["minor"], "" if item["installed"] else " (unavailable)")
            for item in document["python"]
        )
    )
    print("vcpkg: %s" % document["vcpkg"]["version"])
    print("nFPM: %s" % document["nfpm"]["version"])


def selected_environment(release, arguments, root=Path("/"), base=None):
    return build_environment(
        release,
        root=root,
        target=arguments.target,
        python=arguments.python,
        vcpkg=arguments.vcpkg,
        linkage=arguments.linkage,
        base=base,
    )


def managed_environment(environment):
    return {
        key: environment[key]
        for key in sorted(MANAGED_ENVIRONMENT_KEYS)
        if key in environment
    }


def environment_document(release, arguments, root=Path("/"), base=None):
    selected = selected_environment(release, arguments, root=root, base=base)
    return {
        "schema_version": 1,
        "kind": "crossforge-environment",
        "selection": {
            "target": arguments.target or "host",
            "python": arguments.python,
            "vcpkg": arguments.vcpkg,
            "linkage": arguments.linkage if arguments.vcpkg else None,
        },
        "environment": managed_environment(selected),
    }


def environment_command(release, arguments):
    document = environment_document(release, arguments)
    if arguments.json or arguments.format == "json":
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    for key, value in document["environment"].items():
        print("export %s=%s" % (key, shlex.quote(value)))
    return 0


def run_command(release, arguments):
    command = list(arguments.command)
    if command and command[0] == "--":
        command.pop(0)
    require(command, "run requires a command after --")
    os.execvpe(command[0], command, selected_environment(release, arguments))


def shell_command(release, arguments):
    shell = arguments.shell or os.environ.get("SHELL") or "/bin/bash"
    require(os.path.isabs(shell), "shell path must be absolute")
    os.execvpe(shell, [shell], selected_environment(release, arguments))


def package_command(release, arguments):
    config = crosspack.load_json(arguments.config)
    crosspack.validate_config(config)
    target = config["target"]
    triple = TARGETS[target]["triple"]
    nfpm = release["nfpm"]
    nfpm_path = (
        Path("/opt/crossforge/host-tools/nfpm")
        / nfpm["version"]
        / "bin/nfpm"
    )
    tool_root = Path("/opt/crossforge/targets") / triple / "bin"
    readelf = tool_root / (triple + "-readelf")
    objcopy = tool_root / (triple + "-objcopy")
    sysroot = Path("/opt/crossforge/sysroots/el8") / target
    crosspack.package(
        arguments.config,
        arguments.staging_root,
        arguments.output_directory,
        nfpm_path,
        nfpm["version"],
        nfpm["binary"]["extracted_sha256"],
        readelf,
        sysroot,
        objcopy,
    )
    print("packaged %s staged tree: %s" % (target, arguments.output_directory))
    return 0


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--version", action="store_true")
    subparsers = result.add_subparsers(dest="subcommand")
    info = subparsers.add_parser("info", allow_abbrev=False)
    info.add_argument("--json", action="store_true")
    run = subparsers.add_parser("run", allow_abbrev=False)
    selection_arguments(run)
    run.add_argument("command", nargs=argparse.REMAINDER)
    shell = subparsers.add_parser("shell", allow_abbrev=False)
    selection_arguments(shell)
    shell.add_argument("--shell")
    environment = subparsers.add_parser("env", allow_abbrev=False)
    selection_arguments(environment)
    environment.add_argument("--format", choices=("shell", "json"), default="shell")
    environment.add_argument("--json", action="store_true")
    package = subparsers.add_parser("package", allow_abbrev=False)
    package.add_argument("--config", type=Path, required=True)
    package.add_argument("--staging-root", type=Path, required=True)
    package.add_argument("--output-directory", type=Path, required=True)
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        if arguments.version:
            require(
                arguments.subcommand is None,
                "--version cannot be combined with a command",
            )
            release = load_release()
            print("crossforge %s" % release["product"]["version"])
            return 0
        require(
            arguments.subcommand in ("info", "env", "run", "shell", "package"),
            "a subcommand is required",
        )
        release = load_release()
        if arguments.subcommand == "info":
            print_info(release, arguments.json)
            return 0
        if arguments.subcommand == "run":
            return run_command(release, arguments)
        if arguments.subcommand == "shell":
            return shell_command(release, arguments)
        if arguments.subcommand == "env":
            return environment_command(release, arguments)
        return package_command(release, arguments)
    except (CliError, CrosspackError, EnvironmentError, KeyError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


CrosspackError = crosspack.CrosspackError
