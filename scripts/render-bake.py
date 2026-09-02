#!/usr/bin/env python3
"""Render the checked-in Bake override from config/release.json."""

import argparse
import json
import re
import runpy
import sys
from pathlib import Path


ROW_CONTRACT = runpy.run_path(
    str(Path(__file__).with_name("python_row_contract.py"))
)
ContractError = ROW_CONTRACT["ContractError"]
IMPLEMENTED_ROWS = ROW_CONTRACT["IMPLEMENTED_ROWS"]
LATEST_PHASE = ROW_CONTRACT["LATEST_PHASE"]
bind_python_row = ROW_CONTRACT["bind_release"]
rows_for_phase = ROW_CONTRACT["rows_for_phase"]

PYTHON_TARGETS = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
COMPONENT_ARGUMENT_RE = re.compile(
    r"^CROSSFORGE_COMPONENT_[A-Z0-9_]+_SHA256\Z"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")


def component_argument_name(component):
    if not isinstance(component, str) or not component:
        raise ValueError("component argument identity must be non-empty text")
    normalized = component.upper().replace("/", "_").replace("-", "_")
    name = "CROSSFORGE_COMPONENT_%s_SHA256" % normalized
    if COMPONENT_ARGUMENT_RE.match(name) is None:
        raise ValueError("component has no safe Bake argument name: %r" % component)
    return name


def component_digest_arguments(repository, release, require_tracked=True):
    """Derive validated per-component identities without a global digest."""
    renderer = runpy.run_path(
        str(repository / "scripts/render-release-components.py")
    )
    try:
        documents = renderer["render_documents"](
            repository=repository,
            release=release,
            implemented_rows=IMPLEMENTED_ROWS,
        )
        if require_tracked:
            drift = renderer["output_drift"](repository, documents)
            if drift:
                raise ValueError(
                    "release component projections are stale: %s"
                    % "; ".join(drift)
                )
    except renderer["ProjectionError"] as error:
        raise ValueError(str(error)) from error

    binding = documents[renderer["BINDING_PATH"]]
    component_documents = {
        document["component"]: document
        for document in documents.values()
        if document.get("kind") == "crossforge-release-component"
    }
    records = binding["components"]
    if {record["component"] for record in records} != set(component_documents):
        raise ValueError("release binding does not cover every component")

    arguments = {}
    owners = {}
    for record in records:
        component = record["component"]
        name = component_argument_name(component)
        if name in arguments:
            raise ValueError(
                "component Bake argument collision: %s and %s"
                % (owners[name], component)
            )
        digest = record["canonical_sha256"]
        if not isinstance(digest, str) or SHA256_RE.match(digest) is None:
            raise ValueError("component has an invalid canonical digest: %s" % component)
        expected = renderer["canonical_sha256"](component_documents[component])
        if digest != expected:
            raise ValueError("component binding digest differs: %s" % component)
        owners[name] = component
        arguments[name] = digest
    return arguments


def render_zstd_graph(config, targets, component_arguments, rocky_amd64_image):
    version = config["python"]["zstd"]["version"]

    def digest(component):
        name = component_argument_name(component)
        try:
            return component_arguments[name]
        except KeyError as error:
            raise ValueError("missing zstd component digest: %s" % component) from error

    common_args = {
        "ZSTD_VERSION": version,
        "ZSTD_SOURCE_COMPONENT_SHA256": digest("sources/zstd"),
        "ZSTD_BUILD_POLICY_COMPONENT_SHA256": digest(
            "implementation/zstd-build-policy"
        ),
    }
    targets["zstd-source"] = {
        "inherits": ["_zstd_common"],
        "target": "zstd-source",
        "args": {
            "ZSTD_VERSION": version,
            "ZSTD_SOURCE_COMPONENT_SHA256": common_args[
                "ZSTD_SOURCE_COMPONENT_SHA256"
            ],
        },
        "contexts": {
            "crossforge_rocky_amd64": "docker-image://%s" % rocky_amd64_image
        },
        "output": ["type=cacheonly"],
    }
    host_args = dict(common_args)
    host_args["ZSTD_BUILD_COMPONENT_SHA256"] = digest("zstd/host-build")
    targets["zstd-host-build"] = {
        "inherits": ["_zstd_common"],
        "target": "zstd-host-build-export",
        "args": host_args,
        "contexts": {
            "crossforge_host_common": "target:host-build-common-locked",
            "crossforge_zstd_source": "target:zstd-source",
        },
        "output": ["type=cacheonly"],
    }
    for arch, triple in PYTHON_TARGETS.items():
        build_component = "zstd/%s-build" % arch
        arguments = dict(common_args)
        arguments.update(
            {
                "ZSTD_TARGET_ARCH": arch,
                "ZSTD_TARGET_TRIPLE": triple,
                "ZSTD_BUILD_COMPONENT": build_component,
                "ZSTD_BUILD_COMPONENT_SHA256": digest(build_component),
            }
        )
        targets["zstd-%s-build" % arch] = {
            "inherits": ["_zstd_common"],
            "target": "zstd-target-build-export",
            "args": arguments,
            "contexts": {
                "crossforge_host_common": "target:host-build-common-locked",
                "crossforge_zstd_source": "target:zstd-source",
                "crossforge_toolchain": (
                    "target:toolchain-%s-build-export" % arch
                ),
            },
            "output": ["type=cacheonly"],
        }


def python_row(contract, entry):
    version = entry["version"]
    return {
        "row": contract["row"],
        "minor": contract["minor"],
        "version": version,
        "adapter": contract["adapter"],
        "zstd": contract["zstd"],
        "introduced_phase": contract["introduced_phase"],
    }


def cacheonly_python_target(target, row, contexts=None, extra_args=None):
    arguments = {
        "CPYTHON_ROW": row["row"],
        "CPYTHON_MINOR": row["minor"],
        "CPYTHON_VERSION": row["version"],
        "CPYTHON_ADAPTER": row["adapter"],
        "CPYTHON_SOURCE_COMPONENT": row["source_component"],
        "CPYTHON_SOURCE_COMPONENT_SHA256": row["source_component_sha256"],
        "CPYTHON_BUILD_POLICY_COMPONENT": row["build_policy_component"],
        "CPYTHON_BUILD_POLICY_COMPONENT_SHA256": row[
            "build_policy_component_sha256"
        ],
    }
    if extra_args:
        arguments.update(extra_args)
    result = {
        "inherits": ["_python_common"],
        "target": target,
        "args": arguments,
        "output": ["type=cacheonly"],
    }
    if contexts:
        result["contexts"] = contexts
    return result


def render_python_graph(config, targets, component_arguments):
    rows = []
    zstd_version = config["python"]["zstd"]["version"]
    qualification_arguments = {}
    for component in (
        "implementation/python-qualification-policy",
        "python/qualification",
    ):
        argument = component_argument_name(component)
        try:
            qualification_arguments[argument] = component_arguments[argument]
        except KeyError as error:
            raise ValueError(
                "missing Python qualification component digest: %s"
                % component
            ) from error
    for record in IMPLEMENTED_ROWS:
        try:
            binding = bind_python_row(config, row=record["row"])
        except ContractError as error:
            raise ValueError(str(error)) from error
        row = python_row(binding["contract"], binding["entry"])
        source_component = "python/%s-source" % row["row"]
        source_argument = component_argument_name(source_component)
        try:
            source_digest = component_arguments[source_argument]
        except KeyError as error:
            raise ValueError(
                "Python row lacks a source component digest: %s" % row["row"]
            ) from error
        row["source_component"] = source_component
        row["source_component_sha256"] = source_digest
        build_policy_component = (
            "implementation/python-%s-build-policy" % row["row"]
        )
        build_policy_argument = component_argument_name(
            build_policy_component
        )
        try:
            build_policy_digest = component_arguments[build_policy_argument]
        except KeyError as error:
            raise ValueError(
                "Python row lacks a build-policy component digest: %s"
                % row["row"]
            ) from error
        row["build_policy_component"] = build_policy_component
        row["build_policy_component_sha256"] = build_policy_digest
        source = binding["entry"]["source"]
        if source["status"] != "locked":
            raise ValueError(
                "enabled CPython row is not source-locked: %s" % row["minor"]
            )
        patches = binding["entry"].get("patches", [])
        if not isinstance(patches, list):
            raise ValueError("Python row patch list is not an array")
        expected_prefix = "patches/cpython/%s/" % row["minor"]
        if any(
            not isinstance(patch, dict)
            or not isinstance(patch.get("file"), str)
            or not patch["file"].startswith(expected_prefix)
            for patch in patches
        ):
            raise ValueError(
                "Python row patch escapes its minor context: %s" % row["row"]
            )
        row["patch_context_target"] = (
            "cpython-patches-%s" % row["row"] if patches else None
        )
        row["patch_context"] = "target:%s" % (
            row["patch_context_target"] or "cpython-empty-patches"
        )
        row["zstd_version"] = zstd_version if row["zstd"] else "none"
        rows.append(row)

    phase_order = [row["introduced_phase"] for row in rows]
    if phase_order != sorted(phase_order):
        raise ValueError("Python rows must be introduced in append-only phase order")

    release_targets = {
        item["arch"]: item["triple"] for item in config["targets"]
    }
    if release_targets != PYTHON_TARGETS:
        raise ValueError("release targets differ from the Python matrix contract")

    base = config["base_image"]
    rocky_amd64_context = "docker-image://%s:%s@%s" % (
        base["repository"],
        base["tag"],
        base["manifests"]["amd64"],
    )

    targets["cpython-empty-patches"] = {
        "inherits": ["_python_common"],
        "target": "cpython-empty-patches",
        "contexts": {"crossforge_rocky_amd64": rocky_amd64_context},
        "output": ["type=cacheonly"],
    }
    targets["zstd-empty"] = {
        "inherits": ["_python_common"],
        "target": "zstd-empty",
        "contexts": {"crossforge_rocky_amd64": rocky_amd64_context},
        "output": ["type=cacheonly"],
    }
    for row in rows:
        if row["patch_context_target"] is None:
            continue
        targets[row["patch_context_target"]] = {
            "inherits": ["_python_common"],
            "target": "cpython-patch-context",
            "contexts": {
                "crossforge_cpython_patch_files": "patches/cpython/%s"
                % row["minor"]
            },
            "output": ["type=cacheonly"],
        }

    groups = {}
    for row in rows:
        row_name = row["row"]
        source_name = "cpython-source-%s" % row_name
        prepared_name = "cpython-prepared-%s" % row_name
        build_name = "cpython-build-%s" % row_name
        export_name = "python-row-%s" % row_name
        dev_name = "python-%s-dev" % row_name

        targets[source_name] = cacheonly_python_target(
            "cpython-source",
            row,
            {"crossforge_rocky_amd64": rocky_amd64_context},
        )
        targets[prepared_name] = cacheonly_python_target(
            "cpython-prepared",
            row,
            {
                "crossforge_host_python": "target:host-python-build-locked",
                "crossforge_cpython_source": "target:%s" % source_name,
                "crossforge_cpython_patches": row["patch_context"],
            },
        )
        targets[build_name] = cacheonly_python_target(
            "cpython-build",
            row,
            {
                "crossforge_cpython_prepared": "target:%s" % prepared_name,
                "crossforge_zstd": (
                    "target:zstd-host-build"
                    if row["zstd"]
                    else "target:zstd-empty"
                ),
            },
            {"CPYTHON_ZSTD_VERSION": row["zstd_version"]},
        )

        qualification_names = []
        final_qualification = {}
        for arch, triple in PYTHON_TARGETS.items():
            cross_name = "cpython-cross-%s-%s" % (row_name, arch)
            qualify_build_name = "cpython-%s-%s-qualify-build" % (
                row_name,
                arch,
            )
            qualify_name = "cpython-%s-%s-qualify" % (row_name, arch)
            target_args = {
                "CROSSFORGE_TARGET_ARCH": arch,
                "CROSSFORGE_TARGET_TRIPLE": triple,
            }
            targets[cross_name] = cacheonly_python_target(
                "cpython-cross",
                row,
                {
                    "crossforge_host_python": "target:host-python-build-locked",
                    "crossforge_cpython_prepared": "target:%s" % prepared_name,
                    "crossforge_cpython_build": "target:%s" % build_name,
                    "crossforge_toolchain": (
                        "target:toolchain-%s-build-export" % arch
                    ),
                    "crossforge_zstd": (
                        "target:zstd-%s-build" % arch
                        if row["zstd"]
                        else "target:zstd-empty"
                    ),
                },
                dict(
                    target_args,
                    CPYTHON_ZSTD_VERSION=row["zstd_version"],
                ),
            )
            targets[qualify_build_name] = cacheonly_python_target(
                "cpython-qualify-build",
                row,
                {"crossforge_cpython_cross": "target:%s" % cross_name},
                dict(target_args, **qualification_arguments),
            )
            runtime_contexts = {
                "crossforge_host_python": "target:host-python-build-locked",
                "crossforge_cpython_qualify_build": (
                    "target:%s" % qualify_build_name
                ),
                "crossforge_sysroot": "target:sysroot-%s" % arch,
                "crossforge_clean_runtime": (
                    "target:python-runtime-clean-%s" % arch
                ),
            }
            if arch == "aarch64":
                runtime_contexts["crossforge_qemu_validated"] = (
                    "target:qemu-aarch64-validated"
                )
            targets[qualify_name] = cacheonly_python_target(
                "cpython-qualify-%s" % arch,
                row,
                runtime_contexts,
                target_args,
            )
            qualification_names.append(qualify_name)
            final_qualification[arch] = qualify_name

        targets[export_name] = cacheonly_python_target(
            "cpython-row-export",
            row,
            {
                "crossforge_host_python": "target:host-python-build-locked",
                "crossforge_cpython_build": "target:%s" % build_name,
                "crossforge_cpython_x86_64": (
                    "target:%s" % final_qualification["x86_64"]
                ),
                "crossforge_cpython_aarch64": (
                    "target:%s" % final_qualification["aarch64"]
                ),
            },
        )
        targets[dev_name] = cacheonly_python_target(
            "python-sdk-append",
            row,
            {
                "crossforge_sdk_base": "target:sdk-toolchains-dev",
                "crossforge_python_row": "target:%s" % export_name,
            },
        )
        groups["python-%s" % row_name] = {
            "targets": [prepared_name, build_name]
            + qualification_names
            + [export_name, dev_name]
        }

    targets["sdk-toolchains-dev"] = {
        "inherits": ["_python_common"],
        "target": "sdk-toolchains-dev",
        "contexts": {
            "crossforge_host_python": "target:host-python-build-locked",
            "crossforge_toolchain_x86_64": "target:toolchain-x86_64-dev",
            "crossforge_toolchain_aarch64": "target:toolchain-aarch64-dev",
        },
        "output": ["type=cacheonly"],
    }

    rows_by_name = {row["row"]: row for row in rows}
    aggregate_base = "sdk-toolchains-dev"
    append_targets = {}
    for row in rows:
        append_name = "python-dev-append-%s" % row["row"]
        targets[append_name] = cacheonly_python_target(
            "python-sdk-append",
            row,
            {
                "crossforge_sdk_base": "target:%s" % aggregate_base,
                "crossforge_python_row": "target:python-row-%s" % row["row"],
            },
        )
        aggregate_base = append_name
        append_targets[row["row"]] = append_name

    introduced_phases = sorted(
        {row["introduced_phase"] for row in rows}
    )
    for phase in introduced_phases:
        phase_row_names = rows_for_phase(phase)
        if not phase_row_names:
            raise ValueError("Python phase %d has no rows" % phase)
        try:
            phase_rows = [rows_by_name[name] for name in phase_row_names]
        except KeyError as error:
            raise ValueError(
                "Python phase %d references an unknown row: %s"
                % (phase, error.args[0])
            ) from error
        native_targets = [
            "cpython-build-%s" % row["row"] for row in phase_rows
        ]
        groups["python-native-phase%d" % phase] = {
            "targets": native_targets
        }
        snapshot_name = "python-phase%d-dev" % phase
        snapshot_base = append_targets[phase_rows[-1]["row"]]
        targets[snapshot_name] = {
            "inherits": ["_python_common"],
            "target": "python-sdk-final",
            "args": {
                "CROSSFORGE_PYTHON_ROWS": " ".join(
                    row["row"] for row in phase_rows
                )
            },
            "contexts": {
                "crossforge_sdk_base": "target:%s" % snapshot_base
            },
            "output": ["type=cacheonly"],
        }
        qualification_targets = [
            "cpython-%s-%s-qualify" % (row["row"], arch)
            for row in phase_rows
            for arch in PYTHON_TARGETS
        ]
        groups["phase%d" % phase] = {
            "targets": [
                "validate",
                "platform-python-check",
                "host-python-build-locked",
                *native_targets,
                "python-runtime-clean-x86_64",
                "python-runtime-clean-aarch64",
                *qualification_targets,
                snapshot_name,
            ]
        }

    latest_row_names = rows_for_phase(LATEST_PHASE)
    if tuple(row["row"] for row in rows) != latest_row_names:
        raise ValueError("latest Python phase differs from implemented row order")
    groups["python-native-latest"] = {
        "targets": ["cpython-build-%s" % row for row in latest_row_names]
    }
    targets["python-dev"] = {
        "inherits": ["_python_common"],
        "target": "python-sdk-final",
        "args": {
            "CROSSFORGE_PYTHON_ROWS": " ".join(latest_row_names)
        },
        "contexts": {"crossforge_sdk_base": "target:%s" % aggregate_base},
        "output": ["type=cacheonly"],
    }
    groups["python-matrix"] = {"targets": ["python-dev"]}
    return groups


def render(repository):
    validator = runpy.run_path(str(repository / "scripts/validate-release.py"))
    load_json = validator["load_json"]
    validate = validator["validate"]
    validate_schema_subset = validator["validate_schema_subset"]

    config_path = repository / "config/release.json"
    schema_path = repository / "config/schemas/release.schema.json"
    config = load_json(config_path)
    schema = load_json(schema_path)
    validate_schema_subset(schema)
    validate(config, schema, schema, "$")

    base = config["base_image"]
    rocky_amd64_image = "%s:%s@%s" % (
        base["repository"],
        base["tag"],
        base["manifests"]["amd64"],
    )
    rocky_arm64_image = "%s:%s@%s" % (
        base["repository"],
        base["tag"],
        base["manifests"]["arm64"],
    )
    qemu = config["qemu"]
    qemu_executor = qemu["executor"]
    qemu_image = "%s:%s@%s" % (
        qemu_executor["repository"],
        qemu_executor["tag"],
        qemu_executor["manifest_digest"],
    )
    platform = config["platforms"]["image"]
    targets = {}
    plan_names = []
    for target in config["targets"]:
        name = "toolchain-plan-%s" % target["arch"]
        plan_names.append(name)
        targets[name] = {
            "inherits": ["_common"],
            "target": "toolchain-plan",
            "args": {"CROSSFORGE_TARGET_ARCH": target["arch"]},
            "output": ["type=cacheonly"],
        }

    common = {
        "contexts": {
            "crossforge_rocky_amd64": "docker-image://%s" % rocky_amd64_image,
            "crossforge_rocky_arm64": "docker-image://%s" % rocky_arm64_image,
        },
        "platforms": [platform],
    }
    component_arguments = component_digest_arguments(repository, config)
    arguments = {
        "ROCKY_RPM_TRUST_FINGERPRINT": config["trust"]["rocky_rpm_key"][
            "fingerprint"
        ],
        "ROCKY_RPM_TRUST_SHA256": config["trust"]["rocky_rpm_key"]["sha256"],
        "ROCKY_AMD64_MANIFEST_DIGEST": base["manifests"]["amd64"],
        "ROCKY_ARM64_MANIFEST_DIGEST": base["manifests"]["arm64"],
        "QEMU_EXECUTOR_VERSION": qemu["version"],
        "QEMU_EXECUTOR_BINARY_SHA256": qemu_executor["binary_sha256"],
        "QEMU_EXECUTOR_CPU": qemu_executor["cpu"],
        "QEMU_EXECUTOR_UNAME_RELEASE": qemu_executor["uname_release"],
    }
    arguments.update(component_arguments)
    if (
        config["gts"]["source"]["status"] == "locked"
        and config["binutils"]["source"]["status"] == "locked"
    ):
        arguments.update({
            "GTS_BINUTILS_HEADER_ARCH": config["binutils"]["source"]["header_arch"],
            "GTS_BINUTILS_REPOSITORY_NEVRA": config["binutils"]["source"][
                "repository_nevra"
            ],
            "GTS_BINUTILS_SHA256": config["binutils"]["source"]["sha256"],
            "GTS_BINUTILS_SPEC_SHA256": config["binutils"]["source"][
                "spec_sha256"
            ],
            "GTS_GCC_HEADER_ARCH": config["gts"]["source"]["header_arch"],
            "GTS_GCC_REPOSITORY_NEVRA": config["gts"]["source"][
                "repository_nevra"
            ],
            "GTS_GCC_SHA256": config["gts"]["source"]["sha256"],
            "GTS_GCC_SPEC_SHA256": config["gts"]["source"]["spec_sha256"],
        })
    common["args"] = arguments
    targets["_common"] = common
    for name in (
        "qemu-aarch64-validated",
        "runtime-smoke-aarch64",
        "toolchain-aarch64-dev",
    ):
        targets[name] = {
            "contexts": {"crossforge_qemu": "docker-image://%s" % qemu_image}
        }
    render_zstd_graph(config, targets, component_arguments, rocky_amd64_image)
    python_groups = render_python_graph(config, targets, component_arguments)
    document = {
        "group": {
            "toolchain-plan": {"targets": plan_names},
            **python_groups,
        },
        "target": targets,
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main():
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=repository / "docker-bake.override.json",
    )
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    expected = render(repository)

    if arguments.check:
        try:
            actual = arguments.output.read_text(encoding="utf-8")
        except OSError as error:
            print("error: %s" % error, file=sys.stderr)
            return 1
        if actual != expected:
            print(
                "error: %s is stale; run scripts/render-bake.py" % arguments.output,
                file=sys.stderr,
            )
            return 1
        print("valid: %s is generated from config/release.json" % arguments.output)
        return 0

    arguments.output.write_text(expected, encoding="utf-8")
    print("wrote: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
