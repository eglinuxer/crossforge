#!/usr/bin/env python3
"""Assemble a locked Qt-only dependency overlay on a pinned Rocky root."""

import argparse
import json
import os
import runpy
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
PYTHON_OVERLAY = runpy.run_path(
    str(REPOSITORY / "scripts/assemble-python-runtime.py")
)
MATERIALIZER = PYTHON_OVERLAY["MATERIALIZER"]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
COMPONENT = runpy.run_path(str(REPOSITORY / "scripts/release_component.py"))
ValidationError = MATERIALIZER["ValidationError"]
SchemaError = STRICT["ValidationError"]
canonical_sha256 = MATERIALIZER["canonical_sha256"]
load_json = MATERIALIZER["load_json"]
rpm_inventory = PYTHON_OVERLAY["rpm_inventory"]
validate_inventory_arch = PYTHON_OVERLAY["validate_inventory_arch"]
require_runtime_root = PYTHON_OVERLAY["require_runtime_root"]
validate_runtime_root = PYTHON_OVERLAY["validate_runtime_root"]
installation_arguments = PYTHON_OVERLAY["installation_arguments"]
safe_evidence_path = PYTHON_OVERLAY["safe_evidence_path"]

SCHEMA_ID = "https://crossforge.dev/schemas/qt-runtime-overlay.schema.json"
ARCH_TO_OCI = {"x86_64": "amd64", "aarch64": "arm64"}
BASE_PACKAGE_SUBSTITUTIONS = {"coreutils": ("coreutils-single",)}


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def validate_qualification_binding(
    plan_path,
    component_path,
    component_sha256,
    build_component_path,
    context,
):
    schema = STRICT["load_json"](
        REPOSITORY
        / "config/schemas/qt-runtime-qualification-plan.schema.json"
    )
    plan = STRICT["load_json"](plan_path)
    build_plan_path = REPOSITORY / "config/qt-qualification.json"
    build_plan = STRICT["load_json"](build_plan_path)
    build_schema = STRICT["load_json"](
        REPOSITORY / "config/schemas/qt-qualification-plan.schema.json"
    )
    try:
        STRICT["validate_schema_subset"](schema)
        STRICT["validate"](plan, schema, schema, "$")
        STRICT["validate_schema_subset"](build_schema)
        STRICT["validate"](
            build_plan, build_schema, build_schema, "$"
        )
    except SchemaError as error:
        raise ValidationError(str(error)) from error
    try:
        component = COMPONENT["load_component"](
            component_path,
            "future/qt-runtime-qualification",
            "future",
            component_sha256,
        )
    except COMPONENT["ComponentError"] as error:
        raise ValidationError(str(error)) from error
    require(
        len(component["dependencies"]) == 1
        and component["dependencies"][0]["component"]
        == "future/qt-qualification",
        "Qt runtime qualification component dependency shape differs",
    )
    try:
        build_component = COMPONENT["load_component"](
            build_component_path,
            "future/qt-qualification",
            "future",
            component["dependencies"][0]["canonical_sha256"],
        )
    except COMPONENT["ComponentError"] as error:
        raise ValidationError(str(error)) from error
    plan_sha256 = canonical_sha256(plan)
    require(
        plan["build_qualification"]
        == {
            "component": "future/qt-qualification",
            "plan_file": "config/qt-qualification.json",
            "plan_sha256": canonical_sha256(build_plan),
        },
        "Qt runtime plan build qualification differs",
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
        "Qt runtime qualification component materials differ",
    )
    require(
        component["dependencies"]
        == [
            {
                "component": "future/qt-qualification",
                "canonical_sha256": COMPONENT["canonical_sha256"](
                    build_component
                ),
            }
        ],
        "Qt runtime qualification build dependency differs",
    )
    record_id = "qt-runtime-%s" % context["arch"]
    records = [record for record in plan["locks"] if record["id"] == record_id]
    require(len(records) == 1, "Qt runtime lock plan record is not unique")
    record = records[0]
    require(
        record["plan_sha256"]
        == context["transaction"]["plan"]["canonical_sha256"]
        and record["canonical_sha256"] == canonical_sha256(context["lock"]),
        "Qt runtime lock differs from the qualification plan",
    )
    return plan_sha256


def inventory_by_name(rows):
    result = {}
    for name, arch, nevra in rows:
        result.setdefault(name, []).append((arch, nevra))
    for values in result.values():
        values.sort()
    return result


def partition_packages(verified, before):
    before_by_name = inventory_by_name(before)
    locked_names = set()
    retained = []
    selected = []
    for package in verified:
        item = package["item"]
        name = item["name"]
        require(
            name not in locked_names,
            "Qt runtime lock contains duplicate package names",
        )
        locked_names.add(name)
        base_names = (name,) + BASE_PACKAGE_SUBSTITUTIONS.get(name, ())
        matches = [
            (base_name, value)
            for base_name in base_names
            for value in before_by_name.get(base_name, [])
        ]
        if matches:
            require(
                len(matches) == 1,
                "%s contains multiple installed variants" % name,
            )
            base_name, (_base_arch, base_nevra) = matches[0]
            retained.append(
                {
                    "name": name,
                    "base_name": base_name,
                    "base_nevra": base_nevra,
                    "locked_nevra": item["nevra"],
                }
            )
        else:
            selected.append(package)
    retained.sort(key=lambda item: item["name"])
    selected.sort(key=lambda package: package["item"]["name"])
    require(selected, "Qt runtime lock adds no packages to the clean Rocky root")
    return retained, selected


def verify_transition(before, after, retained, selected):
    before_by_name = inventory_by_name(before)
    after_by_name = inventory_by_name(after)
    selected_names = {package["item"]["name"] for package in selected}
    require(
        [row for row in before if row[0] not in selected_names]
        == [row for row in after if row[0] not in selected_names],
        "packages outside the Qt runtime overlay changed",
    )
    expected_selected = {
        package["item"]["name"]: (
            package["item"]["arch"],
            package["item"]["nevra"],
        )
        for package in selected
    }
    actual_selected = {}
    for name in selected_names:
        values = after_by_name.get(name, [])
        require(
            len(values) == 1,
            "installed Qt runtime package is not unique: %s" % name,
        )
        actual_selected[name] = values[0]
    require(
        actual_selected == expected_selected,
        "installed Qt runtime RPMs differ from the lock",
    )
    require(
        all(
            before_by_name[item["base_name"]]
            == after_by_name[item["base_name"]]
            and before_by_name[item["base_name"]][0][1]
            == item["base_nevra"]
            for item in retained
        ),
        "a retained Rocky base package changed",
    )


def build_evidence(
    context,
    release,
    manifest_digest,
    retained,
    selected,
    before,
    after,
    os_release_sha256,
    qualification_component_sha256,
    qualification_plan_sha256,
):
    installed = [
        {
            "name": package["item"]["name"],
            "nevra": package["item"]["nevra"],
            "received_sha256": package["lock"]["received_sha256"],
        }
        for package in selected
    ]
    closure = {
        "retained_base_packages": retained,
        "installed_packages": installed,
    }
    identity = {
        "base_image": {
            "index_digest": release["base_image"]["digest"],
            "manifest_digest": manifest_digest,
        },
        "release_sha256": canonical_sha256(release),
        "target": {
            "arch": context["arch"],
            "triple": context["transaction"]["identity"]["target_triple"],
        },
        "runtime_lock": {
            "plan_sha256": context["transaction"]["plan"]["canonical_sha256"],
            "transaction_sha256": canonical_sha256(context["transaction"]),
            "lock_sha256": canonical_sha256(context["lock"]),
        },
        "retained_base_packages": retained,
        "installed_packages": installed,
        "closure_sha256": canonical_sha256(closure),
    }
    identity["runtime_qualification"] = {
        "component": "future/qt-runtime-qualification",
        "canonical_sha256": qualification_component_sha256,
        "plan_sha256": qualification_plan_sha256,
    }
    before_nevras = sorted(row[2] for row in before)
    after_nevras = sorted(row[2] for row in after)
    return {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-qt-runtime-overlay",
        "qualification_only": True,
        "identity": identity,
        "identity_sha256": canonical_sha256(identity),
        "runtime_inventory": {
            "before_sha256": canonical_sha256(before_nevras),
            "before_item_count": len(before_nevras),
            "after_sha256": canonical_sha256(after_nevras),
            "after_item_count": len(after_nevras),
            "installed_nevras": sorted(item["nevra"] for item in installed),
            "os_release_sha256": os_release_sha256,
        },
    }


def validate_evidence(evidence):
    schema = STRICT["load_json"](
        REPOSITORY / "config/schemas/qt-runtime-overlay.schema.json"
    )
    try:
        STRICT["validate_schema_subset"](schema)
        STRICT["validate"](evidence, schema, schema, "$")
    except SchemaError as error:
        raise ValidationError(str(error)) from error
    identity = evidence["identity"]
    require(
        evidence["identity_sha256"] == canonical_sha256(identity),
        "Qt runtime overlay identity digest differs",
    )
    closure = {
        "retained_base_packages": identity["retained_base_packages"],
        "installed_packages": identity["installed_packages"],
    }
    require(
        identity["closure_sha256"] == canonical_sha256(closure),
        "Qt runtime overlay closure digest differs",
    )
    require(
        identity["retained_base_packages"]
        == sorted(identity["retained_base_packages"], key=lambda item: item["name"]),
        "retained base packages are not sorted",
    )
    require(
        identity["installed_packages"]
        == sorted(identity["installed_packages"], key=lambda item: item["name"]),
        "installed Qt runtime packages are not sorted",
    )


def write_evidence(path, evidence):
    validate_evidence(evidence)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(evidence, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(path))
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def assemble(
    lock_path,
    bundle,
    key,
    release_path,
    plan_path,
    qualification_component,
    qualification_component_sha256,
    build_qualification_component,
    runtime_root,
    manifest_digest,
    evidence_path,
):
    release = load_json(release_path)
    context = MATERIALIZER["load_lock"](
        lock_path, release_path=release_path
    )
    require(
        context["role"] == "qt-runtime",
        "Qt runtime overlay requires a qt-runtime lock",
    )
    qualification_plan_sha256 = validate_qualification_binding(
        plan_path,
        qualification_component,
        qualification_component_sha256,
        build_qualification_component,
        context,
    )
    oci_arch = ARCH_TO_OCI.get(context["arch"])
    require(oci_arch is not None, "unsupported Qt runtime architecture")
    require(
        manifest_digest == release["base_image"]["manifests"][oci_arch],
        "Qt runtime base image manifest differs from release",
    )
    runtime_root = require_runtime_root(runtime_root)
    os_release_sha256 = validate_runtime_root(
        runtime_root, context["transaction"]["identity"]["release"]
    )
    MATERIALIZER["verify_bundle"](context, bundle)
    verified = MATERIALIZER["verify_key_and_headers"](context, bundle, key)
    before = rpm_inventory(runtime_root)
    validate_inventory_arch(before, context["arch"])
    retained, selected = partition_packages(verified, before)
    paths = [
        bundle / MATERIALIZER["package_filename"](package)
        for package in selected
    ]
    MATERIALIZER["command"](
        installation_arguments(runtime_root, context["arch"], paths, True),
        "Qt runtime RPM transaction test",
    )
    require(
        rpm_inventory(runtime_root) == before,
        "Qt runtime test transaction changed the RPMDB",
    )
    MATERIALIZER["command"](
        installation_arguments(runtime_root, context["arch"], paths, False),
        "Qt runtime RPM transaction",
    )
    after = rpm_inventory(runtime_root)
    validate_inventory_arch(after, context["arch"])
    verify_transition(before, after, retained, selected)
    evidence_path = safe_evidence_path(evidence_path, bundle, runtime_root)
    evidence = build_evidence(
        context,
        release,
        manifest_digest,
        retained,
        selected,
        before,
        after,
        os_release_sha256,
        qualification_component_sha256,
        qualification_plan_sha256,
    )
    write_evidence(evidence_path, evidence)
    print(
        "installed %d Qt runtime RPMs and retained %d Rocky base RPMs (%s)"
        % (len(selected), len(retained), evidence["identity_sha256"])
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("lock", type=Path)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--qualification-component", type=Path, required=True)
    parser.add_argument("--qualification-component-sha256", required=True)
    parser.add_argument("--build-qualification-component", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--base-image-manifest-digest", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        assemble(
            arguments.lock,
            arguments.bundle,
            arguments.key,
            arguments.release,
            arguments.plan,
            arguments.qualification_component,
            arguments.qualification_component_sha256,
            arguments.build_qualification_component,
            arguments.runtime_root,
            arguments.base_image_manifest_digest,
            arguments.evidence,
        )
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
