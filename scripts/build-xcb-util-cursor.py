#!/usr/bin/env python3
"""Build and qualify xcb-util-cursor for the Qt host or one target."""

import argparse
import hashlib
import json
import os
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
COMPONENT = runpy.run_path(str(Path(__file__).with_name("release_component.py")))
STRICT = runpy.run_path(str(Path(__file__).with_name("validate-release.py")))
SCHEMA_ID = "https://crossforge.dev/schemas/xcb-util-cursor-build.schema.json"
VERSION = "0.1.6"
SOURCE_COMPONENT = "sources/xcb-util-cursor"
QUALIFICATION_COMPONENT = "future/qt-qualification"
TOP_DIRECTORY = "xcb-util-cursor-0.1.6"
ARCHIVE_NAME = TOP_DIRECTORY + ".tar.xz"
IDENTITIES = {
    "host": {
        "role": "host-qt-build",
        "arch": "x86_64",
        "triple": None,
        "machine": "Advanced Micro Devices X86-64",
        "lock_id": "host-qt-build",
    },
    "x86_64-unknown-linux-gnu": {
        "role": "qt-target",
        "arch": "x86_64",
        "triple": "x86_64-unknown-linux-gnu",
        "machine": "Advanced Micro Devices X86-64",
        "lock_id": "qt-target-x86_64",
    },
    "aarch64-unknown-linux-gnu": {
        "role": "qt-target",
        "arch": "aarch64",
        "triple": "aarch64-unknown-linux-gnu",
        "machine": "AArch64",
        "lock_id": "qt-target-aarch64",
    },
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BuildError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise BuildError(message)


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
        raise BuildError("%s failed: %s" % (label, detail))
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
        raise BuildError(str(error)) from error
    dependencies = {
        record["component"]: record["canonical_sha256"]
        for record in qualification_component["dependencies"]
    }
    require(
        dependencies.get(SOURCE_COMPONENT) == arguments.source_component_sha256,
        "Qt qualification does not bind the xcb-util-cursor source",
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
        "RPM transaction identity differs from the build identity",
    )
    source_manifest = load_schema(
        arguments.source_manifest,
        REPOSITORY
        / "config/schemas/xcb-util-cursor-source-manifest.schema.json",
    )
    require(
        source_manifest["source_component"]
        == {
            "component": SOURCE_COMPONENT,
            "canonical_sha256": arguments.source_component_sha256,
        },
        "source manifest component differs",
    )
    archive_sha256, archive_size = sha256_file(arguments.source_archive)
    require(
        source_manifest["archive"]
        == {
            "file": ARCHIVE_NAME,
            "sha256": archive_sha256,
            "size": archive_size,
        },
        "source archive differs from its authenticated manifest",
    )
    return {
        "source_component": source_component,
        "qualification_component": qualification_component,
        "plan": plan,
        "lock": lock,
        "transaction": transaction,
        "source_manifest": source_manifest,
        "archive_sha256": archive_sha256,
    }


def extract_source(archive_path, destination, expected_count):
    require(not destination.exists() and not destination.is_symlink(), "source directory already exists")
    temporary = Path(
        tempfile.mkdtemp(prefix=".%s." % destination.name, dir=str(destination.parent))
    )
    count = 0
    try:
        with tarfile.open(str(archive_path), "r:xz") as archive:
            for member in archive:
                count += 1
                pure = PurePosixPath(member.name)
                require(
                    not pure.is_absolute()
                    and pure.parts
                    and pure.parts[0] == TOP_DIRECTORY
                    and all(part not in ("", ".", "..") for part in pure.parts),
                    "source archive contains an unsafe path",
                )
                require(member.isfile() or member.isdir(), "source archive contains a special entry")
                relative = PurePosixPath(*pure.parts[1:])
                output = temporary.joinpath(*relative.parts)
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                require(stream is not None, "cannot read source archive member")
                with stream, output.open("xb") as target:
                    shutil.copyfileobj(stream, target, 1024 * 1024)
                os.chmod(str(output), 0o755 if member.mode & 0o111 else 0o644)
        require(count == expected_count, "source archive member count differs")
        for root, directories, filenames in os.walk(str(temporary)):
            for name in directories + filenames:
                os.utime(str(Path(root) / name), (0, 0), follow_symlinks=False)
        os.utime(str(temporary), (0, 0), follow_symlinks=False)
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(str(temporary), ignore_errors=True)
        raise


def dynamic_identity(readelf, library):
    header = command([readelf, "-h", library], "ELF header")
    dynamic = command([readelf, "--wide", "-d", library], "ELF dynamic section")
    machine = next(
        (line.split(":", 1)[1].strip() for line in header.splitlines() if "Machine:" in line),
        None,
    )
    sonames = re.findall(r"\(SONAME\).*\[([^]]+)\]", dynamic)
    needed = sorted(re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic))
    require(machine is not None and len(sonames) == 1, "ELF identity is incomplete")
    require("(RPATH)" not in dynamic and "(RUNPATH)" not in dynamic, "xcb-util-cursor contains an RPATH")
    require("(TEXTREL)" not in dynamic, "xcb-util-cursor contains TEXTREL")
    return machine, sonames[0], needed


def write_manifest(path, document):
    schema = STRICT["load_json"](
        REPOSITORY / "config/schemas/xcb-util-cursor-build.schema.json"
    )
    require(schema.get("$id") == SCHEMA_ID, "build manifest schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.exists() and not path.is_symlink(), "build manifest already exists")
    path.write_text(payload, encoding="utf-8")


def build(arguments):
    identity = IDENTITIES.get(arguments.identity)
    require(identity is not None, "unsupported xcb-util-cursor build identity")
    require(1 <= arguments.jobs <= 64, "jobs must be between 1 and 64")
    require(not arguments.build_root.exists(), "build root already exists")
    if arguments.identity == "host":
        require(arguments.sysroot is None, "host build must not use a sysroot")
        prefix = arguments.prefix
        install_root = prefix
        logical_prefix = str(prefix)
        compiler = arguments.toolchain / "gcc"
        readelf = arguments.toolchain / "readelf"
        configure_prefix = str(prefix)
        configure_libdir = str(prefix / "lib64")
        target_execution = "host-dlopen-probe"
        image = "host-qt-build-locked"
        tier = "host-direct"
    else:
        require(arguments.sysroot is not None, "target build requires a sysroot")
        prefix = arguments.prefix
        require(str(prefix) == "/usr", "target xcb-util-cursor prefix must be /usr")
        install_root = arguments.sysroot / "usr"
        logical_prefix = "/usr"
        compiler = arguments.toolchain / (arguments.identity + "-gcc")
        readelf = arguments.toolchain / (arguments.identity + "-readelf")
        configure_prefix = "/usr"
        configure_libdir = "/usr/lib64"
        target_execution = "forbidden"
        image = "qt-target-%s-locked" % identity["arch"]
        tier = "cross-no-exec"
    required_tools = [compiler, readelf]
    if arguments.identity != "host":
        required_tools.extend(
            [
                arguments.toolchain / (arguments.identity + "-ar"),
                arguments.toolchain / (arguments.identity + "-ranlib"),
            ]
        )
    for tool in required_tools:
        require(tool.is_file() and os.access(str(tool), os.X_OK), "missing build tool: %s" % tool)
    inputs = load_inputs(arguments, identity)
    arguments.build_root.mkdir(parents=True)
    source = arguments.build_root / "source"
    build_directory = arguments.build_root / "build"
    extract_source(
        arguments.source_archive,
        source,
        inputs["source_manifest"]["member_count"],
    )
    build_directory.mkdir()
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    environment["SOURCE_DATE_EPOCH"] = "0"
    environment["CFLAGS"] = (
        "-O2 -g0 -fPIC -ffile-prefix-map=%s=/usr/src/debug/xcb-util-cursor"
        % arguments.build_root
    )
    if arguments.identity == "host":
        environment["CC"] = str(compiler)
    else:
        environment["PATH"] = "%s:%s" % (
            arguments.toolchain,
            environment.get("PATH", ""),
        )
        environment["CC"] = "%s --sysroot=%s" % (compiler, arguments.sysroot)
        environment["PKG_CONFIG_SYSROOT_DIR"] = str(arguments.sysroot)
        environment["PKG_CONFIG_LIBDIR"] = ":".join(
            [
                str(arguments.sysroot / "usr/lib64/pkgconfig"),
                str(arguments.sysroot / "usr/share/pkgconfig"),
            ]
        )
    configure = [
        source / "configure",
        "--prefix=" + configure_prefix,
        "--libdir=" + configure_libdir,
        "--disable-static",
        "--enable-shared",
    ]
    if arguments.identity != "host":
        configure.append("--host=" + arguments.identity)
    command(configure, "xcb-util-cursor configure", build_directory, environment)
    command(["make", "-j%d" % arguments.jobs], "xcb-util-cursor build", build_directory, environment)
    install_command = ["make", "install"]
    if arguments.identity != "host":
        install_command.append("DESTDIR=" + str(arguments.sysroot))
    command(install_command, "xcb-util-cursor install", build_directory, environment)
    license_path = install_root / "share/licenses/xcb-util-cursor/COPYING"
    license_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(source / "COPYING"), str(license_path))
    library = install_root / "lib64/libxcb-cursor.so.0.0.0"
    pkgconfig = install_root / "lib64/pkgconfig/xcb-cursor.pc"
    header = install_root / "include/xcb/xcb_cursor.h"
    require(library.is_file() and pkgconfig.is_file() and header.is_file(), "installed xcb-util-cursor files are incomplete")
    machine, soname, needed = dynamic_identity(readelf, library)
    require(machine == identity["machine"], "xcb-util-cursor ELF machine differs")
    require(soname == "libxcb-cursor.so.0", "xcb-util-cursor SONAME differs")
    require(
        {"libc.so.6", "libxcb.so.1", "libxcb-image.so.0", "libxcb-render-util.so.0"}.issubset(set(needed)),
        "xcb-util-cursor dependency set is incomplete",
    )
    compiler_dumpmachine = command([compiler, "-dumpmachine"], "compiler identity").strip()
    if arguments.identity != "host":
        require(compiler_dumpmachine == arguments.identity, "cross compiler triple differs")
    host_probe = False
    if arguments.identity == "host":
        probe = arguments.build_root / "dlopen-probe.c"
        probe.write_text(
            "#include <dlfcn.h>\nint main(void) { void *h = dlopen(\"libxcb-cursor.so.0\", RTLD_NOW); return h ? 0 : 1; }\n",
            encoding="utf-8",
        )
        executable = arguments.build_root / "dlopen-probe"
        command([compiler, probe, "-ldl", "-o", executable], "host probe compile")
        probe_environment = environment.copy()
        probe_environment["LD_LIBRARY_PATH"] = str(install_root / "lib64")
        command([executable], "host xcb-util-cursor dlopen probe", environment=probe_environment)
        host_probe = True
    library_sha256, library_size = sha256_file(library)
    pkgconfig_sha256, _pkgconfig_size = sha256_file(pkgconfig)
    header_sha256, _header_size = sha256_file(header)
    license_sha256, _license_size = sha256_file(license_path)
    require(
        license_sha256 == inputs["source_manifest"]["license"]["sha256"],
        "installed xcb-util-cursor license differs",
    )
    document = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-xcb-util-cursor-build",
        "version": VERSION,
        "identity": arguments.identity,
        "prefix": logical_prefix,
        "compiler_dumpmachine": compiler_dumpmachine,
        "build_environment": {
            "image": image,
            "target": arguments.identity,
            "tier": tier,
        },
        "builder": {
            "file": "scripts/build-xcb-util-cursor.py",
            "sha256": sha256_file(Path(__file__))[0],
        },
        "source_component": {
            "component": SOURCE_COMPONENT,
            "canonical_sha256": arguments.source_component_sha256,
        },
        "qualification_component": {
            "component": QUALIFICATION_COMPONENT,
            "canonical_sha256": arguments.qualification_component_sha256,
        },
        "rpm_input": {
            "lock_file": inputs["plan"]["locks"][
                [record["id"] for record in inputs["plan"]["locks"]].index(identity["lock_id"])
            ]["lock_file"],
            "lock_sha256": canonical_sha256(inputs["lock"]),
            "transaction_sha256": canonical_sha256(inputs["transaction"]),
            "parent_lock_sha256": inputs["transaction"]["base"][
                "parent_sha256"
            ],
        },
        "artifact": {
            "path": "lib64/libxcb-cursor.so.0.0.0",
            "sha256": library_sha256,
            "size": library_size,
            "machine": machine,
            "soname": soname,
            "needed": needed,
        },
        "header": {
            "path": "include/xcb/xcb_cursor.h",
            "sha256": header_sha256,
        },
        "pkgconfig": {
            "path": "lib64/pkgconfig/xcb-cursor.pc",
            "sha256": pkgconfig_sha256,
        },
        "license": {
            "path": "share/licenses/xcb-util-cursor/COPYING",
            "sha256": license_sha256,
        },
        "checks": {
            "no_rpath": True,
            "no_textrel": True,
            "target_execution": target_execution,
            "host_probe_passed": host_probe,
        },
    }
    write_manifest(arguments.output, document)
    print("built xcb-util-cursor %s: %s" % (arguments.identity, arguments.output))


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
    parser.add_argument("--sysroot", type=Path)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        require(SHA256_RE.match(arguments.source_component_sha256), "invalid source component digest")
        require(SHA256_RE.match(arguments.qualification_component_sha256), "invalid qualification component digest")
        build(arguments)
        return 0
    except (
        BuildError,
        COMPONENT["ComponentError"],
        STRICT["ValidationError"],
        OSError,
        tarfile.TarError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
