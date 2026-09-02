#!/usr/bin/env python3
"""Render the checked-in Bake override from config/release.json."""

import argparse
import json
import runpy
import sys
from pathlib import Path


# Pipeline enablement is implementation state, not source metadata. Exact patch
# versions and adapters are always selected from release.json below. Extend this
# tuple only after the corresponding adapter has a qualified vertical slice.
PYTHON_PIPELINE_MINORS = ("3.13", "3.11")
PYTHON_TARGETS = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}


def python_row(entry):
    version = entry["version"]
    minor = version.rsplit(".", 1)[0]
    return {
        "row": "cp" + minor.replace(".", ""),
        "minor": minor,
        "version": version,
        "adapter": entry["adapter"],
    }


def cacheonly_python_target(target, row, contexts=None, extra_args=None):
    arguments = {
        "CPYTHON_ROW": row["row"],
        "CPYTHON_VERSION": row["version"],
        "CPYTHON_ADAPTER": row["adapter"],
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


def render_python_graph(config, targets):
    entries = config["python"]["versions"]
    rows = []
    for minor in PYTHON_PIPELINE_MINORS:
        matches = [
            python_row(entry)
            for entry in entries
            if entry["version"].rsplit(".", 1)[0] == minor
        ]
        if len(matches) != 1:
            raise ValueError("release must select one CPython %s row" % minor)
        row = matches[0]
        source = next(
            entry["source"]
            for entry in entries
            if entry["version"] == row["version"]
        )
        if source["status"] != "locked":
            raise ValueError("enabled CPython row is not source-locked: %s" % minor)
        rows.append(row)

    release_targets = {
        item["arch"]: item["triple"] for item in config["targets"]
    }
    if release_targets != PYTHON_TARGETS:
        raise ValueError("release targets differ from the Python matrix contract")

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
            {"crossforge_config": "target:validate"},
        )
        targets[prepared_name] = cacheonly_python_target(
            "cpython-prepared",
            row,
            {
                "crossforge_host_python": "target:host-python-build-locked",
                "crossforge_cpython_source": "target:%s" % source_name,
            },
        )
        targets[build_name] = cacheonly_python_target(
            "cpython-build",
            row,
            {"crossforge_cpython_prepared": "target:%s" % prepared_name},
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
                    "crossforge_toolchain": "target:toolchain-%s-dev" % arch,
                },
                target_args,
            )
            targets[qualify_build_name] = cacheonly_python_target(
                "cpython-qualify-build",
                row,
                {"crossforge_cpython_cross": "target:%s" % cross_name},
                target_args,
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

    aggregate_base = "sdk-toolchains-dev"
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
    targets["python-dev"] = {
        "inherits": ["_python_common"],
        "target": "python-sdk-final",
        "args": {
            "CROSSFORGE_PYTHON_ROWS": " ".join(row["row"] for row in rows)
        },
        "contexts": {"crossforge_sdk_base": "target:%s" % aggregate_base},
        "output": ["type=cacheonly"],
    }
    groups["python-matrix"] = {"targets": ["python-dev"]}
    groups["phase6"] = {
        "targets": [
            "validate",
            "platform-python-check",
            "host-python-build-locked",
            "python-runtime-clean-x86_64",
            "python-runtime-clean-aarch64",
            "cpython-cp311-x86_64-qualify",
            "cpython-cp311-aarch64-qualify",
            "python-dev",
        ]
    }
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
    python_groups = render_python_graph(config, targets)
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
