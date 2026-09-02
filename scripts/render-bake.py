#!/usr/bin/env python3
"""Render the checked-in Bake override from config/release.json."""

import argparse
import json
import runpy
import sys
from pathlib import Path


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
    cp313 = next(
        entry
        for entry in config["python"]["versions"]
        if entry["version"].startswith("3.13.")
    )
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
            "crossforge_qemu": "docker-image://%s" % qemu_image,
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
    if cp313["source"]["status"] == "locked":
        arguments.update({
            "CPYTHON_CP313_VERSION": cp313["version"],
            "CPYTHON_CP313_ADAPTER": cp313["adapter"],
        })
    common["args"] = arguments
    targets["_common"] = common
    document = {
        "group": {"toolchain-plan": {"targets": plan_names}},
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
