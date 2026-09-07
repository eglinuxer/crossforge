#!/usr/bin/env python3
"""Qualify an installed Qt cross-target build without executing target code."""

import argparse
import hashlib
import json
import os
import re
import runpy
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
COMPONENT = runpy.run_path(str(Path(__file__).with_name("release_component.py")))
STRICT = runpy.run_path(str(Path(__file__).with_name("validate-release.py")))
SCHEMA_ID = "https://crossforge.dev/schemas/qt-target-build.schema.json"
QUALIFICATION_COMPONENT = "future/qt-qualification"
VERSION = "6.8.4"
TARGETS = {
    "x86_64": {
        "triple": "x86_64-unknown-linux-gnu",
        "machine": "Advanced Micro Devices X86-64",
        "manifest_duplicate_headers": [],
    },
    "aarch64": {
        "triple": "aarch64-unknown-linux-gnu",
        "machine": "AArch64",
        "manifest_duplicate_headers": [
            (
                "usr/include/QtGui/6.8.4/QtGui/private/"
                "qdrawhelper_neon_p.h"
            )
        ],
    },
}
COMMON_MANIFEST_DUPLICATE_HEADERS = [
    "usr/include/QtGui/6.8.4/QtGui/private/qstandarditemmodel_p.h",
    "usr/include/QtGui/qstandarditemmodel.h",
    "usr/include/QtQuick/qsgtexturematerial.h",
    "usr/include/QtUiPlugin/customwidget.h",
    "usr/include/QtUiPlugin/qdesignerexportwidget.h",
]
COMPONENTS = [
    "Core",
    "Gui",
    "Multimedia",
    "Qml",
    "Quick",
    "Quick3D",
    "ShaderTools",
    "WaylandClient",
    "WaylandCompositor",
    "WebEngineCore",
    "WebEngineQuick",
    "WebEngineWidgets",
    "Widgets",
]
LIBRARIES = sorted(
    [
        (name, "usr/lib/libQt6%s.so.%s" % (name, VERSION), "libQt6%s.so.6" % name)
        for name in COMPONENTS
    ]
)
PLUGINS = [
    ("offscreen", "usr/plugins/platforms/libqoffscreen.so"),
    ("xcb", "usr/plugins/platforms/libqxcb.so"),
]
EXECUTABLES = [("QtWebEngineProcess", "usr/libexec/QtWebEngineProcess")]
RESOURCES = [
    "usr/lib/cmake/Qt6/Qt6Config.cmake",
    "usr/resources/icudtl.dat",
    "usr/resources/qtwebengine_devtools_resources.pak",
    "usr/resources/qtwebengine_resources.pak",
]


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


def sha256_file(path):
    path = Path(path)
    information = path.lstat()
    require(
        stat.S_ISREG(information.st_mode) and not path.is_symlink(),
        "input is not a regular file: %s" % path,
    )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest(), information.st_size


def command(arguments, label, cwd=None, environment=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=str(cwd) if cwd is not None else None,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "%s failed: %s" % (label, (process.stdout + process.stderr).strip()),
    )
    return process.stdout.strip()


def load_schema(path, schema_name):
    document = STRICT["load_json"](path)
    schema = STRICT["load_json"](REPOSITORY / "config/schemas" / schema_name)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    return document


def parse_elf(header, dynamic, label, require_soname=True):
    machine = next(
        (
            line.split(":", 1)[1].strip()
            for line in header.splitlines()
            if "Machine:" in line
        ),
        None,
    )
    sonames = re.findall(r"\(SONAME\).*\[([^]]+)\]", dynamic)
    needed = sorted(re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic))
    runpath = sorted(
        entry
        for value in re.findall(r"\((?:RPATH|RUNPATH)\).*\[([^]]*)\]", dynamic)
        for entry in value.split(":")
        if entry
    )
    require(machine is not None, "%s ELF machine is missing" % label)
    require(
        len(sonames) == 1 if require_soname else len(sonames) <= 1,
        "%s ELF SONAME identity differs" % label,
    )
    require(
        all(
            entry == "/usr/lib"
            or entry.startswith("/usr/lib/")
            or entry == "$ORIGIN"
            or entry.startswith("$ORIGIN/")
            or entry == "${ORIGIN}"
            or entry.startswith("${ORIGIN}/")
            for entry in runpath
        ),
        "%s contains an unsafe RPATH: %r" % (label, runpath),
    )
    require("/work/" not in dynamic, "%s leaks the build root" % label)
    require("/opt/crossforge/qualification" not in dynamic, "%s leaks the staging root" % label)
    require("(TEXTREL)" not in dynamic, "%s contains TEXTREL" % label)
    return machine, sonames[0] if sonames else None, needed, runpath


def provider_roots(arguments):
    return [
        arguments.install_root / "usr/lib",
        arguments.sysroot / "usr/lib64",
        arguments.sysroot / "lib64",
        arguments.toolchain_root / "lib/gcc" / arguments.triple / "15",
        arguments.toolchain_root / arguments.triple / "lib64",
        arguments.toolchain_root / "lib64",
    ]


def resolve_needed(arguments, needed, label):
    roots = provider_roots(arguments)
    resolved = []
    for soname in needed:
        providers = [root / soname for root in roots if (root / soname).exists()]
        require(providers, "%s dependency is unresolved: %s" % (label, soname))
        resolved.append({"soname": soname, "provider": str(providers[0])})
    return resolved


def audit_elf(arguments, path, label, expected_soname=None, evidence_path=None):
    header = command([arguments.readelf, "-h", path], "%s ELF header" % label)
    dynamic = command(
        [arguments.readelf, "--wide", "-d", path],
        "%s dynamic section" % label,
    )
    machine, soname, needed, runpath = parse_elf(
        header, dynamic, label, require_soname=expected_soname is not None
    )
    require(machine == TARGETS[arguments.arch]["machine"], "%s ELF machine differs" % label)
    if expected_soname is not None:
        require(soname == expected_soname, "%s SONAME differs" % label)
    if evidence_path is None:
        evidence_path = path.relative_to(arguments.install_root)
    evidence_path = Path(evidence_path)
    require(
        not evidence_path.is_absolute() and ".." not in evidence_path.parts,
        "%s evidence path escapes its logical root" % label,
    )
    digest, size = sha256_file(path)
    record = {
        "name": label,
        "path": str(evidence_path),
        "sha256": digest,
        "size": size,
        "machine": machine,
        "needed": needed,
        "needed_providers": resolve_needed(arguments, needed, label),
        "runpath": runpath,
    }
    if soname is not None:
        record["soname"] = soname
    return record


def qualification_binding(arguments):
    try:
        qualification = COMPONENT["load_component"](
            arguments.qualification_component,
            QUALIFICATION_COMPONENT,
            "future",
            arguments.qualification_component_sha256,
        )
    except COMPONENT["ComponentError"] as error:
        raise QualificationError(str(error)) from error
    plan = load_schema(arguments.plan, "qt-qualification-plan.schema.json")
    plan_sha256 = canonical_sha256(plan)
    materials = {
        item["path"]: item["value"] for item in qualification["materials"]
    }
    require(
        materials
        == {
            "/qt/qualification/plan/canonical_sha256": plan_sha256,
            "/qt/qualification/plan/file": "config/qt-qualification.json",
            "/qt/qualification/status": "locked",
        },
        "Qt qualification component material set differs",
    )
    configure = load_schema(
        arguments.configure_evidence, "qt-target-configure.schema.json"
    )
    require(
        configure["identity"]
        == {"arch": arguments.arch, "triple": arguments.triple},
        "Qt target configure evidence identity differs",
    )
    require(configure["install_root"] == str(arguments.install_root), "Qt target configure prefix differs")
    require(
        configure["qualification_component"]
        == {
            "component": QUALIFICATION_COMPONENT,
            "canonical_sha256": arguments.qualification_component_sha256,
        },
        "Qt target configure qualification binding differs",
    )
    require(configure["inputs"]["plan_sha256"] == plan_sha256, "Qt target configure plan binding differs")
    require(
        configure["inputs"]["patch_sha256"] == sha256_file(arguments.patch)[0],
        "Qt target configure patch binding differs",
    )
    return plan_sha256, configure


def manifest_evidence(arguments):
    manifest = arguments.build_root / "install_manifest.txt"
    lines = manifest.read_text(encoding="utf-8").splitlines()
    prefix = str(arguments.install_root) + "/"
    require(lines and all(line.startswith(prefix) for line in lines), "Qt install manifest escapes the target prefix")
    relative = [line[len(prefix) :] for line in lines]
    counts = Counter(relative)
    duplicates = {name: count for name, count in counts.items() if count > 1}
    prl_duplicates = {
        name: count for name, count in duplicates.items() if name.endswith(".prl")
    }
    require(
        len(prl_duplicates) == 125
        and set(prl_duplicates.values()) == {15},
        "Qt install manifest PRL duplicate contract differs",
    )
    header_duplicates = {
        name: count for name, count in duplicates.items() if not name.endswith(".prl")
    }
    expected_headers = sorted(
        COMMON_MANIFEST_DUPLICATE_HEADERS
        + TARGETS[arguments.arch]["manifest_duplicate_headers"]
    )
    require(
        sorted(header_duplicates) == expected_headers
        and set(header_duplicates.values()) == {2},
        "Qt install manifest header duplicate contract differs",
    )
    return {
        "entries": len(relative),
        "unique_entries": len(set(relative)),
        "reviewed_duplicates": {
            "prl_files": len(prl_duplicates),
            "prl_occurrences": 15,
            "headers": expected_headers,
            "header_occurrences": 2,
        },
        "sha256": sha256_file(manifest)[0],
    }


def consumer_environment(arguments):
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": "%s:%s:%s:%s:%s"
            % (
                arguments.cmake.parent,
                arguments.ninja.parent,
                arguments.qt_host / "bin",
                arguments.cxx.parent,
                environment.get("PATH", ""),
            ),
            "PKG_CONFIG_SYSROOT_DIR": str(arguments.sysroot),
            "PKG_CONFIG_LIBDIR": "%s:%s"
            % (
                arguments.sysroot / "usr/lib64/pkgconfig",
                arguments.sysroot / "usr/share/pkgconfig",
            ),
        }
    )
    return environment


def consumer_probe(arguments):
    root = arguments.build_root / "target-consumer-qualification"
    require(not root.exists() and not root.is_symlink(), "Qt target consumer root already exists")
    source = root / "source"
    output = root / "build"
    source.mkdir(parents=True)
    consumer_toolchain = root / "consumer-toolchain.cmake"
    consumer_toolchain.write_text(
        'include("%s")\n' % arguments.toolchain_file
        + 'set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE BOTH CACHE STRING "" FORCE)\n',
        encoding="utf-8",
    )
    source.joinpath("CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(crossforge_qt_target_consumer LANGUAGES CXX)\n"
        "find_package(Qt6 6.8.4 EXACT REQUIRED COMPONENTS %s)\n"
        "add_executable(qt-target-consumer main.cpp)\n"
        "target_link_libraries(qt-target-consumer PRIVATE %s)\n"
        'target_link_options(qt-target-consumer PRIVATE "LINKER:-rpath,/usr/lib")\n'
        "set_property(TARGET qt-target-consumer PROPERTY CXX_STANDARD 20)\n"
        % (
            " ".join(COMPONENTS),
            " ".join("Qt6::" + name for name in COMPONENTS),
        ),
        encoding="utf-8",
    )
    source.joinpath("main.cpp").write_text(
        "#include <QtCore/qglobal.h>\n"
        "#include <QtWidgets/QApplication>\n"
        "#include <QtWidgets/QWidget>\n"
        "int main(int argc, char **argv) {\n"
        "  QApplication application(argc, argv);\n"
        "  QWidget widget;\n"
        "  return qVersion()[0] == 0 || QT_VERSION != QT_VERSION_CHECK(6, 8, 4);\n"
        "}\n",
        encoding="utf-8",
    )
    command(
        [
            arguments.cmake,
            "-S",
            source,
            "-B",
            output,
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_TOOLCHAIN_FILE=" + str(consumer_toolchain),
            "-DCMAKE_MAKE_PROGRAM=" + str(arguments.ninja),
            "-DCMAKE_PREFIX_PATH=" + str(arguments.install_root / "usr"),
            "-DQt6_DIR=" + str(arguments.install_root / "usr/lib/cmake/Qt6"),
            "-DQT_HOST_PATH=" + str(arguments.qt_host),
            "-DCMAKE_SKIP_BUILD_RPATH=TRUE",
            "-DQT_DEBUG_FIND_PACKAGE=ON",
        ],
        "Qt target downstream CMake configure",
        environment=consumer_environment(arguments),
    )
    command(
        [arguments.cmake, "--build", output],
        "Qt target downstream CMake build",
        environment=consumer_environment(arguments),
    )
    executable = output / "qt-target-consumer"
    require(executable.is_file(), "Qt target downstream executable is missing")
    record = audit_elf(
        arguments,
        executable,
        "target-consumer",
        evidence_path="consumer/qt-target-consumer",
    )
    require("libQt6Core.so.6" in record["needed"], "Qt target consumer did not retain QtCore")
    return record


def qualify(arguments):
    target = TARGETS.get(arguments.arch)
    require(target is not None and target["triple"] == arguments.triple, "Qt target identity differs")
    expected_root = Path(
        "/opt/crossforge/qualification/qt/6.8.4/targets/%s" % arguments.triple
    )
    require(arguments.install_root == expected_root, "Qt target install root differs")
    require(arguments.install_root.is_dir(), "Qt target install root is missing")
    require(arguments.build_root.is_dir(), "Qt target build root is missing")
    require(
        not os.environ.get("HOSTRUNNER")
        and not os.environ.get("CMAKE_CROSSCOMPILING_EMULATOR"),
        "Qt target build qualification forbids execution adapters",
    )
    plan_sha256, configure = qualification_binding(arguments)
    logs = {
        "webengine": arguments.build_root / "webengine-build.log",
        "build": arguments.build_root / "build.log",
        "install": arguments.build_root / "install.log",
    }
    for name, path in logs.items():
        require(path.is_file() and not path.is_symlink(), "Qt target %s log is missing" % name)
    for name in ("webengine", "build"):
        content = logs[name].read_text(encoding="utf-8", errors="replace")
        require("FAILED:" not in content and "ninja: build stopped" not in content, "Qt target %s log reports a failure" % name)
    require("-- Installing:" in logs["install"].read_text(encoding="utf-8", errors="replace"), "Qt target install log lacks installed files")
    manifest = manifest_evidence(arguments)

    libraries = []
    for name, relative, soname in LIBRARIES:
        path = arguments.install_root / relative
        require(path.is_file() and not path.is_symlink(), "Qt target library is missing: %s" % relative)
        libraries.append(audit_elf(arguments, path, name, soname))
    plugins = []
    for name, relative in PLUGINS:
        path = arguments.install_root / relative
        require(path.is_file() and not path.is_symlink(), "Qt target plugin is missing: %s" % relative)
        plugins.append(audit_elf(arguments, path, name))
    executables = []
    for name, relative in EXECUTABLES:
        path = arguments.install_root / relative
        require(path.is_file() and not path.is_symlink(), "Qt target executable is missing: %s" % relative)
        executables.append(audit_elf(arguments, path, name))
    for relative in RESOURCES:
        require((arguments.install_root / relative).is_file(), "Qt target resource is missing: %s" % relative)
    for component in COMPONENTS:
        config = arguments.install_root / "usr/lib/cmake" / ("Qt6" + component) / ("Qt6%sConfig.cmake" % component)
        require(config.is_file(), "Qt target CMake package is missing: %s" % component)
    consumer = consumer_probe(arguments)

    builders = []
    for relative, path in (
        ("scripts/configure-qt-target.sh", arguments.configure_builder),
        ("scripts/qualify-qt-target-configure.py", arguments.configure_qualifier),
        ("scripts/build-qt-target.sh", arguments.builder),
        ("scripts/check-qt-target-install.sh", arguments.install_checker),
        ("scripts/print-build-log-diagnostics.py", arguments.diagnostics),
        ("scripts/run-with-heartbeat.py", arguments.heartbeat),
        ("scripts/qualify-qt-target-build.py", Path(__file__)),
    ):
        builders.append({"file": relative, "sha256": sha256_file(path)[0]})
    document = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-qt-target-build",
        "qt_version": VERSION,
        "identity": {"arch": arguments.arch, "triple": arguments.triple},
        "install_root": str(arguments.install_root),
        "qualification_component": {
            "component": QUALIFICATION_COMPONENT,
            "canonical_sha256": arguments.qualification_component_sha256,
        },
        "build_environment": {
            "image": "host-qt-build-locked",
            "target": arguments.triple,
            "tier": "cross-compile",
        },
        "inputs": {
            "plan_sha256": plan_sha256,
            "configure_evidence_sha256": canonical_sha256(configure),
            "toolchain_file_sha256": sha256_file(arguments.toolchain_file)[0],
            "patch_sha256": sha256_file(arguments.patch)[0],
            "webengine_log_sha256": sha256_file(logs["webengine"])[0],
            "build_log_sha256": sha256_file(logs["build"])[0],
            "install_log_sha256": sha256_file(logs["install"])[0],
        },
        "builders": builders,
        "components": COMPONENTS,
        "libraries": libraries,
        "plugins": plugins,
        "executables": executables,
        "resources": RESOURCES,
        "install_manifest": manifest,
        "consumer": consumer,
        "checks": {
            "webengine_build_completed": True,
            "build_completed": True,
            "install_completed": True,
            "required_cmake_packages": True,
            "required_resources": True,
            "elf_machine": True,
            "sonames": True,
            "dependency_closure": True,
            "safe_runpath": True,
            "no_textrel": True,
            "cmake_consumer_cross_compile": True,
            "no_target_execution": True,
        },
    }
    schema = STRICT["load_json"](arguments.schema)
    require(schema.get("$id") == SCHEMA_ID, "Qt target build schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    require(not arguments.output.exists() and not arguments.output.is_symlink(), "Qt target build evidence already exists")
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("qualified Qt target build: %s" % arguments.output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--triple", required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path, required=True)
    parser.add_argument("--toolchain-root", type=Path, required=True)
    parser.add_argument("--qt-host", type=Path, required=True)
    parser.add_argument("--toolchain-file", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--configure-evidence", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--qualification-component", type=Path, required=True)
    parser.add_argument("--qualification-component-sha256", required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--cmake", type=Path, required=True)
    parser.add_argument("--ninja", type=Path, required=True)
    parser.add_argument("--cxx", type=Path, required=True)
    parser.add_argument("--readelf", type=Path, required=True)
    parser.add_argument("--configure-builder", type=Path, required=True)
    parser.add_argument("--configure-qualifier", type=Path, required=True)
    parser.add_argument("--builder", type=Path, required=True)
    parser.add_argument("--install-checker", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--heartbeat", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/qt-target-build.schema.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        require(re.match(r"^[0-9a-f]{64}$", arguments.qualification_component_sha256), "invalid Qt qualification component digest")
        qualify(arguments)
        return 0
    except (
        QualificationError,
        COMPONENT["ComponentError"],
        STRICT["ValidationError"],
        OSError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
