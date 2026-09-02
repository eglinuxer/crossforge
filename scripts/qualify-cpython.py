#!/usr/bin/env python3
"""Compile a target extension and statically qualify a cross-built CPython SDK."""

import argparse
import hashlib
import json
import os
import re
import runpy
import shlex
import subprocess
import sys
from pathlib import Path


class QualificationError(RuntimeError):
    pass


SDK_IDENTITY = runpy.run_path(
    str(Path(__file__).with_name("python_sdk_identity.py"))
)
SDKIdentityError = SDK_IDENTITY["IdentityError"]
sdk_tree_identity = SDK_IDENTITY["sdk_tree_identity"]
TARGET_AUDIT = runpy.run_path(
    str(Path(__file__).with_name("target_artifact_audit.py"))
)
TargetAuditError = TARGET_AUDIT["AuditError"]
EXEC_OPERATIONS = TARGET_AUDIT["EXEC_OPERATIONS"]
LOADER_OPERATIONS = TARGET_AUDIT["LOADER_OPERATIONS"]
ROW_CONTRACT = runpy.run_path(
    str(Path(__file__).with_name("python_row_contract.py"))
)
ContractError = ROW_CONTRACT["ContractError"]
RELEASE_COMPONENTS = runpy.run_path(
    str(Path(__file__).with_name("render-release-components.py"))
)
ProjectionError = RELEASE_COMPONENTS["ProjectionError"]
ZSTD_EVIDENCE = runpy.run_path(
    str(Path(__file__).with_name("python_zstd_evidence.py"))
)
ZstdEvidenceError = ZSTD_EVIDENCE["ZstdEvidenceError"]


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
ZSTD_FAMILY = ZSTD_EVIDENCE["FAMILY"]
ZSTD_REQUIRED_DEFINITIONS = tuple(ZSTD_EVIDENCE["REQUIRED_DEFINITIONS"])


def require(condition, message):
    if not condition:
        raise QualificationError(message)


def require_abi_value(actual, expected, name):
    if type(expected) is int:
        require(
            type(actual) is int and actual == expected,
            "target sysconfig %s mismatch: %r" % (name, actual),
        )
    else:
        require(
            actual == expected,
            "target sysconfig %s mismatch: %r" % (name, actual),
        )


def validate_configure_arguments(
    config_args, contract, target, build_triple, build_python
):
    require(isinstance(config_args, str), "target CONFIG_ARGS is not text")
    try:
        tokens = shlex.split(config_args)
    except ValueError as error:
        raise QualificationError("target CONFIG_ARGS cannot be parsed") from error
    require(tokens, "target CONFIG_ARGS is empty")

    def option_matches(name):
        return [
            token
            for token in tokens
            if token == name or token.startswith(name + "=")
        ]

    required = [
        "--host=" + target,
        "--build=" + build_triple,
        "--prefix=/opt/crossforge/python/%s/targets/%s"
        % (contract["row"], target),
        "--with-computed-gotos=yes",
        "--with-ensurepip=no",
        "--disable-test-modules",
    ]
    adapter = contract["adapter"]
    if adapter == "legacy":
        require(
            not option_matches("--with-build-python"),
            "legacy target CONFIG_ARGS contains unsupported --with-build-python",
        )
        require(
            not option_matches("--with-pkg-config"),
            "legacy target CONFIG_ARGS contains unsupported --with-pkg-config",
        )
    elif adapter in ("transition", "modern"):
        required.extend(
            [
                "--with-build-python=" + str(build_python),
                "--with-pkg-config=yes",
            ]
        )
    else:
        raise QualificationError("unsupported CPython adapter")
    for option in required:
        name = option.split("=", 1)[0]
        require(
            option_matches(name) == [option],
            "target CONFIG_ARGS must contain exactly %s" % option,
        )
    require(
        all("HOSTRUNNER" not in token for token in tokens),
        "target execution leaked into CONFIG_ARGS",
    )
    return config_args


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


def canonical_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


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


def parse_target_artifact_audit(lines, build_directory, prefix):
    try:
        return TARGET_AUDIT["parse_lines"](lines, build_directory, prefix)
    except TargetAuditError as error:
        raise QualificationError(str(error)) from error


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
    require(
        "(RPATH)" not in dynamic and "(RUNPATH)" not in dynamic,
        "%s contains RPATH/RUNPATH" % path,
    )
    require("/work" not in dynamic, "%s embeds a build path" % path)
    require("/usr/local" not in dynamic, "%s embeds a host-local path" % path)
    needed = sorted(set(re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic)))
    require(
        all("/" not in dependency for dependency in needed),
        "%s contains a path-qualified DT_NEEDED" % path,
    )

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


def expected_zstd_components(release, target_arch):
    try:
        return ZSTD_EVIDENCE["expected_components"](
            release,
            target_arch,
            RELEASE_COMPONENTS["render_component_documents"],
        )
    except ZstdEvidenceError as error:
        raise QualificationError(str(error)) from error


def validate_global_zstd_linkage(elf_audit):
    try:
        return ZSTD_EVIDENCE["validate_no_dynamic_libzstd"](
            elf_audit, "compile ELF audit"
        )
    except ZstdEvidenceError as error:
        raise QualificationError(str(error)) from error


def load_zstd_build_evidence(
    manifest_path,
    identity,
    prefix,
    machine,
    component_identity,
    policy_identity,
    path,
):
    require(
        manifest_path.is_file() and not manifest_path.is_symlink(),
        "%s is missing or unsafe" % path,
    )
    document = load_json(manifest_path)
    try:
        ZSTD_EVIDENCE["validate_build_manifest"](
            document,
            identity,
            prefix,
            machine,
            component_identity,
            policy_identity,
            path,
        )
    except ZstdEvidenceError as error:
        raise QualificationError(str(error)) from error
    return {"manifest": document, "manifest_sha256": sha256_file(manifest_path)}


def _nm_family_symbols(nm, path, option):
    output, _ = run([nm, "--format=posix", option, path])
    symbols = set()
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[0].split("@", 1)[0]
        if ZSTD_FAMILY.match(name):
            symbols.add(name)
    return sorted(symbols)


def audit_zstd_module(readelf, nm, path, audit):
    dynamic, _ = run([readelf, "--wide", "-d", path])
    require(
        "TEXTREL" not in dynamic
        and "(RPATH)" not in dynamic
        and "(RUNPATH)" not in dynamic,
        "%s violates the static zstd relocation/path policy" % path,
    )
    require(
        not any(name.startswith("libzstd.so") for name in audit["needed"]),
        "%s dynamically depends on libzstd" % path,
    )
    dynsym, _ = run([readelf, "--wide", "--dyn-syms", path])
    dynamic_exports = set()
    for line in dynsym.splitlines():
        fields = line.split()
        if len(fields) < 8 or not fields[0].endswith(":") or fields[6] == "UND":
            continue
        name = fields[7].split("@", 1)[0]
        if ZSTD_FAMILY.match(name):
            dynamic_exports.add(name)
    defined = _nm_family_symbols(nm, path, "--defined-only")
    undefined = _nm_family_symbols(nm, path, "--undefined-only")
    require(not dynamic_exports, "%s exports private zstd symbols" % path)
    require(not undefined, "%s has unresolved zstd symbols" % path)
    require(
        set(ZSTD_REQUIRED_DEFINITIONS).issubset(defined),
        "%s lacks required statically linked zstd definitions" % path,
    )
    symbol_evidence = {
        "required_definitions": list(ZSTD_REQUIRED_DEFINITIONS),
        "defined": defined,
        "undefined": undefined,
        "dynamic_exports": sorted(dynamic_exports),
    }
    symbol_evidence["canonical_sha256"] = canonical_sha256(symbol_evidence)
    return {
        "needed": audit["needed"],
        "path": None,
        "sha256": audit["sha256"],
        "symbols": symbol_evidence,
    }


def zstd_compile_evidence(
    contract,
    release,
    profile,
    target,
    build_prefix,
    target_prefix,
    lib_dynload,
    readelf,
    nm,
    elf_audit,
):
    module_matches = sorted(lib_dynload.glob("_zstd.*.so"))
    host_manifest_path = build_prefix / ".crossforge" / "zstd-build.json"
    target_manifest_path = target_prefix / ".crossforge" / "zstd-build.json"
    if not contract["zstd"]:
        require(not module_matches, "pre-3.14 SDK unexpectedly contains _zstd")
        require(
            not (host_manifest_path.exists() or host_manifest_path.is_symlink())
            and not (target_manifest_path.exists() or target_manifest_path.is_symlink()),
            "pre-3.14 SDK unexpectedly contains zstd build evidence",
        )
        return {"policy": "absent", "module": None, "builds": None}

    require(len(module_matches) == 1, "CPython 3.14 _zstd module is not unique")
    components = expected_zstd_components(release, profile["arch"])
    host_prefix = "/opt/crossforge/deps/zstd/1.5.7/host"
    target_zstd_prefix = "/opt/crossforge/deps/zstd/1.5.7/%s" % target
    builds = {
        "host": load_zstd_build_evidence(
            host_manifest_path,
            "host",
            host_prefix,
            TARGETS["x86_64-unknown-linux-gnu"]["machine"],
            components["host"],
            components["policy"],
            "host zstd build manifest",
        ),
        "target": load_zstd_build_evidence(
            target_manifest_path,
            target,
            target_zstd_prefix,
            profile["machine"],
            components["target"],
            components["policy"],
            "target zstd build manifest",
        ),
    }
    require(
        builds["host"]["manifest"]["source_manifest_sha256"]
        == builds["target"]["manifest"]["source_manifest_sha256"],
        "host and target zstd builds used different source manifests",
    )
    module = module_matches[0]
    relative = module.relative_to(target_prefix).as_posix()
    require(relative in elf_audit, "_zstd is absent from the ELF audit")
    module_evidence = audit_zstd_module(readelf, nm, module, elf_audit[relative])
    module_evidence["path"] = relative
    return {
        "policy": "required",
        "version": "1.5.7",
        "module": module_evidence,
        "builds": builds,
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
    parser.add_argument(
        "--qualification-policy-component-sha256", required=True
    )
    parser.add_argument("--qualification-component-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()

    profile = TARGETS.get(arguments.target)
    require(profile is not None, "unsupported CPython target")
    try:
        contract = ROW_CONTRACT["contract_for_version"](arguments.version)
    except ContractError as error:
        raise QualificationError(str(error)) from error
    minor = contract["minor"]
    compact_minor = minor.replace(".", "")
    expected_prefix = Path(
        "/opt/crossforge/python/cp%s/targets/%s"
        % (compact_minor, arguments.target)
    )
    expected_sysroot = Path("/opt/crossforge/sysroots/el8") / profile["arch"]
    expected_toolchain = Path("/opt/crossforge/targets") / arguments.target
    expected_build_prefix = Path(
        "/opt/crossforge/python/cp%s/build" % compact_minor
    )
    expected_build_python = expected_build_prefix / "bin" / ("python" + minor)
    require(arguments.prefix == expected_prefix, "unexpected target Python prefix")
    require(arguments.sysroot == expected_sysroot, "unexpected target sysroot")
    require(arguments.toolchain == expected_toolchain, "unexpected target toolchain")
    require(
        arguments.build_python == expected_build_python,
        "unexpected build Python path",
    )
    require(arguments.extension_source.is_file(), "minimal extension source is missing")
    expected_build_directory = Path(
        "/work/build/cpython-cp%s-%s" % (compact_minor, profile["arch"])
    )
    require(
        arguments.build_directory == expected_build_directory,
        "unexpected target Python build directory",
    )
    exec_audit_path = arguments.build_directory / "target-artifact-audit.log"
    try:
        exec_audit = exec_audit_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise QualificationError("target-execution audit is missing: %s" % error) from error
    exec_audit_result = parse_target_artifact_audit(
        exec_audit, arguments.build_directory, arguments.prefix
    )

    release = load_json(arguments.release)
    release_sha256 = hashlib.sha256(canonical_bytes(release)).hexdigest()
    qualification_components = RELEASE_COMPONENTS[
        "bind_python_qualification_components"
    ](
        release,
        arguments.qualification_policy_component_sha256,
        arguments.qualification_component_sha256,
    )
    try:
        binding = ROW_CONTRACT["bind_release"](
            release,
            version=arguments.version,
            adapter=contract["adapter"],
        )
    except ContractError as error:
        raise QualificationError(str(error)) from error
    python_entry = binding["entry"]
    source = python_entry["source"]
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
        [
            arguments.build_python,
            "-B",
            "-I",
            "-c",
            "import platform;print(platform.python_version())",
        ]
    )
    require(build_version.strip() == arguments.version, "build Python version mismatch")

    sysconfig_files = list((arguments.prefix / "lib" / ("python" + minor)).glob("_sysconfigdata_*.py"))
    require(len(sysconfig_files) == 1, "target SDK must contain one sysconfigdata module")
    variables = runpy.run_path(str(sysconfig_files[0])).get("build_time_vars")
    require(isinstance(variables, dict), "invalid target sysconfigdata")
    expected_soabi = "cpython-%s-%s" % (compact_minor, profile["multiarch"])
    expected_suffix = ".%s.so" % expected_soabi
    expected_values = {
        "HOST_GNU_TYPE": arguments.target,
        "MULTIARCH": profile["multiarch"],
        "SOABI": expected_soabi,
        "EXT_SUFFIX": expected_suffix,
        "Py_DEBUG": 0,
        "HAVE_ALIGNED_REQUIRED": 0,
        "HAVE_USABLE_WCHAR_T": profile["usable_wchar"],
        "SIZEOF_WCHAR_T": 4,
    }
    for name, expected in expected_values.items():
        actual = variables.get(name, 0 if name == "HAVE_ALIGNED_REQUIRED" else None)
        require_abi_value(actual, expected, name)
    if contract["gil_policy"] == "absent":
        require(
            "Py_GIL_DISABLED" not in variables,
            "CPython %s unexpectedly exposes Py_GIL_DISABLED" % minor,
        )
    elif contract["gil_policy"] == "zero":
        require(
            "Py_GIL_DISABLED" in variables
            and type(variables["Py_GIL_DISABLED"]) is int
            and variables["Py_GIL_DISABLED"] == 0,
            "CPython %s must explicitly disable the free-threaded ABI" % minor,
        )
        expected_values["Py_GIL_DISABLED"] = 0
    else:
        raise QualificationError("unsupported CPython GIL policy")
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
    validate_configure_arguments(
        variables.get("CONFIG_ARGS"),
        contract,
        arguments.target,
        build_triple,
        arguments.build_python,
    )

    gcc = arguments.toolchain / "bin" / (arguments.target + "-gcc")
    readelf = arguments.toolchain / "bin" / (arguments.target + "-readelf")
    nm = arguments.toolchain / "bin" / (arguments.target + "-nm")
    require(
        gcc.is_file() and readelf.is_file() and nm.is_file(),
        "target compiler tools are missing",
    )
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
    selected_modules = REQUIRED_MODULES + (("_zstd",) if contract["zstd"] else ())
    required_modules = {}
    for module in selected_modules:
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
    validate_global_zstd_linkage(elf_audit)
    extension_symbols, _ = run([readelf, "--wide", "--dyn-syms", extension])
    require("PyInit__crossforge" in extension_symbols, "extension initializer is missing")
    zstd = zstd_compile_evidence(
        contract,
        release,
        profile,
        arguments.target,
        expected_build_prefix,
        arguments.prefix,
        lib_dynload,
        readelf,
        nm,
        elf_audit,
    )

    report = {
        "qualification_schema_version": 3,
        "report_kind": "crossforge-cpython-compile",
        "target": arguments.target,
        "version": arguments.version,
        "adapter": contract["adapter"],
        "release_sha256": release_sha256,
        "qualification_components": qualification_components,
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
            "sdk_tree": sdk_tree_identity(expected_build_prefix),
        },
        "target_artifact_guard": {
            "execution_canaries": list(EXEC_OPERATIONS),
            "loader_canaries": list(LOADER_OPERATIONS),
            "records": exec_audit_result["records"],
            "denied_execution_attempts": exec_audit_result[
                "denied_execution_attempts"
            ],
            "denied_loader_attempts": exec_audit_result[
                "denied_loader_attempts"
            ],
            "canonical_sha256": TARGET_AUDIT["canonical_sha256"](
                exec_audit_result["records"]
            ),
        },
        "python_sha256": sha256_file(python),
        "extension": {"name": extension.name, "sha256": sha256_file(extension)},
        "required_modules": required_modules,
        "sysconfig": {name: variables.get(name, 0) for name in sorted(expected_values)},
        "sdk_tree": sdk_tree_identity(arguments.prefix),
        "elf_audit": elf_audit,
        "zstd": zstd,
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
    except (
        QualificationError,
        SDKIdentityError,
        ProjectionError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
