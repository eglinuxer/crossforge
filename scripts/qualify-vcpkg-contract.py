#!/usr/bin/env python3
"""Qualify real vcpkg host/target port builds across all Crossforge triplets."""

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
ComponentError = COMPONENT_READER["ComponentError"]
QualificationError = COMMON["QualificationError"]
require = COMMON["require"]
run = COMMON["run"]
profile_tools = COMMON["profile_tools"]
verify_machine = COMMON["verify_machine"]
validate_shared_library_dynamic = COMMON["validate_shared_library_dynamic"]
isolated_install = COMMON["isolated_install"]
HOST_TRIPLET = COMMON["HOST_TRIPLET"]
TRIPLETS = COMMON["TRIPLETS"]
PATCHELF_ASSET = {
    "filename": "patchelf-0.19.0-x86_64.tar.gz",
    "url": "https://github.com/NixOS/patchelf/releases/download/0.19.0/patchelf-0.19.0-x86_64.tar.gz",
    "sha256": "a493df96abeecee55d539071e9bace94d32458a3baf54d9495da94f44c647d86",
    "sha512": "2a65c9cbdddcc7952cdbd6e98a2cf3da01386cf0f0b927a6bbcfe8131ecf0bfb17c534246635b5e6a090652ee54c903f9f9c4f3f1d2412dba59f287ae2ae8070",
    "size": 569003,
}
def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        with path.open("r", encoding="utf-8") as stream:
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


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path):
    sha256 = hashlib.sha256()
    sha512 = hashlib.sha512()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha256.update(block)
            sha512.update(block)
            size += len(block)
    return {
        "sha256": sha256.hexdigest(),
        "sha512": sha512.hexdigest(),
        "size": size,
    }


def load_component(path, name, scope, digest):
    try:
        return COMPONENT_READER["load_component"](path, name, scope, digest)
    except ComponentError as error:
        raise QualificationError("invalid %s component: %s" % (name, error)) from error


def material_map(document):
    return {item["path"]: item["value"] for item in document["materials"]}


def policy_files(policy_component, fixture_root):
    materials = material_map(policy_component)
    expected_triplets = sorted(TRIPLETS)
    require(
        materials.get("/@implementation/vcpkg-contract/schema_version") == 1
        and materials.get("/@implementation/vcpkg-contract/binary_sources")
        == "clear"
        and materials.get("/@implementation/vcpkg-contract/downloads")
        == "forbidden"
        and materials.get("/@implementation/vcpkg-contract/host_triplet")
        == HOST_TRIPLET,
        "vcpkg contract policy differs",
    )
    require(
        {
            name: materials.get(
                "/@implementation/vcpkg-contract/assets/patchelf/" + name
            )
            for name in PATCHELF_ASSET
        }
        == PATCHELF_ASSET,
        "vcpkg contract patchelf asset differs",
    )
    observed_triplets = sorted(
        value
        for path, value in materials.items()
        if re.match(r"^/@implementation/vcpkg-contract/triplets/[0-9]+$", path)
    )
    require(observed_triplets == expected_triplets, "vcpkg contract triplets differ")
    file_records = {}
    for path, value in materials.items():
        match = re.match(
            r"^/@implementation/vcpkg-contract/files/([0-9]+)/(path|sha256)$",
            path,
        )
        if not match:
            continue
        index = int(match.group(1))
        file_records.setdefault(index, {})[match.group(2)] = value
    require(
        sorted(file_records) == list(range(12))
        and all(set(record) == {"path", "sha256"} for record in file_records.values()),
        "vcpkg contract fixture records differ",
    )
    expected = [file_records[index] for index in sorted(file_records)]
    actual = []
    for path in fixture_root.rglob("*"):
        require(
            not path.is_symlink() and (path.is_dir() or path.is_file()),
            "vcpkg contract fixture contains an unsafe entry",
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
        == sorted(expected, key=lambda item: item["path"]),
        "vcpkg contract fixture inventory differs",
    )
    return expected


def validate_component_closure(
    release,
    contract_component,
    policy_component_sha256,
    sdk_report,
    toolchain_reports,
):
    dependencies = {
        item["component"]: item["canonical_sha256"]
        for item in contract_component["dependencies"]
    }
    require(
        set(dependencies)
        == {
            "implementation/vcpkg-contract-qualification",
            "toolchain/x86_64-qualification",
            "toolchain/aarch64-qualification",
            "vcpkg/sdk-build",
        }
        and dependencies["implementation/vcpkg-contract-qualification"]
        == policy_component_sha256,
        "vcpkg contract component dependency closure differs",
    )
    release_sha256 = canonical_sha256(release)
    require(
        sdk_report.get("status") == "passed"
        and sdk_report.get("release_sha256") == release_sha256
        and sdk_report.get("integration", {}).get("sdk_component_sha256")
        == dependencies["vcpkg/sdk-build"],
        "vcpkg SDK prerequisite qualification differs",
    )
    for arch, report in toolchain_reports.items():
        require(
            report.get("release_sha256") == release_sha256
            and report.get("qualification_component")
            == {
                "component": "toolchain/%s-qualification" % arch,
                "canonical_sha256": dependencies[
                    "toolchain/%s-qualification" % arch
                ],
            },
            "%s toolchain prerequisite qualification differs" % arch,
        )
    return dependencies


def parse_metadata(path, expected_keys):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        require(separator and name not in values, "invalid metadata line in %s" % path)
        values[name] = value
    require(set(values) == set(expected_keys), "metadata keys differ in %s" % path)
    return values


def cmake_bool(value):
    normalized = value.upper()
    if normalized in ("1", "ON", "TRUE", "YES", "Y"):
        return True
    if normalized in ("0", "OFF", "FALSE", "NO", "N", "IGNORE", ""):
        return False
    raise QualificationError("invalid CMake boolean: %r" % value)


def build_consumer(profile, include, library, work, qemu, release):
    compiler, _cxx, readelf, sysroot = profile_tools(profile)
    source = work / "consumer.c"
    executable = work / "consumer"
    source.write_text(
        "#include <stdio.h>\n"
        "#include <crossforge-target-probe/probe.h>\n"
        "int main(void){printf(\"%d\\n\",crossforge_target_probe());return 0;}\n",
        encoding="utf-8",
    )
    command = [compiler]
    if sysroot is not None:
        command.append("--sysroot=" + str(sysroot))
    command.extend(
        [
            source,
            "-I" + str(include),
            "-L" + str(library),
            "-lcrossforge-target-probe",
            "-o",
            executable,
        ]
    )
    run(command)
    verify_machine(readelf, executable, profile["machine"])
    program_headers, _stderr = run([readelf, "-l", executable])
    dynamic, _stderr = run([readelf, "-d", executable])
    require(
        "[Requesting program interpreter: %s]" % profile["interpreter"]
        in program_headers
        and all(tag not in dynamic for tag in ("RPATH", "RUNPATH", "TEXTREL")),
        "consumer ELF policy differs",
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
    require(stdout == "42\n", "vcpkg contract consumer output differs")
    return {
        "sha256": sha256_file(executable),
        "interpreter": profile["interpreter"],
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
    }


def qualify_triplet(
    vcpkg_root,
    fixture_root,
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
        (patchelf_archive,),
        work,
        overlay_ports=fixture_root / "ports",
    )
    triplet_work = work / triplet
    installed = roots["installed"]
    stdout = roots["stdout"]
    stderr = roots["stderr"]
    host_root = installed / HOST_TRIPLET
    target_root = installed / triplet
    host_tool = host_root / "tools/crossforge-host-probe/crossforge-host-probe"
    host_metadata = parse_metadata(
        host_root / "share/crossforge-host-probe/crossforge-host-build.txt",
        ("cross", "compiler", "make_program", "processor", "sysroot"),
    )
    ninja = "/opt/crossforge/host-tools/ninja/1.13.2/bin/ninja"
    require(
        not cmake_bool(host_metadata["cross"])
        and host_metadata["compiler"]
        == "/opt/rh/gcc-toolset-15/root/usr/bin/gcc"
        and host_metadata["make_program"] == ninja
        and host_metadata["sysroot"] == "",
        "%s host-triplet build identity differs" % triplet,
    )
    generated, _stderr = run([host_tool])
    require(
        generated == "#define CROSSFORGE_GENERATED_VALUE 42\n",
        "host-triplet generator output differs",
    )
    target_metadata = parse_metadata(
        target_root / "share/crossforge-target-probe/crossforge-target-build.txt",
        (
            "cross",
            "compiler",
            "host_tool",
            "linkage",
            "make_program",
            "processor",
            "sysroot",
        ),
    )
    compiler, _cxx, readelf, sysroot = profile_tools(profile)
    require(
        cmake_bool(target_metadata["cross"]) is profile["cross"]
        and cmake_bool(target_metadata["linkage"])
        is (profile["linkage"] == "dynamic")
        and target_metadata["compiler"] == str(compiler)
        and target_metadata["host_tool"] == str(host_tool)
        and target_metadata["make_program"] == ninja
        and target_metadata["sysroot"]
        == ("" if sysroot is None else str(sysroot)),
        "%s target build identity differs: %r" % (triplet, target_metadata),
    )
    suffix = ".so" if profile["linkage"] == "dynamic" else ".a"
    library = target_root / "lib" / ("libcrossforge-target-probe" + suffix)
    require(library.is_file(), "%s target library is missing" % triplet)
    require(
        not (
            target_root
            / "lib"
            / (
                "libcrossforge-target-probe.a"
                if suffix == ".so"
                else "libcrossforge-target-probe.so"
            )
        ).exists(),
        "%s emitted the opposite linkage" % triplet,
    )
    verify_machine(readelf, library, profile["machine"])
    library_runpath = None
    if profile["linkage"] == "dynamic":
        dynamic, _stderr = run([readelf, "-d", library])
        library_runpath = validate_shared_library_dynamic(dynamic, triplet)
    consumer_work = triplet_work / "consumer"
    consumer_work.mkdir()
    consumer = build_consumer(
        profile,
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
        "host_tool_sha256": sha256_file(host_tool),
        "library_sha256": sha256_file(library),
        "library_runpath": library_runpath,
        "consumer": consumer,
        "install_stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "install_stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
    }


def qualify(
    release_path,
    vcpkg_root,
    fixture_root,
    patchelf_archive,
    qemu,
    policy_component_path,
    policy_sha256,
    contract_component_path,
    contract_sha256,
):
    release = load_json(release_path)
    policy_component = load_component(
        policy_component_path,
        "implementation/vcpkg-contract-qualification",
        "qualification",
        policy_sha256,
    )
    contract_component = load_component(
        contract_component_path,
        "vcpkg/contract-qualification",
        "qualification",
        contract_sha256,
    )
    files = policy_files(policy_component, fixture_root)
    require(
        patchelf_archive.name == PATCHELF_ASSET["filename"]
        and file_identity(patchelf_archive)
        == {
            "sha256": PATCHELF_ASSET["sha256"],
            "sha512": PATCHELF_ASSET["sha512"],
            "size": PATCHELF_ASSET["size"],
        },
        "vcpkg contract patchelf archive identity differs",
    )
    sdk_report = load_json(
        Path("/opt/crossforge/qualification/vcpkg/sdk.json")
    )
    toolchain_reports = {
        arch: load_json(
            Path("/opt/crossforge/qualification/toolchain")
            / (arch + ".json")
        )
        for arch in ("x86_64", "aarch64")
    }
    dependencies = validate_component_closure(
        release,
        contract_component,
        policy_sha256,
        sdk_report,
        toolchain_reports,
    )
    require(
        os.environ.get("VCPKG_DEFAULT_HOST_TRIPLET") == HOST_TRIPLET
        and "VCPKG_DEFAULT_TRIPLET" not in os.environ
        and os.environ.get("VCPKG_FORCE_SYSTEM_BINARIES") == "1"
        and os.environ.get("VCPKG_DISABLE_METRICS") == "1",
        "vcpkg contract environment differs",
    )
    with tempfile.TemporaryDirectory(prefix="crossforge-vcpkg-contract-") as temporary:
        work = Path(temporary)
        results = [
            qualify_triplet(
                vcpkg_root,
                fixture_root,
                patchelf_archive,
                triplet,
                TRIPLETS[triplet],
                qemu,
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
        require(not (vcpkg_root / name).exists(), "vcpkg root was polluted: %s" % name)
    return {
        "schema_version": 1,
        "kind": "crossforge-vcpkg-contract-qualification",
        "status": "passed",
        "release_sha256": canonical_sha256(release),
        "components": {
            "policy": {
                "component": "implementation/vcpkg-contract-qualification",
                "canonical_sha256": canonical_sha256(policy_component),
            },
            "qualification": {
                "component": "vcpkg/contract-qualification",
                "canonical_sha256": canonical_sha256(contract_component),
            },
        },
        "dependencies": dependencies,
        "fixture_files": files,
        "patchelf_asset": PATCHELF_ASSET,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--vcpkg-root", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--patchelf-archive", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--policy-component", type=Path, required=True)
    parser.add_argument("--policy-component-sha256", required=True)
    parser.add_argument("--contract-component", type=Path, required=True)
    parser.add_argument("--contract-component-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = qualify(
        arguments.release,
        arguments.vcpkg_root,
        arguments.fixture_root,
        arguments.patchelf_archive,
        arguments.qemu,
        arguments.policy_component,
        arguments.policy_component_sha256,
        arguments.contract_component,
        arguments.contract_component_sha256,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(arguments.output)
    print("qualified vcpkg host/target contract: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        KeyError,
        OSError,
        QualificationError,
        TypeError,
        ValueError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
