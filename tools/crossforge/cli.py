"""Single user-facing command line for the Crossforge SDK."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import crosspack
from .environment import EnvironmentError, TARGETS, build_environment


RELEASE_PATH = Path("/opt/crossforge/release.json")


class CliError(ValueError):
    pass


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
    print("Crossforge EL8 SDK")
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


def selected_environment(release, arguments):
    return build_environment(
        release,
        target=arguments.target,
        python=arguments.python,
        vcpkg=arguments.vcpkg,
        linkage=arguments.linkage,
    )


def run_command(release, arguments):
    command = list(arguments.command)
    if command and command[0] == "--":
        command.pop(0)
    require(command, "run requires a command after --")
    return subprocess.call(command, env=selected_environment(release, arguments))


def shell_command(release, arguments):
    shell = arguments.shell or os.environ.get("SHELL") or "/bin/bash"
    require(os.path.isabs(shell), "shell path must be absolute")
    return subprocess.call([shell], env=selected_environment(release, arguments))


def package_command(release, arguments):
    config = crosspack.load_json(arguments.config)
    crosspack.validate_config(config)
    target = config["target"]
    nfpm = release["nfpm"]
    nfpm_path = (
        Path("/opt/crossforge/host-tools/nfpm")
        / nfpm["version"]
        / "bin/nfpm"
    )
    crosspack.package(
        arguments.config,
        arguments.staging_root,
        arguments.output_directory,
        nfpm_path,
        nfpm["version"],
        nfpm["binary"]["extracted_sha256"],
    )
    print("packaged %s staged tree: %s" % (target, arguments.output_directory))
    return 0


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = result.add_subparsers(dest="subcommand")
    info = subparsers.add_parser("info", allow_abbrev=False)
    info.add_argument("--json", action="store_true")
    run = subparsers.add_parser("run", allow_abbrev=False)
    selection_arguments(run)
    run.add_argument("command", nargs=argparse.REMAINDER)
    shell = subparsers.add_parser("shell", allow_abbrev=False)
    selection_arguments(shell)
    shell.add_argument("--shell")
    package = subparsers.add_parser("package", allow_abbrev=False)
    package.add_argument("--config", type=Path, required=True)
    package.add_argument("--staging-root", type=Path, required=True)
    package.add_argument("--output-directory", type=Path, required=True)
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        require(
            arguments.subcommand in ("info", "run", "shell", "package"),
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
        return package_command(release, arguments)
    except (CliError, CrosspackError, EnvironmentError, KeyError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


CrosspackError = crosspack.CrosspackError
