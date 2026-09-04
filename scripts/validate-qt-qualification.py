#!/usr/bin/env python3
"""Validate the release-bound Qt host/target qualification plan."""

import argparse
import hashlib
import json
import re
import runpy
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]
PLAN_SCHEMA = REPOSITORY / "config/schemas/qt-qualification-plan.schema.json"
MODULES = [
    "qtbase",
    "qtshadertools",
    "qtdeclarative",
    "qttools",
    "qtwayland",
    "qtmultimedia",
    "qtquick3d",
    "qtwebengine",
]
HOST = {
    "platform": "linux/amd64",
    "compiler": "native-gts15",
    "qt_host_path": "/opt/crossforge/qualification/qt/6.8.4/host",
    "cmake_version": "4.4.0",
    "ninja_version": "1.13.2",
    "required_tools": [
        "bison",
        "flex",
        "gperf",
        "nodejs>=14",
        "perl",
        "pkg-config",
        "python>=3.8+html5lib",
    ],
}
LOCKS = [
    ("host-qt-build", "config/rpm/host-qt-build-el8-x86_64.plan.json"),
    ("qt-target-x86_64", "config/rpm/qt-target-el8-x86_64.plan.json"),
    ("qt-target-aarch64", "config/rpm/qt-target-el8-aarch64.plan.json"),
]
BUILD = {
    "configuration": "release",
    "shared": True,
    "developer_build": False,
    "examples": False,
    "tests": False,
    "documentation": False,
    "commercial": False,
    "confirm_license": True,
    "target_execution": "forbidden",
    "output": "qualification-only",
}
FEATURES = {
    "qtbase": [
        "c++20",
        "cross_compile",
        "dbus",
        "gui",
        "network",
        "openssl",
        "openssl-linked",
        "opensslv11",
        "pkg-config",
        "thread",
        "widgets",
        "xml",
    ],
    "qtwebengine": [
        "qtwebengine-build",
        "qtwebengine-core-build",
        "qtwebengine-quick-build",
        "qtwebengine-widgets-build",
        "webengine-build-gn",
        "webengine-build-ninja",
        "webengine-ozone-x11",
    ],
}
SUPPORT_CHECKS = [
    "supported-linux-cross-architecture",
    "non-static-webengine",
    "nodejs>=14-64-bit",
    "python>=3.8",
    "python-html5lib",
    "gperf",
    "bison",
    "flex",
    "pkg-config",
    "glibc>=2.17",
    "khronos-development-headers",
    "fontconfig",
    "nss>=3.26",
    "dbus",
    "gnu-c++20",
]
TARGETS = [
    {
        "arch": "x86_64",
        "triple": "x86_64-unknown-linux-gnu",
        "sysroot_component": "rpm/sysroot-x86_64",
        "dependency_lock": "qt-target-x86_64",
        "runtime_tiers": ["clean-rocky"],
    },
    {
        "arch": "aarch64",
        "triple": "aarch64-unknown-linux-gnu",
        "sysroot_component": "rpm/sysroot-aarch64",
        "dependency_lock": "qt-target-aarch64",
        "runtime_tiers": ["clean-rocky-qemu", "native-release"],
    },
]
ACCEPTANCE = {
    "required_modules_must_build": True,
    "required_features_must_be_enabled": True,
    "configure_errors_forbidden": True,
    "configure_warnings_reviewed": True,
    "host_target_version_match": True,
    "target_elf_audit": True,
    "runtime_smoke": True,
    "artifacts_enter_sdk": False,
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def canonical_sha256(document):
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_and_validate(path, schema_path):
    document = STRICT["load_json"](path)
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    return document


def validate_plan(plan, require_locked=False):
    require(plan["profile"] == "linux-desktop-full", "Qt profile differs")
    require(plan["qt_version"] == "6.8.4", "Qt plan version differs")
    require(plan["host"] == HOST, "Qt host contract differs")
    require(plan["modules"] == MODULES, "Qt module order or set differs")
    require(plan["build"] == BUILD, "Qt build contract differs")
    require(plan["required_features"] == FEATURES, "Qt feature contract differs")
    require(
        plan["required_support_checks"] == SUPPORT_CHECKS,
        "Qt support-check contract differs",
    )
    require(plan["targets"] == TARGETS, "Qt target/runtime contract differs")
    require(plan["acceptance"] == ACCEPTANCE, "Qt acceptance contract differs")
    require(
        [(record["id"], record["plan_file"]) for record in plan["locks"]]
        == LOCKS,
        "Qt lock order or plan paths differ",
    )
    statuses = [record["status"] for record in plan["locks"]]
    if plan["status"] == "planned":
        require(
            statuses == ["pending"] * len(LOCKS)
            and all(record["lock_file"] is None for record in plan["locks"])
            and all(
                record["canonical_sha256"] is None for record in plan["locks"]
            ),
            "planned Qt qualification must expose only pending locks",
        )
    else:
        require(statuses == ["locked"] * len(LOCKS), "Qt locks are incomplete")
        for record in plan["locks"]:
            require(
                isinstance(record["lock_file"], str)
                and record["lock_file"].startswith("locks/")
                and isinstance(record["canonical_sha256"], str)
                and SHA256_RE.match(record["canonical_sha256"]),
                "locked Qt input identity is invalid",
            )
    if require_locked:
        require(plan["status"] == "locked", "Qt qualification is still planned")
    return plan


def binding_component(binding, name):
    matches = [
        record for record in binding["components"] if record["component"] == name
    ]
    require(len(matches) == 1, "release binding has no unique %s" % name)
    return matches[0]


def validate_release_contract(release_path, require_locked=False):
    release = load_and_validate(
        release_path, REPOSITORY / "config/schemas/release.schema.json"
    )
    contract = release["qt"]["qualification"]
    plan_path = REPOSITORY / contract["plan"]["file"]
    require(
        plan_path.resolve() == (REPOSITORY / "config/qt-qualification.json").resolve(),
        "Qt plan path differs",
    )
    plan = validate_plan(load_and_validate(plan_path, PLAN_SCHEMA), require_locked)
    plan_sha256 = canonical_sha256(plan)
    require(
        contract["plan"]["canonical_sha256"] == plan_sha256,
        "release Qt plan digest differs",
    )
    require(contract["status"] == plan["status"], "Qt release/plan status differs")
    binding = load_and_validate(
        REPOSITORY / "config/generated/release-binding.json",
        REPOSITORY / "config/schemas/release-binding.schema.json",
    )
    source = binding_component(binding, "sources/qt")
    require(
        plan["source_component"]
        == {
            "component": "sources/qt",
            "canonical_sha256": source["canonical_sha256"],
        },
        "Qt plan source component differs from release binding",
    )
    future = binding_component(binding, "future/qt-qualification")
    require(future["scope"] == "future", "Qt planned component is not future")
    if require_locked:
        require(contract["status"] == "locked", "release Qt status is not locked")
    return {
        "release": release,
        "plan": plan,
        "plan_sha256": plan_sha256,
        "source_component_sha256": source["canonical_sha256"],
        "qualification_component": future,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--release",
        type=Path,
        default=REPOSITORY / "config/release.json",
    )
    parser.add_argument("--require-locked", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        result = validate_release_contract(
            arguments.release, require_locked=arguments.require_locked
        )
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "valid Qt qualification plan: %s (%s; 8 modules; 2 targets)"
        % (result["plan_sha256"], result["plan"]["status"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
