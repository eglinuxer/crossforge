#!/usr/bin/env python3
"""Qualify the composed Python, vcpkg, toolchain and packaging SDK."""

import argparse
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
COMPONENT_READER = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release_component.py")
)
ComponentError = COMPONENT_READER["ComponentError"]
TARGETS = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
PYTHON_MINORS = ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14")


class QualificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise QualificationError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            result = json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError) as error:
        raise QualificationError("cannot load %s: %s" % (path, error)) from error
    require(isinstance(result, dict), "%s must contain an object" % path)
    return result


def load_component(path, name, scope, digest):
    try:
        return COMPONENT_READER["load_component"](path, name, scope, digest)
    except ComponentError as error:
        raise QualificationError("invalid %s component: %s" % (name, error)) from error


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
    return process.stdout


def environment_output(arguments):
    output = run(arguments + ["--", "/usr/bin/env"])
    result = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        require(separator and key not in result, "invalid child environment output")
        result[key] = value
    return result


def dependency_map(component):
    return {
        item["component"]: item["canonical_sha256"]
        for item in component["dependencies"]
    }


def component_record(document):
    return {
        "component": document["component"],
        "canonical_sha256": COMPONENT_READER["canonical_sha256"](document),
    }


def qualify(arguments):
    policy = load_component(
        arguments.policy_component,
        "implementation/complete-sdk-qualification",
        "qualification",
        arguments.policy_component_sha256,
    )
    launcher = load_component(
        arguments.launcher_component,
        "implementation/launcher",
        "build",
        arguments.launcher_component_sha256,
    )
    packaging = load_component(
        arguments.packaging_component,
        "packaging/qualification",
        "qualification",
        arguments.packaging_component_sha256,
    )
    python = load_component(
        arguments.python_component,
        "python/qualification",
        "qualification",
        arguments.python_component_sha256,
    )
    qualification = load_component(
        arguments.qualification_component,
        "product/sdk-qualification",
        "qualification",
        arguments.qualification_component_sha256,
    )
    dependencies = dependency_map(qualification)
    expected_dependencies = {
        "implementation/complete-sdk-qualification": arguments.policy_component_sha256,
        "implementation/launcher": arguments.launcher_component_sha256,
        "packaging/qualification": arguments.packaging_component_sha256,
        "python/qualification": arguments.python_component_sha256,
    }
    require(dependencies == expected_dependencies, "complete SDK component closure differs")

    packaging_report = load_json(arguments.packaging_report)
    python_report = load_json(arguments.python_report)
    require(
        packaging_report.get("kind") == "crossforge-crosspack-qualification"
        and packaging_report.get("status") == "passed"
        and packaging_report.get("components", {}).get("qualification")
        == component_record(packaging),
        "packaging qualification report differs",
    )
    require(
        python_report.get("kind") == "crossforge-final-sdk-qualification"
        and python_report.get("status") == "passed"
        and python_report.get("phase") == 10
        and len(python_report.get("build_pythons", [])) == 6
        and len(python_report.get("target_pythons", [])) == 6
        and all(
            len(item.get("targets", [])) == 2
            for item in python_report.get("target_pythons", [])
        ),
        "Python final SDK qualification report differs",
    )

    info = json.loads(run([arguments.crossforge, "info", "--json"]))
    require(
        info.get("kind") == "crossforge-info"
        and info.get("targets") == ["aarch64", "x86_64"]
        and all(item.get("installed") for item in info.get("python", []))
        and len(info.get("python", [])) == 6
        and info.get("vcpkg", {}).get("installed") is True
        and info.get("nfpm", {}).get("installed") is True,
        "crossforge info does not describe the complete SDK",
    )
    require(
        run([arguments.crossforge, "--version"]).strip()
        == "crossforge %s" % info["version"],
        "crossforge version output differs from info",
    )
    environment_info = json.loads(
        run([arguments.crossforge, "env", "--json"])
    )
    require(
        environment_info.get("kind") == "crossforge-environment"
        and environment_info.get("selection", {}).get("target") == "host"
        and environment_info.get("environment", {}).get("CROSSFORGE_TARGET")
        == "host"
        and "SECRET_TOKEN" not in environment_info.get("environment", {}),
        "crossforge env does not describe a safe native environment",
    )

    parent_environment = dict(os.environ)
    host = environment_output([arguments.crossforge, "run"])
    require(
        host.get("CROSSFORGE_TARGET") == "host"
        and host.get("CC") == "/opt/rh/gcc-toolset-15/root/usr/bin/gcc",
        "native launcher environment differs",
    )
    matrix = []
    for arch in ("x86_64", "aarch64"):
        triple = TARGETS[arch]
        for minor in PYTHON_MINORS:
            for linkage in ("static", "dynamic"):
                child = environment_output(
                    [
                        arguments.crossforge,
                        "run",
                        "--target",
                        arch,
                        "--python",
                        minor,
                        "--vcpkg",
                        "--linkage",
                        linkage,
                    ]
                )
                expected_triplet = (
                    "crossforge-%s-el8%s"
                    % (
                        "x64" if arch == "x86_64" else "arm64",
                        "" if linkage == "static" else "-dynamic",
                    )
                )
                require(
                    child.get("CROSSFORGE_TARGET") == arch
                    and child.get("CROSSFORGE_TARGET_TRIPLE") == triple
                    and child.get("CROSSFORGE_PYTHON") == minor
                    and child.get("VCPKG_DEFAULT_TRIPLET") == expected_triplet
                    and child.get("CC", "").startswith(
                        "/opt/crossforge/targets/%s/bin/%s-gcc --sysroot="
                        % (triple, triple)
                    )
                    and child.get("CMAKE_TOOLCHAIN_FILE")
                    == "/opt/crossforge/vcpkg/root/scripts/buildsystems/vcpkg.cmake"
                    and child.get("MESON_CROSS_FILE")
                    == "/opt/crossforge/meson/%s.ini" % triple
                    and "PYTHONPATH" not in child,
                    "launcher matrix differs for %s/Python %s/%s"
                    % (arch, minor, linkage),
                )
                matrix.append(
                    {"target": arch, "python": minor, "linkage": linkage}
                )
    require(os.environ == parent_environment, "launcher mutated its parent environment")
    require(len(matrix) == 24, "launcher qualification matrix size differs")
    return {
        "schema_version": 1,
        "kind": "crossforge-complete-sdk-qualification",
        "status": "passed",
        "components": {
            "policy": component_record(policy),
            "launcher": component_record(launcher),
            "packaging": component_record(packaging),
            "python": component_record(python),
            "qualification": component_record(qualification),
        },
        "matrix": matrix,
        "python_phase": python_report["phase"],
        "package_targets": packaging_report["targets"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for role in ("policy", "launcher", "packaging", "python", "qualification"):
        parser.add_argument("--%s-component" % role, type=Path, required=True)
        parser.add_argument("--%s-component-sha256" % role, required=True)
    parser.add_argument("--packaging-report", type=Path, required=True)
    parser.add_argument("--python-report", type=Path, required=True)
    parser.add_argument("--crossforge", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = qualify(arguments)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(arguments.output)
    print("qualified complete Crossforge SDK: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, QualificationError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
