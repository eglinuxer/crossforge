#!/usr/bin/env python3
"""Install and qualify the vcpkg-selected CMake host-tool overlay."""

import argparse
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import warnings
from pathlib import Path, PurePosixPath


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


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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


def run(arguments, cwd=None, env=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "command failed (%s):\n%s"
        % (
            " ".join(str(argument) for argument in arguments),
            process.stdout + process.stderr,
        ),
    )
    return process.stdout, process.stderr


def load_component(path, name, digest):
    try:
        return COMPONENT_READER["load_component"](path, name, "build", digest)
    except ComponentError as error:
        raise QualificationError("invalid %s component: %s" % (name, error)) from error


def material_map(document):
    return {record["path"]: record["value"] for record in document["materials"]}


def indexed_payloads(materials):
    records = {}
    for path, value in materials.items():
        match = re.match(
            r"^/host_tools/cmake/payloads/([0-9]+)/(path|sha256|sha512|size)$",
            path,
        )
        if match:
            records.setdefault(int(match.group(1)), {})[match.group(2)] = value
    require(
        sorted(records) == [0, 1, 2]
        and all(
            set(record) == {"path", "sha256", "sha512", "size"}
            for record in records.values()
        ),
        "CMake payload records differ",
    )
    payloads = [records[index] for index in sorted(records)]
    require(
        [item["path"] for item in payloads]
        == ["bin/cmake", "bin/cpack", "bin/ctest"],
        "CMake payload inventory differs",
    )
    return payloads


def load_identity(component):
    materials = material_map(component)
    payloads = indexed_payloads(materials)
    payload_paths = {
        "/host_tools/cmake/payloads/%d/%s" % (index, field)
        for index in range(3)
        for field in ("path", "sha256", "sha512", "size")
    }
    expected_paths = payload_paths | {
        "/host_tools/cmake/version",
        "/host_tools/cmake/binary/status",
        "/host_tools/cmake/binary/url",
        "/host_tools/cmake/binary/sha256",
        "/host_tools/cmake/binary/sha512",
        "/host_tools/cmake/binary/size",
        "/host_tools/cmake/binary/archive_root",
        "/host_tools/cmake/license/expression",
        "/host_tools/cmake/license/path",
        "/host_tools/cmake/license/sha256",
        "/host_tools/cmake/license/size",
    }
    require(set(materials) == expected_paths, "CMake source material set differs")
    require(
        materials["/host_tools/cmake/version"] == "4.4.0"
        and materials["/host_tools/cmake/binary/status"] == "locked"
        and materials["/host_tools/cmake/binary/archive_root"]
        == "cmake-4.4.0-linux-x86_64"
        and materials["/host_tools/cmake/license/expression"] == "BSD-3-Clause",
        "CMake locked release policy differs",
    )
    return {
        "version": materials["/host_tools/cmake/version"],
        "binary": {
            field: materials["/host_tools/cmake/binary/" + field]
            for field in ("archive_root", "sha256", "sha512", "size", "url")
        },
        "payloads": payloads,
        "license": {
            field: materials["/host_tools/cmake/license/" + field]
            for field in ("expression", "path", "sha256", "size")
        },
    }


def load_policy(component):
    materials = material_map(component)
    expected = {
        "/@implementation/host-tools/cmake/binary_relative_paths/0": "bin/cmake",
        "/@implementation/host-tools/cmake/binary_relative_paths/1": "bin/cpack",
        "/@implementation/host-tools/cmake/binary_relative_paths/2": "bin/ctest",
        "/@implementation/host-tools/cmake/consumers/0": "cmake",
        "/@implementation/host-tools/cmake/consumers/1": "ctest",
        "/@implementation/host-tools/cmake/consumers/2": "cpack",
        "/@implementation/host-tools/cmake/consumers/3": "vcpkg",
        "/@implementation/host-tools/cmake/install_prefix": "/opt/crossforge/host-tools/cmake",
        "/@implementation/host-tools/cmake/path_precedence": "before-system",
        "/@implementation/host-tools/cmake/schema_version": 1,
        "/@implementation/host-tools/cmake/system_binary": "/usr/bin/cmake",
    }
    require(materials == expected, "CMake host-tool policy differs")
    return {
        "install_prefix": materials[
            "/@implementation/host-tools/cmake/install_prefix"
        ],
        "system_binary": materials[
            "/@implementation/host-tools/cmake/system_binary"
        ],
    }


def validate_archive(archive_path, identity, extraction_root):
    require(
        file_identity(archive_path)
        == {
            "sha256": identity["binary"]["sha256"],
            "sha512": identity["binary"]["sha512"],
            "size": identity["binary"]["size"],
        },
        "CMake archive identity differs",
    )
    archive_root = identity["binary"]["archive_root"]
    with tarfile.open(str(archive_path), "r:gz") as archive:
        members = archive.getmembers()
        require(members, "CMake archive is empty")
        names = []
        for member in members:
            logical = PurePosixPath(member.name)
            normalized_name = (
                member.name.rstrip("/") if member.isdir() else member.name
            )
            require(
                not logical.is_absolute()
                and logical.parts
                and logical.parts[0] == archive_root
                and all(part not in ("", ".", "..") for part in logical.parts)
                and normalized_name == logical.as_posix()
                and (member.isdir() or member.isfile()),
                "CMake archive contains an unsafe member: %s" % member.name,
            )
            names.append(member.name)
        require(len(names) == len(set(names)), "CMake archive repeats a member")
        with warnings.catch_warnings():
            # Rocky backports a tarfile warning about its changed extraction
            # default. Every member was already restricted to regular files
            # and directories below the one fixed root above.
            warnings.simplefilter("ignore", RuntimeWarning)
            archive.extractall(str(extraction_root))
    root = extraction_root / archive_root
    for path in root.rglob("*"):
        require(
            not path.is_symlink() and (path.is_dir() or path.is_file()),
            "extracted CMake tree contains an unsafe entry",
        )
    for payload in identity["payloads"]:
        path = root / payload["path"]
        require(
            path.is_file()
            and not path.is_symlink()
            and file_identity(path)
            == {
                "sha256": payload["sha256"],
                "sha512": payload["sha512"],
                "size": payload["size"],
            },
            "CMake payload identity differs: %s" % payload["path"],
        )
    license_path = root / identity["license"]["path"]
    require(
        license_path.is_file()
        and not license_path.is_symlink()
        and file_identity(license_path)["sha256"] == identity["license"]["sha256"]
        and license_path.stat().st_size == identity["license"]["size"],
        "CMake license identity differs",
    )
    return root


def validate_elf(tool):
    file_output, _stderr = run(["file", tool])
    header, _stderr = run(["readelf", "-h", tool])
    program_headers, _stderr = run(["readelf", "-l", tool])
    dynamic, _stderr = run(["readelf", "-d", tool])
    versions, _stderr = run(["readelf", "--wide", "--version-info", tool])
    ldd, _stderr = run(["ldd", tool])
    glibc_versions = {
        tuple(int(part) for part in version.split("."))
        for version in re.findall(r"GLIBC_([0-9]+(?:\.[0-9]+)+)", versions)
    }
    require(
        "ELF 64-bit LSB executable, x86-64" in file_output
        and re.search(
            r"^\s*Machine:\s+Advanced Micro Devices X86-64\s*$",
            header,
            re.MULTILINE,
        )
        and "[Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]"
        in program_headers
        and all(tag not in dynamic for tag in ("RPATH", "RUNPATH", "TEXTREL"))
        and "not found" not in ldd
        and glibc_versions
        and max(glibc_versions) <= (2, 17),
        "CMake ELF/runtime policy differs: %s" % tool,
    )


def smoke_build(cmake, ctest, cpack, ninja, work, environment):
    source = work / "source"
    build = work / "build"
    source.mkdir()
    (source / "probe.c").write_text(
        '#include <stdio.h>\nint main(void){puts("42");return 0;}\n',
        encoding="utf-8",
    )
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\n"
        "project(crossforge_cmake C)\n"
        "enable_testing()\n"
        "add_executable(crossforge-cmake probe.c)\n"
        "add_test(NAME probe COMMAND crossforge-cmake)\n"
        "install(TARGETS crossforge-cmake RUNTIME DESTINATION bin)\n"
        "set(CPACK_GENERATOR TGZ)\n"
        "set(CPACK_PACKAGE_NAME crossforge-cmake-probe)\n"
        "set(CPACK_PACKAGE_VERSION 1)\n"
        "include(CPack)\n",
        encoding="utf-8",
    )
    compiler = "/opt/rh/gcc-toolset-15/root/usr/bin/gcc"
    run(
        [
            cmake,
            "-S",
            source,
            "-B",
            build,
            "-G",
            "Ninja",
            "-DCMAKE_C_COMPILER=" + compiler,
            "-DCMAKE_MAKE_PROGRAM=" + str(ninja),
        ],
        env=environment,
    )
    run([cmake, "--build", build], env=environment)
    test_stdout, _stderr = run(
        [ctest, "--test-dir", build, "--output-on-failure"], env=environment
    )
    run([cpack, "--config", build / "CPackConfig.cmake", "-G", "TGZ"], cwd=build)
    stdout, _stderr = run([build / "crossforge-cmake"])
    packages = sorted(build.glob("crossforge-cmake-probe-*.tar.gz"))
    cache = (build / "CMakeCache.txt").read_text(encoding="utf-8")
    make_programs = re.findall(
        r"^CMAKE_MAKE_PROGRAM:[^=]+=(.*)$", cache, re.MULTILINE
    )
    require(
        stdout == "42\n"
        and "100% tests passed" in test_stdout
        and len(packages) == 1
        and make_programs == [str(ninja)],
        "CMake/Ninja/CTest/CPack smoke differs: "
        "stdout=%r tests=%r packages=%r make_programs=%r"
        % (stdout, test_stdout, [path.name for path in packages], make_programs),
    )
    return {
        "executable_sha256": file_identity(build / "crossforge-cmake")["sha256"],
        "package_sha256": file_identity(packages[0])["sha256"],
    }


def qualify(
    archive,
    source_component_path,
    source_digest,
    policy_component_path,
    policy_digest,
    tool_component_path,
    tool_digest,
    destination_root,
    ninja,
):
    source_component = load_component(
        source_component_path, "sources/cmake", source_digest
    )
    policy_component = load_component(
        policy_component_path, "implementation/cmake-host-tool", policy_digest
    )
    tool_component = load_component(
        tool_component_path, "host-tools/cmake", tool_digest
    )
    identity = load_identity(source_component)
    policy = load_policy(policy_component)
    dependencies = {
        item["component"]: item["canonical_sha256"]
        for item in tool_component["dependencies"]
    }
    require(
        set(dependencies)
        == {
            "host-tools/ninja",
            "implementation/cmake-host-tool",
            "rpm/host-runtime",
            "sources/cmake",
        }
        and dependencies["implementation/cmake-host-tool"] == policy_digest
        and dependencies["sources/cmake"] == source_digest,
        "CMake host-tool component dependency closure differs",
    )
    host_report = load_json(Path("/opt/crossforge/qualification/host-runtime.json"))
    require(
        host_report.get("status") == "passed"
        and host_report.get("rpm", {})
        .get("release_binding", {})
        .get("canonical_sha256")
        == dependencies["rpm/host-runtime"],
        "CMake base host-runtime qualification differs",
    )
    ninja_report = load_json(
        Path("/opt/crossforge/qualification/host-tools/ninja.json")
    )
    require(
        ninja_report.get("status") == "passed"
        and ninja_report.get("components", {}).get("tool")
        == {
            "component": "host-tools/ninja",
            "canonical_sha256": dependencies["host-tools/ninja"],
        },
        "CMake Ninja qualification prerequisite differs",
    )
    expected_root = Path(policy["install_prefix"]) / identity["version"]
    require(destination_root == expected_root, "CMake install root differs")
    require(not destination_root.exists(), "CMake install root already exists")
    destination_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=".%s." % identity["version"],
            dir=str(destination_root.parent),
        )
    )
    try:
        extracted = validate_archive(archive, identity, temporary)
        extracted.rename(destination_root)
    finally:
        shutil.rmtree(str(temporary), ignore_errors=True)

    environment = dict(os.environ)
    tool = destination_root / "bin/cmake"
    require(
        environment.get("PATH", "").split(":", 1)[0] == str(tool.parent)
        and shutil.which("cmake", path=environment["PATH"]) == str(tool),
        "CMake overlay does not take PATH precedence",
    )
    versions = {}
    for name in ("cmake", "cpack", "ctest"):
        binary = destination_root / "bin" / name
        stdout, _stderr = run([binary, "--version"], env=environment)
        require(
            re.search(r"\bversion 4\.4\.0\b", stdout),
            "%s version differs" % name,
        )
        validate_elf(binary)
        versions[name] = stdout.splitlines()[0]
    system_binary = Path(policy["system_binary"])
    system_version, _stderr = run([system_binary, "--version"])
    require(
        system_binary.is_file() and "4.4.0" not in system_version,
        "CMake overlay replaced or matched the RPM-owned CMake",
    )
    require(Path(ninja).is_file(), "locked Ninja input is missing")
    with tempfile.TemporaryDirectory(prefix="crossforge-cmake-smoke-") as directory:
        smoke = smoke_build(
            tool,
            destination_root / "bin/ctest",
            destination_root / "bin/cpack",
            ninja,
            Path(directory),
            environment,
        )
    return {
        "schema_version": 1,
        "kind": "crossforge-cmake-host-tool-qualification",
        "status": "passed",
        "components": {
            "source": {
                "component": "sources/cmake",
                "canonical_sha256": canonical_sha256(source_component),
            },
            "policy": {
                "component": "implementation/cmake-host-tool",
                "canonical_sha256": canonical_sha256(policy_component),
            },
            "tool": {
                "component": "host-tools/cmake",
                "canonical_sha256": canonical_sha256(tool_component),
            },
        },
        "dependencies": dependencies,
        "install_root": str(destination_root),
        "archive": file_identity(archive),
        "payloads": identity["payloads"],
        "license": identity["license"],
        "versions": versions,
        "system_version": system_version.splitlines()[0],
        "smoke": smoke,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source-component", type=Path, required=True)
    parser.add_argument("--source-component-sha256", required=True)
    parser.add_argument("--policy-component", type=Path, required=True)
    parser.add_argument("--policy-component-sha256", required=True)
    parser.add_argument("--tool-component", type=Path, required=True)
    parser.add_argument("--tool-component-sha256", required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--ninja", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = qualify(
        arguments.archive,
        arguments.source_component,
        arguments.source_component_sha256,
        arguments.policy_component,
        arguments.policy_component_sha256,
        arguments.tool_component,
        arguments.tool_component_sha256,
        arguments.destination_root,
        arguments.ninja,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(arguments.output)
    print("qualified CMake host tool: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, QualificationError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
