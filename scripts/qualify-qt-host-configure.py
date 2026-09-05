#!/usr/bin/env python3
"""Qualify the locked Qt host configure result and reviewed warnings."""

import argparse
import hashlib
import json
import re
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
COMPONENT = runpy.run_path(str(Path(__file__).with_name("release_component.py")))
STRICT = runpy.run_path(str(Path(__file__).with_name("validate-release.py")))
SCHEMA_ID = "https://crossforge.dev/schemas/qt-host-configure.schema.json"
QUALIFICATION_COMPONENT = "future/qt-qualification"
MODULES = [
    "qtbase",
    "qtshadertools",
    "qtdeclarative",
    "qttools",
    "qtwayland",
    "qtmultimedia",
    "qtquick3d",
    "qtwebengine",
]
ENABLED_FEATURES = sorted(
    [
        "dbus",
        "dbus_linked",
        "egl",
        "ffmpeg",
        "fontconfig",
        "freetype",
        "gbm",
        "gui",
        "libinput",
        "libudev",
        "network",
        "opengl",
        "opengl_desktop",
        "openssl",
        "openssl_linked",
        "opensslv11",
        "pkg_config",
        "pulseaudio",
        "qtwebengine_build",
        "qtwebengine_core_build",
        "qtwebengine_quick_build",
        "qtwebengine_widgets_build",
        "shared",
        "thread",
        "wayland",
        "webengine_build_gn",
        "webengine_ozone_x11",
        "widgets",
        "xcb",
        "xcb_xlib",
        "xkbcommon",
        "xkbcommon_x11",
        "xml",
    ]
)
DISABLED_REVIEWED = sorted(
    [
        "alsa",
        "clangcpp",
        "cross_compile",
        "cxx20",
        "debug",
        "developer_build",
        "gstreamer",
        "qdoc",
        "static",
        "webengine_build_ninja",
        "webengine_proprietary_codecs",
        "webengine_system_ffmpeg",
    ]
)
REVIEWED_WARNINGS = sorted(
    [
        "documentation-qdoc-disabled-without-clang",
        "clang-lupdate-parser-disabled",
    ]
)
FORBIDDEN_LOG = (
    "No media backend found",
    "System GBM is disabled",
    "CMake Error",
    "ERROR:",
)


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


def load_schema(path, schema_path):
    document = STRICT["load_json"](path)
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    return document


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_cache(path):
    result = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "//")) or "=" not in line:
            continue
        key_type, value = line.split("=", 1)
        if ":" not in key_type:
            continue
        key, _value_type = key_type.rsplit(":", 1)
        require(key not in result, "Qt CMake cache repeats %s" % key)
        result[key] = value
    return result


def command(arguments, label):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "%s failed: %s" % (label, (process.stdout + process.stderr).strip()),
    )
    return process.stdout.strip()


def component_dependencies(document):
    return {
        record["component"]: record["canonical_sha256"]
        for record in document["dependencies"]
    }


def qualify(arguments):
    try:
        qualification = COMPONENT["load_component"](
            arguments.qualification_component,
            QUALIFICATION_COMPONENT,
            "future",
            arguments.qualification_component_sha256,
        )
    except COMPONENT["ComponentError"] as error:
        raise QualificationError(str(error)) from error
    plan = load_schema(
        arguments.plan,
        REPOSITORY / "config/schemas/qt-qualification-plan.schema.json",
    )
    materials = {record["path"]: record["value"] for record in qualification["materials"]}
    plan_sha256 = canonical_sha256(plan)
    require(
        materials == {
            "/qt/qualification/plan/canonical_sha256": plan_sha256,
            "/qt/qualification/plan/file": "config/qt-qualification.json",
            "/qt/qualification/status": "locked",
        },
        "Qt qualification component material set differs",
    )
    require(plan["modules"] == MODULES, "Qt module order differs")
    require(
        plan["build"]
        == {
            "configuration": "release",
            "shared": True,
            "developer_build": False,
            "examples": False,
            "tests": False,
            "documentation": False,
            "commercial": False,
            "confirm_license": True,
            "target_execution": "forbidden",
            "output": "qualification-only",
        },
        "Qt build policy differs",
    )
    dependencies = component_dependencies(qualification)
    qt_source = load_schema(
        arguments.qt_source_manifest,
        REPOSITORY / "config/schemas/qt-source-manifest.schema.json",
    )
    xcb_build = load_schema(
        arguments.xcb_build_manifest,
        REPOSITORY / "config/schemas/xcb-util-cursor-build.schema.json",
    )
    ffmpeg_build = load_schema(
        arguments.ffmpeg_build_manifest,
        REPOSITORY / "config/schemas/ffmpeg-build.schema.json",
    )
    require(
        qt_source["source_component"]
        == {
            "component": "sources/qt",
            "canonical_sha256": dependencies["sources/qt"],
        },
        "Qt source manifest differs from the qualification component",
    )
    for name, build, component in (
        ("xcb-util-cursor", xcb_build, "sources/xcb-util-cursor"),
        ("FFmpeg", ffmpeg_build, "sources/ffmpeg"),
    ):
        require(build["identity"] == "host", "%s build is not the host row" % name)
        require(
            build["source_component"]
            == {"component": component, "canonical_sha256": dependencies[component]},
            "%s source binding differs" % name,
        )
        require(
            build["qualification_component"]
            == {
                "component": QUALIFICATION_COMPONENT,
                "canonical_sha256": arguments.qualification_component_sha256,
            },
            "%s qualification binding differs" % name,
        )
    cache = parse_cache(arguments.cache)
    for module in MODULES:
        require(cache.get("BUILD_" + module) == "ON", "Qt module is disabled: %s" % module)
    for feature in ENABLED_FEATURES:
        require(
            cache.get("QT_FEATURE_" + feature) == "ON",
            "required Qt host feature is disabled: %s" % feature,
        )
    for feature in DISABLED_REVIEWED:
        require(
            cache.get("QT_FEATURE_" + feature) == "OFF",
            "reviewed disabled Qt host feature changed: %s" % feature,
        )
    require(cache.get("CMAKE_BUILD_TYPE") == "Release", "Qt build type differs")
    require(cache.get("FFMPEG_DIR") == str(arguments.ffmpeg_prefix), "Qt FFmpeg directory differs")
    summary = Path(arguments.summary).read_text(encoding="utf-8")
    log = Path(arguments.log).read_text(encoding="utf-8")
    require(
        "Qt is now configured for building" in log
        and "FFmpeg ............................... yes" in summary,
        "Qt configure completion or FFmpeg summary is missing",
    )
    for message in FORBIDDEN_LOG:
        require(message not in log, "Qt configure contains a forbidden diagnostic: %s" % message)
    require(log.count("WARNING:") == 2, "Qt configure warning count changed")
    require(
        "QDoc will not be compiled" in log
        and "The Clang-based lupdate parser will not be available" in log,
        "Qt reviewed warning set differs",
    )
    cmake_version = command([arguments.cmake, "--version"], "CMake version").splitlines()[0]
    ninja_version = command([arguments.ninja, "--version"], "Ninja version")
    cxx_version = command([arguments.cxx, "-dumpfullversion"], "C++ compiler version")
    require(cmake_version == "cmake version 4.4.0", "CMake version differs")
    require(ninja_version == "1.13.2", "Ninja version differs")
    require(cxx_version == "15.2.1", "C++ compiler version differs")
    with tempfile.TemporaryDirectory(prefix="crossforge-qt-cxx20-") as temporary:
        output = Path(temporary) / "probe.o"
        process = subprocess.run(
            [str(arguments.cxx), "-std=gnu++20", "-x", "c++", "-c", "-o", str(output), "-"],
            input="#include <concepts>\nstatic_assert(std::integral<int>);\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        require(process.returncode == 0 and output.is_file(), "GNU C++20 compile probe failed")
    document = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-qt-host-configure",
        "qt_version": "6.8.4",
        "identity": "host",
        "qualification_component": {
            "component": QUALIFICATION_COMPONENT,
            "canonical_sha256": arguments.qualification_component_sha256,
        },
        "inputs": {
            "qt_source_component": qt_source["source_component"],
            "xcb_util_cursor_build_sha256": canonical_sha256(xcb_build),
            "ffmpeg_build_sha256": canonical_sha256(ffmpeg_build),
            "plan_sha256": plan_sha256,
        },
        "configuration": {
            "build_type": "Release",
            "shared": True,
            "developer_build": False,
            "examples": False,
            "tests": False,
            "documentation": False,
            "modules": MODULES,
        },
        "tools": {"cmake": "4.4.0", "ninja": "1.13.2", "cxx": "15.2.1"},
        "features": {
            "enabled": ENABLED_FEATURES,
            "disabled_reviewed": DISABLED_REVIEWED,
        },
        "reviewed_warnings": REVIEWED_WARNINGS,
        "checks": {
            "configure_completed": True,
            "critical_warnings_absent": True,
            "cxx20_compile": True,
            "required_modules_enabled": True,
            "required_features_enabled": True,
        },
    }
    schema = STRICT["load_json"](arguments.schema)
    require(schema.get("$id") == SCHEMA_ID, "Qt host configure schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    require(not arguments.output.exists() and not arguments.output.is_symlink(), "Qt configure evidence already exists")
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(payload, encoding="utf-8")
    print("qualified Qt host configure: %s" % arguments.output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--qualification-component", type=Path, required=True)
    parser.add_argument("--qualification-component-sha256", required=True)
    parser.add_argument("--qt-source-manifest", type=Path, required=True)
    parser.add_argument("--xcb-build-manifest", type=Path, required=True)
    parser.add_argument("--ffmpeg-build-manifest", type=Path, required=True)
    parser.add_argument("--ffmpeg-prefix", type=Path, required=True)
    parser.add_argument("--cmake", type=Path, required=True)
    parser.add_argument("--ninja", type=Path, required=True)
    parser.add_argument("--cxx", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/qt-host-configure.schema.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        require(
            re.match(r"^[0-9a-f]{64}$", arguments.qualification_component_sha256),
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
