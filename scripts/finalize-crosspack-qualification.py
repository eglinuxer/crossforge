#!/usr/bin/env python3
"""Bind crosspack package/install observations into one qualification report."""

import argparse
import json
import runpy
import sys
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
COMPONENT_READER = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release_component.py")
)
ComponentError = COMPONENT_READER["ComponentError"]


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


def dependency_map(component):
    return {
        item["component"]: item["canonical_sha256"]
        for item in component["dependencies"]
    }


def finalize(arguments):
    source = load_component(
        arguments.source_component,
        "sources/nfpm",
        "build",
        arguments.source_component_sha256,
    )
    implementation = load_component(
        arguments.implementation_component,
        "implementation/crosspack",
        "build",
        arguments.implementation_component_sha256,
    )
    launcher = load_component(
        arguments.launcher_component,
        "implementation/launcher",
        "build",
        arguments.launcher_component_sha256,
    )
    sdk = load_component(
        arguments.sdk_component,
        "packaging/sdk-build",
        "build",
        arguments.sdk_component_sha256,
    )
    policy = load_component(
        arguments.policy_component,
        "implementation/crosspack-qualification",
        "qualification",
        arguments.policy_component_sha256,
    )
    qualification = load_component(
        arguments.qualification_component,
        "packaging/qualification",
        "qualification",
        arguments.qualification_component_sha256,
    )
    sdk_dependencies = dependency_map(sdk)
    require(
        set(sdk_dependencies)
        == {
            "implementation/crosspack",
            "implementation/launcher",
            "sources/nfpm",
            "vcpkg/sdk-build",
        }
        and sdk_dependencies["implementation/crosspack"]
        == arguments.implementation_component_sha256
        and sdk_dependencies["implementation/launcher"]
        == arguments.launcher_component_sha256
        and sdk_dependencies["sources/nfpm"]
        == arguments.source_component_sha256,
        "crosspack SDK dependency closure differs",
    )
    qualification_dependencies = dependency_map(qualification)
    require(
        set(qualification_dependencies)
        == {
            "implementation/crosspack-qualification",
            "packaging/sdk-build",
            "toolchain/aarch64-qualification",
            "toolchain/x86_64-qualification",
        }
        and qualification_dependencies["implementation/crosspack-qualification"]
        == arguments.policy_component_sha256
        and qualification_dependencies["packaging/sdk-build"]
        == arguments.sdk_component_sha256,
        "crosspack qualification dependency closure differs",
    )
    source_report = load_json(arguments.source_report)
    package_report = load_json(arguments.package_report)
    require(
        source_report.get("kind") == "crossforge-nfpm-source"
        and source_report.get("component")
        == {
            "name": "sources/nfpm",
            "canonical_sha256": arguments.source_component_sha256,
        },
        "nFPM source report differs",
    )
    require(
        package_report.get("kind")
        == "crossforge-crosspack-package-qualification"
        and package_report.get("status") == "passed"
        and package_report.get("nfpm", {}).get("sha256")
        == source_report.get("binary", {}).get("sha256")
        and set(package_report.get("targets", {})) == {"x86_64", "aarch64"},
        "crosspack package report differs",
    )
    markers = {}
    for arch in ("x86_64", "aarch64"):
        artifacts = package_report["targets"][arch].get("artifacts", [])
        require(
            len(artifacts) == 6
            and {(item.get("format"), item.get("component")) for item in artifacts}
            == {
                (format_name, component)
                for format_name in ("deb", "rpm")
                for component in ("runtime", "development", "tools")
            },
            "crosspack artifact matrix differs for %s" % arch,
        )
        markers[arch] = {}
        for format_name in ("deb", "rpm"):
            marker = arguments.marker_root / (
                "%s-%s.ok" % (format_name, arch)
            )
            expected = "crosspack-install-v1 %s %s passed\n" % (
                format_name,
                arch,
            )
            try:
                observed = marker.read_text(encoding="utf-8")
            except OSError as error:
                raise QualificationError("cannot read %s: %s" % (marker, error)) from error
            require(observed == expected, "crosspack install marker differs")
            markers[arch][format_name] = "passed"

    components = {}
    for name, document in (
        ("source", source),
        ("implementation", implementation),
        ("launcher", launcher),
        ("sdk", sdk),
        ("policy", policy),
        ("qualification", qualification),
    ):
        components[name] = {
            "component": document["component"],
            "canonical_sha256": COMPONENT_READER["canonical_sha256"](document),
        }
    return {
        "schema_version": 1,
        "kind": "crossforge-crosspack-qualification",
        "status": "passed",
        "components": components,
        "nfpm": package_report["nfpm"],
        "targets": markers,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for role in (
        "source",
        "implementation",
        "launcher",
        "sdk",
        "policy",
        "qualification",
    ):
        parser.add_argument("--%s-component" % role, type=Path, required=True)
        parser.add_argument("--%s-component-sha256" % role, required=True)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--package-report", type=Path, required=True)
    parser.add_argument("--marker-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = finalize(arguments)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(arguments.output)
    print("qualified crosspack DEB/RPM installation: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, QualificationError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
