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
RPM = runpy.run_path(str(REPOSITORY / "scripts/validate-rpm-lock.py"))
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
    "module_streams": ["nodejs:20", "python38:3.8"],
    "required_tools": [
        "bison",
        "flex",
        "gperf",
        "nodejs>=14",
        "perl",
        "pkg-config",
        "python>=3.8+html5lib",
    ],
    "python_module_bridge": {
        "interpreter": "/usr/bin/python3.8",
        "pythonpath": "/usr/lib/python3.6/site-packages",
        "providers": ["python38", "python3-html5lib"],
        "qualification": "import-html5lib-six-webencodings",
    },
}
SOURCE_DEPENDENCIES = [
    {
        "name": "ffmpeg",
        "component": "sources/ffmpeg",
        "usage": "host-and-target-qt-multimedia-backend-build",
    },
    {
        "name": "xcb-util-cursor",
        "component": "sources/xcb-util-cursor",
        "usage": "host-and-target-xcb-platform-plugin-build",
    }
]
PATCHES = [
    {
        "file": "patches/qt/0001-xnnpack-use-uint16-neon-fp16-load.patch",
        "scope": "target-builds",
        "sha256": "f206a8f686e9208c424f9b7a1a0aab960da4ffbc2cae112b6489408ac20953b3",
        "upstream": {
            "repository": "https://github.com/google/XNNPACK",
            "commit": "1b11a8b0620afe8c047304273674c4c57c289755",
            "url": (
                "https://github.com/google/XNNPACK/commit/"
                "1b11a8b0620afe8c047304273674c4c57c289755"
            ),
        },
    }
]
LOCKS = [
    ("host-qt-build", "config/rpm/host-qt-build-el8-x86_64.plan.json"),
    ("qt-target-x86_64", "config/rpm/qt-target-el8-x86_64.plan.json"),
    ("qt-target-aarch64", "config/rpm/qt-target-el8-aarch64.plan.json"),
]
LOCK_IDENTITIES = {
    "host-qt-build": ("host-qt-build", "x86_64", None),
    "qt-target-x86_64": (
        "qt-target",
        "x86_64",
        "x86_64-unknown-linux-gnu",
    ),
    "qt-target-aarch64": (
        "qt-target",
        "aarch64",
        "aarch64-unknown-linux-gnu",
    ),
}
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
        "egl",
        "fontconfig",
        "freetype",
        "gbm",
        "gui",
        "libinput",
        "libudev",
        "network",
        "opengl",
        "opengl-desktop",
        "openssl",
        "openssl-linked",
        "opensslv11",
        "pkg-config",
        "thread",
        "wayland",
        "widgets",
        "xcb",
        "xcb-xlib",
        "xkbcommon",
        "xkbcommon-x11",
        "xml",
    ],
    "qtmultimedia": ["ffmpeg", "pulseaudio"],
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
    "ffmpeg-7.1.1-lgpl-shared",
    "gnu-c++20",
]
TARGETS = [
    {
        "arch": "x86_64",
        "triple": "x86_64-unknown-linux-gnu",
        "sysroot_component": "rpm/sysroot-x86_64",
        "dependency_lock": "qt-target-x86_64",
    },
    {
        "arch": "aarch64",
        "triple": "aarch64-unknown-linux-gnu",
        "sysroot_component": "rpm/sysroot-aarch64",
        "dependency_lock": "qt-target-aarch64",
    },
]
ACCEPTANCE = {
    "required_modules_must_build": True,
    "required_features_must_be_enabled": True,
    "configure_errors_forbidden": True,
    "configure_warnings_reviewed": True,
    "host_target_version_match": True,
    "target_elf_audit": True,
    "artifacts_enter_sdk": False,
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
X86_ONLY_TARGET_PACKAGES = {
    "hwdata",
    "libpciaccess",
    "libpciaccess-devel",
}


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
    require(
        [
            {key: record[key] for key in ("name", "component", "usage")}
            for record in plan["source_dependencies"]
        ]
        == SOURCE_DEPENDENCIES,
        "Qt source dependency contract differs",
    )
    require(plan["patches"] == PATCHES, "Qt patch contract differs")
    for record in plan["patches"]:
        patch_path = REPOSITORY / record["file"]
        require(
            patch_path.is_file()
            and hashlib.sha256(patch_path.read_bytes()).hexdigest()
            == record["sha256"],
            "Qt patch bytes differ: %s" % record["file"],
        )
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
    for record in plan["locks"]:
        plan_path = REPOSITORY / record["plan_file"]
        rpm_plan = load_and_validate(
            plan_path, REPOSITORY / "config/schemas/rpm-plan.schema.json"
        )
        try:
            RPM["validate_plan_semantics"](rpm_plan)
        except RPM["ValidationError"] as error:
            raise ValidationError(str(error)) from error
        require(
            canonical_sha256(rpm_plan) == record["plan_sha256"],
            "Qt RPM plan digest differs: %s" % record["id"],
        )
        identity = rpm_plan["identity"]
        require(
            (
                identity["role"],
                identity["arch"],
                identity["target_triple"],
            )
            == LOCK_IDENTITIES[record["id"]],
            "Qt RPM plan identity differs: %s" % record["id"],
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


def validate_target_pair(transactions):
    targets = {
        transaction["identity"]["arch"]: transaction
        for transaction in transactions
        if transaction["identity"]["role"] == "qt-target"
    }
    require(
        set(targets) == {"x86_64", "aarch64"} and len(targets) == 2,
        "Qt target lock pair is incomplete",
    )

    def identities(transaction):
        records = [
            item for item in transaction["items"] if item["action"] != "remove"
        ]
        result = {
            item["name"]: (item["epoch"], item["version"], item["release"])
            for item in records
        }
        require(
            len(result) == len(records),
            "Qt target lock contains multiple packages with one name",
        )
        return result

    x86_64 = identities(targets["x86_64"])
    aarch64 = identities(targets["aarch64"])
    require(
        set(x86_64) - set(aarch64) == X86_ONLY_TARGET_PACKAGES,
        "Qt x86_64-only dependency set differs",
    )
    require(not set(aarch64) - set(x86_64), "Qt AArch64-only dependency appeared")
    require(
        all(x86_64[name] == aarch64[name] for name in set(x86_64) & set(aarch64)),
        "Qt target package EVRs differ across architectures",
    )
    return {
        "x86_64_packages": len(x86_64),
        "aarch64_packages": len(aarch64),
        "x86_64_only": sorted(X86_ONLY_TARGET_PACKAGES),
    }


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
    source_dependencies = [
        binding_component(binding, record["component"])
        for record in plan["source_dependencies"]
    ]
    require(
        all(
            record["canonical_sha256"] == component["canonical_sha256"]
            for record, component in zip(
                plan["source_dependencies"], source_dependencies
            )
        ),
        "Qt plan source dependency differs from release binding",
    )
    future = binding_component(binding, "future/qt-qualification")
    require(future["scope"] == "future", "Qt planned component is not future")
    locked_inputs = []
    locked_transactions = []
    if plan["status"] == "locked":
        for record in plan["locks"]:
            lock_path = REPOSITORY / record["lock_file"]
            lock = load_and_validate(
                lock_path, REPOSITORY / "config/schemas/rpm-lock.schema.json"
            )
            require(
                canonical_sha256(lock) == record["canonical_sha256"],
                "Qt RPM lock digest differs: %s" % record["id"],
            )
            try:
                transaction, _identity = RPM["validate_lock_binding"](
                    lock, lock_path, release_path=release_path
                )
            except RPM["ValidationError"] as error:
                raise ValidationError(str(error)) from error
            locked_inputs.append(lock)
            locked_transactions.append(transaction)
    target_pair = (
        validate_target_pair(locked_transactions)
        if plan["status"] == "locked"
        else None
    )
    if require_locked:
        require(contract["status"] == "locked", "release Qt status is not locked")
    return {
        "release": release,
        "plan": plan,
        "plan_sha256": plan_sha256,
        "source_component_sha256": source["canonical_sha256"],
        "source_dependencies": source_dependencies,
        "locked_inputs": locked_inputs,
        "locked_transactions": locked_transactions,
        "target_pair": target_pair,
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
