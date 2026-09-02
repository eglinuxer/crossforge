#!/usr/bin/env python3
"""Compile a target extension and statically qualify a cross-built CPython SDK."""

import argparse
import hashlib
import json
import os
import re
import runpy
import shlex
import stat
import subprocess
import sys
from pathlib import Path


class QualificationError(RuntimeError):
    pass


TARGETS = {
    "x86_64-unknown-linux-gnu": {
        "arch": "x86_64",
        "machine": "Advanced Micro Devices X86-64",
        "multiarch": "x86_64-linux-gnu",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
        "wchar_type": "int",
        "usable_wchar": 0,
    },
    "aarch64-unknown-linux-gnu": {
        "arch": "aarch64",
        "machine": "AArch64",
        "multiarch": "aarch64-linux-gnu",
        "interpreter": "/lib/ld-linux-aarch64.so.1",
        "wchar_type": "unsigned int",
        "usable_wchar": 1,
    },
}
REQUIRED_MODULES = (
    "_bz2",
    "_ctypes",
    "_hashlib",
    "_lzma",
    "_sqlite3",
    "_ssl",
    "_uuid",
    "zlib",
)


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
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationError("%s: %s" % (path, error)) from error


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_file(path):
    digest = hashlib.sha256()
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
    if process.returncode != 0:
        raise QualificationError(
            "command failed (%s):\n%s"
            % (
                " ".join(shlex.quote(str(argument)) for argument in arguments),
                process.stdout + process.stderr,
            )
        )
    return process.stdout, process.stderr


def version_tuple(value):
    return tuple(int(part) for part in value.split("."))


def audit_versions(text, path):
    ceilings = {
        "GLIBC": version_tuple("2.28"),
        "GCC": version_tuple("7.0.0"),
    }
    observed = {}
    for namespace, version in re.findall(
        r"\b(GLIBC|GCC)_([0-9]+(?:\.[0-9]+)+)\b", text
    ):
        parsed = version_tuple(version)
        observed[namespace] = max(parsed, observed.get(namespace, (0,)))
        require(
            parsed <= ceilings[namespace],
            "%s requires %s_%s above the EL8 contract"
            % (path, namespace, version),
        )
    return {
        namespace: ".".join(str(part) for part in value)
        for namespace, value in sorted(observed.items())
    }


def audit_elf(readelf, path, profile, require_interpreter=False):
    headers, _ = run([readelf, "-h", path])
    require("Class:" in headers and "ELF64" in headers, "%s is not ELF64" % path)
    require(profile["machine"] in headers, "%s has the wrong ELF machine" % path)

    dynamic, _ = run([readelf, "--wide", "-d", path])
    require("TEXTREL" not in dynamic, "%s contains text relocations" % path)
    require("RELR" not in dynamic, "%s requires DT_RELR" % path)
    require("/work" not in dynamic, "%s embeds a build path" % path)
    require("/usr/local" not in dynamic, "%s embeds a host-local path" % path)
    needed = sorted(set(re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic)))

    program_headers, _ = run([readelf, "--wide", "-l", path])
    stack = [line for line in program_headers.splitlines() if "GNU_STACK" in line]
    require(stack and all("RWE" not in line for line in stack), "%s has executable stack" % path)
    if require_interpreter:
        require(
            profile["interpreter"] in program_headers,
            "%s has the wrong ELF interpreter" % path,
        )

    symbols, _ = run([readelf, "--wide", "--dyn-syms", path])
    require("GLIBC_PRIVATE" not in symbols, "%s requires GLIBC_PRIVATE" % path)
    return {
        "needed": needed,
        "required_versions": audit_versions(symbols, path),
        "sha256": sha256_file(path),
    }


def sdk_tree_identity(prefix):
    entries = []
    for path in sorted(prefix.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(prefix).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            resolved = path.resolve()
            resolved_prefix = prefix.resolve()
            require(
                resolved == resolved_prefix
                or str(resolved).startswith(str(resolved_prefix) + os.sep),
                "SDK symlink escapes target prefix: %s" % path,
            )
            entries.append({"path": relative, "type": "symlink", "target": os.readlink(path)})
        elif stat.S_ISREG(metadata.st_mode):
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "size": metadata.st_size,
                    "sha256": sha256_file(path),
                }
            )
        elif stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": relative, "type": "directory"})
        else:
            raise QualificationError("unsupported SDK file type: %s" % path)
    return {
        "entries": len(entries),
        "canonical_sha256": hashlib.sha256(canonical_bytes(entries)).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--build-python", type=Path, required=True)
    parser.add_argument("--build-directory", type=Path, required=True)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--extension-source", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()

    profile = TARGETS.get(arguments.target)
    require(profile is not None, "unsupported CPython target")
    require(re.fullmatch(r"3\.13\.[0-9]+", arguments.version), "unsupported CPython version")
    minor = ".".join(arguments.version.split(".")[:2])
    compact_minor = minor.replace(".", "")
    expected_prefix = Path(
        "/opt/crossforge/python/cp%s/targets/%s"
        % (compact_minor, arguments.target)
    )
    expected_sysroot = Path("/opt/crossforge/sysroots/el8") / profile["arch"]
    expected_toolchain = Path("/opt/crossforge/targets") / arguments.target
    require(arguments.prefix == expected_prefix, "unexpected target Python prefix")
    require(arguments.sysroot == expected_sysroot, "unexpected target sysroot")
    require(arguments.toolchain == expected_toolchain, "unexpected target toolchain")
    require(arguments.extension_source.is_file(), "minimal extension source is missing")
    expected_build_directory = Path("/work/build/cpython-cp313-%s" % profile["arch"])
    require(
        arguments.build_directory == expected_build_directory,
        "unexpected target Python build directory",
    )
    exec_audit_path = arguments.build_directory / "target-exec-audit.log"
    try:
        exec_audit = exec_audit_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise QualificationError("target-execution audit is missing: %s" % error) from error
    require(exec_audit and all(path.startswith("/") for path in exec_audit), "invalid target-execution audit")
    canary = str(arguments.build_directory / "target-exec-canary")
    require(canary in exec_audit, "target-execution canary was not denied")
    for path in exec_audit:
        require(
            path == str(arguments.build_directory)
            or path.startswith(str(arguments.build_directory) + os.sep)
            or path == str(arguments.prefix)
            or path.startswith(str(arguments.prefix) + os.sep),
            "target-execution audit path escaped guarded roots",
        )

    release = load_json(arguments.release)
    release_sha256 = hashlib.sha256(canonical_bytes(release)).hexdigest()
    python_entries = [
        item
        for item in release["python"]["versions"]
        if item["version"] == arguments.version
    ]
    require(len(python_entries) == 1, "release does not select one CPython source")
    source = python_entries[0]["source"]
    require(source["status"] == "locked", "CPython source is not locked")

    target_entries = [
        item for item in release["targets"] if item["triple"] == arguments.target
    ]
    require(len(target_entries) == 1, "release does not select one target")
    sysroot_lock_path = arguments.sysroot / "usr/share/crossforge/sysroot-lock.json"
    sysroot_lock = load_json(sysroot_lock_path)
    sysroot_sha256 = hashlib.sha256(canonical_bytes(sysroot_lock)).hexdigest()
    require(
        target_entries[0]["sysroot"]["canonical_sha256"] == sysroot_sha256,
        "target sysroot differs from release",
    )
    sysroot_transaction_path = (
        arguments.sysroot / "usr/share/crossforge/sysroot-transaction.json"
    )
    sysroot_transaction = load_json(sysroot_transaction_path)
    sysroot_transaction_sha256 = hashlib.sha256(
        canonical_bytes(sysroot_transaction)
    ).hexdigest()
    require(
        sysroot_lock["transaction"]["canonical_sha256"]
        == sysroot_transaction_sha256,
        "embedded sysroot transaction differs from its lock",
    )

    build_version, _ = run(
        [arguments.build_python, "-I", "-c", "import platform;print(platform.python_version())"]
    )
    require(build_version.strip() == arguments.version, "build Python version mismatch")

    sysconfig_files = list((arguments.prefix / "lib" / ("python" + minor)).glob("_sysconfigdata_*.py"))
    require(len(sysconfig_files) == 1, "target SDK must contain one sysconfigdata module")
    variables = runpy.run_path(str(sysconfig_files[0])).get("build_time_vars")
    require(isinstance(variables, dict), "invalid target sysconfigdata")
    expected_soabi = "cpython-313-%s" % profile["multiarch"]
    expected_suffix = ".%s.so" % expected_soabi
    expected_values = {
        "HOST_GNU_TYPE": arguments.target,
        "MULTIARCH": profile["multiarch"],
        "SOABI": expected_soabi,
        "EXT_SUFFIX": expected_suffix,
        "Py_DEBUG": 0,
        "Py_GIL_DISABLED": 0,
        "HAVE_ALIGNED_REQUIRED": 0,
        "HAVE_USABLE_WCHAR_T": profile["usable_wchar"],
        "SIZEOF_WCHAR_T": 4,
    }
    for name, expected in expected_values.items():
        actual = variables.get(name, 0 if name == "HAVE_ALIGNED_REQUIRED" else None)
        require(actual == expected, "target sysconfig %s mismatch: %r" % (name, actual))
    build_triple = variables.get("BUILD_GNU_TYPE", "")
    require(
        re.fullmatch(r"x86_64-[A-Za-z0-9_.-]+-linux-gnu", build_triple)
        and build_triple != arguments.target,
        "target Python is not a real cross build",
    )
    expected_cc = "%s/bin/%s-gcc --sysroot=%s" % (
        arguments.toolchain,
        arguments.target,
        arguments.sysroot,
    )
    expected_cxx = "%s/bin/%s-g++ --sysroot=%s" % (
        arguments.toolchain,
        arguments.target,
        arguments.sysroot,
    )
    require(variables.get("CC") == expected_cc, "target sysconfig CC mismatch")
    require(variables.get("CXX") == expected_cxx, "target sysconfig CXX mismatch")
    require(
        variables.get("AR") == str(arguments.toolchain / "bin" / (arguments.target + "-ar")),
        "target sysconfig AR mismatch",
    )
    config_args = variables.get("CONFIG_ARGS", "")
    for option in (
        "--host=" + arguments.target,
        "--build=" + build_triple,
        "--with-build-python=" + str(arguments.build_python),
        "--with-ensurepip=no",
        "--disable-test-modules",
    ):
        require(option in config_args, "target CONFIG_ARGS is missing %s" % option)
    require("HOSTRUNNER" not in config_args, "target execution leaked into CONFIG_ARGS")

    gcc = arguments.toolchain / "bin" / (arguments.target + "-gcc")
    readelf = arguments.toolchain / "bin" / (arguments.target + "-readelf")
    require(gcc.is_file() and readelf.is_file(), "target compiler tools are missing")
    macros, _ = run([gcc, "--sysroot=" + str(arguments.sysroot), "-dM", "-E", "-xc", "/dev/null"])
    require(
        "#define __WCHAR_TYPE__ %s" % profile["wchar_type"] in macros,
        "target wchar_t type differs from ABI contract",
    )

    python_config = arguments.prefix / "bin" / ("python" + minor + "-config")
    includes, _ = run([python_config, "--includes"])
    suffix, _ = run([python_config, "--extension-suffix"])
    require(suffix.strip() == expected_suffix, "python-config extension suffix mismatch")
    arguments.work.mkdir(parents=True, exist_ok=True)
    extension = arguments.work / ("_crossforge" + expected_suffix)
    run(
        [
            gcc,
            "--sysroot=" + str(arguments.sysroot),
            "-shared",
            "-fPIC",
            "-O2",
            "-Wl,-z,relro,-z,now",
        ]
        + shlex.split(includes)
        + [arguments.extension_source, "-o", extension]
    )

    python = arguments.prefix / "bin" / ("python" + minor)
    require(python.is_file(), "target Python executable is missing")
    lib_dynload = arguments.prefix / "lib" / ("python" + minor) / "lib-dynload"
    required_modules = {}
    for module in REQUIRED_MODULES:
        matches = list(lib_dynload.glob(module + ".*.so"))
        require(len(matches) == 1, "required module %s is not unique" % module)
        required_modules[module] = matches[0].relative_to(arguments.prefix).as_posix()

    elf_paths = [python, extension] + sorted(lib_dynload.glob("*.so"))
    elf_audit = {}
    for path in elf_paths:
        name = (
            "qualification/" + path.name
            if path == extension
            else path.relative_to(arguments.prefix).as_posix()
        )
        require(name not in elf_audit, "duplicate qualified ELF path")
        elf_audit[name] = audit_elf(
            readelf,
            path,
            profile,
            require_interpreter=(path == python),
        )
    extension_symbols, _ = run([readelf, "--wide", "--dyn-syms", extension])
    require("PyInit__crossforge" in extension_symbols, "extension initializer is missing")

    report = {
        "qualification_schema_version": 1,
        "report_kind": "crossforge-cpython-compile",
        "target": arguments.target,
        "version": arguments.version,
        "release_sha256": release_sha256,
        "source": {
            "url": source["url"],
            "size": source["size"],
            "sha256": source["sha256"],
            "sigstore_bundle_sha256": source["sigstore"]["bundle_sha256"],
            "sigstore_verification": source["sigstore"]["verification"],
        },
        "sysroot_sha256": sysroot_sha256,
        "sysroot_transaction_sha256": sysroot_transaction_sha256,
        "target_prefix": str(arguments.prefix),
        "build_python": {
            "path": str(arguments.build_python),
            "version": build_version.strip(),
            "sha256": sha256_file(arguments.build_python),
        },
        "target_exec_guard": {
            "canary_observed": True,
            "denied_attempts": len(exec_audit),
            "canonical_sha256": hashlib.sha256(canonical_bytes(exec_audit)).hexdigest(),
        },
        "python_sha256": sha256_file(python),
        "extension": {"name": extension.name, "sha256": sha256_file(extension)},
        "required_modules": required_modules,
        "sysconfig": {name: variables.get(name, 0) for name in sorted(expected_values)},
        "sdk_tree": sdk_tree_identity(arguments.prefix),
        "elf_audit": elf_audit,
    }
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.report.with_name(arguments.report.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(arguments.report)
    print("qualified CPython compile SDK: %s %s" % (arguments.version, arguments.target))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QualificationError, KeyError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
