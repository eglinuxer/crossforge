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
from pathlib import Path, PurePosixPath


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import abi_contract  # noqa: E402
import python_abi_audit  # noqa: E402
import python_runtime_providers  # noqa: E402


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
ABI_BASELINE_LOGICAL_PATH = "abi/el8/{arch}.json"
ABI_PROVIDER_MANIFEST_LOGICAL_PATH = "config/abi-providers.json"
ABI_SYSROOT_INVENTORY_LOGICAL_PATH = (
    "evidence/abi/el8-{arch}-sysroot.json"
)
RUNTIME_PROVIDER_POLICY_LOGICAL_PATH = (
    "config/python-runtime-providers.json"
)
PYTHON_PROVIDER_CATALOG_LOGICAL_PATH = (
    "evidence/abi/el8-{arch}-python-provider-catalog.json"
)


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
    ]
    if contract["minor"] == "3.9":
        require(
            not option_matches("--disable-test-modules"),
            "CPython 3.9 CONFIG_ARGS contains unsupported "
            "--disable-test-modules",
        )
    else:
        required.append("--disable-test-modules")
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


def readelf_evidence(readelf, path):
    """Read every input used by the frozen ABI and ownership auditors."""
    evidence = {}
    for name, options in (
        ("elf_header", ("-h",)),
        ("dynamic_section", ("--wide", "-d")),
        ("program_headers", ("--wide", "-l")),
        ("dynamic_symbols", ("--wide", "--dyn-syms")),
        ("version_info", ("--wide", "--version-info")),
        ("relocations", ("--wide", "--relocs")),
    ):
        evidence[name], _ = run([readelf] + list(options) + [path])
    return evidence


def elf_record(readelf, path, identity, expected_soname=None):
    evidence = readelf_evidence(readelf, path)
    record = python_abi_audit.elf_record_from_readelf(
        identity,
        evidence["dynamic_symbols"],
        evidence["version_info"],
        evidence["dynamic_section"],
        evidence["relocations"],
        expected_soname=expected_soname,
    )
    return evidence, record


def contained_file_sha256(root, logical_path, expected_sha256, label):
    """Hash a logical sysroot file without permitting a symlink escape."""
    require(type(logical_path) is str, "%s path is not text" % label)
    relative = PurePosixPath(logical_path.lstrip("/"))
    require(
        logical_path.startswith("/usr/lib64/")
        and not relative.is_absolute()
        and ".." not in relative.parts,
        "%s path is unsafe" % label,
    )
    try:
        root_details = os.lstat(str(root))
    except OSError as error:
        raise QualificationError("%s root is unavailable: %s" % (label, error))
    require(
        stat.S_ISDIR(root_details.st_mode)
        and not stat.S_ISLNK(root_details.st_mode),
        "%s root is unsafe" % label,
    )
    real_root = Path(os.path.realpath(str(root)))
    current = real_root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            details = os.lstat(str(current))
        except OSError as error:
            raise QualificationError(
                "%s parent is unavailable: %s" % (label, error)
            )
        require(
            stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode),
            "%s parent is unsafe" % label,
        )
    candidate = real_root.joinpath(*relative.parts)
    require(os.path.lexists(str(candidate)), "%s is missing" % label)
    resolved = Path(os.path.realpath(str(candidate)))
    require(
        resolved == real_root or real_root in resolved.parents,
        "%s escapes the sysroot" % label,
    )
    require(resolved.is_file(), "%s is not a regular file" % label)
    observed = sha256_file(resolved)
    require(observed == expected_sha256, "%s SHA256 differs" % label)
    return observed


def bind_abi_inputs(
    baseline_path,
    provider_manifest_path,
    sysroot_inventory_path,
    runtime_provider_policy_path,
    provider_catalog_path,
    sysroot_lock,
    sysroot_sha256,
    arch,
    triple,
):
    """Load and bind every reviewed ABI input to this embedded sysroot."""
    baseline = abi_contract.load_baseline(
        baseline_path, expected_arch=arch, expected_triple=triple
    )
    provider_manifest = abi_contract.load_provider_manifest(
        provider_manifest_path
    )
    sysroot_inventory = abi_contract.load_inventory(
        sysroot_inventory_path, expected_arch=arch, expected_triple=triple
    )
    abi_contract.validate_inventory_provider_manifest(
        sysroot_inventory, provider_manifest
    )
    abi_contract.validate_inventory_superset(sysroot_inventory, baseline)
    require(
        sysroot_inventory["source"]["kind"] == "locked-sysroot",
        "target ABI inventory is not a locked-sysroot inventory",
    )
    require(
        sysroot_inventory["source"]["identity_sha256"] == sysroot_sha256,
        "target ABI inventory source differs from the embedded sysroot lock",
    )

    runtime_policy = python_runtime_providers.load_json(
        runtime_provider_policy_path
    )
    python_runtime_providers.validate_policy(runtime_policy)
    runtime_target = python_runtime_providers.policy_target(
        runtime_policy, arch, triple
    )
    require(
        runtime_target["sysroot_lock"]["canonical_sha256"] == sysroot_sha256,
        "runtime provider policy differs from the embedded sysroot lock",
    )
    python_runtime_providers.validate_policy_target_against_lock(
        runtime_policy, arch, sysroot_lock
    )
    reviewed_provider_catalog = python_runtime_providers.load_json(
        provider_catalog_path
    )
    require(
        provider_catalog_path.read_bytes()
        == python_runtime_providers.canonical_bytes(
            reviewed_provider_catalog
        )
        + b"\n",
        "reviewed Python provider catalog is not canonical JSON",
    )
    external_providers = [
        provider["soname"] for provider in runtime_target["providers"]
    ]
    try:
        python_abi_audit.validate_provider_catalog(
            baseline,
            external_providers,
            reviewed_provider_catalog,
        )
    except python_abi_audit.PythonAbiAuditError as error:
        raise QualificationError(str(error)) from error
    require(
        python_runtime_providers.canonical_sha256(
            reviewed_provider_catalog
        )
        == runtime_target["provider_catalog_sha256"],
        "reviewed Python provider catalog differs from policy",
    )

    manifest_sha256 = abi_contract.canonical_sha256(provider_manifest)
    require(
        sysroot_inventory["source"]["provider_manifest_sha256"]
        == manifest_sha256,
        "target ABI inventory provider manifest differs",
    )
    identities = {
        "baseline": {
            "file": ABI_BASELINE_LOGICAL_PATH.format(arch=arch),
            "canonical_sha256": abi_contract.canonical_sha256(baseline),
            "source_inventory": baseline["review"]["source_inventory"],
            "source_inventory_sha256": baseline["review"][
                "source_inventory_sha256"
            ],
        },
        "provider_manifest": {
            "file": ABI_PROVIDER_MANIFEST_LOGICAL_PATH,
            "canonical_sha256": manifest_sha256,
        },
        "sysroot_inventory": {
            "file": ABI_SYSROOT_INVENTORY_LOGICAL_PATH.format(arch=arch),
            "canonical_sha256": abi_contract.canonical_sha256(
                sysroot_inventory
            ),
            "source": dict(sysroot_inventory["source"]),
        },
        "runtime_provider_policy": {
            "file": RUNTIME_PROVIDER_POLICY_LOGICAL_PATH,
            "canonical_sha256": python_runtime_providers.canonical_sha256(
                runtime_policy
            ),
            "sysroot_lock_sha256": runtime_target["sysroot_lock"][
                "canonical_sha256"
            ],
            "provider_catalog_sha256": runtime_target[
                "provider_catalog_sha256"
            ],
        },
    }
    return {
        "baseline": baseline,
        "provider_manifest": provider_manifest,
        "sysroot_inventory": sysroot_inventory,
        "runtime_policy": runtime_policy,
        "runtime_target": runtime_target,
        "reviewed_provider_catalog": reviewed_provider_catalog,
        "identities": identities,
    }


def build_provider_catalog(readelf, sysroot, abi_inputs):
    """Build the exact core plus Python-runtime provider catalog."""
    baseline = abi_inputs["baseline"]
    inventory = abi_inputs["sysroot_inventory"]
    runtime_evidence = python_runtime_providers.runtime_provider_evidence(
        abi_inputs["runtime_policy"], baseline["target"]["arch"], root=sysroot
    )
    runtime_by_soname = {
        item["soname"]: item for item in runtime_evidence["providers"]
    }
    catalog = {}
    summaries = []
    for soname in sorted(inventory["providers"]):
        provider = inventory["providers"][soname]
        contained_file_sha256(
            sysroot,
            provider["path"],
            provider["sha256"],
            "core provider %s" % soname,
        )
        path = sysroot / provider["path"].lstrip("/")
        _evidence, record = elf_record(
            readelf, path, soname, expected_soname=soname
        )
        catalog[soname] = record
        summaries.append(
            {
                "soname": soname,
                "path": provider["path"],
                "source": "frozen-core",
                "dso_sha256": provider["sha256"],
                "elf_record_sha256": canonical_sha256(record),
                "rpm_owner": None,
            }
        )
    for soname in sorted(runtime_by_soname):
        provider = runtime_by_soname[soname]
        path = sysroot / provider["path"].lstrip("/")
        _evidence, record = elf_record(
            readelf, path, soname, expected_soname=soname
        )
        catalog[soname] = record
        summaries.append(
            {
                "soname": soname,
                "path": provider["path"],
                "source": "python-runtime",
                "dso_sha256": provider["dso_sha256"],
                "elf_record_sha256": canonical_sha256(record),
                "rpm_owner": dict(provider["owner"]),
            }
        )
    summaries.sort(key=lambda item: item["soname"])
    external = sorted(runtime_by_soname)
    python_abi_audit.validate_provider_catalog(baseline, external, catalog)
    require(
        len(catalog) == len(baseline["providers"]) + len(external),
        "provider catalog count differs from its reviewed inputs",
    )
    catalog_sha256 = canonical_sha256(catalog)
    require(
        catalog_sha256
        == abi_inputs["runtime_target"]["provider_catalog_sha256"],
        "provider catalog differs from the reviewed target policy",
    )
    require(
        catalog == abi_inputs["reviewed_provider_catalog"],
        "provider catalog differs from the reviewed catalog bytes",
    )
    return catalog, external, {
        "file": PYTHON_PROVIDER_CATALOG_LOGICAL_PATH.format(
            arch=baseline["target"]["arch"]
        ),
        "provider_count": len(catalog),
        "elf_records_sha256": catalog_sha256,
        "records": catalog,
        "providers": summaries,
    }


def audit_python_artifact(
    baseline,
    external_providers,
    catalog,
    python_global,
    identity,
    evidence,
    record,
    path,
):
    """Apply the qualified ELF profile and deterministic ownership audit."""
    policy = abi_contract.audit_elf_policy(
        baseline,
        identity,
        evidence["dynamic_section"],
        evidence["program_headers"],
        evidence["elf_header"],
        profile_name="crossforge-qualified-v1",
    )
    ownership = python_abi_audit.audit_python_elf(
        baseline,
        external_providers,
        catalog,
        python_global,
        record,
    )
    require(
        policy["artifact"] == identity
        and ownership["artifact"] == identity,
        "ABI audit artifact identity differs",
    )
    return {
        "needed": list(record["needed"]),
        "sha256": sha256_file(path),
        "elf_record_sha256": canonical_sha256(record),
        "elf_record": record,
        "elf_policy": policy,
        "ownership": ownership,
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
        "needed": sorted(audit["needed"]),
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
    parser.add_argument("--abi-baseline", type=Path, required=True)
    parser.add_argument("--abi-provider-manifest", type=Path, required=True)
    parser.add_argument("--sysroot-abi-inventory", type=Path, required=True)
    parser.add_argument(
        "--runtime-provider-policy", type=Path, required=True
    )
    parser.add_argument(
        "--python-provider-catalog", type=Path, required=True
    )
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
    abi_inputs = bind_abi_inputs(
        arguments.abi_baseline,
        arguments.abi_provider_manifest,
        arguments.sysroot_abi_inventory,
        arguments.runtime_provider_policy,
        arguments.python_provider_catalog,
        sysroot_lock,
        sysroot_sha256,
        profile["arch"],
        arguments.target,
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
        variables.get("LDSHARED")
        == expected_cc
        + " -shared -Wl,-z,relro,-z,now -Wl,-z,relro,-z,now",
        "target sysconfig LDSHARED mismatch",
    )
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
    provider_catalog, external_providers, provider_catalog_report = (
        build_provider_catalog(readelf, arguments.sysroot, abi_inputs)
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
    elf_records = {}
    elf_evidence = {}
    elf_files = {}
    for path in elf_paths:
        name = (
            "qualification/" + path.name
            if path == extension
            else path.relative_to(arguments.prefix).as_posix()
        )
        require(name not in elf_records, "duplicate qualified ELF path")
        evidence, record = elf_record(readelf, path, name)
        elf_evidence[name] = evidence
        elf_records[name] = record
        elf_files[name] = path
    python_name = python.relative_to(arguments.prefix).as_posix()
    python_global = elf_records[python_name]
    elf_audit = {}
    for name in sorted(elf_records):
        elf_audit[name] = audit_python_artifact(
            abi_inputs["baseline"],
            external_providers,
            provider_catalog,
            python_global,
            name,
            elf_evidence[name],
            elf_records[name],
            elf_files[name],
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
        "qualification_schema_version": 4,
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
        "abi": dict(
            abi_inputs["identities"],
            provider_catalog=provider_catalog_report,
            python_global={
                "identity": python_global["identity"],
                "sha256": sha256_file(python),
                "elf_record_sha256": canonical_sha256(python_global),
                "needed": list(python_global["needed"]),
                "default_export_count": len(
                    python_global["default_exports"]
                ),
                "record": python_global,
            },
        ),
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
