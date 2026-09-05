#!/usr/bin/env python3
"""Qualify one locked Qt cross-target configure result."""

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
SCHEMA_ID = "https://crossforge.dev/schemas/qt-target-configure.schema.json"
QUALIFICATION_COMPONENT = "future/qt-qualification"
QT_HOST = "/opt/crossforge/qualification/qt/6.8.4/host"
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
TARGETS = {
    "x86_64": {
        "triple": "x86_64-unknown-linux-gnu",
        "machine": "Advanced Micro Devices X86-64",
    },
    "aarch64": {
        "triple": "aarch64-unknown-linux-gnu",
        "machine": "AArch64",
    },
}
ENABLED_FEATURES = sorted(
    [
        "cross_compile",
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
        "cxx20",
        "debug",
        "developer_build",
        "gstreamer",
        "qdoc",
        "static",
        "webengine_build_gn",
        "webengine_build_ninja",
        "webengine_proprietary_codecs",
        "webengine_system_ffmpeg",
    ]
)
COMMON_WARNINGS = [
    "documentation-qdoc-disabled-without-clang",
    "clang-lupdate-parser-disabled",
]
FORBIDDEN_LOG = (
    "No media backend found",
    "System GBM is disabled",
    "CMake Error",
    "ERROR:",
)
PATCHED_PATHS = [
    (
        "qtwebengine/src/3rdparty/chromium/third_party/xnnpack/"
        "src/src/amalgam/gen/neonfp16arith.c"
    ),
    (
        "qtwebengine/src/3rdparty/chromium/third_party/xnnpack/"
        "src/src/qs8-f16-vcvt/neon.c.in"
    ),
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
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_schema(path, schema_name):
    document = STRICT["load_json"](path)
    schema = STRICT["load_json"](REPOSITORY / "config/schemas" / schema_name)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    return document


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


def reviewed_warnings(arch):
    result = list(COMMON_WARNINGS)
    if arch == "aarch64":
        result.append("qtwebengine-thumb-check-matches-arm64")
    return sorted(result)


def qualify(arguments):
    target = TARGETS.get(arguments.arch)
    require(target is not None, "Qt target architecture differs")
    require(target["triple"] == arguments.triple, "Qt target triple differs")
    expected_prefix = (
        "/opt/crossforge/qualification/qt/6.8.4/targets/%s" % arguments.triple
    )
    require(str(arguments.install_root) == expected_prefix, "Qt target prefix differs")
    require(
        str(arguments.sysroot) == "/opt/crossforge/sysroots/el8/%s" % arguments.arch,
        "Qt target sysroot differs",
    )
    require(str(arguments.qt_host) == QT_HOST, "Qt target host path differs")
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
        record["path"]: record["value"] for record in qualification["materials"]
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
    target_rows = [
        record
        for record in plan["targets"]
        if record["arch"] == arguments.arch
        and record["triple"] == arguments.triple
    ]
    require(len(target_rows) == 1, "Qt target plan row differs")
    require(plan["modules"] == MODULES, "Qt target module order differs")
    require(plan["build"]["target_execution"] == "forbidden", "Qt target execution policy differs")
    require(len(plan["patches"]) == 1, "Qt target patch plan differs")
    patch_record = plan["patches"][0]
    require(
        patch_record["scope"] == "target-builds"
        and sha256_file(arguments.patch) == patch_record["sha256"],
        "Qt target patch identity differs",
    )

    dependencies = component_dependencies(qualification)
    qt_source = load_schema(arguments.qt_source_manifest, "qt-source-manifest.schema.json")
    xcb_build = load_schema(arguments.xcb_build_manifest, "xcb-util-cursor-build.schema.json")
    ffmpeg_build = load_schema(arguments.ffmpeg_build_manifest, "ffmpeg-build.schema.json")
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
        require(build["identity"] == arguments.triple, "%s target row differs" % name)
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
        require(cache.get("QT_FEATURE_" + feature) == "ON", "required Qt target feature is disabled: %s" % feature)
    for feature in DISABLED_REVIEWED:
        require(cache.get("QT_FEATURE_" + feature) == "OFF", "reviewed disabled Qt target feature changed: %s" % feature)
    require(cache.get("CMAKE_BUILD_TYPE") == "Release", "Qt target build type differs")
    require(cache.get("CMAKE_INSTALL_PREFIX") == "/usr", "Qt target install prefix differs")
    require(cache.get("CMAKE_STAGING_PREFIX") == expected_prefix + "/usr", "Qt target staging prefix differs")
    require(cache.get("QT_HOST_PATH") == QT_HOST, "Qt host cache path differs")
    require(cache.get("CMAKE_TOOLCHAIN_FILE") == str(arguments.toolchain_file), "Qt target toolchain file differs")
    require(cache.get("FFMPEG_DIR") == str(arguments.ffmpeg_prefix), "Qt target FFmpeg directory differs")
    require(cache.get("PKG_CONFIG_HOST_EXECUTABLE") == "/usr/bin/pkg-config", "Qt host pkg-config escaped to the target sysroot")
    require(not cache.get("CMAKE_CROSSCOMPILING_EMULATOR"), "Qt target configure gained an execution adapter")

    summary = arguments.summary.read_text(encoding="utf-8")
    log = arguments.log.read_text(encoding="utf-8")
    patch_log = arguments.patch_log.read_text(encoding="utf-8")
    require("Qt is now configured for building" in log and "FFmpeg ............................... yes" in summary, "Qt target configure completion differs")
    for message in FORBIDDEN_LOG:
        require(message not in log, "Qt target configure contains a forbidden diagnostic: %s" % message)
    warnings = reviewed_warnings(arguments.arch)
    require(log.count("WARNING:") == len(warnings), "Qt target configure warning count changed")
    require("QDoc will not be compiled" in log and "The Clang-based lupdate parser will not be available" in log, "Qt target reviewed warning set differs")
    thumb_warning = "Thumb instruction set is required to build ffmpeg for QtWebEngine."
    require((thumb_warning in log) == (arguments.arch == "aarch64"), "Qt target Thumb warning scope differs")
    require("qemu" not in log.lower(), "Qt target configure attempted target execution")
    require(
        patch_log.splitlines() == ["patching file " + path for path in PATCHED_PATHS],
        "Qt target patch application log differs",
    )

    cmake_version = command([arguments.cmake, "--version"], "CMake version").splitlines()[0]
    ninja_version = command([arguments.ninja, "--version"], "Ninja version")
    cxx_version = command([arguments.cxx, "-dumpfullversion"], "C++ compiler version")
    cxx_target = command([arguments.cxx, "-dumpmachine"], "C++ compiler target")
    require(cmake_version == "cmake version 4.4.0", "CMake version differs")
    require(ninja_version == "1.13.2", "Ninja version differs")
    require(cxx_version == "15.2.1" and cxx_target == arguments.triple, "target C++ compiler identity differs")
    with tempfile.TemporaryDirectory(prefix="crossforge-qt-target-cxx20-") as temporary:
        output = Path(temporary) / "probe.o"
        process = subprocess.run(
            [str(arguments.cxx), "-std=gnu++20", "-x", "c++", "-c", "-o", str(output), "-"],
            input="#include <concepts>\nstatic_assert(std::integral<int>);\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        require(process.returncode == 0 and output.is_file(), "target GNU C++20 compile probe failed")

    document = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-qt-target-configure",
        "qt_version": "6.8.4",
        "identity": {"arch": arguments.arch, "triple": arguments.triple},
        "install_root": expected_prefix,
        "qualification_component": {
            "component": QUALIFICATION_COMPONENT,
            "canonical_sha256": arguments.qualification_component_sha256,
        },
        "inputs": {
            "qt_source_component": qt_source["source_component"],
            "xcb_util_cursor_build_sha256": canonical_sha256(xcb_build),
            "ffmpeg_build_sha256": canonical_sha256(ffmpeg_build),
            "plan_sha256": plan_sha256,
            "patch_sha256": patch_record["sha256"],
            "patch_log_sha256": sha256_file(arguments.patch_log),
        },
        "configuration": {
            "build_type": "Release",
            "shared": True,
            "examples": False,
            "tests": False,
            "documentation": False,
            "modules": MODULES,
        },
        "tools": {
            "cmake": "4.4.0",
            "ninja": "1.13.2",
            "cxx": "15.2.1",
            "cxx_target": arguments.triple,
            "host_pkg_config": "/usr/bin/pkg-config",
        },
        "features": {"enabled": ENABLED_FEATURES, "disabled_reviewed": DISABLED_REVIEWED},
        "reviewed_warnings": warnings,
        "checks": {
            "configure_completed": True,
            "critical_warnings_absent": True,
            "target_cxx20_compile": True,
            "required_modules_enabled": True,
            "required_features_enabled": True,
            "host_tools_native": True,
            "no_target_execution": True,
            "patch_applied": True,
        },
    }
    schema = STRICT["load_json"](arguments.schema)
    require(schema.get("$id") == SCHEMA_ID, "Qt target configure schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    require(not arguments.output.exists() and not arguments.output.is_symlink(), "Qt target configure evidence already exists")
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("qualified Qt target configure: %s" % arguments.output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--triple", required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path, required=True)
    parser.add_argument("--qt-host", type=Path, required=True)
    parser.add_argument("--toolchain-file", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--patch-log", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
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
        default=REPOSITORY / "config/schemas/qt-target-configure.schema.json",
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
