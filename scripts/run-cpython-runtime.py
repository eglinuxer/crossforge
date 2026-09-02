#!/usr/bin/env python3
"""Run a cross-built CPython in one explicit Rocky runtime tier."""

import argparse
import atexit
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

from loader_evidence import normalize_loader_listing


class RuntimeError_(RuntimeError):
    pass


TARGETS = {
    "x86_64-unknown-linux-gnu": {
        "arch": "x86_64",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
    },
    "aarch64-unknown-linux-gnu": {
        "arch": "aarch64",
        "interpreter": "/lib/ld-linux-aarch64.so.1",
    },
}
ROW_CONTRACT = runpy.run_path(
    str(Path(__file__).with_name("python_row_contract.py"))
)
ContractError = ROW_CONTRACT["ContractError"]
RELEASE_COMPONENTS = runpy.run_path(
    str(Path(__file__).with_name("render-release-components.py"))
)
ProjectionError = RELEASE_COMPONENTS["ProjectionError"]
RUNTIME_PROVIDERS = runpy.run_path(
    str(Path(__file__).with_name("python_runtime_providers.py"))
)
RuntimeProviderPolicyError = RUNTIME_PROVIDERS[
    "RuntimeProviderPolicyError"
]
ABI_CONTRACT = runpy.run_path(
    str(Path(__file__).with_name("abi_contract.py"))
)
AbiContractError = ABI_CONTRACT["AbiContractError"]
PYTHON_ABI = runpy.run_path(
    str(Path(__file__).with_name("python_abi_audit.py"))
)
PythonAbiAuditError = PYTHON_ABI["PythonAbiAuditError"]


def require(condition, message):
    if not condition:
        raise RuntimeError_(message)


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
        raise RuntimeError_("%s: %s" % (path, error)) from error


def canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_exact_keys(value, expected, label):
    require(isinstance(value, dict), "%s must be an object" % label)
    actual = set(value)
    require(
        actual == set(expected),
        "%s has unexpected fields (missing=%s; extra=%s)"
        % (
            label,
            ",".join(sorted(set(expected) - actual)),
            ",".join(sorted(actual - set(expected))),
        ),
    )


def require_sha256(value, label):
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
        "%s is not a SHA256" % label,
    )


def validate_compile_qualification_components(compile_report, release):
    require(
        compile_report.get("qualification_schema_version") == 4,
        "compile report schema version mismatch",
    )
    try:
        return RELEASE_COMPONENTS["validate_python_qualification_components"](
            compile_report.get("qualification_components"), release
        )
    except ProjectionError as error:
        raise RuntimeError_(
            "compile report qualification_components: %s" % error
        ) from error


def validate_overlay_evidence(
    value,
    release,
    profile,
    target,
    compile_report,
    root,
    runtime_package_names,
):
    require_exact_keys(
        value,
        {
            "schema_version",
            "kind",
            "qualification_only",
            "identity",
            "identity_sha256",
            "runtime_inventory",
        },
        "clean runtime evidence",
    )
    require(value["schema_version"] == 1, "clean runtime evidence schema mismatch")
    require(
        value["kind"] == "crossforge-python-runtime-overlay"
        and value["qualification_only"] is True,
        "clean runtime evidence kind mismatch",
    )
    identity = value["identity"]
    require_exact_keys(
        identity,
        {
            "base_image",
            "release_sha256",
            "target",
            "sysroot",
            "selected_packages",
            "selected_packages_sha256",
        },
        "clean runtime identity",
    )
    require_exact_keys(
        identity["base_image"],
        {"index_digest", "manifest_digest"},
        "clean runtime base image",
    )
    oci_arch = "amd64" if profile["arch"] == "x86_64" else "arm64"
    require(
        identity["base_image"]
        == {
            "index_digest": release["base_image"]["digest"],
            "manifest_digest": release["base_image"]["manifests"][oci_arch],
        },
        "clean runtime base image differs from release",
    )
    require(
        identity["release_sha256"] == canonical_sha256(release),
        "clean runtime release digest mismatch",
    )
    require_exact_keys(identity["target"], {"arch", "triple"}, "clean runtime target")
    require(
        identity["target"] == {"arch": profile["arch"], "triple": target},
        "clean runtime target mismatch",
    )
    require_exact_keys(
        identity["sysroot"],
        {"lock_sha256", "transaction_sha256"},
        "clean runtime sysroot",
    )
    require(
        identity["sysroot"]
        == {
            "lock_sha256": compile_report["sysroot_sha256"],
            "transaction_sha256": compile_report["sysroot_transaction_sha256"],
        },
        "clean runtime RPMs came from a different sysroot contract",
    )

    packages = identity["selected_packages"]
    require(
        isinstance(packages, list) and len(packages) == len(runtime_package_names),
        "clean runtime package set has the wrong size",
    )
    names = []
    for index, package in enumerate(packages):
        require_exact_keys(
            package,
            {"name", "nevra", "received_sha256"},
            "clean runtime package %d" % index,
        )
        require(isinstance(package["name"], str), "runtime package name is invalid")
        require(isinstance(package["nevra"], str) and package["nevra"], "runtime NEVRA is invalid")
        require_sha256(package["received_sha256"], "runtime package digest")
        names.append(package["name"])
    require(names == list(runtime_package_names), "clean runtime package names/order mismatch")
    require(
        identity["selected_packages_sha256"] == canonical_sha256(packages),
        "clean runtime package-set digest mismatch",
    )
    require(
        value["identity_sha256"] == canonical_sha256(identity),
        "clean runtime identity digest mismatch",
    )

    inventory = value["runtime_inventory"]
    require_exact_keys(
        inventory,
        {
            "before_sha256",
            "before_item_count",
            "after_sha256",
            "after_item_count",
            "installed_nevras",
            "os_release_sha256",
        },
        "clean runtime inventory",
    )
    for name in ("before_sha256", "after_sha256", "os_release_sha256"):
        require_sha256(inventory[name], "clean runtime inventory %s" % name)
    require(
        type(inventory["before_item_count"]) is int
        and type(inventory["after_item_count"]) is int
        and inventory["after_item_count"] >= inventory["before_item_count"] > 0,
        "clean runtime inventory counts are invalid",
    )
    installed = inventory["installed_nevras"]
    require(
        installed == sorted(package["nevra"] for package in packages),
        "clean runtime installed NEVRAs differ from evidence",
    )
    rpm_stdout, _ = run(
        [
            "rpm",
            "--root",
            root,
            "-qa",
            "--qf",
            "%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\n",
        ]
    )
    actual_inventory = sorted(line for line in rpm_stdout.splitlines() if line)
    require(
        len(actual_inventory) == inventory["after_item_count"]
        and canonical_sha256(actual_inventory) == inventory["after_sha256"],
        "clean runtime rpmdb differs from overlay evidence",
    )
    require(
        set(installed).issubset(actual_inventory),
        "clean runtime rpmdb is missing selected packages",
    )
    os_release = root / "usr/lib/os-release"
    require(
        os_release.is_file()
        and sha256_file(os_release) == inventory["os_release_sha256"],
        "clean runtime os-release differs from overlay evidence",
    )
    return value


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(arguments, environment=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=60,
    )
    if process.returncode != 0:
        raise RuntimeError_(
            "command failed (%s):\n%s"
            % (" ".join(str(argument) for argument in arguments), process.stdout + process.stderr)
        )
    return process.stdout, process.stderr


def validate_locked_runtime(root, lock, compile_report):
    transaction_path = root / "usr/share/crossforge/sysroot-transaction.json"
    transaction = load_json(transaction_path)
    transaction_sha256 = canonical_sha256(transaction)
    require(
        transaction_sha256 == compile_report["sysroot_transaction_sha256"]
        and lock["transaction"]["canonical_sha256"] == transaction_sha256,
        "locked runtime transaction differs from compile report",
    )
    expected_inventory = transaction["manifests"]["result"]["packages"]
    require(
        isinstance(expected_inventory, list)
        and expected_inventory == sorted(set(expected_inventory)),
        "locked runtime expected inventory is not canonical",
    )
    stdout, _ = run(
        [
            "rpm",
            "--root",
            root,
            "-qa",
            "--qf",
            "%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\n",
        ]
    )
    actual_inventory = sorted(line for line in stdout.splitlines() if line)
    require(
        actual_inventory == expected_inventory,
        "locked runtime rpmdb differs from its transaction",
    )
    return transaction_sha256


def parse_probe(stdout, label):
    lines = stdout.splitlines()
    require(len(lines) == 1, "%s did not emit one JSON line" % label)
    try:
        value = json.loads(lines[0], object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise RuntimeError_("%s emitted invalid JSON: %s" % (label, error)) from error
    require(isinstance(value, dict), "%s result is not an object" % label)
    return value


def safe_root_path(root, absolute):
    require(absolute.startswith("/"), "guest path is not absolute")
    path = root / absolute.lstrip("/")
    resolved_root = root.resolve()
    resolved = path.resolve()
    require(
        resolved == resolved_root or str(resolved).startswith(str(resolved_root) + os.sep),
        "guest path escapes runtime root: %s" % absolute,
    )
    return path


def validate_runtime_provider_catalog(
    runtime_root,
    compile_report,
    provider_policy,
    provider_catalog_path,
    arch,
    target,
    tier,
    baseline_path=Path("/work/config/abi-baseline.json"),
    inventory_path=Path("/work/config/abi-sysroot-inventory.json"),
    readelf=Path("/usr/bin/readelf"),
):
    """Rebuild the complete core+external catalog from one runtime tier."""
    require(
        tier in {"locked-sysroot", "clean-rocky"},
        "runtime provider catalog tier is invalid",
    )
    try:
        baseline = ABI_CONTRACT["load_baseline"](
            baseline_path,
            expected_arch=arch,
            expected_triple=target,
        )
        inventory = ABI_CONTRACT["load_inventory"](
            inventory_path,
            expected_arch=arch,
            expected_triple=target,
        )
    except AbiContractError as error:
        raise RuntimeError_(str(error)) from error
    require(
        inventory_path.read_bytes()
        == ABI_CONTRACT["canonical_bytes"](inventory) + b"\n",
        "runtime ABI inventory is not canonical JSON",
    )
    policy_target = RUNTIME_PROVIDERS["policy_target"](
        provider_policy, arch, target
    )
    external = [
        provider["soname"] for provider in policy_target["providers"]
    ]
    reviewed = RUNTIME_PROVIDERS["load_json"](provider_catalog_path)
    require(
        provider_catalog_path.read_bytes()
        == RUNTIME_PROVIDERS["canonical_bytes"](reviewed) + b"\n",
        "runtime provider catalog is not canonical JSON",
    )
    try:
        PYTHON_ABI["validate_provider_catalog"](
            baseline, external, reviewed
        )
    except PythonAbiAuditError as error:
        raise RuntimeError_(str(error)) from error
    reviewed_sha256 = RUNTIME_PROVIDERS["canonical_sha256"](reviewed)
    require(
        reviewed_sha256 == policy_target["provider_catalog_sha256"],
        "runtime provider catalog differs from policy",
    )

    compile_abi = compile_report["abi"]
    require(
        compile_abi["sysroot_inventory"]["canonical_sha256"]
        == ABI_CONTRACT["canonical_sha256"](inventory)
        and inventory["source"]["identity_sha256"]
        == policy_target["sysroot_lock"]["canonical_sha256"],
        "runtime ABI inventory differs from compile/policy",
    )
    compile_catalog = compile_abi["provider_catalog"]
    require_exact_keys(
        compile_catalog,
        {
            "file",
            "provider_count",
            "elf_records_sha256",
            "records",
            "providers",
        },
        "compile provider catalog",
    )
    require(
        compile_catalog["file"]
        == "evidence/abi/el8-%s-python-provider-catalog.json" % arch
        and compile_catalog["elf_records_sha256"] == reviewed_sha256
        and compile_catalog["records"] == reviewed,
        "compile provider catalog differs from reviewed bytes",
    )

    expected_summaries = []
    for soname, provider in inventory["providers"].items():
        expected_summaries.append(
            {
                "soname": soname,
                "path": provider["path"],
                "source": "frozen-core",
                "dso_sha256": provider["sha256"],
                "elf_record_sha256": RUNTIME_PROVIDERS[
                    "canonical_sha256"
                ](reviewed[soname]),
                "rpm_owner": None,
            }
        )
    owners = {owner["name"]: owner for owner in policy_target["owners"]}
    for provider in policy_target["providers"]:
        expected_summaries.append(
            {
                "soname": provider["soname"],
                "path": provider["path"],
                "source": "python-runtime",
                "dso_sha256": provider["dso_sha256"],
                "elf_record_sha256": RUNTIME_PROVIDERS[
                    "canonical_sha256"
                ](reviewed[provider["soname"]]),
                "rpm_owner": dict(owners[provider["owner"]]),
            }
        )
    expected_summaries.sort(key=lambda item: item["soname"])
    require(
        compile_catalog["provider_count"] == len(expected_summaries)
        and compile_catalog["providers"] == expected_summaries,
        "compile provider summaries differ from reviewed inputs",
    )

    records = {}
    canonical_paths = {}
    for summary in expected_summaries:
        soname = summary["soname"]
        path = safe_root_path(runtime_root, summary["path"])
        require(
            path.is_file(),
            "runtime provider is missing: %s" % soname,
        )
        if summary["source"] == "python-runtime" or tier == "locked-sysroot":
            require(
                sha256_file(path) == summary["dso_sha256"],
                "runtime provider bytes differ: %s" % soname,
            )
        canonical_paths[soname] = path
        try:
            _evidence, record = PYTHON_ABI["elf_record_from_file"](
                readelf,
                path,
                soname,
                expected_soname=soname,
            )
        except PythonAbiAuditError as error:
            raise RuntimeError_(str(error)) from error
        records[soname] = record
    try:
        PYTHON_ABI["validate_provider_catalog"](
            baseline, external, records
        )
    except PythonAbiAuditError as error:
        raise RuntimeError_(str(error)) from error
    require(
        records == reviewed,
        "runtime provider catalog differs from compile/reviewed policy",
    )
    return {
        "canonical_sha256": reviewed_sha256,
        "paths": canonical_paths,
        "required_external": external,
    }


def validate_device_loader_listing(text, runtime_root):
    require("not found" not in text, "device loader has unresolved dependencies")
    dependencies = normalize_loader_listing(text)
    require(dependencies, "device loader evidence is empty")
    reject_dynamic_zstd(dependencies, "device loader")
    root = str(runtime_root.resolve())
    for dependency in dependencies:
        if dependency.startswith("linux-vdso"):
            continue
        if " => " in dependency:
            resolved = dependency.split(" => ", 1)[1]
        elif dependency.startswith("/"):
            resolved = dependency
        else:
            continue
        if os.path.basename(resolved).startswith("ld-linux-"):
            continue
        require(
            resolved == root or resolved.startswith(root + os.sep),
            "device loader escaped selected runtime: %s" % dependency,
        )
    return dependencies


def validate_device_loaded_objects(
    text,
    runtime_root,
    target_prefix,
    provider_catalog,
    guest_interpreter,
):
    raw_objects = sorted(
        set(re.findall(r"calling init: (\/\S+)", text))
    )
    require(
        raw_objects,
        "device probe produced no dynamic-loader object evidence",
    )
    reject_dynamic_zstd(raw_objects, "device probe")
    root = runtime_root.resolve()
    prefix = target_prefix.resolve()
    provider_paths = provider_catalog["paths"]
    seen_providers = {soname: [] for soname in provider_paths}
    objects = []
    for raw_path in raw_objects:
        if raw_path == guest_interpreter:
            loader_soname = Path(raw_path).name
            require(
                loader_soname in provider_paths,
                "device probe used an unreviewed guest loader",
            )
            guest_loader = safe_root_path(runtime_root, raw_path)
            require(
                guest_loader.is_file()
                and guest_loader.resolve()
                == provider_paths[loader_soname].resolve(),
                "device probe used an unreviewed guest loader",
            )
            canonical = str(provider_paths[loader_soname])
            seen_providers[loader_soname].append(canonical)
            objects.append(canonical)
            continue
        resolved = Path(raw_path).resolve()
        if resolved.name.startswith("ld-linux-"):
            loader_soname = Path(raw_path).name
            if loader_soname in provider_paths:
                require(
                    resolved == provider_paths[loader_soname].resolve(),
                    "device probe loaded an unreviewed loader path: %s"
                    % raw_path,
                )
                canonical = str(provider_paths[loader_soname])
                seen_providers[loader_soname].append(canonical)
                objects.append(canonical)
            else:
                objects.append(str(resolved))
            continue
        require(
            root in resolved.parents or prefix in resolved.parents,
            "device probe loaded an object outside the selected runtime/SDK: %s"
            % raw_path,
        )
        soname = Path(raw_path).name
        if soname in provider_paths:
            require(
                resolved == provider_paths[soname].resolve(),
                "device probe loaded an unreviewed provider path: %s"
                % raw_path,
            )
            canonical = str(provider_paths[soname])
            seen_providers[soname].append(canonical)
            objects.append(canonical)
        else:
            objects.append(str(resolved))
    for soname, paths in seen_providers.items():
        require(
            len(paths) <= 1,
            "device probe loaded one provider through multiple paths: %s"
            % soname,
        )
    require(
        all(
            len(seen_providers[soname]) == 1
            for soname in provider_catalog["required_external"]
        ),
        "device probe did not map every runtime library",
    )
    return sorted(set(objects))


def reject_dynamic_zstd(values, label):
    require(
        not any(re.search(r"(?:^|[/\s])libzstd\.so(?:\.|\s|$)", item) for item in values),
        "%s loaded a dynamic libzstd" % label,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-report", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--runtime-provider-policy", type=Path, required=True)
    parser.add_argument("--python-provider-catalog", type=Path, required=True)
    parser.add_argument("--runtime-evidence", type=Path)
    parser.add_argument("--target-prefix", type=Path, required=True)
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tier", choices=("locked-sysroot", "clean-rocky"), required=True)
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    profile = TARGETS.get(arguments.target)
    require(profile is not None, "unsupported CPython runtime target")
    require(
        arguments.runtime_root.is_dir() and not arguments.runtime_root.is_symlink(),
        "runtime root is missing or is a symlink",
    )
    require(arguments.runtime_root.resolve() != Path("/"), "unsafe runtime root")
    release = load_json(arguments.release)
    release_sha256 = canonical_sha256(release)
    require(
        arguments.runtime_provider_policy
        == Path("/src/config/python-runtime-providers.json"),
        "unexpected runtime provider policy path",
    )
    require(
        arguments.python_provider_catalog
        == Path("/work/config/python-provider-catalog.json"),
        "unexpected Python provider catalog path",
    )
    provider_policy = RUNTIME_PROVIDERS["load_json"](
        arguments.runtime_provider_policy
    )
    provider_target = RUNTIME_PROVIDERS["policy_target"](
        provider_policy, profile["arch"], arguments.target
    )
    runtime_provider_evidence = RUNTIME_PROVIDERS[
        "runtime_provider_evidence"
    ](provider_policy, profile["arch"], arguments.runtime_root)
    runtime_package_names = [
        owner["name"] for owner in provider_target["owners"]
    ]
    try:
        binding = ROW_CONTRACT["bind_release"](
            release, version=arguments.version
        )
    except ContractError as error:
        raise RuntimeError_(str(error)) from error
    contract = binding["contract"]
    zstd_policy = "required" if contract["zstd"] else "absent"
    compile_report = load_json(arguments.compile_report)
    require(compile_report.get("report_kind") == "crossforge-cpython-compile", "compile report kind mismatch")
    require(compile_report.get("target") == arguments.target, "compile report target mismatch")
    require(compile_report.get("version") == arguments.version, "compile report version mismatch")
    require(compile_report.get("adapter") == contract["adapter"], "compile report adapter mismatch")
    require(compile_report.get("release_sha256") == release_sha256, "compile report release mismatch")
    validate_compile_qualification_components(compile_report, release)
    compile_abi = compile_report.get("abi")
    require(isinstance(compile_abi, dict), "compile report ABI evidence is missing")
    compile_provider_policy = compile_abi.get("runtime_provider_policy")
    require_exact_keys(
        compile_provider_policy,
        {
            "file",
            "canonical_sha256",
            "sysroot_lock_sha256",
            "provider_catalog_sha256",
        },
        "compile report runtime provider policy",
    )
    require(
        compile_provider_policy
        == {
            "file": "config/python-runtime-providers.json",
            "canonical_sha256": runtime_provider_evidence["policy_sha256"],
            "sysroot_lock_sha256": runtime_provider_evidence[
                "sysroot_lock_sha256"
            ],
            "provider_catalog_sha256": runtime_provider_evidence[
                "provider_catalog_sha256"
            ],
        },
        "compile report runtime provider policy differs from runtime",
    )
    observed_provider_catalog = validate_runtime_provider_catalog(
        arguments.runtime_root,
        compile_report,
        provider_policy,
        arguments.python_provider_catalog,
        profile["arch"],
        arguments.target,
        arguments.tier,
    )
    require(
        observed_provider_catalog["canonical_sha256"]
        == runtime_provider_evidence["provider_catalog_sha256"],
        "runtime provider catalog digest differs from policy",
    )
    compile_report_sha256 = sha256_file(arguments.compile_report)

    minor = contract["minor"]
    expected_prefix = Path(
        "/opt/crossforge/python/cp%s/targets/%s"
        % (minor.replace(".", ""), arguments.target)
    )
    require(arguments.target_prefix == expected_prefix, "target prefix mismatch")
    host_python = arguments.target_prefix / "bin" / ("python" + minor)
    require(host_python.is_file(), "target Python is missing")
    require(
        sha256_file(host_python) == compile_report["python_sha256"],
        "runtime Python differs from compile report",
    )
    require(arguments.extension.is_file(), "qualification extension is missing")
    require(
        arguments.extension.name == compile_report["extension"]["name"]
        and sha256_file(arguments.extension) == compile_report["extension"]["sha256"],
        "qualification extension differs from compile report",
    )
    require(arguments.probe.is_file(), "runtime probe is missing")

    sysroot_marker = arguments.runtime_root / "usr/share/crossforge/sysroot-lock.json"
    if arguments.tier == "locked-sysroot":
        require(arguments.runtime_evidence is None, "locked tier accepts no overlay evidence")
        sysroot_lock = load_json(sysroot_marker)
        try:
            RUNTIME_PROVIDERS["validate_policy_target_against_lock"](
                provider_policy, profile["arch"], sysroot_lock
            )
        except RuntimeProviderPolicyError as error:
            raise RuntimeError_(str(error)) from error
        identity_sha256 = canonical_sha256(sysroot_lock)
        require(
            identity_sha256 == compile_report["sysroot_sha256"],
            "locked runtime differs from compile sysroot",
        )
        validate_locked_runtime(arguments.runtime_root, sysroot_lock, compile_report)
        runtime_kind = "locked-sysroot"
    else:
        require(not sysroot_marker.exists(), "clean runtime contains a Crossforge sysroot lock")
        require(arguments.runtime_evidence is not None, "clean runtime evidence is required")
        runtime_evidence = validate_overlay_evidence(
            load_json(arguments.runtime_evidence),
            release,
            profile,
            arguments.target,
            compile_report,
            arguments.runtime_root,
            runtime_package_names,
        )
        identity_sha256 = runtime_evidence["identity_sha256"]
        runtime_kind = "clean-rocky-overlay"

    os_release = arguments.runtime_root / "etc/os-release"
    require(os_release.is_file(), "runtime root has no os-release")
    os_release_text = os_release.read_text(encoding="utf-8")
    require(re.search(r'^ID="?rocky"?$', os_release_text, re.MULTILINE), "runtime is not Rocky Linux")
    require(re.search(r'^VERSION_ID="?8\.10"?$', os_release_text, re.MULTILINE), "runtime is not Rocky 8.10")
    loader_guest = profile["interpreter"]
    loader_host = safe_root_path(arguments.runtime_root, loader_guest)
    require(loader_host.is_file(), "runtime loader is missing")

    guest_prefix = safe_root_path(arguments.runtime_root, str(arguments.target_prefix))
    require(not guest_prefix.exists(), "runtime target prefix is not clean")
    guest_prefix.parent.mkdir(parents=True, exist_ok=True)
    cleanup_paths = []

    def cleanup_runtime_inputs():
        for path in reversed(cleanup_paths):
            shutil.rmtree(str(path), ignore_errors=True)

    atexit.register(cleanup_runtime_inputs)
    cleanup_paths.append(guest_prefix)
    shutil.copytree(arguments.target_prefix, guest_prefix, symlinks=True)
    guest_qualification = arguments.runtime_root / "opt/crossforge-qualification/python"
    cleanup_paths.append(guest_qualification)
    guest_qualification.mkdir(parents=True, exist_ok=False)
    shutil.copy2(arguments.extension, guest_qualification / arguments.extension.name)
    shutil.copy2(arguments.probe, guest_qualification / "runtime_probe.py")
    (arguments.runtime_root / "dev/shm").mkdir(parents=True, exist_ok=True)
    (arguments.runtime_root / "tmp").mkdir(parents=True, exist_ok=True)

    guest_python = str(arguments.target_prefix / "bin" / ("python" + minor))
    guest_probe = "/opt/crossforge-qualification/python/runtime_probe.py"
    guest_extension_dir = "/opt/crossforge-qualification/python"
    environment = {
        "HOME": "/tmp",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "TZ": "UTC0",
    }
    probe_policy_arguments = [
        "--gil-policy",
        contract["gil_policy"],
        "--zstd-policy",
        zstd_policy,
        "--hash-algorithm",
        contract["hash_algorithm"],
    ]
    if arguments.target == "x86_64-unknown-linux-gnu":
        require(arguments.qemu is None, "x86_64 runtime must not use QEMU")
        executor = {
            "kind": "native-chroot",
            "binary_sha256": None,
            "version": None,
            "cpu": None,
            "uname_release": None,
        }
        core_command = [
            "chroot",
            arguments.runtime_root,
            guest_python,
            "-B",
            "-I",
            "-S",
            guest_probe,
            "--mode",
            "core",
            "--target",
            arguments.target,
            "--version",
            arguments.version,
        ] + probe_policy_arguments + [
            "--extension-dir",
            guest_extension_dir,
        ]
        loader_command = [
            "chroot",
            arguments.runtime_root,
            loader_guest,
            "--list",
            guest_python,
        ]
        library_path = "%s/lib64:%s/usr/lib64" % (
            arguments.runtime_root,
            arguments.runtime_root,
        )
        device_command = [
            loader_host,
            "--library-path",
            library_path,
            host_python,
            "-B",
            "-I",
            "-S",
            arguments.probe,
            "--mode",
            "devices",
            "--target",
            arguments.target,
            "--version",
            arguments.version,
        ] + probe_policy_arguments
        device_loader_command = [
            loader_host,
            "--library-path",
            library_path,
            "--list",
            host_python,
        ]
    else:
        require(arguments.qemu is not None and arguments.qemu.is_file(), "locked QEMU is required")
        qemu_release = release["qemu"]
        qemu_policy = qemu_release["executor"]
        require(sha256_file(arguments.qemu) == qemu_policy["binary_sha256"], "QEMU digest mismatch")
        qemu_version_stdout, _ = run([arguments.qemu, "--version"])
        require(
            qemu_version_stdout.splitlines()
            and qemu_version_stdout.splitlines()[0].startswith(
                "qemu-aarch64 version " + qemu_release["version"]
            ),
            "QEMU version mismatch",
        )
        guest_qemu = "/.crossforge/qemu-aarch64"
        mounted_qemu = safe_root_path(arguments.runtime_root, guest_qemu)
        require(mounted_qemu.is_file(), "QEMU is not mounted in runtime root")
        require(
            sha256_file(mounted_qemu) == qemu_policy["binary_sha256"],
            "chroot QEMU digest mismatch",
        )
        executor = {
            "kind": "explicit-qemu",
            "binary_sha256": qemu_policy["binary_sha256"],
            "version": qemu_release["version"],
            "cpu": qemu_policy["cpu"],
            "uname_release": qemu_policy["uname_release"],
        }
        qemu_options = [
            guest_qemu,
            "-L",
            "/",
            "-cpu",
            qemu_policy["cpu"],
            "-r",
            qemu_policy["uname_release"],
        ]
        core_command = ["chroot", arguments.runtime_root] + qemu_options + [
            guest_python,
            "-B",
            "-I",
            "-S",
            guest_probe,
            "--mode",
            "core",
            "--target",
            arguments.target,
            "--version",
            arguments.version,
        ] + probe_policy_arguments + [
            "--extension-dir",
            guest_extension_dir,
        ]
        loader_command = ["chroot", arguments.runtime_root] + qemu_options + [
            "-E",
            "LD_TRACE_LOADED_OBJECTS=1",
            guest_python,
        ]
        library_path = "%s/lib64:%s/usr/lib64" % (
            arguments.runtime_root,
            arguments.runtime_root,
        )
        device_command = [
            arguments.qemu,
            "-L",
            arguments.runtime_root,
            "-cpu",
            qemu_policy["cpu"],
            "-r",
            qemu_policy["uname_release"],
            "-E",
            "LD_LIBRARY_PATH=" + library_path,
            host_python,
            "-B",
            "-I",
            "-S",
            arguments.probe,
            "--mode",
            "devices",
            "--target",
            arguments.target,
            "--version",
            arguments.version,
        ] + probe_policy_arguments
        device_loader_command = [
            arguments.qemu,
            "-L",
            arguments.runtime_root,
            "-cpu",
            qemu_policy["cpu"],
            "-r",
            qemu_policy["uname_release"],
            "-E",
            "LD_TRACE_LOADED_OBJECTS=1",
            "-E",
            "LD_LIBRARY_PATH=" + library_path,
            host_python,
        ]

    core_stdout, _ = run(core_command, environment)
    probe = parse_probe(core_stdout, "core probe")
    loader_stdout, loader_stderr = run(loader_command, environment)
    loader_text = loader_stdout + loader_stderr
    require("not found" not in loader_text, "runtime loader has unresolved dependencies")
    loader_dependencies = normalize_loader_listing(loader_text)
    require(loader_dependencies, "runtime loader evidence is empty")
    reject_dynamic_zstd(loader_dependencies, "runtime loader")
    device_loader_stdout, device_loader_stderr = run(device_loader_command, environment)
    device_loader_dependencies = validate_device_loader_listing(
        device_loader_stdout + device_loader_stderr,
        arguments.runtime_root,
    )
    device_environment = dict(environment)
    device_environment["LD_DEBUG"] = "libs"
    device_stdout, device_stderr = run(device_command, device_environment)
    device_probe = parse_probe(device_stdout, "device probe")
    device_loaded_objects = validate_device_loaded_objects(
        device_stderr,
        arguments.runtime_root,
        arguments.target_prefix,
        observed_provider_catalog,
        profile["interpreter"],
    )

    result = {
        "qualification_schema_version": 3,
        "report_kind": "crossforge-cpython-runtime",
        "target": arguments.target,
        "version": arguments.version,
        "adapter": contract["adapter"],
        "tier": arguments.tier,
        "status": "passed",
        "release_sha256": release_sha256,
        "compile_report_sha256": compile_report_sha256,
        "python_sha256": compile_report["python_sha256"],
        "extension_sha256": compile_report["extension"]["sha256"],
        "probe_sha256": sha256_file(arguments.probe),
        "runtime": {
            "kind": runtime_kind,
            "identity_sha256": identity_sha256,
            "os_release_sha256": sha256_file(os_release),
            "loader_sha256": sha256_file(loader_host),
            "overlay_evidence": runtime_evidence if arguments.tier == "clean-rocky" else None,
        },
        "executor": executor,
        "loader_dependencies": loader_dependencies,
        "device_loader_dependencies": device_loader_dependencies,
        "device_loaded_objects": device_loaded_objects,
        "runtime_providers": runtime_provider_evidence,
        "probe": probe,
        "device_probe": device_probe,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(arguments.output)
    print("qualified CPython runtime: %s %s" % (arguments.target, arguments.tier))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError_, KeyError, TypeError, ValueError, subprocess.TimeoutExpired) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
