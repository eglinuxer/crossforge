#!/usr/bin/env python3
"""Qualify pinned upstream vcpkg ports across every Crossforge triplet."""

import argparse
import hashlib
import json
import os
import re
import runpy
import sys
import tempfile
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
COMPONENT_READER = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release_component.py")
)
COMMON = runpy.run_path(
    str(SCRIPT_DIRECTORY / "vcpkg_qualification.py")
)
ASSETS = runpy.run_path(
    str(SCRIPT_DIRECTORY / "fetch-vcpkg-assets.py")
)
ComponentError = COMPONENT_READER["ComponentError"]
QualificationError = COMMON["QualificationError"]
require = COMMON["require"]
run = COMMON["run"]
sha256_file = COMMON["sha256_file"]
profile_tools = COMMON["profile_tools"]
verify_machine = COMMON["verify_machine"]
validate_shared_library_dynamic = COMMON["validate_shared_library_dynamic"]
isolated_install = COMMON["isolated_install"]
HOST_TRIPLET = COMMON["HOST_TRIPLET"]
TRIPLETS = COMMON["TRIPLETS"]
POLICY_COMPONENT = "implementation/vcpkg-upstream-tier1-qualification"
QUALIFICATION_COMPONENT = "vcpkg/upstream-tier1-qualification"
EXPECTED_PORTS = (
    {"name": "fmt", "port_version": 1, "version": "12.2.0"},
    {"name": "zlib", "port_version": 1, "version": "1.3.2"},
)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError) as error:
        raise QualificationError("cannot load %s: %s" % (path, error)) from error
    require(isinstance(value, dict), "%s must contain an object" % path)
    return value


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_component(path, name, digest):
    try:
        return COMPONENT_READER["load_component"](
            path, name, "qualification", digest
        )
    except ComponentError as error:
        raise QualificationError("invalid %s component: %s" % (name, error)) from error


def material_map(document):
    return {item["path"]: item["value"] for item in document["materials"]}


def indexed_records(materials, prefix, fields):
    records = {}
    expression = re.compile(
        r"^%s([0-9]+)/(%s)$"
        % (re.escape(prefix), "|".join(sorted(fields)))
    )
    for path, value in materials.items():
        match = expression.match(path)
        if match:
            records.setdefault(int(match.group(1)), {})[match.group(2)] = value
    require(
        sorted(records) == list(range(len(records)))
        and all(set(record) == set(fields) for record in records.values()),
        "policy records differ under %s" % prefix,
    )
    return [records[index] for index in sorted(records)]


def policy_context(policy_component, fixture_root):
    materials = material_map(policy_component)
    prefix = "/@implementation/vcpkg-upstream-tier1/"
    require(
        materials.get(prefix + "schema_version") == 1
        and materials.get(prefix + "binary_sources") == "clear"
        and materials.get(prefix + "downloads") == "forbidden",
        "upstream vcpkg policy differs",
    )
    ports = indexed_records(
        materials,
        prefix + "ports/",
        {"name", "port_version", "version"},
    )
    require(tuple(ports) == EXPECTED_PORTS, "upstream vcpkg port set differs")
    triplets = sorted(
        value
        for path, value in materials.items()
        if re.match(r"^%striplets/[0-9]+$" % re.escape(prefix), path)
    )
    require(triplets == sorted(TRIPLETS), "upstream vcpkg triplets differ")
    files = indexed_records(
        materials,
        prefix + "files/",
        {"path", "sha256"},
    )
    actual = []
    for path in fixture_root.rglob("*"):
        require(
            not path.is_symlink() and (path.is_dir() or path.is_file()),
            "upstream vcpkg fixture contains an unsafe entry",
        )
        if path.is_file():
            actual.append(
                {
                    "path": path.relative_to(fixture_root).as_posix(),
                    "sha256": sha256_file(path),
                }
            )
    require(
        sorted(actual, key=lambda item: item["path"])
        == sorted(files, key=lambda item: item["path"]),
        "upstream vcpkg fixture inventory differs",
    )
    assets = ASSETS["policy_assets"](policy_component)
    return {"assets": assets, "files": files, "ports": ports}


def validate_component_closure(
    release,
    qualification_component,
    policy_digest,
    contract_report,
):
    dependencies = {
        item["component"]: item["canonical_sha256"]
        for item in qualification_component["dependencies"]
    }
    require(
        set(dependencies)
        == {POLICY_COMPONENT, "vcpkg/contract-qualification"}
        and dependencies[POLICY_COMPONENT] == policy_digest,
        "upstream vcpkg component dependency closure differs",
    )
    contract_identity = contract_report.get("components", {}).get(
        "qualification"
    )
    require(
        contract_report.get("status") == "passed"
        and contract_report.get("release_sha256") == canonical_sha256(release)
        and contract_identity
        == {
            "component": "vcpkg/contract-qualification",
            "canonical_sha256": dependencies["vcpkg/contract-qualification"],
        },
        "vcpkg contract prerequisite differs",
    )
    return dependencies


def parse_status(path):
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise QualificationError("cannot read vcpkg status: %s" % error) from error
    records = []
    for paragraph in re.split(r"\n\s*\n", content.strip()):
        fields = {}
        for line in paragraph.splitlines():
            name, separator, value = line.partition(": ")
            require(separator and name not in fields, "invalid vcpkg status record")
            fields[name] = value
        if fields:
            records.append(fields)
    return records


def installed_port_versions(installed, triplet):
    expected = {item["name"]: item for item in EXPECTED_PORTS}
    observed = {}
    for record in parse_status(installed / "vcpkg/status"):
        name = record.get("Package")
        if name not in expected or record.get("Architecture") != triplet:
            continue
        require(name not in observed, "installed vcpkg port is duplicated")
        observed[name] = record.get("Version")
    require(set(observed) == set(expected), "%s port inventory differs" % triplet)
    for name, version in observed.items():
        port = expected[name]
        accepted = {port["version"]}
        if port["port_version"]:
            accepted.add("%s#%d" % (port["version"], port["port_version"]))
        require(version in accepted, "%s installed version differs" % name)
    return observed


def resolved_library(root, filename):
    path = root / "lib" / filename
    require(path.exists(), "installed library is missing: %s" % path)
    resolved = path.resolve()
    try:
        resolved.relative_to((root / "lib").resolve())
    except ValueError:
        raise QualificationError("installed library symlink escapes: %s" % path)
    require(resolved.is_file(), "installed library target is missing: %s" % path)
    if path.is_symlink():
        require(
            not Path(os.readlink(str(path))).is_absolute(),
            "installed library uses an absolute symlink: %s" % path,
        )
    return path, resolved


def build_consumer(profile, source, include, library, work, qemu, release):
    _cc, cxx, readelf, sysroot = profile_tools(profile)
    executable = work / "consumer"
    command = [cxx]
    if sysroot is not None:
        command.append("--sysroot=" + str(sysroot))
    command.extend(
        [
            "-std=c++17",
            "-O2",
            source,
            "-I" + str(include),
            "-L" + str(library),
            "-Wl,-z,relro,-z,now",
            "-lfmt",
            "-lz",
            "-o",
            executable,
        ]
    )
    run(command)
    verify_machine(readelf, executable, profile["machine"])
    headers, _stderr = run([readelf, "-l", executable])
    dynamic, _stderr = run([readelf, "-d", executable])
    require(
        "[Requesting program interpreter: %s]" % profile["interpreter"] in headers
        and all(tag not in dynamic for tag in ("RPATH", "RUNPATH", "TEXTREL")),
        "upstream consumer ELF policy differs",
    )
    environment = dict(os.environ)
    if profile["linkage"] == "dynamic":
        environment["LD_LIBRARY_PATH"] = str(library)
    if profile["arch"] == "aarch64":
        executor = release["qemu"]["executor"]
        command = [
            qemu,
            "-cpu",
            executor["cpu"],
            "-r",
            executor["uname_release"],
            "-L",
            sysroot,
        ]
        if profile["linkage"] == "dynamic":
            command.extend(["-E", "LD_LIBRARY_PATH=" + str(library)])
        command.append(executable)
        stdout, _stderr = run(command)
    else:
        stdout, _stderr = run([executable], env=environment)
    require(
        stdout == "crossforge-vcpkg-tier1:42\n",
        "upstream consumer output differs",
    )
    return {
        "sha256": sha256_file(executable),
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
    }


def qualify_triplet(
    vcpkg_root,
    fixture_root,
    asset_paths,
    patchelf_archive,
    triplet,
    profile,
    qemu,
    release,
    work,
):
    roots = isolated_install(
        vcpkg_root,
        fixture_root / "manifest",
        triplet,
        tuple(asset_paths) + (patchelf_archive,),
        work,
    )
    installed = roots["installed"]
    target_root = installed / triplet
    versions = installed_port_versions(installed, triplet)
    suffix = ".so" if profile["linkage"] == "dynamic" else ".a"
    libraries = {}
    _cc, _cxx, readelf, _sysroot = profile_tools(profile)
    for name, stem in (("fmt", "libfmt"), ("zlib", "libz")):
        path, resolved = resolved_library(target_root, stem + suffix)
        opposite = target_root / "lib" / (
            stem + (".a" if suffix == ".so" else ".so")
        )
        require(not opposite.exists(), "%s emitted opposite linkage" % name)
        verify_machine(readelf, resolved, profile["machine"])
        runpath = None
        if profile["linkage"] == "dynamic":
            dynamic, _stderr = run([readelf, "-d", resolved])
            runpath = validate_shared_library_dynamic(
                dynamic, "%s %s" % (triplet, name)
            )
        libraries[name] = {
            "path": path.relative_to(target_root).as_posix(),
            "sha256": sha256_file(resolved),
            "runpath": runpath,
        }
    consumer_work = Path(work) / triplet / "consumer"
    consumer_work.mkdir()
    consumer = build_consumer(
        profile,
        fixture_root / "consumer.cpp",
        target_root / "include",
        target_root / "lib",
        consumer_work,
        qemu,
        release,
    )
    return {
        "triplet": triplet,
        "cross_compiling": profile["cross"],
        "linkage": profile["linkage"],
        "ports": versions,
        "libraries": libraries,
        "consumer": consumer,
    }


def qualify(arguments):
    release = load_json(arguments.release)
    policy_component = load_component(
        arguments.policy_component, POLICY_COMPONENT, arguments.policy_component_sha256
    )
    qualification_component = load_component(
        arguments.qualification_component,
        QUALIFICATION_COMPONENT,
        arguments.qualification_component_sha256,
    )
    policy = policy_context(policy_component, arguments.fixture_root)
    ASSETS["verify_asset_root"](arguments.asset_root, policy["assets"])
    contract_report = load_json(
        "/opt/crossforge/qualification/vcpkg/contract.json"
    )
    dependencies = validate_component_closure(
        release,
        qualification_component,
        arguments.policy_component_sha256,
        contract_report,
    )
    expected_patchelf = contract_report.get("patchelf_asset")
    require(
        isinstance(expected_patchelf, dict)
        and arguments.patchelf_archive.name == expected_patchelf.get("filename")
        and ASSETS["file_identity"](arguments.patchelf_archive)
        == {
            "sha256": expected_patchelf.get("sha256"),
            "sha512": expected_patchelf.get("sha512"),
            "size": expected_patchelf.get("size"),
        },
        "upstream vcpkg patchelf asset differs",
    )
    require(
        os.environ.get("VCPKG_DEFAULT_HOST_TRIPLET") == HOST_TRIPLET
        and "VCPKG_DEFAULT_TRIPLET" not in os.environ
        and os.environ.get("VCPKG_FORCE_SYSTEM_BINARIES") == "1"
        and os.environ.get("VCPKG_DISABLE_METRICS") == "1",
        "upstream vcpkg environment differs",
    )
    asset_paths = [
        arguments.asset_root / asset["filename"] for asset in policy["assets"]
    ]
    with tempfile.TemporaryDirectory(
        prefix="crossforge-vcpkg-upstream-tier1-"
    ) as temporary:
        work = Path(temporary)
        results = [
            qualify_triplet(
                arguments.vcpkg_root,
                arguments.fixture_root,
                asset_paths,
                arguments.patchelf_archive,
                triplet,
                TRIPLETS[triplet],
                arguments.qemu,
                release,
                work,
            )
            for triplet in (
                HOST_TRIPLET,
                "crossforge-x64-el8",
                "crossforge-x64-el8-dynamic",
                "crossforge-arm64-el8",
                "crossforge-arm64-el8-dynamic",
            )
        ]
    for name in ("downloads", "buildtrees", "packages", "installed", "vcpkg_installed"):
        require(
            not (arguments.vcpkg_root / name).exists(),
            "vcpkg root was polluted: %s" % name,
        )
    return {
        "schema_version": 1,
        "kind": "crossforge-vcpkg-upstream-tier1-qualification",
        "status": "passed",
        "release_sha256": canonical_sha256(release),
        "components": {
            "policy": {
                "component": POLICY_COMPONENT,
                "canonical_sha256": canonical_sha256(policy_component),
            },
            "qualification": {
                "component": QUALIFICATION_COMPONENT,
                "canonical_sha256": canonical_sha256(qualification_component),
            },
        },
        "dependencies": dependencies,
        "assets": policy["assets"],
        "fixture_files": policy["files"],
        "ports": policy["ports"],
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--vcpkg-root", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--patchelf-archive", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--policy-component", type=Path, required=True)
    parser.add_argument("--policy-component-sha256", required=True)
    parser.add_argument("--qualification-component", type=Path, required=True)
    parser.add_argument("--qualification-component-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = qualify(arguments)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(arguments.output)
    print("qualified upstream vcpkg tier1: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ASSETS["AssetError"],
        KeyError,
        OSError,
        QualificationError,
        TypeError,
        ValueError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
