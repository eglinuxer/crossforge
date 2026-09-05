#!/usr/bin/env python3
"""Qualify one locked host or cross FFmpeg build without target execution."""

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
SCHEMA_ID = "https://crossforge.dev/schemas/ffmpeg-build.schema.json"
VERSION = "7.1.1"
SOURCE_COMPONENT = "sources/ffmpeg"
QUALIFICATION_COMPONENT = "future/qt-qualification"
ARCHIVE_NAME = "ffmpeg-7.1.1.tar.xz"
LICENSE_EXPRESSION = (
    "LGPL-2.1-or-later AND BSD-3-Clause AND BSD-2-Clause AND "
    "BSD-Source-Code AND ISC AND MIT AND MPL-2.0"
)
IDENTITIES = {
    "host": {
        "role": "host-qt-build",
        "arch": "x86_64",
        "triple": None,
        "machine": "Advanced Micro Devices X86-64",
        "lock_id": "host-qt-build",
        "image": "host-qt-build-locked",
        "tier": "host-direct",
    },
    "x86_64-unknown-linux-gnu": {
        "role": "qt-target",
        "arch": "x86_64",
        "triple": "x86_64-unknown-linux-gnu",
        "machine": "Advanced Micro Devices X86-64",
        "lock_id": "qt-target-x86_64",
        "image": "qt-target-x86_64-locked",
        "tier": "cross-no-exec",
    },
    "aarch64-unknown-linux-gnu": {
        "role": "qt-target",
        "arch": "aarch64",
        "triple": "aarch64-unknown-linux-gnu",
        "machine": "AArch64",
        "lock_id": "qt-target-aarch64",
        "image": "qt-target-aarch64-locked",
        "tier": "cross-no-exec",
    },
}
LIBRARIES = (
    ("avcodec", "libavcodec.so.61.19.101", "libavcodec.so.61"),
    ("avformat", "libavformat.so.61.7.100", "libavformat.so.61"),
    ("avutil", "libavutil.so.59.39.100", "libavutil.so.59"),
    ("swresample", "libswresample.so.5.3.100", "libswresample.so.5"),
    ("swscale", "libswscale.so.8.3.100", "libswscale.so.8"),
)
CONFIGURATION = {
    "shared": True,
    "static": False,
    "pic": True,
    "network": True,
    "pthreads": True,
    "openssl": True,
    "zlib": True,
    "gpl": False,
    "version3": False,
    "nonfree": False,
    "programs": False,
    "avdevice": False,
    "avfilter": False,
    "autodetect": False,
}
CONFIG_MACROS = {
    "shared": "CONFIG_SHARED",
    "static": "CONFIG_STATIC",
    "pic": "CONFIG_PIC",
    "network": "CONFIG_NETWORK",
    "pthreads": "HAVE_PTHREADS",
    "openssl": "CONFIG_OPENSSL",
    "zlib": "CONFIG_ZLIB",
    "gpl": "CONFIG_GPL",
    "version3": "CONFIG_VERSION3",
    "nonfree": "CONFIG_NONFREE",
    "avdevice": "CONFIG_AVDEVICE",
    "avfilter": "CONFIG_AVFILTER",
    "autodetect": "CONFIG_AUTODETECT",
}
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
    if process.returncode != 0:
        detail = (process.stdout + process.stderr).strip()
        raise QualificationError("%s failed: %s" % (label, detail))
    return process.stdout


def load_schema(path, schema_path):
    document = STRICT["load_json"](path)
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    return document


def component_materials(document):
    result = {}
    for record in document["materials"]:
        require(record["path"] not in result, "component repeats a material path")
        result[record["path"]] = record["value"]
    return result


def load_inputs(arguments, identity):
    try:
        source_component = COMPONENT["load_component"](
            arguments.source_component,
            SOURCE_COMPONENT,
            "build",
            arguments.source_component_sha256,
        )
        qualification_component = COMPONENT["load_component"](
            arguments.qualification_component,
            QUALIFICATION_COMPONENT,
            "future",
            arguments.qualification_component_sha256,
        )
    except COMPONENT["ComponentError"] as error:
        raise QualificationError(str(error)) from error
    dependencies = {
        record["component"]: record["canonical_sha256"]
        for record in qualification_component["dependencies"]
    }
    require(
        dependencies.get(SOURCE_COMPONENT) == arguments.source_component_sha256,
        "Qt qualification does not bind the FFmpeg source",
    )
    qualification_materials = component_materials(qualification_component)
    plan = load_schema(
        arguments.plan,
        REPOSITORY / "config/schemas/qt-qualification-plan.schema.json",
    )
    require(
        canonical_sha256(plan)
        == qualification_materials["/qt/qualification/plan/canonical_sha256"],
        "Qt plan digest differs from the qualification component",
    )
    dependency = next(
        (record for record in plan["source_dependencies"] if record["name"] == "ffmpeg"),
        None,
    )
    require(
        dependency
        == {
            "name": "ffmpeg",
            "component": SOURCE_COMPONENT,
            "canonical_sha256": arguments.source_component_sha256,
            "usage": "host-and-target-qt-multimedia-backend-build",
        },
        "Qt plan FFmpeg dependency differs",
    )
    lock_record = next(
        (record for record in plan["locks"] if record["id"] == identity["lock_id"]),
        None,
    )
    require(lock_record is not None and lock_record["status"] == "locked", "RPM lock is not bound")
    lock = load_schema(
        arguments.rpm_lock, REPOSITORY / "config/schemas/rpm-lock.schema.json"
    )
    transaction = load_schema(
        arguments.rpm_transaction,
        REPOSITORY / "config/schemas/rpm-transaction.schema.json",
    )
    require(
        canonical_sha256(lock) == lock_record["canonical_sha256"],
        "RPM lock digest differs from the Qt plan",
    )
    require(
        canonical_sha256(transaction) == lock["transaction"]["canonical_sha256"],
        "RPM transaction digest differs from its lock",
    )
    transaction_identity = transaction["identity"]
    require(
        transaction_identity["role"] == identity["role"]
        and transaction_identity["arch"] == identity["arch"]
        and transaction_identity["target_triple"] == identity["triple"],
        "RPM transaction identity differs from the FFmpeg build identity",
    )
    source_manifest = load_schema(
        arguments.source_manifest,
        REPOSITORY / "config/schemas/ffmpeg-source-manifest.schema.json",
    )
    require(
        source_manifest["source_component"]
        == {
            "component": SOURCE_COMPONENT,
            "canonical_sha256": arguments.source_component_sha256,
        },
        "FFmpeg source manifest component differs",
    )
    archive_sha256, archive_size = sha256_file(arguments.source_archive)
    require(
        source_manifest["archive"]
        == {
            "file": ARCHIVE_NAME,
            "sha256": archive_sha256,
            "size": archive_size,
        },
        "FFmpeg archive differs from its authenticated manifest",
    )
    return {
        "source_component": source_component,
        "qualification_component": qualification_component,
        "plan": plan,
        "lock": lock,
        "transaction": transaction,
        "source_manifest": source_manifest,
    }


def parse_configuration(config_header):
    values = {}
    for line in Path(config_header).read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#define ([A-Z0-9_]+) ([01])$", line)
        if match:
            values[match.group(1)] = match.group(2) == "1"
    configuration = {}
    for name, macro in CONFIG_MACROS.items():
        require(macro in values, "FFmpeg configuration lacks %s" % macro)
        configuration[name] = values[macro]
    configuration["programs"] = any(
        values.get(name, False) for name in ("CONFIG_FFMPEG", "CONFIG_FFPLAY", "CONFIG_FFPROBE")
    )
    require(configuration == CONFIGURATION, "FFmpeg build configuration differs")
    return configuration


def dynamic_identity(readelf, library):
    header = command([readelf, "-h", library], "FFmpeg ELF header")
    dynamic = command([readelf, "--wide", "-d", library], "FFmpeg dynamic section")
    machine = next(
        (line.split(":", 1)[1].strip() for line in header.splitlines() if "Machine:" in line),
        None,
    )
    sonames = re.findall(r"\(SONAME\).*\[([^]]+)\]", dynamic)
    needed = sorted(re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic))
    require(machine is not None and len(sonames) == 1, "FFmpeg ELF identity is incomplete")
    require("(RPATH)" not in dynamic and "(RUNPATH)" not in dynamic, "FFmpeg contains an RPATH")
    require("(TEXTREL)" not in dynamic, "FFmpeg contains TEXTREL")
    return machine, sonames[0], needed


def write_manifest(path, document):
    schema = STRICT["load_json"](REPOSITORY / "config/schemas/ffmpeg-build.schema.json")
    require(schema.get("$id") == SCHEMA_ID, "FFmpeg build manifest schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.exists() and not path.is_symlink(), "FFmpeg build manifest already exists")
    path.write_text(payload, encoding="utf-8")


def qualify(arguments):
    identity = IDENTITIES.get(arguments.identity)
    require(identity is not None, "unsupported FFmpeg build identity")
    require(arguments.install_root.is_dir(), "FFmpeg install root is missing")
    require(arguments.build_root.is_dir(), "FFmpeg build root is missing")
    if arguments.identity == "host":
        require(
            str(arguments.prefix)
            == "/opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg",
            "FFmpeg host prefix differs",
        )
        compiler = arguments.toolchain / "gcc"
        readelf = arguments.toolchain / "readelf"
        target_execution = "host-version-probe"
    else:
        require(str(arguments.prefix) == "/usr", "FFmpeg target prefix differs")
        compiler = arguments.toolchain / (arguments.identity + "-gcc")
        readelf = arguments.toolchain / (arguments.identity + "-readelf")
        target_execution = "forbidden"
    for tool in (compiler, readelf):
        require(tool.is_file() and os.access(str(tool), os.X_OK), "missing qualification tool: %s" % tool)
    inputs = load_inputs(arguments, identity)
    configuration = parse_configuration(arguments.build_root / "source/config.h")
    compiler_dumpmachine = command([compiler, "-dumpmachine"], "FFmpeg compiler identity").strip()
    if arguments.identity != "host":
        require(compiler_dumpmachine == arguments.identity, "FFmpeg cross compiler triple differs")
    library_records = []
    for name, filename, expected_soname in LIBRARIES:
        library = arguments.install_root / "lib64" / filename
        require(library.is_file() and not library.is_symlink(), "FFmpeg library is missing: %s" % filename)
        machine, soname, needed = dynamic_identity(readelf, library)
        require(machine == identity["machine"], "FFmpeg ELF machine differs: %s" % name)
        require(soname == expected_soname, "FFmpeg SONAME differs: %s" % name)
        digest, size = sha256_file(library)
        library_records.append(
            {
                "name": name,
                "path": "lib64/" + filename,
                "sha256": digest,
                "size": size,
                "machine": machine,
                "soname": soname,
                "needed": needed,
            }
        )
        require(
            (arguments.install_root / "include" / ("lib%s" % name)).is_dir(),
            "FFmpeg headers are missing: %s" % name,
        )
        require(
            (arguments.install_root / "lib64/pkgconfig" / ("lib%s.pc" % name)).is_file(),
            "FFmpeg pkg-config file is missing: %s" % name,
        )
    license_path = arguments.install_root / "share/licenses/ffmpeg/COPYING.LGPLv2.1"
    license_sha256, _license_size = sha256_file(license_path)
    require(
        license_sha256 == inputs["source_manifest"]["license"]["sha256"],
        "installed FFmpeg license differs",
    )
    host_probe = False
    if arguments.identity == "host":
        probe = arguments.build_root / "version-probe.c"
        probe.write_text(
            "#include <libavutil/avutil.h>\n"
            "#include <string.h>\n"
            "int main(void) { return strcmp(av_version_info(), \"7.1.1\"); }\n",
            encoding="utf-8",
        )
        executable = arguments.build_root / "version-probe"
        library_directory = arguments.install_root / "lib64"
        command(
            [
                compiler,
                "-I" + str(arguments.install_root / "include"),
                probe,
                "-L" + str(library_directory),
                "-Wl,-rpath-link," + str(library_directory),
                "-lavutil",
                "-o",
                executable,
            ],
            "FFmpeg host version probe compile",
        )
        environment = os.environ.copy()
        environment["LD_LIBRARY_PATH"] = str(library_directory)
        command([executable], "FFmpeg host version probe", environment=environment)
        host_probe = True
    lock_record = next(
        record for record in inputs["plan"]["locks"] if record["id"] == identity["lock_id"]
    )
    document = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-ffmpeg-build",
        "version": VERSION,
        "identity": arguments.identity,
        "prefix": str(arguments.prefix),
        "compiler_dumpmachine": compiler_dumpmachine,
        "build_environment": {
            "image": identity["image"],
            "target": arguments.identity,
            "tier": identity["tier"],
        },
        "builders": [
            {
                "file": "scripts/build-ffmpeg.sh",
                "sha256": sha256_file(arguments.builder)[0],
            },
            {
                "file": "scripts/qualify-ffmpeg-build.py",
                "sha256": sha256_file(Path(__file__))[0],
            },
        ],
        "source_component": {
            "component": SOURCE_COMPONENT,
            "canonical_sha256": arguments.source_component_sha256,
        },
        "qualification_component": {
            "component": QUALIFICATION_COMPONENT,
            "canonical_sha256": arguments.qualification_component_sha256,
        },
        "rpm_input": {
            "lock_file": lock_record["lock_file"],
            "lock_sha256": canonical_sha256(inputs["lock"]),
            "transaction_sha256": canonical_sha256(inputs["transaction"]),
            "parent_lock_sha256": inputs["transaction"]["base"]["parent_sha256"],
        },
        "configuration": configuration,
        "libraries": library_records,
        "license": {
            "expression": LICENSE_EXPRESSION,
            "path": "share/licenses/ffmpeg/COPYING.LGPLv2.1",
            "sha256": license_sha256,
        },
        "checks": {
            "no_rpath": True,
            "no_textrel": True,
            "required_headers": True,
            "required_pkgconfig": True,
            "target_execution": target_execution,
            "host_probe_passed": host_probe,
        },
    }
    write_manifest(arguments.output, document)
    print("qualified FFmpeg %s: %s" % (arguments.identity, arguments.output))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--source-component", type=Path, required=True)
    parser.add_argument("--source-component-sha256", required=True)
    parser.add_argument("--qualification-component", type=Path, required=True)
    parser.add_argument("--qualification-component-sha256", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--rpm-lock", type=Path, required=True)
    parser.add_argument("--rpm-transaction", type=Path, required=True)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--builder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        require(SHA256_RE.match(arguments.source_component_sha256), "invalid FFmpeg component digest")
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
