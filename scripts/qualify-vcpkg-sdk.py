#!/usr/bin/env python3
"""Qualify the installed vcpkg root, triplets, and CMake toolchains offline."""

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
ComponentError = COMPONENT_READER["ComponentError"]
HISTORY = runpy.run_path(str(SCRIPT_DIRECTORY / "fetch-vcpkg-history.py"))
HistoryError = HISTORY["FetchError"]
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
TARGETS = {
    "x86_64": {
        "triple": "x86_64-unknown-linux-gnu",
        "processor": "x86_64",
        "machine": "Advanced Micro Devices X86-64",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
        "triplets": (
            "crossforge-x64-el8",
            "crossforge-x64-el8-dynamic",
        ),
    },
    "aarch64": {
        "triple": "aarch64-unknown-linux-gnu",
        "processor": "aarch64",
        "machine": "AArch64",
        "interpreter": "/lib/ld-linux-aarch64.so.1",
        "triplets": (
            "crossforge-arm64-el8",
            "crossforge-arm64-el8-dynamic",
        ),
    },
}
HOST_TRIPLET = "crossforge-host-x64-el8"


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
            value = json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError) as error:
        raise QualificationError("cannot load %s: %s" % (path, error)) from error
    require(isinstance(value, dict), "%s must contain an object" % path)
    return value


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha512_file(path):
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
    return {item["path"]: item["value"] for item in document["materials"]}


def flatten_policy(value, path=()):
    if isinstance(value, dict) and value:
        result = {}
        for name in sorted(value):
            result.update(flatten_policy(value[name], path + (name,)))
        return result
    if isinstance(value, list) and value:
        result = {}
        for index, child in enumerate(value):
            result.update(flatten_policy(child, path + (index,)))
        return result
    return {
        "/@implementation/vcpkg/" + "/".join(str(item) for item in path): value
    }


def qualify_source(root, manifest_path, source_component):
    manifest = load_json(manifest_path)
    require(
        set(manifest) == {
            "schema_version",
            "kind",
            "component",
            "registry",
            "tool",
            "licenses",
        }
        and manifest["schema_version"] == 1
        and manifest["kind"] == "crossforge-vcpkg-source"
        and manifest["component"]
        == {
            "name": "sources/vcpkg",
            "canonical_sha256": COMPONENT_READER["canonical_sha256"](
                source_component
            ),
        },
        "vcpkg source manifest differs",
    )
    materials = material_map(source_component)
    registry = manifest["registry"]
    require(
        set(registry)
        == {
            "commit",
            "history_commit_count",
            "tag",
            "tag_object",
            "tree",
            "version_database",
        }
        and registry["commit"] == materials["/vcpkg/release/commit"]
        and registry["tag"] == materials["/vcpkg/release/tag"]
        and registry["tag_object"]
        == materials["/vcpkg/release/tag_object"]
        and registry["history_commit_count"] == 30001,
        "vcpkg registry manifest identity differs",
    )
    require(
        root.is_dir() and not root.is_symlink() and (root / ".git").is_dir(),
        "vcpkg root is invalid",
    )
    head, _stderr = run(["git", "rev-parse", "HEAD"], cwd=root)
    tree, _stderr = run(["git", "rev-parse", "HEAD^{tree}"], cwd=root)
    shallow, _stderr = run(
        ["git", "rev-parse", "--is-shallow-repository"], cwd=root
    )
    remote, _stderr = run(["git", "remote", "get-url", "origin"], cwd=root)
    refs, _stderr = run(
        ["git", "for-each-ref", "--format=%(refname)"], cwd=root
    )
    tag_object, _stderr = run(
        ["git", "rev-parse", "refs/tags/" + registry["tag"]], cwd=root
    )
    tag_commit, _stderr = run(
        ["git", "rev-parse", "refs/tags/" + registry["tag"] + "^{}"],
        cwd=root,
    )
    history_count, _stderr = run(
        ["git", "rev-list", "--count", registry["commit"]], cwd=root
    )
    object_inventory, _stderr = run(
        [
            "git",
            "rev-list",
            "--objects",
            "--missing=print",
            registry["commit"],
        ],
        cwd=root,
    )
    tracked, _stderr = run(["git", "diff", "--quiet"], cwd=root)
    require(tracked == "", "vcpkg tracked checkout differs")
    require(
        head.strip() == registry["commit"]
        and tree.strip() == registry["tree"]
        and shallow.strip() == "false"
        and remote.strip() == materials["/vcpkg/repository"]
        and refs.splitlines() == ["refs/tags/" + registry["tag"]]
        and tag_object.strip() == registry["tag_object"]
        and tag_commit.strip() == registry["commit"]
        and int(history_count.strip()) == registry["history_commit_count"]
        and not any(
            line.startswith("?") for line in object_inventory.splitlines()
        ),
        "installed vcpkg Git identity differs",
    )
    run(["git", "fsck", "--full", "--no-dangling"], cwd=root)
    status, _stderr = run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=root
    )
    require(
        status.splitlines()
        == [
            "?? licenses/vcpkg-tool/LICENSE.txt",
            "?? licenses/vcpkg-tool/NOTICE.txt",
        ],
        "installed vcpkg tree has unexpected files",
    )
    try:
        trees = HISTORY["version_trees"](root)
        missing = HISTORY["missing_trees"](root, trees)
    except HistoryError as error:
        raise QualificationError(str(error)) from error
    tree_set_sha256 = hashlib.sha256(
        ("\n".join(trees) + "\n").encode("ascii")
    ).hexdigest()
    require(
        not missing
        and registry["version_database"]
        == {
            "files": HISTORY["EXPECTED_VERSION_FILES"],
            "tree_set_sha256": tree_set_sha256,
            "unique_trees": HISTORY["EXPECTED_VERSION_TREES"],
        },
        "installed vcpkg version database differs",
    )
    for role in ("license", "notice"):
        path = root / materials[
            "/vcpkg/registry_license/%s_file" % role
        ]
        require(
            path.is_file()
            and not path.is_symlink()
            and sha256_file(path)
            == materials["/vcpkg/registry_license/%s_sha256" % role],
            "installed vcpkg registry %s differs" % role,
        )
    tool = root / "vcpkg"
    require(tool.is_file() and not tool.is_symlink(), "vcpkg tool is missing")
    version, _stderr = run([tool, "version", "--disable-metrics"], cwd=root)
    tool_manifest = manifest["tool"]
    expected_version = "vcpkg package management program version %s-%s" % (
        materials["/vcpkg/tool/tag"],
        materials["/vcpkg/tool/commit"],
    )
    require(
        set(tool_manifest)
        == {"commit", "sha256", "sha512", "signature_sha256", "version"}
        and tool_manifest
        == {
            "commit": materials["/vcpkg/tool/commit"],
            "sha256": materials["/vcpkg/tool/sha256"],
            "sha512": materials["/vcpkg/tool/sha512"],
            "signature_sha256": materials["/vcpkg/tool/signature/sha256"],
            "version": expected_version,
        }
        and sha256_file(tool) == tool_manifest["sha256"]
        and sha512_file(tool) == tool_manifest["sha512"]
        and version.splitlines()
        and version.splitlines()[0] == expected_version,
        "installed vcpkg tool differs",
    )
    expected_licenses = {
        role + "_sha256": materials[
            "/vcpkg/tool/license/%s_sha256" % role
        ]
        for role in ("license", "notice")
    }
    require(
        manifest["licenses"] == expected_licenses,
        "vcpkg-tool license manifest differs",
    )
    for role in ("license", "notice"):
        path = root / "licenses/vcpkg-tool" / (
            "LICENSE.txt" if role == "license" else "NOTICE.txt"
        )
        require(
            path.is_file()
            and not path.is_symlink()
            and sha256_file(path)
            == materials["/vcpkg/tool/license/%s_sha256" % role],
            "installed vcpkg-tool %s differs" % role,
        )
    for name in (
        "downloads",
        "buildtrees",
        "packages",
        "installed",
        "vcpkg_installed",
    ):
        require(not (root / name).exists(), "vcpkg build output leaked: %s" % name)
    return {
        "manifest_sha256": sha256_file(manifest_path),
        "registry": registry,
        "tool_sha256": sha256_file(tool),
        "tool_version": version.splitlines()[0],
    }


def qualify_integration(
    cmake_root,
    triplet_root,
    manifest_path,
    integration_component,
    sdk_component,
    source_component_sha256,
    ninja_component_sha256,
    cmake_component_sha256,
):
    manifest = load_json(manifest_path)
    integration_sha256 = COMPONENT_READER["canonical_sha256"](
        integration_component
    )
    sdk_sha256 = COMPONENT_READER["canonical_sha256"](sdk_component)
    require(
        set(manifest) == {
            "schema_version",
            "kind",
            "components",
            "files",
            "policy",
        }
        and manifest["schema_version"] == 1
        and manifest["kind"] == "crossforge-vcpkg-integration"
        and manifest["components"]
        == {
            "integration": {
                "component": "implementation/vcpkg-integration",
                "canonical_sha256": integration_sha256,
            },
            "sdk": {
                "component": "vcpkg/sdk-build",
                "canonical_sha256": sdk_sha256,
            },
        }
        and flatten_policy(manifest["policy"])
        == material_map(integration_component),
        "vcpkg integration manifest differs",
    )
    dependencies = {
        item["component"]: item["canonical_sha256"]
        for item in sdk_component["dependencies"]
    }
    require(
        set(dependencies)
        == {
            "rpm/host-runtime",
            "host-tools/ninja",
            "host-tools/cmake",
            "sources/vcpkg",
            "implementation/vcpkg-integration",
            "toolchain/x86_64-build",
            "toolchain/aarch64-build",
        }
        and dependencies["sources/vcpkg"] == source_component_sha256
        and dependencies["host-tools/ninja"] == ninja_component_sha256
        and dependencies["host-tools/cmake"] == cmake_component_sha256
        and dependencies["implementation/vcpkg-integration"]
        == integration_sha256,
        "vcpkg SDK component dependency closure differs",
    )
    records = manifest["files"]
    require(isinstance(records, list) and len(records) == 8, "vcpkg file inventory differs")
    observed_paths = []
    for record in records:
        require(
            isinstance(record, dict)
            and set(record) == {"path", "sha256"}
            and SHA256_RE.match(record["sha256"]),
            "invalid vcpkg integration file record",
        )
        logical = record["path"]
        if logical.startswith("cmake/"):
            path = cmake_root / logical.split("/", 1)[1]
        elif logical.startswith("triplets/"):
            path = triplet_root / logical.split("/", 1)[1]
        else:
            raise QualificationError("invalid vcpkg integration path")
        require(
            path.is_file()
            and not path.is_symlink()
            and sha256_file(path) == record["sha256"],
            "installed vcpkg integration file differs: %s" % logical,
        )
        observed_paths.append(logical)
    actual = []
    for root, prefix in ((cmake_root, "cmake"), (triplet_root, "triplets")):
        for path in root.rglob("*"):
            require(
                path.is_file() and not path.is_symlink(),
                "unexpected vcpkg integration entry: %s"
                % path.relative_to(root),
            )
            actual.append(prefix + "/" + path.relative_to(root).as_posix())
    actual.sort()
    require(sorted(observed_paths) == actual, "installed vcpkg integration inventory differs")
    target_text = "\n".join(
        (cmake_root / (profile["triple"] + ".cmake")).read_text(
            encoding="utf-8"
        )
        for profile in TARGETS.values()
    )
    require(
        "qemu" not in target_text.lower()
        and target_text.count('set(CMAKE_SYSTEM_NAME "Linux"') == 2
        and "set(CMAKE_CROSSCOMPILING" not in target_text
        and "HOSTRUNNER" in target_text,
        "target toolchain execution policy differs",
    )
    return {
        "manifest_sha256": sha256_file(manifest_path),
        "component_sha256": integration_sha256,
        "sdk_component_sha256": sdk_sha256,
        "files": records,
        "dependencies": dependencies,
    }


def write_project(source):
    source.mkdir()
    (source / "probe.c").write_text(
        "int crossforge_probe(void){return 42;}\n", encoding="utf-8"
    )
    (source / "bridge.c").write_text(
        "extern int crossforge_probe(void);\n"
        "int crossforge_bridge(void){return crossforge_probe();}\n",
        encoding="utf-8",
    )
    (source / "main.cpp").write_text(
        "#include <iostream>\n"
        "extern \"C\" int crossforge_probe(void);\n"
        "int main(){std::cout<<crossforge_probe()<<'\\n';}\n",
        encoding="utf-8",
    )
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\n"
        "project(crossforge_vcpkg_sdk LANGUAGES C CXX)\n"
        "file(WRITE \"${CMAKE_BINARY_DIR}/identity.txt\"\n"
        "  \"${CMAKE_CROSSCOMPILING}\\n${CMAKE_C_COMPILER}\\n\"\n"
        "  \"${CMAKE_CXX_COMPILER}\\n${CMAKE_SYSROOT}\\n\")\n"
        "add_library(crossforge-probe STATIC probe.c)\n"
        "add_library(crossforge-bridge SHARED bridge.c)\n"
        "target_link_libraries(crossforge-bridge PRIVATE crossforge-probe)\n"
        "add_executable(crossforge-vcpkg-smoke main.cpp)\n"
        "target_link_libraries(crossforge-vcpkg-smoke PRIVATE crossforge-probe)\n",
        encoding="utf-8",
    )


def cmake_smoke(cmake_root, qemu, release, work):
    source = work / "source"
    write_project(source)
    configurations = [
        (
            "host",
            cmake_root / "host-gts15.cmake",
            False,
            None,
        )
    ]
    for arch in ("x86_64", "aarch64"):
        profile = TARGETS[arch]
        configurations.append(
            (
                arch,
                cmake_root / (profile["triple"] + ".cmake"),
                True,
                profile,
            )
        )
    results = []
    for name, toolchain, cross, profile in configurations:
        build = work / ("build-" + name)
        run(
            [
                "cmake",
                "-S",
                source,
                "-B",
                build,
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_TOOLCHAIN_FILE=" + str(toolchain),
            ]
        )
        run(["cmake", "--build", build])
        cache = (build / "CMakeCache.txt").read_text(encoding="utf-8")
        ninja = Path(os.environ["NINJA_ROOT"]) / "bin/ninja"
        require(
            "CMAKE_MAKE_PROGRAM:FILEPATH=%s" % ninja in cache,
            "%s CMake did not select the locked Ninja tool" % name,
        )
        identity = (build / "identity.txt").read_text(encoding="utf-8").splitlines()
        require(len(identity) == 4, "%s CMake identity is incomplete" % name)
        cmake_boolean = identity[0].upper()
        require(
            cmake_boolean
            in (
                ("1", "ON", "TRUE", "YES", "Y")
                if cross
                else ("0", "OFF", "FALSE", "NO", "N", "IGNORE", "")
            ),
            "%s CMake cross state differs: %r" % (name, identity[0]),
        )
        if profile is None:
            compiler_root = "/opt/rh/gcc-toolset-15/root/usr/bin"
            expected_identity = [
                compiler_root + "/gcc",
                compiler_root + "/g++",
                "",
            ]
        else:
            triple = profile["triple"]
            compiler_root = "/opt/crossforge/targets/%s/bin" % triple
            expected_identity = [
                compiler_root + "/" + triple + "-gcc",
                compiler_root + "/" + triple + "-g++",
                "/opt/crossforge/sysroots/el8/" + name,
            ]
        require(
            identity[1:] == expected_identity,
            "%s CMake compiler/sysroot identity differs: %r"
            % (name, identity[1:]),
        )
        executable = build / "crossforge-vcpkg-smoke"
        shared = build / "libcrossforge-bridge.so"
        require(executable.is_file() and shared.is_file(), "%s CMake output missing" % name)
        if profile is None:
            stdout, _stderr = run([executable])
            require(stdout == "42\n", "host CMake smoke output differs")
            machine = "Advanced Micro Devices X86-64"
        else:
            triple = profile["triple"]
            readelf = Path("/opt/crossforge/targets") / triple / "bin" / (triple + "-readelf")
            header, _stderr = run([readelf, "-h", executable])
            require(
                re.search(
                    r"^\s*Machine:\s+%s\s*$" % re.escape(profile["machine"]),
                    header,
                    re.MULTILINE,
                )
                is not None,
                "%s CMake output machine differs" % name,
            )
            dynamic, _stderr = run([readelf, "-d", shared])
            executable_dynamic, _stderr = run([readelf, "-d", executable])
            program_headers, _stderr = run([readelf, "-l", executable])
            require(
                all(
                    tag not in output
                    for output in (dynamic, executable_dynamic)
                    for tag in ("RPATH", "RUNPATH", "TEXTREL")
                )
                and "[Requesting program interpreter: %s]"
                % profile["interpreter"]
                in program_headers,
                "%s CMake shared library policy differs" % name,
            )
            if name == "x86_64":
                command = [executable]
            else:
                executor = release["qemu"]["executor"]
                command = [
                    qemu,
                    "-cpu",
                    executor["cpu"],
                    "-r",
                    executor["uname_release"],
                    "-L",
                    "/opt/crossforge/sysroots/el8/aarch64",
                    executable,
                ]
            stdout, _stderr = run(command)
            require(stdout == "42\n", "%s CMake smoke output differs" % name)
            machine = profile["machine"]
        results.append(
            {
                "name": name,
                "cross_compiling": cross,
                "executable_sha256": sha256_file(executable),
                "machine": machine,
                "interpreter": None if profile is None else profile["interpreter"],
                "shared_sha256": sha256_file(shared),
                "toolchain_sha256": sha256_file(toolchain),
            }
        )
    return results


def qualify_ninja(root, release, component_path, component_sha256, report_path):
    component = load_component(
        component_path, "host-tools/ninja", component_sha256
    )
    report = load_json(report_path)
    release_ninja = release["host_tools"]["ninja"]
    version = release_ninja["version"]
    ninja_root = Path("/opt/crossforge/host-tools/ninja") / version
    tool = ninja_root / "bin/ninja"
    license_path = ninja_root / "share/licenses/ninja/COPYING"
    require(
        report.get("schema_version") == 1
        and report.get("kind") == "crossforge-ninja-host-tool-qualification"
        and report.get("status") == "passed"
        and report.get("components", {}).get("tool")
        == {
            "component": "host-tools/ninja",
            "canonical_sha256": COMPONENT_READER["canonical_sha256"](
                component
            ),
        }
        and report.get("install_root") == str(ninja_root),
        "Ninja host-tool qualification report differs",
    )
    require(
        os.environ.get("NINJA_ROOT") == str(ninja_root)
        and shutil.which("ninja", path=os.environ["PATH"]) == str(tool),
        "Ninja host-tool PATH selection differs",
    )
    version_stdout, _stderr = run([tool, "--version"])
    require(
        tool.is_file()
        and not tool.is_symlink()
        and sha256_file(tool)
        == release_ninja["binary"]["extracted_sha256"]
        and version_stdout == version + "\n"
        and license_path.is_file()
        and not license_path.is_symlink()
        and sha256_file(license_path) == release_ninja["license"]["sha256"],
        "installed Ninja host-tool identity differs",
    )
    fetched, _stderr = run(
        [root / "vcpkg", "fetch", "ninja", "--disable-metrics"], cwd=root
    )
    fetch_lines = [line.strip() for line in fetched.splitlines() if line.strip()]
    require(
        fetch_lines and fetch_lines[-1] == str(tool),
        "vcpkg did not select the locked Ninja host tool: %r" % fetch_lines,
    )
    return {
        "component_sha256": COMPONENT_READER["canonical_sha256"](component),
        "report_sha256": sha256_file(report_path),
        "path": str(tool),
        "sha256": sha256_file(tool),
        "version": version,
        "vcpkg_fetch": fetch_lines,
    }


def qualify_cmake(root, release, component_path, component_sha256, report_path):
    component = load_component(
        component_path, "host-tools/cmake", component_sha256
    )
    report = load_json(report_path)
    release_cmake = release["host_tools"]["cmake"]
    version = release_cmake["version"]
    cmake_root = Path("/opt/crossforge/host-tools/cmake") / version
    tool = cmake_root / "bin/cmake"
    require(
        report.get("schema_version") == 1
        and report.get("kind") == "crossforge-cmake-host-tool-qualification"
        and report.get("status") == "passed"
        and report.get("components", {}).get("tool")
        == {
            "component": "host-tools/cmake",
            "canonical_sha256": COMPONENT_READER["canonical_sha256"](
                component
            ),
        }
        and report.get("install_root") == str(cmake_root),
        "CMake host-tool qualification report differs",
    )
    require(
        os.environ.get("CROSSFORGE_CMAKE_ROOT") == str(cmake_root)
        and os.environ.get("PATH", "").split(":", 1)[0] == str(tool.parent)
        and shutil.which("cmake", path=os.environ["PATH"]) == str(tool),
        "CMake host-tool PATH selection differs",
    )
    version_stdout, _stderr = run([tool, "--version"])
    payload = next(
        item for item in release_cmake["payloads"] if item["path"] == "bin/cmake"
    )
    tools = load_json(root / "scripts/vcpkg-tools.json")
    selections = [
        item
        for item in tools.get("tools", [])
        if item.get("name") == "cmake"
        and item.get("os") == "linux"
        and item.get("arch") == "amd64"
    ]
    require(
        tool.is_file()
        and not tool.is_symlink()
        and sha256_file(tool) == payload["sha256"]
        and version_stdout.splitlines()[0] == "cmake version " + version,
        "installed CMake host-tool identity differs",
    )
    require(
        len(selections) == 1
        and selections[0].get("version") == version
        and selections[0].get("url") == release_cmake["binary"]["url"]
        and selections[0].get("sha512") == release_cmake["binary"]["sha512"]
        and selections[0].get("archive")
        == "cmake-4.4.0-linux-x86_64.tar.gz"
        and selections[0].get("executable")
        == "cmake-4.4.0-linux-x86_64/bin/cmake",
        "vcpkg CMake tool selection differs from the release lock",
    )
    fetched, _stderr = run(
        [root / "vcpkg", "fetch", "cmake", "--disable-metrics"], cwd=root
    )
    fetch_lines = [line.strip() for line in fetched.splitlines() if line.strip()]
    require(
        fetch_lines and fetch_lines[-1] == str(tool),
        "vcpkg did not select the locked CMake host tool: %r" % fetch_lines,
    )
    return {
        "component_sha256": COMPONENT_READER["canonical_sha256"](component),
        "report_sha256": sha256_file(report_path),
        "path": str(tool),
        "sha256": sha256_file(tool),
        "version": version,
        "vcpkg_fetch": fetch_lines,
        "vcpkg_tools_record": selections[0],
    }


def qualify(
    release_path,
    root,
    source_manifest_path,
    integration_manifest_path,
    cmake_root,
    triplet_root,
    qemu,
    ninja_report_path,
    cmake_report_path,
    component_paths,
    component_sha256,
):
    release = load_json(release_path)
    release_sha256 = hashlib.sha256(
        json.dumps(
            release, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    final_report = load_json(
        Path("/opt/crossforge/qualification/final-sdk.json")
    )
    require(
        final_report.get("status") == "passed"
        and final_report.get("release_sha256") == release_sha256,
        "base SDK qualification is absent or stale",
    )
    source_component = load_component(
        component_paths["source"],
        "sources/vcpkg",
        component_sha256["source"],
    )
    integration_component = load_component(
        component_paths["integration"],
        "implementation/vcpkg-integration",
        component_sha256["integration"],
    )
    sdk_component = load_component(
        component_paths["sdk"],
        "vcpkg/sdk-build",
        component_sha256["sdk"],
    )
    ninja_component = load_component(
        component_paths["ninja"],
        "host-tools/ninja",
        component_sha256["ninja"],
    )
    cmake_component = load_component(
        component_paths["cmake"],
        "host-tools/cmake",
        component_sha256["cmake"],
    )
    require(
        COMPONENT_READER["canonical_sha256"](ninja_component)
        == component_sha256["ninja"],
        "Ninja component digest differs",
    )
    expected_environment = {
        "VCPKG_ROOT": str(root),
        "VCPKG_OVERLAY_TRIPLETS": str(triplet_root),
        "VCPKG_DEFAULT_HOST_TRIPLET": HOST_TRIPLET,
        "VCPKG_DISABLE_METRICS": "1",
        "VCPKG_FORCE_SYSTEM_BINARIES": "1",
        "NINJA_ROOT": "/opt/crossforge/host-tools/ninja/1.13.2",
        "CROSSFORGE_CMAKE_ROOT": "/opt/crossforge/host-tools/cmake/4.4.0",
    }
    require(
        all(os.environ.get(name) == value for name, value in expected_environment.items())
        and "VCPKG_DEFAULT_TRIPLET" not in os.environ
        and "VCPKG_FORCE_DOWNLOADED_BINARIES" not in os.environ,
        "vcpkg SDK environment differs",
    )
    ninja = qualify_ninja(
        root,
        release,
        component_paths["ninja"],
        component_sha256["ninja"],
        ninja_report_path,
    )
    cmake_host_tool = qualify_cmake(
        root,
        release,
        component_paths["cmake"],
        component_sha256["cmake"],
        cmake_report_path,
    )
    source = qualify_source(root, source_manifest_path, source_component)
    integration = qualify_integration(
        cmake_root,
        triplet_root,
        integration_manifest_path,
        integration_component,
        sdk_component,
        component_sha256["source"],
        component_sha256["ninja"],
        component_sha256["cmake"],
    )
    help_text, _stderr = run(
        [
            root / "vcpkg",
            "help",
            "triplet",
            "--disable-metrics",
        ],
        cwd=root,
    )
    overlay = help_text.split("Overlay Triplets from", 1)
    require(len(overlay) == 2, "vcpkg did not load the overlay triplets")
    observed_triplets = sorted(
        line.strip()
        for line in overlay[1].split("See ", 1)[0].splitlines()[1:]
        if line.strip()
    )
    expected_triplets = sorted(
        [HOST_TRIPLET]
        + [name for profile in TARGETS.values() for name in profile["triplets"]]
    )
    require(
        observed_triplets == expected_triplets,
        "vcpkg triplet inventory differs: expected %r, observed %r"
        % (expected_triplets, observed_triplets),
    )
    with tempfile.TemporaryDirectory(prefix="crossforge-vcpkg-sdk-") as temporary:
        cmake = cmake_smoke(
            cmake_root, qemu, release, Path(temporary)
        )
    for name in ("downloads", "buildtrees", "packages", "installed", "vcpkg_installed"):
        require(not (root / name).exists(), "vcpkg probe polluted its root")
    return {
        "schema_version": 1,
        "kind": "crossforge-vcpkg-sdk-qualification",
        "status": "passed",
        "release_sha256": release_sha256,
        "environment": expected_environment,
        "source": source,
        "ninja": ninja,
        "cmake_host_tool": cmake_host_tool,
        "integration": integration,
        "triplets": expected_triplets,
        "cmake": cmake,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--integration-manifest", type=Path, required=True)
    parser.add_argument("--cmake-root", type=Path, required=True)
    parser.add_argument("--triplet-root", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--ninja-report", type=Path, required=True)
    parser.add_argument("--cmake-report", type=Path, required=True)
    for role in ("source", "integration", "sdk", "ninja", "cmake"):
        parser.add_argument("--%s-component" % role, type=Path, required=True)
        parser.add_argument("--%s-component-sha256" % role, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = qualify(
        arguments.release,
        arguments.root,
        arguments.source_manifest,
        arguments.integration_manifest,
        arguments.cmake_root,
        arguments.triplet_root,
        arguments.qemu,
        arguments.ninja_report,
        arguments.cmake_report,
        {
            "source": arguments.source_component,
            "integration": arguments.integration_component,
            "sdk": arguments.sdk_component,
            "ninja": arguments.ninja_component,
            "cmake": arguments.cmake_component,
        },
        {
            "source": arguments.source_component_sha256,
            "integration": arguments.integration_component_sha256,
            "sdk": arguments.sdk_component_sha256,
            "ninja": arguments.ninja_component_sha256,
            "cmake": arguments.cmake_component_sha256,
        },
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(arguments.output)
    print("qualified vcpkg SDK integration: %s" % arguments.output)
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
