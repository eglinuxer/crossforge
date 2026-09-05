#!/usr/bin/env python3
"""Qualify the installed Qt host toolchain as a downstream CMake consumer."""

import argparse
import hashlib
import json
import os
import re
import runpy
import stat
import subprocess
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
COMPONENT = runpy.run_path(str(Path(__file__).with_name("release_component.py")))
STRICT = runpy.run_path(str(Path(__file__).with_name("validate-release.py")))
SCHEMA_ID = "https://crossforge.dev/schemas/qt-host-build.schema.json"
QUALIFICATION_COMPONENT = "future/qt-qualification"
VERSION = "6.8.4"
PREFIX = "/opt/crossforge/qualification/qt/6.8.4/host"
MACHINE = "Advanced Micro Devices X86-64"
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
LIBRARIES = [
    (name, "lib/libQt6%s.so.%s" % (name, VERSION), "libQt6%s.so.6" % name)
    for name in COMPONENTS
    if name not in ("Gui", "Widgets")
] + [
    ("Gui", "lib/libQt6Gui.so.%s" % VERSION, "libQt6Gui.so.6"),
    ("Widgets", "lib/libQt6Widgets.so.%s" % VERSION, "libQt6Widgets.so.6"),
]
LIBRARIES = sorted(LIBRARIES)
PLUGINS = [
    ("offscreen", "plugins/platforms/libqoffscreen.so"),
    ("xcb", "plugins/platforms/libqxcb.so"),
]
EXECUTABLES = [("QtWebEngineProcess", "libexec/QtWebEngineProcess")]
HOST_TOOLS = [
    "bin/assistant",
    "bin/designer",
    "bin/lrelease",
    "bin/lupdate",
    "bin/qmake6",
    "bin/qt-cmake",
    "bin/qt-configure-module",
    "bin/qtpaths6",
    "libexec/QtWebEngineProcess",
]
RESOURCES = [
    "lib/cmake/Qt6/Qt6Config.cmake",
    "resources/icudtl.dat",
    "resources/qtwebengine_devtools_resources.pak",
    "resources/qtwebengine_resources.pak",
]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
        (line.split(":", 1)[1].strip() for line in header.splitlines() if "Machine:" in line),
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
            (entry == "$ORIGIN" or entry.startswith("$ORIGIN/"))
            or (entry == "${ORIGIN}" or entry.startswith("${ORIGIN}/"))
            or entry.startswith("/opt/crossforge/qualification/qt/6.8.4/")
            for entry in runpath
        ),
        "%s contains an unsafe RPATH" % label,
    )
    require("/work/" not in dynamic, "%s leaks the build root" % label)
    require("(TEXTREL)" not in dynamic, "%s contains TEXTREL" % label)
    return machine, sonames[0] if sonames else None, needed, runpath


def audit_elf(readelf, path, label, expected_soname=None):
    header = command([readelf, "-h", path], "%s ELF header" % label)
    dynamic = command([readelf, "--wide", "-d", path], "%s dynamic section" % label)
    machine, soname, needed, runpath = parse_elf(
        header, dynamic, label, require_soname=expected_soname is not None
    )
    require(machine == MACHINE, "%s ELF machine differs" % label)
    if expected_soname is not None:
        require(soname == expected_soname, "%s SONAME differs" % label)
    digest, size = sha256_file(path)
    record = {
        "name": label,
        "path": str(path),
        "sha256": digest,
        "size": size,
        "machine": machine,
        "needed": needed,
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
    materials = {item["path"]: item["value"] for item in qualification["materials"]}
    plan_sha256 = canonical_sha256(plan)
    require(
        materials
        == {
            "/qt/qualification/plan/canonical_sha256": plan_sha256,
            "/qt/qualification/plan/file": "config/qt-qualification.json",
            "/qt/qualification/status": "locked",
        },
        "Qt qualification component material set differs",
    )
    configure = load_schema(arguments.configure_evidence, "qt-host-configure.schema.json")
    require(configure["identity"] == "host", "Qt configure evidence is not the host row")
    require(configure["qt_version"] == VERSION, "Qt configure version differs")
    require(
        configure["qualification_component"]
        == {
            "component": QUALIFICATION_COMPONENT,
            "canonical_sha256": arguments.qualification_component_sha256,
        },
        "Qt configure qualification binding differs",
    )
    require(configure["inputs"]["plan_sha256"] == plan_sha256, "Qt configure plan binding differs")
    return plan_sha256, configure


def runtime_environment(arguments):
    environment = os.environ.copy()
    environment.update(
        {
            "LD_LIBRARY_PATH": "%s/lib:%s/lib64:%s/lib64"
            % (arguments.prefix, arguments.ffmpeg_prefix, arguments.xcb_prefix),
            "QT_PLUGIN_PATH": str(arguments.prefix / "plugins"),
            "QT_QPA_PLATFORM": "offscreen",
            "PATH": "%s:%s:%s:%s"
            % (
                arguments.cmake.parent,
                arguments.ninja.parent,
                arguments.cxx.parent,
                environment.get("PATH", ""),
            ),
        }
    )
    return environment


def consumer_probe(arguments):
    root = arguments.build_root / "host-consumer-qualification"
    require(not root.exists() and not root.is_symlink(), "Qt consumer probe root already exists")
    source = root / "source"
    output = root / "build"
    source.mkdir(parents=True)
    source.joinpath("CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(crossforge_qt_host_consumer LANGUAGES CXX)\n"
        "find_package(Qt6 6.8.4 EXACT REQUIRED COMPONENTS %s)\n"
        "add_executable(qt-host-consumer main.cpp)\n"
        "target_link_libraries(qt-host-consumer PRIVATE %s)\n"
        "set_property(TARGET qt-host-consumer PROPERTY CXX_STANDARD 20)\n"
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
        "#include <cstring>\n"
        "int main(int argc, char **argv) {\n"
        "  QApplication application(argc, argv);\n"
        "  QWidget widget;\n"
        "  return std::strcmp(qVersion(), \"6.8.4\");\n"
        "}\n",
        encoding="utf-8",
    )
    command(
        [
            arguments.prefix / "bin/qt-cmake",
            "-S",
            source,
            "-B",
            output,
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_CXX_COMPILER=" + str(arguments.cxx),
            "-DCMAKE_MAKE_PROGRAM=" + str(arguments.ninja),
        ],
        "Qt downstream CMake configure",
        environment=runtime_environment(arguments),
    )
    command([arguments.cmake, "--build", output], "Qt downstream CMake build")
    executable = output / "qt-host-consumer"
    command(
        [executable],
        "Qt downstream offscreen probe",
        environment=runtime_environment(arguments),
    )


def qualify(arguments):
    require(str(arguments.prefix) == PREFIX, "Qt host prefix differs")
    require(arguments.prefix.is_dir(), "Qt host prefix is missing")
    require(arguments.build_root.is_dir(), "Qt host build root is missing")
    plan_sha256, configure = qualification_binding(arguments)
    build_log = arguments.build_root / "build.log"
    install_log = arguments.build_root / "install.log"
    install_manifest = arguments.build_root / "install_manifest.txt"
    for path in (build_log, install_log, install_manifest):
        require(path.is_file() and not path.is_symlink(), "Qt build evidence is missing: %s" % path)
    build_text = build_log.read_text(encoding="utf-8", errors="replace")
    install_text = install_log.read_text(encoding="utf-8", errors="replace")
    require("FAILED:" not in build_text and "ninja: build stopped" not in build_text, "Qt build log reports a failure")
    require("-- Installing:" in install_text, "Qt install log lacks installed files")
    readelf = arguments.toolchain / "readelf"
    require(readelf.is_file() and os.access(str(readelf), os.X_OK), "Qt readelf is missing")
    ldd = Path("/usr/bin/ldd")
    require(ldd.is_file() and os.access(str(ldd), os.X_OK), "Qt ldd is missing")
    runtime = runtime_environment(arguments)
    libraries = []
    for name, relative, soname in LIBRARIES:
        path = arguments.prefix / relative
        require(path.is_file() and not path.is_symlink(), "Qt library is missing: %s" % relative)
        record = audit_elf(readelf, path, name, soname)
        require(
            "not found" not in command([ldd, path], "%s dependency closure" % name, environment=runtime),
            "Qt library dependency is unresolved: %s" % relative,
        )
        record["path"] = relative
        libraries.append(record)
    plugins = []
    for name, relative in PLUGINS:
        path = arguments.prefix / relative
        require(path.is_file() and not path.is_symlink(), "Qt plugin is missing: %s" % relative)
        record = audit_elf(readelf, path, name)
        require(
            "not found" not in command([ldd, path], "%s dependency closure" % name, environment=runtime),
            "Qt plugin dependency is unresolved: %s" % relative,
        )
        record["path"] = relative
        plugins.append(record)
    executables = []
    for name, relative in EXECUTABLES:
        path = arguments.prefix / relative
        record = audit_elf(readelf, path, name)
        require(
            "not found" not in command([ldd, path], "%s dependency closure" % name, environment=runtime),
            "Qt executable dependency is unresolved: %s" % relative,
        )
        record["path"] = relative
        executables.append(record)
    for relative in RESOURCES:
        require((arguments.prefix / relative).is_file(), "Qt resource is missing: %s" % relative)
    for relative in HOST_TOOLS:
        path = arguments.prefix / relative
        require(path.is_file() and os.access(str(path), os.X_OK), "Qt host tool is missing: %s" % relative)
    for component in COMPONENTS:
        require(
            (arguments.prefix / "lib/cmake" / ("Qt6" + component) / ("Qt6%sConfig.cmake" % component)).is_file(),
            "Qt CMake package is missing: %s" % component,
        )
    qmake_version = command(
        [arguments.prefix / "bin/qmake6", "-query", "QT_VERSION"],
        "qmake version",
        environment=runtime_environment(arguments),
    )
    qtpaths_version = command(
        [arguments.prefix / "bin/qtpaths6", "--qt-version"],
        "qtpaths version",
        environment=runtime_environment(arguments),
    )
    require(qmake_version == VERSION and qtpaths_version == VERSION, "Qt host tool version differs")
    consumer_probe(arguments)
    builders = []
    for relative, path in (
        ("scripts/build-qt-host.sh", arguments.builder),
        ("scripts/check-qt-host-install.sh", arguments.install_checker),
        ("scripts/print-build-log-diagnostics.py", arguments.diagnostics),
        ("scripts/qualify-qt-host-build.py", Path(__file__)),
    ):
        builders.append({"file": relative, "sha256": sha256_file(path)[0]})
    document = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-qt-host-build",
        "qt_version": VERSION,
        "identity": "host",
        "prefix": PREFIX,
        "qualification_component": {
            "component": QUALIFICATION_COMPONENT,
            "canonical_sha256": arguments.qualification_component_sha256,
        },
        "build_environment": {
            "image": "host-qt-build-locked",
            "target": "host",
            "tier": "host-direct",
        },
        "inputs": {
            "plan_sha256": plan_sha256,
            "configure_evidence_sha256": canonical_sha256(configure),
            "build_log_sha256": sha256_file(build_log)[0],
            "install_log_sha256": sha256_file(install_log)[0],
            "install_manifest_sha256": sha256_file(install_manifest)[0],
        },
        "builders": builders,
        "components": COMPONENTS,
        "libraries": libraries,
        "plugins": plugins,
        "executables": executables,
        "host_tools": HOST_TOOLS,
        "resources": RESOURCES,
        "tools": {"qmake": qmake_version, "qtpaths": qtpaths_version},
        "checks": {
            "build_completed": True,
            "install_completed": True,
            "required_cmake_packages": True,
            "required_resources": True,
            "elf_machine": True,
            "sonames": True,
            "dependency_closure": True,
            "safe_runpath": True,
            "no_textrel": True,
            "cmake_consumer_compile": True,
            "offscreen_runtime": True,
        },
    }
    schema = STRICT["load_json"](arguments.schema)
    require(schema.get("$id") == SCHEMA_ID, "Qt host build schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    require(not arguments.output.exists() and not arguments.output.is_symlink(), "Qt host build evidence already exists")
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("qualified Qt host build: %s" % arguments.output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--configure-evidence", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--qualification-component", type=Path, required=True)
    parser.add_argument("--qualification-component-sha256", required=True)
    parser.add_argument("--ffmpeg-prefix", type=Path, required=True)
    parser.add_argument("--xcb-prefix", type=Path, required=True)
    parser.add_argument("--cmake", type=Path, required=True)
    parser.add_argument("--ninja", type=Path, required=True)
    parser.add_argument("--cxx", type=Path, required=True)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--builder", type=Path, required=True)
    parser.add_argument("--install-checker", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/qt-host-build.schema.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        require(
            SHA256_RE.match(arguments.qualification_component_sha256),
            "invalid Qt qualification component digest",
        )
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
