#!/usr/bin/env python3
"""Install and qualify the locked Ninja host-tool overlay offline."""

import argparse
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
COMPONENT_READER = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release_component.py")
)
PREPARER = runpy.run_path(str(SCRIPT_DIRECTORY / "prepare-ninja-tool.py"))
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
        with path.open("r", encoding="utf-8") as stream:
            result = json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError) as error:
        raise QualificationError("cannot load %s: %s" % (path, error)) from error
    require(isinstance(result, dict), "%s must contain an object" % path)
    return result


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
        return COMPONENT_READER["load_component"](
            path, name, "build", digest
        )
    except ComponentError as error:
        raise QualificationError("invalid %s component: %s" % (name, error)) from error


def material_map(document):
    return {record["path"]: record["value"] for record in document["materials"]}


def load_policy(component):
    materials = material_map(component)
    expected = {
        "/@implementation/host-tools/ninja/binary_relative_path": "bin/ninja",
        "/@implementation/host-tools/ninja/consumers/0": "cmake",
        "/@implementation/host-tools/ninja/consumers/1": "meson",
        "/@implementation/host-tools/ninja/consumers/2": "vcpkg",
        "/@implementation/host-tools/ninja/install_prefix": "/opt/crossforge/host-tools/ninja",
        "/@implementation/host-tools/ninja/license_relative_path": "share/licenses/ninja/COPYING",
        "/@implementation/host-tools/ninja/path_precedence": "before-system",
        "/@implementation/host-tools/ninja/schema_version": 1,
        "/@implementation/host-tools/ninja/system_binary": "/usr/bin/ninja",
    }
    require(materials == expected, "Ninja host-tool policy differs")
    return {
        "binary_relative_path": materials[
            "/@implementation/host-tools/ninja/binary_relative_path"
        ],
        "install_prefix": materials[
            "/@implementation/host-tools/ninja/install_prefix"
        ],
        "license_relative_path": materials[
            "/@implementation/host-tools/ninja/license_relative_path"
        ],
        "system_binary": materials[
            "/@implementation/host-tools/ninja/system_binary"
        ],
    }


def expected_source_manifest(source_component, identity):
    return {
        "schema_version": 1,
        "kind": "crossforge-ninja-source",
        "component": {
            "name": "sources/ninja",
            "canonical_sha256": canonical_sha256(source_component),
        },
        "release": {
            "version": identity["/host_tools/ninja/version"],
            "tag": identity["/host_tools/ninja/tag"],
            "commit": identity["/host_tools/ninja/commit"],
            "tag_evidence_sha256": identity[
                "/host_tools/ninja/tag_evidence_sha256"
            ],
            "release_evidence_sha256": identity[
                "/host_tools/ninja/release/evidence_sha256"
            ],
        },
        "binary_archive": {
            "sha256": identity["/host_tools/ninja/binary/sha256"],
            "sha512": identity["/host_tools/ninja/binary/sha512"],
            "size": identity["/host_tools/ninja/binary/size"],
        },
        "binary": {
            "sha256": identity["/host_tools/ninja/binary/extracted_sha256"],
            "sha512": identity["/host_tools/ninja/binary/extracted_sha512"],
            "size": identity["/host_tools/ninja/binary/extracted_size"],
        },
        "source_archive": {
            "sha256": identity["/host_tools/ninja/source/sha256"],
            "sha512": identity["/host_tools/ninja/source/sha512"],
            "size": identity["/host_tools/ninja/source/size"],
        },
        "license": {
            "expression": identity["/host_tools/ninja/license/expression"],
            "sha256": identity["/host_tools/ninja/license/sha256"],
            "size": identity["/host_tools/ninja/license/size"],
        },
    }


def validate_prepared(prepared_root, source_component_path, source_sha256):
    try:
        source_component, identity = PREPARER["load_identity"](
            source_component_path, source_sha256
        )
        binary, license_payload, _binary_archive, _source_archive = PREPARER[
            "verify_archives"
        ](
            prepared_root / "materials/ninja-linux.zip",
            prepared_root / "materials/ninja-source.tar.gz",
            identity,
        )
    except PREPARER["PreparationError"] as error:
        raise QualificationError("invalid prepared Ninja source: %s" % error) from error
    manifest = load_json(prepared_root / "source.json")
    require(
        manifest == expected_source_manifest(source_component, identity),
        "prepared Ninja source manifest differs",
    )
    require(
        (prepared_root / "prepared/ninja").read_bytes() == binary
        and (prepared_root / "prepared/COPYING").read_bytes() == license_payload,
        "prepared Ninja files differ from locked archives",
    )
    actual = []
    for path in prepared_root.rglob("*"):
        require(
            not path.is_symlink() and (path.is_dir() or path.is_file()),
            "prepared Ninja tree contains an unsafe entry",
        )
        if path.is_file():
            actual.append(path.relative_to(prepared_root).as_posix())
    require(
        sorted(actual)
        == [
            "materials/ninja-linux.zip",
            "materials/ninja-source.tar.gz",
            "prepared/COPYING",
            "prepared/ninja",
            "source.json",
        ],
        "prepared Ninja file inventory differs",
    )
    return source_component, identity, manifest


def write_probe(source):
    source.mkdir(parents=True)
    (source / "probe.c").write_text(
        '#include <stdio.h>\nint main(void){puts("42");return 0;}\n',
        encoding="utf-8",
    )


def smoke_builds(tool, environment, work):
    compiler = "/opt/rh/gcc-toolset-15/root/usr/bin/gcc"
    direct = work / "direct"
    write_probe(direct)
    (direct / "build.ninja").write_text(
        "rule cc\n"
        "  command = %s $in -o $out\n"
        "build probe: cc probe.c\n"
        "default probe\n" % compiler,
        encoding="utf-8",
    )
    run([tool, "-f", "build.ninja"], cwd=direct, env=environment)
    direct_stdout, _stderr = run([direct / "probe"], env=environment)

    cmake_source = work / "cmake-source"
    write_probe(cmake_source)
    (cmake_source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\n"
        "project(crossforge_ninja C)\n"
        "add_executable(crossforge-ninja probe.c)\n",
        encoding="utf-8",
    )
    cmake_build = work / "cmake-build"
    run(
        [
            "cmake",
            "-S",
            cmake_source,
            "-B",
            cmake_build,
            "-G",
            "Ninja",
            "-DCMAKE_C_COMPILER=" + compiler,
            "-DCMAKE_MAKE_PROGRAM=" + str(tool),
        ],
        env=environment,
    )
    run(["cmake", "--build", cmake_build], env=environment)
    cmake_stdout, _stderr = run(
        [cmake_build / "crossforge-ninja"], env=environment
    )
    cache = (cmake_build / "CMakeCache.txt").read_text(encoding="utf-8")
    make_programs = re.findall(
        r"^CMAKE_MAKE_PROGRAM:[^=]+=(.*)$", cache, re.MULTILINE
    )
    require(
        make_programs == [str(tool)],
        "CMake did not bind the locked Ninja tool: %r" % make_programs,
    )

    meson_source = work / "meson-source"
    write_probe(meson_source)
    (meson_source / "meson.build").write_text(
        "project('crossforge-ninja', 'c')\n"
        "executable('crossforge-ninja', 'probe.c')\n",
        encoding="utf-8",
    )
    meson_build = work / "meson-build"
    meson_environment = dict(environment)
    meson_environment["CC"] = compiler
    run(
        ["meson", "setup", meson_build, meson_source, "--backend=ninja"],
        env=meson_environment,
    )
    run(["meson", "compile", "-C", meson_build], env=meson_environment)
    meson_stdout, _stderr = run(
        [meson_build / "crossforge-ninja"], env=meson_environment
    )
    require(
        direct_stdout == cmake_stdout == meson_stdout == "42\n",
        "Ninja consumer smoke output differs",
    )
    return {
        "direct": sha256_file(direct / "probe"),
        "cmake": sha256_file(cmake_build / "crossforge-ninja"),
        "meson": sha256_file(meson_build / "crossforge-ninja"),
    }


def qualify(
    prepared_root,
    source_component_path,
    source_sha256,
    policy_component_path,
    policy_sha256,
    tool_component_path,
    tool_sha256,
    destination_root,
):
    source_component, identity, source_manifest = validate_prepared(
        prepared_root, source_component_path, source_sha256
    )
    policy_component = load_component(
        policy_component_path,
        "implementation/ninja-host-tool",
        policy_sha256,
    )
    tool_component = load_component(
        tool_component_path, "host-tools/ninja", tool_sha256
    )
    policy = load_policy(policy_component)
    dependencies = {
        item["component"]: item["canonical_sha256"]
        for item in tool_component["dependencies"]
    }
    require(
        set(dependencies)
        == {
            "implementation/ninja-host-tool",
            "rpm/host-runtime",
            "sources/ninja",
        }
        and dependencies["implementation/ninja-host-tool"] == policy_sha256
        and dependencies["sources/ninja"] == source_sha256,
        "Ninja host-tool component dependency closure differs",
    )
    host_report = load_json(
        Path("/opt/crossforge/qualification/host-runtime.json")
    )
    require(
        host_report.get("status") == "passed"
        and host_report.get("rpm", {})
        .get("release_binding", {})
        .get("canonical_sha256")
        == dependencies["rpm/host-runtime"],
        "base host-runtime qualification differs",
    )
    version = identity["/host_tools/ninja/version"]
    expected_root = Path(policy["install_prefix"]) / version
    require(destination_root == expected_root, "Ninja install root differs")
    require(
        not destination_root.exists() and not destination_root.is_symlink(),
        "Ninja install root already exists",
    )
    destination_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=".%s." % version,
            dir=str(destination_root.parent),
        )
    )
    try:
        binary = temporary / policy["binary_relative_path"]
        license_path = temporary / policy["license_relative_path"]
        binary.parent.mkdir(parents=True)
        license_path.parent.mkdir(parents=True)
        shutil.copy2(str(prepared_root / "prepared/ninja"), str(binary))
        shutil.copy2(str(prepared_root / "prepared/COPYING"), str(license_path))
        os.chmod(str(binary), 0o755)
        os.chmod(str(license_path), 0o644)
        temporary.rename(destination_root)
    except BaseException:
        shutil.rmtree(str(temporary), ignore_errors=True)
        raise
    tool = destination_root / policy["binary_relative_path"]
    license_path = destination_root / policy["license_relative_path"]
    environment = dict(os.environ)
    expected_path_prefix = str(tool.parent)
    require(
        environment.get("PATH", "").split(":", 1)[0] == expected_path_prefix
        and shutil.which("ninja", path=environment["PATH"]) == str(tool),
        "Ninja overlay does not take PATH precedence",
    )
    version_stdout, _stderr = run([tool, "--version"], env=environment)
    require(version_stdout == version + "\n", "Ninja version differs")
    system_binary = Path(policy["system_binary"])
    system_version, _stderr = run([system_binary, "--version"])
    require(
        system_binary.is_file()
        and system_binary != tool
        and system_version.strip() != version,
        "Ninja overlay replaced or matched the RPM-owned system binary",
    )
    file_output, _stderr = run(["file", tool])
    elf_header, _stderr = run(["readelf", "-h", tool])
    program_headers, _stderr = run(["readelf", "-l", tool])
    dynamic, _stderr = run(["readelf", "-d", tool])
    ldd, _stderr = run(["ldd", tool])
    require(
        "ELF 64-bit LSB executable, x86-64" in file_output
        and re.search(
            r"^\s*Machine:\s+Advanced Micro Devices X86-64\s*$",
            elf_header,
            re.MULTILINE,
        )
        and "[Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]"
        in program_headers
        and all(tag not in dynamic for tag in ("RPATH", "RUNPATH", "TEXTREL"))
        and "not found" not in ldd,
        "Ninja ELF/runtime policy differs",
    )
    require(
        sha256_file(tool)
        == identity["/host_tools/ninja/binary/extracted_sha256"]
        and sha256_file(license_path)
        == identity["/host_tools/ninja/license/sha256"],
        "installed Ninja material differs",
    )
    with tempfile.TemporaryDirectory(prefix="crossforge-ninja-smoke-") as temporary:
        smokes = smoke_builds(tool, environment, Path(temporary))
    return {
        "schema_version": 1,
        "kind": "crossforge-ninja-host-tool-qualification",
        "status": "passed",
        "components": {
            "source": {
                "component": "sources/ninja",
                "canonical_sha256": canonical_sha256(source_component),
            },
            "policy": {
                "component": "implementation/ninja-host-tool",
                "canonical_sha256": canonical_sha256(policy_component),
            },
            "tool": {
                "component": "host-tools/ninja",
                "canonical_sha256": canonical_sha256(tool_component),
            },
        },
        "source_manifest_sha256": sha256_file(prepared_root / "source.json"),
        "install_root": str(destination_root),
        "binary": {
            "path": str(tool),
            "sha256": sha256_file(tool),
            "version": version,
            "interpreter": "/lib64/ld-linux-x86-64.so.2",
        },
        "license": {
            "path": str(license_path),
            "expression": "Apache-2.0",
            "sha256": sha256_file(license_path),
        },
        "system_binary": {
            "path": str(system_binary),
            "version": system_version.strip(),
        },
        "smoke": smokes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--prepared-root", type=Path, required=True)
    for role in ("source", "policy", "tool"):
        parser.add_argument("--%s-component" % role, type=Path, required=True)
        parser.add_argument("--%s-component-sha256" % role, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = qualify(
        arguments.prepared_root,
        arguments.source_component,
        arguments.source_component_sha256,
        arguments.policy_component,
        arguments.policy_component_sha256,
        arguments.tool_component,
        arguments.tool_component_sha256,
        arguments.destination_root,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(arguments.output)
    print("qualified Ninja host-tool overlay: %s" % arguments.output)
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
