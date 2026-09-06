#!/usr/bin/env python3
"""Validate the release-bound Qt runtime qualification contract."""

import argparse
import hashlib
import json
import runpy
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
RPM = runpy.run_path(str(REPOSITORY / "scripts/validate-rpm-lock.py"))
BUILD = runpy.run_path(str(REPOSITORY / "scripts/validate-qt-qualification.py"))
COMPONENT = runpy.run_path(str(REPOSITORY / "scripts/release_component.py"))
ValidationError = STRICT["ValidationError"]
PLAN_SCHEMA = REPOSITORY / "config/schemas/qt-runtime-qualification-plan.schema.json"
LOCKS = [
    ("qt-runtime-x86_64", "config/rpm/qt-runtime-el8-x86_64.plan.json"),
    ("qt-runtime-aarch64", "config/rpm/qt-runtime-el8-aarch64.plan.json"),
]
LOCK_IDENTITIES = {
    "qt-runtime-x86_64": (
        "qt-runtime",
        "x86_64",
        "x86_64-unknown-linux-gnu",
    ),
    "qt-runtime-aarch64": (
        "qt-runtime",
        "aarch64",
        "aarch64-unknown-linux-gnu",
    ),
}
TARGETS = [
    {
        "arch": "x86_64",
        "triple": "x86_64-unknown-linux-gnu",
        "runtime_tiers": [
            {"tier": "clean-rocky", "executor": "native"}
        ],
    },
    {
        "arch": "aarch64",
        "triple": "aarch64-unknown-linux-gnu",
        "runtime_tiers": [
            {"tier": "clean-rocky-qemu", "executor": "explicit-qemu"},
            {"tier": "native-release", "executor": "native"},
        ],
    },
]
ACCEPTANCE = {
    "clean_rocky_runtime": True,
    "offscreen_widget": True,
    "platform_plugin_load": True,
    "plugin_dependency_closure": True,
    "dependency_closure": True,
    "native_aarch64_release": True,
    "artifacts_enter_sdk": False,
}
X86_ONLY_PACKAGES = {"hwdata", "libpciaccess"}


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


def validate_runtime_pair(transactions):
    runtimes = {
        transaction["identity"]["arch"]: transaction
        for transaction in transactions
        if transaction["identity"]["role"] == "qt-runtime"
    }
    require(
        set(runtimes) == {"x86_64", "aarch64"} and len(runtimes) == 2,
        "Qt runtime lock pair is incomplete",
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
            "Qt runtime lock contains multiple packages with one name",
        )
        return result

    x86_64 = identities(runtimes["x86_64"])
    aarch64 = identities(runtimes["aarch64"])
    require(
        set(x86_64) - set(aarch64) == X86_ONLY_PACKAGES,
        "Qt runtime x86_64-only dependency set differs",
    )
    require(
        not set(aarch64) - set(x86_64),
        "Qt runtime AArch64-only dependency appeared",
    )
    require(
        all(x86_64[name] == aarch64[name] for name in set(x86_64) & set(aarch64)),
        "Qt runtime package EVRs differ across architectures",
    )
    return {
        "x86_64_packages": len(x86_64),
        "aarch64_packages": len(aarch64),
        "x86_64_only": sorted(X86_ONLY_PACKAGES),
    }


def validate_plan(plan):
    require(plan["qt_version"] == "6.8.4", "Qt runtime version differs")
    build = BUILD["validate_plan"](
        load_and_validate(
            REPOSITORY / "config/qt-qualification.json",
            REPOSITORY / "config/schemas/qt-qualification-plan.schema.json",
        ),
        require_locked=True,
    )
    require(
        plan["build_qualification"]
        == {
            "component": "future/qt-qualification",
            "plan_file": "config/qt-qualification.json",
            "plan_sha256": canonical_sha256(build),
        },
        "Qt runtime/build qualification binding differs",
    )
    require(plan["targets"] == TARGETS, "Qt runtime target tiers differ")
    require(plan["acceptance"] == ACCEPTANCE, "Qt runtime acceptance differs")
    require(
        [(record["id"], record["plan_file"]) for record in plan["locks"]]
        == LOCKS,
        "Qt runtime lock order or plan path differs",
    )
    for record in plan["locks"]:
        rpm_plan = load_and_validate(
            REPOSITORY / record["plan_file"],
            REPOSITORY / "config/schemas/rpm-plan.schema.json",
        )
        try:
            RPM["validate_plan_semantics"](rpm_plan)
        except RPM["ValidationError"] as error:
            raise ValidationError(str(error)) from error
        require(
            canonical_sha256(rpm_plan) == record["plan_sha256"],
            "Qt runtime RPM plan digest differs: %s" % record["id"],
        )
        identity = rpm_plan["identity"]
        require(
            (identity["role"], identity["arch"], identity["target_triple"])
            == LOCK_IDENTITIES[record["id"]],
            "Qt runtime RPM plan identity differs: %s" % record["id"],
        )
    return plan


def validate_release_contract(release_path):
    release = load_and_validate(
        release_path, REPOSITORY / "config/schemas/release.schema.json"
    )
    contract = release["qt"]["runtime_qualification"]
    plan_path = REPOSITORY / contract["plan"]["file"]
    require(
        plan_path.resolve()
        == (REPOSITORY / "config/qt-runtime-qualification.json").resolve(),
        "Qt runtime plan path differs",
    )
    plan = validate_plan(load_and_validate(plan_path, PLAN_SCHEMA))
    plan_sha256 = canonical_sha256(plan)
    require(
        contract
        == {
            "status": "locked",
            "plan": {
                "file": "config/qt-runtime-qualification.json",
                "canonical_sha256": plan_sha256,
            },
        },
        "release Qt runtime plan binding differs",
    )
    binding = load_and_validate(
        REPOSITORY / "config/generated/release-binding.json",
        REPOSITORY / "config/schemas/release-binding.schema.json",
    )
    require(
        binding["release"]
        == {
            "schema": "./schemas/release.schema.json",
            "schema_version": 1,
            "canonical_sha256": canonical_sha256(release),
        },
        "Qt runtime release binding differs from selected release",
    )
    build_record = BUILD["binding_component"](
        binding, "future/qt-qualification"
    )
    runtime_record = BUILD["binding_component"](
        binding, "future/qt-runtime-qualification"
    )
    require(
        build_record["scope"] == "future"
        and runtime_record["scope"] == "future",
        "Qt runtime component scope differs",
    )
    try:
        component = COMPONENT["load_component"](
            REPOSITORY
            / "config/generated/components/future/qt-runtime-qualification.json",
            "future/qt-runtime-qualification",
            "future",
            runtime_record["canonical_sha256"],
        )
    except COMPONENT["ComponentError"] as error:
        raise ValidationError(str(error)) from error
    require(
        component["dependencies"]
        == [
            {
                "component": "future/qt-qualification",
                "canonical_sha256": build_record["canonical_sha256"],
            }
        ],
        "Qt runtime component build dependency differs",
    )
    require(
        component["materials"]
        == [
            {
                "path": "/qt/runtime_qualification/plan/canonical_sha256",
                "value": plan_sha256,
            },
            {
                "path": "/qt/runtime_qualification/plan/file",
                "value": "config/qt-runtime-qualification.json",
            },
            {
                "path": "/qt/runtime_qualification/status",
                "value": "locked",
            },
        ],
        "Qt runtime component materials differ",
    )
    transactions = []
    for record in plan["locks"]:
        lock_path = REPOSITORY / record["lock_file"]
        lock = load_and_validate(
            lock_path, REPOSITORY / "config/schemas/rpm-lock.schema.json"
        )
        require(
            canonical_sha256(lock) == record["canonical_sha256"],
            "Qt runtime RPM lock digest differs: %s" % record["id"],
        )
        try:
            transaction, _identity = RPM["validate_lock_binding"](
                lock, lock_path, release_path=release_path
            )
        except RPM["ValidationError"] as error:
            raise ValidationError(str(error)) from error
        transactions.append(transaction)
    return {
        "release": release,
        "plan": plan,
        "plan_sha256": plan_sha256,
        "locked_transactions": transactions,
        "runtime_pair": validate_runtime_pair(transactions),
        "qualification_component": component,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--release",
        type=Path,
        default=REPOSITORY / "config/release.json",
    )
    arguments = parser.parse_args(argv)
    try:
        result = validate_release_contract(arguments.release)
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "valid Qt runtime qualification plan: %s (locked; %d/%d RPMs)"
        % (
            result["plan_sha256"],
            result["runtime_pair"]["x86_64_packages"],
            result["runtime_pair"]["aarch64_packages"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
