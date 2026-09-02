#!/usr/bin/env python3
"""Bind two qualified target SDKs into one append-only CPython row artifact."""

import argparse
import hashlib
import json
import re
import runpy
import subprocess
import sys
from pathlib import Path


class FinalizationError(RuntimeError):
    pass


def support_script(name):
    sibling = Path(__file__).with_name(name)
    if sibling.is_file():
        return sibling
    repository_script = Path(__file__).resolve().parents[1] / "scripts" / name
    if repository_script.is_file():
        return repository_script
    raise FinalizationError("missing row support script: %s" % name)


QUALIFICATION_VALIDATOR = runpy.run_path(
    str(support_script("finalize-cpython-qualification.py"))
)
QualificationError = QUALIFICATION_VALIDATOR["FinalizationError"]
SDK_IDENTITY = runpy.run_path(str(support_script("python_sdk_identity.py")))
SDKIdentityError = SDK_IDENTITY["IdentityError"]
sdk_tree_identity = SDK_IDENTITY["sdk_tree_identity"]
SOURCE_BINDING = runpy.run_path(
    str(support_script("python_source_release_binding.py"))
)
SourceBindingError = SOURCE_BINDING["BindingError"]


TARGETS = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
QUALIFICATION_SOURCE_KEYS = {
    "url",
    "size",
    "sha256",
    "sigstore_bundle_sha256",
    "sigstore_verification",
}
QUALIFICATION_KEYS = {
    "qualification_schema_version",
    "report_kind",
    "status",
    "target",
    "version",
    "adapter",
    "release_sha256",
    "source",
    "sysroot_sha256",
    "python_sha256",
    "extension_sha256",
    "probe_sha256",
    "compile_report_sha256",
    "compile",
    "zstd",
    "runtime_result_sha256",
    "executions",
}


def require(condition, message):
    if not condition:
        raise FinalizationError(message)


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
        raise FinalizationError("%s: %s" % (path, error)) from error


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def host_binutils(name):
    for path in (
        Path("/opt/rh/gcc-toolset-15/root/usr/bin") / name,
        Path("/usr/bin") / name,
    ):
        if path.is_file():
            return path
    raise FinalizationError("missing host %s for row ELF revalidation" % name)


def run_tool(arguments):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "row ELF revalidation failed: %s" % (process.stdout + process.stderr),
    )
    return process.stdout


def audit_exported_zstd_module(module_path, relative_path, expected_machine):
    """Recompute one exported module's static-link proof with host readelf."""
    readelf = host_binutils("readelf")
    header = run_tool([readelf, "--wide", "-h", module_path])
    require(
        re.search(r"^\s*Class:\s+ELF64\s*$", header, re.MULTILINE) is not None
        and re.search(r"^\s*Type:\s+DYN\b", header, re.MULTILINE) is not None
        and re.search(
            r"^\s*Machine:\s+%s\s*$" % re.escape(expected_machine),
            header,
            re.MULTILINE,
        )
        is not None
        and re.search(
            r"^\s*Entry point address:\s+0x0\s*$", header, re.MULTILINE
        )
        is not None,
        "exported _zstd has the wrong ELF class/type/machine",
    )
    dynamic = run_tool([readelf, "--wide", "-d", module_path])
    require(
        "TEXTREL" not in dynamic
        and "(RPATH)" not in dynamic
        and "(RUNPATH)" not in dynamic
        and re.search(r"\(FLAGS_1\).*\bPIE\b", dynamic) is None,
        "exported _zstd violates the relocation/path policy",
    )
    program_headers = run_tool([readelf, "--wide", "-l", module_path])
    require(
        not any("INTERP" in line for line in program_headers.splitlines()),
        "exported _zstd contains an executable interpreter",
    )
    needed = sorted(set(re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic)))
    require(
        all("/" not in item for item in needed)
        and not any(item.startswith("libzstd.so") for item in needed),
        "exported _zstd has a path-qualified or dynamic libzstd dependency",
    )
    dynsym = run_tool([readelf, "--wide", "--dyn-syms", module_path])
    dynamic_exports = set()
    for line in dynsym.splitlines():
        fields = line.split()
        if len(fields) < 8 or not fields[0].endswith(":") or fields[6] == "UND":
            continue
        name = fields[7].split("@", 1)[0]
        if re.match(r"(?:ZSTD|ZDICT|FSE|HUF|XXH)_", name):
            dynamic_exports.add(name)
    symbols = run_tool([readelf, "--wide", "--syms", module_path])
    defined = set()
    undefined = set()
    for line in symbols.splitlines():
        fields = line.split()
        if len(fields) < 8 or not fields[0].endswith(":"):
            continue
        name = fields[7].split("@", 1)[0]
        if not re.match(r"(?:ZSTD|ZDICT|FSE|HUF|XXH)_", name):
            continue
        if fields[6] == "UND":
            undefined.add(name)
        else:
            defined.add(name)
    symbol_evidence = {
        "required_definitions": list(
            QUALIFICATION_VALIDATOR["ZSTD_REQUIRED_DEFINITIONS"]
        ),
        "defined": sorted(defined),
        "undefined": sorted(undefined),
        "dynamic_exports": sorted(dynamic_exports),
    }
    require(
        set(symbol_evidence["required_definitions"]).issubset(defined)
        and not undefined
        and not dynamic_exports,
        "exported _zstd static-link symbol proof failed",
    )
    symbol_evidence["canonical_sha256"] = canonical_sha256(symbol_evidence)
    return {
        "needed": needed,
        "path": relative_path,
        "sha256": sha256_file(module_path),
        "symbols": symbol_evidence,
    }


def revalidate_exported_zstd_module(module_path, evidence, expected_machine):
    observed = audit_exported_zstd_module(
        module_path, evidence["path"], expected_machine
    )
    require(observed == evidence, "exported _zstd static-link evidence differs")


def exported_zstd_modules(prefix):
    return sorted(prefix.glob("lib/python*/lib-dynload/_zstd*.so"))


def audit_build_zstd_module(build_prefix, evidence_by_arch, minor):
    policies = {value["policy"] for value in evidence_by_arch.values()}
    require(len(policies) == 1, "row zstd policy differs across targets")
    modules = exported_zstd_modules(build_prefix)
    if policies == {"absent"}:
        require(not modules, "absent zstd policy exported build-Python _zstd")
        return None
    require(policies == {"required"}, "row zstd policy is invalid")
    expected = (
        build_prefix
        / ("lib/python%s/lib-dynload" % minor)
        / ("_zstd.cpython-%s-x86_64-linux-gnu.so" % minor.replace(".", ""))
    )
    require(
        modules == [expected]
        and expected.is_file()
        and not expected.is_symlink(),
        "required zstd policy needs the exact safe build-Python _zstd ABI path",
    )
    relative = expected.relative_to(build_prefix).as_posix()
    return audit_exported_zstd_module(
        expected, relative, "Advanced Micro Devices X86-64"
    )


def verify_exported_zstd_manifest(path, evidence, label):
    require(path.is_file() and not path.is_symlink(), "%s is missing or unsafe" % label)
    require(
        sha256_file(path) == evidence["manifest_sha256"],
        "%s digest differs from qualification" % label,
    )
    require(
        load_json(path) == evidence["manifest"],
        "%s content differs from qualification" % label,
    )


def revalidate_exported_zstd(evidence, build_prefix, target_prefix, arch):
    host_manifest = build_prefix / ".crossforge/zstd-build.json"
    target_manifest = target_prefix / ".crossforge/zstd-build.json"
    if evidence["policy"] == "absent":
        require(
            not host_manifest.exists()
            and not host_manifest.is_symlink()
            and not target_manifest.exists()
            and not target_manifest.is_symlink(),
            "%s absent zstd policy exported build evidence" % arch,
        )
        require(
            not exported_zstd_modules(target_prefix),
            "%s absent zstd policy exported _zstd" % arch,
        )
        return

    verify_exported_zstd_manifest(
        host_manifest, evidence["builds"]["host"], "%s host zstd manifest" % arch
    )
    verify_exported_zstd_manifest(
        target_manifest,
        evidence["builds"]["target"],
        "%s target zstd manifest" % arch,
    )
    relative = Path(evidence["module"]["path"])
    require(
        not relative.is_absolute() and ".." not in relative.parts,
        "%s _zstd path is unsafe" % arch,
    )
    module_path = target_prefix / relative
    require(
        module_path.is_file() and not module_path.is_symlink(),
        "%s exported _zstd is missing or unsafe" % arch,
    )
    require(
        sha256_file(module_path) == evidence["module"]["sha256"],
        "%s exported _zstd digest differs from qualification" % arch,
    )
    require(
        exported_zstd_modules(target_prefix) == [module_path],
        "%s exported _zstd inventory differs from qualification" % arch,
    )
    machine = (
        "Advanced Micro Devices X86-64" if arch == "x86_64" else "AArch64"
    )
    revalidate_exported_zstd_module(module_path, evidence["module"], machine)


def aggregate_zstd(evidence_by_arch, host_module):
    policies = {value["policy"] for value in evidence_by_arch.values()}
    require(len(policies) == 1, "row zstd policy differs across targets")
    if policies == {"absent"}:
        expected = {"policy": "absent", "module": None, "builds": None}
        require(
            all(value == expected for value in evidence_by_arch.values()),
            "row absent zstd evidence is not normalized",
        )
        require(host_module is None, "absent zstd policy has a host module")
        return expected

    require(policies == {"required"}, "row zstd policy is invalid")
    require(isinstance(host_module, dict), "required zstd host module is missing")
    x86 = evidence_by_arch["x86_64"]
    arm = evidence_by_arch["aarch64"]
    require(x86["version"] == arm["version"] == "1.5.7", "row zstd version differs")
    require(x86["builds"]["host"] == arm["builds"]["host"], "row host zstd build differs")

    host = x86["builds"]["host"]["manifest"]
    target_manifests = {
        arch: value["builds"]["target"]["manifest"]
        for arch, value in evidence_by_arch.items()
    }
    manifests = [host] + [target_manifests[arch] for arch in sorted(target_manifests)]
    source_manifest_sha256 = host["source_manifest_sha256"]
    build_policy = host["build_policy"]
    headers = host["headers"]
    member_names = [item["name"] for item in host["archive"]["members"]]
    for manifest in manifests[1:]:
        require(
            manifest["source_manifest_sha256"] == source_manifest_sha256,
            "row zstd source identity differs across builds",
        )
        require(
            manifest["build_policy"] == build_policy,
            "row zstd policy component differs across builds",
        )
        require(manifest["headers"] == headers, "row zstd headers differ across builds")
        require(
            [item["name"] for item in manifest["archive"]["members"]]
            == member_names,
            "row zstd archive inventory differs across builds",
        )
    required_definitions = x86["module"]["symbols"]["required_definitions"]
    host_symbols = host_module["symbols"]
    require(
        host_symbols["required_definitions"] == required_definitions
        and not host_symbols["undefined"]
        and not host_symbols["dynamic_exports"]
        and not any(item.startswith("libzstd.so") for item in host_module["needed"]),
        "row build-Python _zstd static-link policy differs",
    )
    for evidence in (x86, arm):
        symbols = evidence["module"]["symbols"]
        require(
            symbols["required_definitions"] == required_definitions
            and not symbols["undefined"]
            and not symbols["dynamic_exports"]
            and not any(
                item.startswith("libzstd.so")
                for item in evidence["module"]["needed"]
            ),
            "row _zstd static-link policy differs across targets",
        )

    targets = {}
    for arch in sorted(evidence_by_arch):
        evidence = evidence_by_arch[arch]
        manifest = target_manifests[arch]
        targets[arch] = {
            "target": TARGETS[arch],
            "build_component": manifest["build_component"],
            "build_manifest_sha256": evidence["builds"]["target"][
                "manifest_sha256"
            ],
            "archive_sha256": manifest["archive"]["sha256"],
            "pic_probe_sha256": manifest["pic_probe"]["sha256"],
            "module": {
                "path": evidence["module"]["path"],
                "sha256": evidence["module"]["sha256"],
            },
            "static_link": {
                "needed": evidence["module"]["needed"],
                "symbols_canonical_sha256": evidence["module"]["symbols"][
                    "canonical_sha256"
                ],
            },
        }
    return {
        "policy": "required",
        "version": "1.5.7",
        "source_manifest_sha256": source_manifest_sha256,
        "build_policy": build_policy,
        "headers": headers,
        "archive_members_sha256": canonical_sha256(member_names),
        "host": {
            "build_component": host["build_component"],
            "build_manifest_sha256": x86["builds"]["host"]["manifest_sha256"],
            "archive_sha256": host["archive"]["sha256"],
            "pic_probe_sha256": host["pic_probe"]["sha256"],
            "module": {
                "path": host_module["path"],
                "sha256": host_module["sha256"],
            },
            "static_link": {
                "needed": host_module["needed"],
                "symbols_canonical_sha256": host_module["symbols"][
                    "canonical_sha256"
                ],
            },
        },
        "static_link": {
            "required_definitions": required_definitions,
            "undefined": [],
            "dynamic_exports": [],
            "dynamic_libzstd": False,
        },
        "targets": targets,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--row", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    require(re.fullmatch(r"cp[0-9]+", arguments.row), "invalid CPython row")
    minor = arguments.version.rsplit(".", 1)[0]
    require(arguments.row == "cp" + minor.replace(".", ""), "row/version mismatch")
    release = load_json(arguments.release)
    release_sha256 = canonical_sha256(release)
    source_manifest = load_json(arguments.source_manifest)
    try:
        source_context = SOURCE_BINDING["bind_source_manifest"](
            source_manifest,
            release,
            arguments.row,
            arguments.version,
            arguments.adapter,
        )
    except SourceBindingError as error:
        raise FinalizationError(
            "prepared source manifest is not release-bound: %s" % error
        ) from error
    require(
        source_context["release_sha256"] == release_sha256,
        "prepared source bridge release identity differs",
    )
    source = source_context["source"]
    patches = source_context["patches"]
    build_python = (
        arguments.root
        / "opt/crossforge/python"
        / arguments.row
        / "build/bin"
        / ("python" + minor)
    )
    require(build_python.is_file(), "row export is missing the build Python")
    build_python_sha256 = sha256_file(build_python)
    build_prefix = build_python.parents[1]
    build_tree = sdk_tree_identity(build_prefix)

    reports = {}
    zstd_evidence = {}
    for arch, target in TARGETS.items():
        report_path = (
            arguments.root
            / "opt/crossforge/qualification/python"
            / arguments.row
            / (arch + ".json")
        )
        report = load_json(report_path)
        try:
            QUALIFICATION_VALIDATOR["validate_final_report"](
                report, release, target, arguments.version
            )
            validated_zstd = QUALIFICATION_VALIDATOR[
                "validate_qualification_zstd"
            ](report, release, target, arguments.version)
        except QualificationError as error:
            raise FinalizationError(
                "%s qualification report is invalid: %s" % (arch, error)
            ) from error
        require(
            set(report) == QUALIFICATION_KEYS
            and report.get("qualification_schema_version") == 2
            and report.get("report_kind") == "crossforge-cpython-qualification"
            and report.get("status") == "passed",
            "%s qualification did not pass" % arch,
        )
        require(report.get("target") == target, "%s target mismatch" % arch)
        require(report.get("version") == arguments.version, "%s version mismatch" % arch)
        require(
            report.get("adapter") == arguments.adapter,
            "%s adapter mismatch" % arch,
        )
        embedded_compile = report.get("compile")
        require(
            isinstance(embedded_compile, dict)
            and embedded_compile.get("adapter") == arguments.adapter,
            "%s embedded compile adapter mismatch" % arch,
        )
        require(
            report.get("release_sha256") == release_sha256,
            "%s release identity mismatch" % arch,
        )
        report_source = report.get("source")
        require(
            isinstance(report_source, dict)
            and set(report_source) == QUALIFICATION_SOURCE_KEYS
            and all(
                report_source.get(name) == source.get(name)
                for name in ("url", "size", "sha256")
            ),
            "%s source identity mismatch" % arch,
        )
        target_python = (
            arguments.root
            / "opt/crossforge/python"
            / arguments.row
            / "targets"
            / target
            / "bin"
            / ("python" + minor)
        )
        require(target_python.is_file(), "%s target Python is missing" % arch)
        require(
            sha256_file(target_python) == report.get("python_sha256"),
            "%s target Python differs from qualification" % arch,
        )
        target_prefix = target_python.parents[1]
        target_tree = sdk_tree_identity(target_prefix)
        require(
            target_tree == embedded_compile.get("sdk_tree"),
            "%s target SDK tree differs from qualification" % arch,
        )
        lib_dynload = target_prefix / "lib" / ("python" + minor) / "lib-dynload"
        actual_elf_paths = [target_python]
        if lib_dynload.is_dir():
            actual_elf_paths.extend(sorted(lib_dynload.glob("*.so")))
        actual_elf_audit = {
            path.relative_to(target_prefix).as_posix(): sha256_file(path)
            for path in actual_elf_paths
        }
        compile_elf_audit = embedded_compile.get("elf_audit")
        require(
            isinstance(compile_elf_audit, dict)
            and set(actual_elf_audit)
            == {
                name
                for name in compile_elf_audit
                if not name.startswith("qualification/")
            },
            "%s SDK ELF inventory differs from qualification" % arch,
        )
        for name, digest in actual_elf_audit.items():
            audit = compile_elf_audit[name]
            require(
                isinstance(audit, dict) and audit.get("sha256") == digest,
                "%s SDK ELF digest differs from qualification: %s"
                % (arch, name),
            )
        compile_build = embedded_compile.get("build_python")
        require(
            isinstance(compile_build, dict)
            and compile_build.get("sha256") == build_python_sha256
            and compile_build.get("sdk_tree") == build_tree,
            "%s build Python tree differs from qualification" % arch,
        )
        revalidate_exported_zstd(
            validated_zstd, build_prefix, target_prefix, arch
        )
        zstd_evidence[arch] = validated_zstd
        reports[arch] = {
            "target": target,
            "report_sha256": sha256_file(report_path),
            "python_sha256": report["python_sha256"],
            "sdk_tree": target_tree,
        }

    host_zstd_module = audit_build_zstd_module(
        build_prefix, zstd_evidence, minor
    )
    zstd = aggregate_zstd(zstd_evidence, host_zstd_module)

    manifest = {
        "schema_version": 1,
        "kind": "crossforge-cpython-row",
        "row": arguments.row,
        "version": arguments.version,
        "adapter": arguments.adapter,
        "support": source_context["support"],
        "release_sha256": release_sha256,
        "source": source,
        "source_manifest_sha256": sha256_file(arguments.source_manifest),
        "patches": patches,
        "build_python_sha256": build_python_sha256,
        "build_python_sdk_tree": build_tree,
        "zstd": zstd,
        "qualifications": reports,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(arguments.output)
    print("finalized CPython row: %s" % arguments.row)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        FinalizationError,
        SDKIdentityError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
