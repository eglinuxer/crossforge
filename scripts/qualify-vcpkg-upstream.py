#!/usr/bin/env python3
"""Qualify pinned upstream vcpkg ports across every Crossforge triplet."""

import argparse
import hashlib
import json
import os
import re
import runpy
import shutil
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
TIER1_PORTS = (
    {"name": "fmt", "port_version": 1, "version": "12.2.0"},
    {"name": "zlib", "port_version": 1, "version": "1.3.2"},
)
TIER2_PORTS = (
    {"name": "curl", "port_version": 1, "version": "8.21.0"},
    {"name": "openssl", "port_version": 0, "version": "3.6.3"},
    {"name": "zlib", "port_version": 1, "version": "1.3.2"},
)
TIER3_PORTS = (
    {"name": "boost-json", "port_version": 0, "version": "1.91.0"},
    {"name": "protobuf", "port_version": 2, "version": "6.33.4"},
    {"name": "zlib", "port_version": 1, "version": "1.3.2"},
)
TIER_PROFILES = {
    "tier1": {
        "policy_component": "implementation/vcpkg-upstream-tier1-qualification",
        "qualification_component": "vcpkg/upstream-tier1-qualification",
        "prerequisite_component": "vcpkg/contract-qualification",
        "prerequisite_report": "/opt/crossforge/qualification/vcpkg/contract.json",
        "ports": TIER1_PORTS,
        "required_features": {
            "fmt": ("core",),
            "zlib": ("core",),
        },
        "libraries": (("fmt", "libfmt"), ("zlib", "libz")),
        "consumer_source": "consumer.cpp",
        "consumer_language": "cxx",
        "consumer_compile_flags": ("-std=c++17", "-O2"),
        "consumer_link_flags": ("-lfmt", "-lz"),
        "consumer_stdout": "crossforge-vcpkg-tier1:42\n",
    },
    "tier2": {
        "policy_component": "implementation/vcpkg-upstream-tier2-qualification",
        "qualification_component": "vcpkg/upstream-tier2-qualification",
        "prerequisite_component": "vcpkg/upstream-tier1-qualification",
        "prerequisite_report": (
            "/opt/crossforge/qualification/vcpkg/upstream-tier1.json"
        ),
        "ports": TIER2_PORTS,
        "required_features": {
            "curl": ("core", "openssl"),
            "openssl": ("core",),
            "zlib": ("core",),
        },
        "libraries": (
            ("curl", "libcurl"),
            ("openssl-crypto", "libcrypto"),
            ("openssl-ssl", "libssl"),
            ("zlib", "libz"),
        ),
        "consumer_source": "consumer.c",
        "consumer_language": "cc",
        "consumer_compile_flags": ("-std=c11", "-O2"),
        "consumer_link_flags": (
            "-lcurl",
            "-lssl",
            "-lcrypto",
            "-lz",
            "-ldl",
            "-pthread",
        ),
        "consumer_stdout": "crossforge-vcpkg-tier2:42\n",
    },
    "tier3": {
        "policy_component": "implementation/vcpkg-upstream-tier3-qualification",
        "qualification_component": "vcpkg/upstream-tier3-qualification",
        "prerequisite_component": "vcpkg/upstream-tier2-qualification",
        "prerequisite_report": (
            "/opt/crossforge/qualification/vcpkg/upstream-tier2.json"
        ),
        "ports": TIER3_PORTS,
        "required_features": {
            "boost-json": ("core",),
            "protobuf": ("core", "zlib"),
            "zlib": ("core",),
        },
        "libraries": (
            ("boost-json", "libboost_json"),
            ("protobuf", "libprotobuf"),
            ("zlib", "libz"),
        ),
        "consumer_language": "cmake",
        "consumer_stdout": '{"text":"crossforge-vcpkg-tier3","value":42}\n',
    },
}
POLICY_COMPONENT = TIER_PROFILES["tier1"]["policy_component"]
QUALIFICATION_COMPONENT = TIER_PROFILES["tier1"]["qualification_component"]
EXPECTED_PORTS = TIER1_PORTS


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


def indexed_feature_sets(materials, prefix):
    records = {}
    expression = re.compile(r"^%s([^/]+)/([0-9]+)$" % re.escape(prefix))
    for path, value in materials.items():
        match = expression.match(path)
        if match:
            records.setdefault(match.group(1), {})[int(match.group(2))] = value
    result = {}
    for name, features in records.items():
        require(
            sorted(features) == list(range(len(features))),
            "feature indexes differ for %s" % name,
        )
        result[name] = tuple(features[index] for index in sorted(features))
    return result


def policy_context(policy_component, fixture_root, tier="tier1"):
    gate = TIER_PROFILES.get(tier)
    require(gate is not None, "unsupported upstream vcpkg tier")
    materials = material_map(policy_component)
    prefix = "/@implementation/vcpkg-upstream-%s/" % tier
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
    require(tuple(ports) == gate["ports"], "upstream vcpkg port set differs")
    required_features = indexed_feature_sets(
        materials, prefix + "required_features/"
    )
    require(
        required_features == gate["required_features"],
        "upstream vcpkg required feature set differs",
    )
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
    prerequisite_report,
    tier="tier1",
):
    gate = TIER_PROFILES.get(tier)
    require(gate is not None, "unsupported upstream vcpkg tier")
    dependencies = {
        item["component"]: item["canonical_sha256"]
        for item in qualification_component["dependencies"]
    }
    require(
        set(dependencies)
        == {gate["policy_component"], gate["prerequisite_component"]}
        and dependencies[gate["policy_component"]] == policy_digest,
        "upstream vcpkg component dependency closure differs",
    )
    prerequisite_identity = prerequisite_report.get("components", {}).get(
        "qualification"
    )
    require(
        prerequisite_report.get("status") == "passed"
        and prerequisite_report.get("release_sha256") == canonical_sha256(release)
        and prerequisite_identity
        == {
            "component": gate["prerequisite_component"],
            "canonical_sha256": dependencies[gate["prerequisite_component"]],
        },
        "upstream vcpkg prerequisite differs",
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
        previous = None
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")):
                require(previous is not None, "orphan vcpkg status continuation")
                fields[previous] += "\n" + line[1:]
                continue
            name, separator, value = line.partition(": ")
            require(separator and name not in fields, "invalid vcpkg status record")
            fields[name] = value
            previous = name
        if fields:
            records.append(fields)
    return records


def installed_port_versions(
    installed,
    triplet,
    expected_ports=EXPECTED_PORTS,
    expected_features=None,
):
    expected = {item["name"]: item for item in expected_ports}
    if expected_features is None:
        expected_features = {
            name: ("core",) for name in expected
        }
    observed = {}
    for record in parse_status(installed / "vcpkg/status"):
        name = record.get("Package")
        if name not in expected or record.get("Architecture") != triplet:
            continue
        require(
            record.get("Status") == "install ok installed",
            "%s installed status differs" % name,
        )
        version = record.get("Version")
        feature = record.get("Feature") or "core"
        if name not in observed:
            observed[name] = {"features": set(), "version": None}
        if version:
            require(
                observed[name]["version"] in (None, version),
                "%s feature versions differ" % name,
            )
            observed[name]["version"] = version
        require(
            feature not in observed[name]["features"],
            "%s installed feature is duplicated" % name,
        )
        observed[name]["features"].add(feature)
    require(set(observed) == set(expected), "%s port inventory differs" % triplet)
    versions = {}
    for name, record in observed.items():
        port = expected[name]
        require(record["version"], "%s installed version is missing" % name)
        accepted = {port["version"]}
        if port["port_version"]:
            accepted.add("%s#%d" % (port["version"], port["port_version"]))
        require(
            record["version"] in accepted,
            "%s installed version differs" % name,
        )
        require(
            record["features"] == set(expected_features[name]),
            "%s installed features differ" % name,
        )
        versions[name] = record["version"]
    return versions


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


def build_consumer(profile, gate, source, include, library, work, qemu, release):
    cc, cxx, readelf, sysroot = profile_tools(profile)
    executable = work / "consumer"
    compiler = cc if gate["consumer_language"] == "cc" else cxx
    command = [compiler]
    if sysroot is not None:
        command.append("--sysroot=" + str(sysroot))
    command.extend(gate["consumer_compile_flags"])
    command.extend(
        [source, "-I" + str(include), "-L" + str(library)]
    )
    command.extend(gate["consumer_link_flags"])
    command.extend(["-Wl,-z,relro,-z,now", "-o", executable])
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
        stdout == gate["consumer_stdout"],
        "upstream consumer output differs",
    )
    return {
        "sha256": sha256_file(executable),
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
    }


def qualify_host_protoc(installed):
    host_root = installed / HOST_TRIPLET
    path = host_root / "tools/protobuf/protoc"
    require(path.exists(), "host protoc is missing")
    resolved = path.resolve()
    try:
        resolved.relative_to((host_root / "tools/protobuf").resolve())
    except ValueError:
        raise QualificationError("host protoc symlink escapes its tool root")
    require(resolved.is_file(), "host protoc target is missing")
    if path.is_symlink():
        require(
            not Path(os.readlink(str(path))).is_absolute(),
            "host protoc uses an absolute symlink",
        )
    readelf = profile_tools(TRIPLETS[HOST_TRIPLET])[2]
    verify_machine(readelf, resolved, TRIPLETS[HOST_TRIPLET]["machine"])
    dynamic, _stderr = run([readelf, "-d", resolved])
    tags = re.findall(
        r"\((RPATH|RUNPATH)\).*Library (?:rpath|runpath): \[([^]]*)\]",
        dynamic,
    )
    require(
        "TEXTREL" not in dynamic
        and tags
        == [("RUNPATH", "$ORIGIN:$ORIGIN/../../lib:$ORIGIN/../lib")],
        "host protoc dynamic policy differs: %r" % tags,
    )
    stdout, _stderr = run([path, "--version"])
    require(stdout == "libprotoc 33.4\n", "host protoc version differs")
    return {
        "path": path.relative_to(host_root).as_posix(),
        "sha256": sha256_file(resolved),
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "runpath": tags[0][1],
    }


def build_generated_consumer(
    profile,
    gate,
    fixture_root,
    installed,
    triplet,
    work,
    qemu,
    release,
):
    source = work / "source"
    source.mkdir()
    for name in ("CMakeLists.txt", "consumer.cpp", "message.proto"):
        shutil.copy2(str(fixture_root / name), str(source / name))
    generated = source / "generated"
    generated.mkdir()
    host_root = installed / HOST_TRIPLET
    protoc = host_root / "tools/protobuf/protoc"
    run(
        [
            protoc,
            "--cpp_out=" + str(generated),
            "--proto_path=" + str(source),
            source / "message.proto",
        ]
    )
    generated_files = {
        name: sha256_file(generated / name)
        for name in ("message.pb.cc", "message.pb.h")
    }
    build = work / "build"
    cmake = Path("/opt/crossforge/host-tools/cmake/4.4.0/bin/cmake")
    ninja = Path("/opt/crossforge/host-tools/ninja/1.13.2/bin/ninja")
    if triplet == HOST_TRIPLET:
        chainload = Path("/opt/crossforge/cmake/host-gts15.cmake")
    else:
        chainload = Path("/opt/crossforge/cmake") / (profile["triple"] + ".cmake")
    run(
        [
            cmake,
            "-S",
            source,
            "-B",
            build,
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_SKIP_RPATH=ON",
            "-DCMAKE_MAKE_PROGRAM=" + str(ninja),
            "-DCMAKE_TOOLCHAIN_FILE=/opt/crossforge/vcpkg/root/scripts/buildsystems/vcpkg.cmake",
            "-DVCPKG_CHAINLOAD_TOOLCHAIN_FILE=" + str(chainload),
            "-DVCPKG_TARGET_TRIPLET=" + triplet,
            "-DVCPKG_HOST_TRIPLET=" + HOST_TRIPLET,
            "-DVCPKG_INSTALLED_DIR=" + str(installed),
            "-DVCPKG_MANIFEST_INSTALL=OFF",
        ]
    )
    run([cmake, "--build", build, "--parallel", "4"])
    executable = build / "crossforge-vcpkg-tier3"
    _cc, _cxx, readelf, sysroot = profile_tools(profile)
    verify_machine(readelf, executable, profile["machine"])
    headers, _stderr = run([readelf, "-l", executable])
    dynamic, _stderr = run([readelf, "-d", executable])
    require(
        "[Requesting program interpreter: %s]" % profile["interpreter"] in headers
        and all(tag not in dynamic for tag in ("RPATH", "RUNPATH", "TEXTREL")),
        "generated consumer ELF policy differs",
    )
    target_root = installed / triplet
    environment = dict(os.environ)
    if profile["linkage"] == "dynamic":
        environment["LD_LIBRARY_PATH"] = str(target_root / "lib")
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
            command.extend(
                ["-E", "LD_LIBRARY_PATH=" + str(target_root / "lib")]
            )
        command.append(executable)
        stdout, _stderr = run(command)
    else:
        stdout, _stderr = run([executable], env=environment)
    require(stdout == gate["consumer_stdout"], "generated consumer output differs")
    cache = (build / "CMakeCache.txt").read_text(encoding="utf-8")
    make_programs = re.findall(
        r"^CMAKE_MAKE_PROGRAM:[^=]+=(.+)$", cache, re.MULTILINE
    )
    require(
        make_programs == [str(ninja)],
        "generated consumer did not use locked Ninja",
    )
    return {
        "sha256": sha256_file(executable),
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "generated": generated_files,
    }


def qualify_triplet(
    vcpkg_root,
    fixture_root,
    gate,
    asset_paths,
    patchelf_archive,
    triplet,
    profile,
    qemu,
    release,
    work,
):
    seed_installed = None
    if gate["consumer_language"] == "cmake" and triplet != HOST_TRIPLET:
        seed_installed = Path(work) / HOST_TRIPLET / "installed"
    roots = isolated_install(
        vcpkg_root,
        fixture_root / "manifest",
        triplet,
        tuple(asset_paths) + (patchelf_archive,),
        work,
        seed_installed=seed_installed,
    )
    installed = roots["installed"]
    target_root = installed / triplet
    versions = installed_port_versions(
        installed,
        triplet,
        gate["ports"],
        gate["required_features"],
    )
    suffix = ".so" if profile["linkage"] == "dynamic" else ".a"
    libraries = {}
    _cc, _cxx, readelf, _sysroot = profile_tools(profile)
    for name, stem in gate["libraries"]:
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
    host_tool = None
    if gate["consumer_language"] == "cmake":
        host_tool = qualify_host_protoc(installed)
        consumer = build_generated_consumer(
            profile,
            gate,
            fixture_root,
            installed,
            triplet,
            consumer_work,
            qemu,
            release,
        )
    else:
        consumer = build_consumer(
            profile,
            gate,
            fixture_root / gate["consumer_source"],
            target_root / "include",
            target_root / "lib",
            consumer_work,
            qemu,
            release,
        )
    result = {
        "triplet": triplet,
        "cross_compiling": profile["cross"],
        "linkage": profile["linkage"],
        "ports": versions,
        "libraries": libraries,
        "consumer": consumer,
    }
    if host_tool is not None:
        result["host_tool"] = host_tool
    return result


def qualify(arguments):
    gate = TIER_PROFILES.get(arguments.tier)
    require(gate is not None, "unsupported upstream vcpkg tier")
    release = load_json(arguments.release)
    policy_component = load_component(
        arguments.policy_component,
        gate["policy_component"],
        arguments.policy_component_sha256,
    )
    qualification_component = load_component(
        arguments.qualification_component,
        gate["qualification_component"],
        arguments.qualification_component_sha256,
    )
    policy = policy_context(
        policy_component, arguments.fixture_root, arguments.tier
    )
    ASSETS["verify_asset_root"](arguments.asset_root, policy["assets"])
    prerequisite_report = load_json(gate["prerequisite_report"])
    contract_report = load_json(
        "/opt/crossforge/qualification/vcpkg/contract.json"
    )
    dependencies = validate_component_closure(
        release,
        qualification_component,
        arguments.policy_component_sha256,
        prerequisite_report,
        arguments.tier,
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
        prefix="crossforge-vcpkg-upstream-%s-" % arguments.tier
    ) as temporary:
        work = Path(temporary)
        results = [
            qualify_triplet(
                arguments.vcpkg_root,
                arguments.fixture_root,
                gate,
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
        "kind": "crossforge-vcpkg-upstream-%s-qualification" % arguments.tier,
        "status": "passed",
        "release_sha256": canonical_sha256(release),
        "components": {
            "policy": {
                "component": gate["policy_component"],
                "canonical_sha256": canonical_sha256(policy_component),
            },
            "qualification": {
                "component": gate["qualification_component"],
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
    parser.add_argument("--tier", choices=tuple(sorted(TIER_PROFILES)), required=True)
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
    print("qualified upstream vcpkg %s: %s" % (arguments.tier, arguments.output))
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
