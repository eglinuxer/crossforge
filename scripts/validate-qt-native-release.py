#!/usr/bin/env python3
"""Revalidate candidate-bound native AArch64 Qt runtime evidence."""

import argparse
import re
import runpy
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
CANDIDATE = runpy.run_path(
    str(REPOSITORY / "scripts/candidate_manifest.py")
)
OVERLAY = runpy.run_path(
    str(REPOSITORY / "scripts/assemble-qt-runtime.py")
)
RUNTIME = runpy.run_path(
    str(REPOSITORY / "scripts/run-qt-target-runtime.py")
)
QUALIFICATION = runpy.run_path(
    str(REPOSITORY / "scripts/validate-qt-runtime-qualification.py")
)
ValidationError = RUNTIME["ValidationError"]
OCI_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TARGET = {
    "arch": "aarch64",
    "triple": "aarch64-unknown-linux-gnu",
}


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def qualification_identities(contract):
    component = contract["qualification_component"]
    dependencies = component["dependencies"]
    require(
        len(dependencies) == 1
        and dependencies[0]["component"] == "future/qt-qualification",
        "Qt runtime qualification build dependency differs",
    )
    runtime = {
        "component": "future/qt-runtime-qualification",
        "canonical_sha256": QUALIFICATION["canonical_sha256"](
            component
        ),
        "plan_sha256": contract["plan_sha256"],
    }
    build = {
        "component": dependencies[0]["component"],
        "canonical_sha256": dependencies[0]["canonical_sha256"],
    }
    return runtime, build


def candidate_identity(document, canonical_sha256):
    return {
        "source_commit": document["source_commit"],
        "repository": document["repository"],
        "digest": document["digest"],
        "platform_manifest_digest": document[
            "platform_manifest_digest"
        ],
        "canonical_sha256": canonical_sha256,
    }


def validate_native_binding(
    report,
    candidate,
    candidate_sha256,
    release,
    contract,
    build_evidence,
    overlay_evidence,
    expected_source_commit,
    expected_candidate_digest,
    input_rootfs_sha256,
):
    require(
        OCI_DIGEST_RE.match(expected_candidate_digest or ""),
        "expected candidate digest is invalid",
    )
    require(
        SHA256_RE.match(input_rootfs_sha256 or ""),
        "expected Qt rootfs digest is invalid",
    )
    require(
        candidate["source_commit"] == expected_source_commit,
        "candidate source commit differs",
    )
    require(
        candidate["digest"] == expected_candidate_digest,
        "candidate OCI digest differs",
    )
    expected_candidate = candidate_identity(candidate, candidate_sha256)
    expected_base = {
        "index_digest": release["base_image"]["digest"],
        "manifest_digest": release["base_image"]["manifests"]["arm64"],
    }
    runtime_qualification, build_qualification = (
        qualification_identities(contract)
    )
    release_sha256 = QUALIFICATION["canonical_sha256"](release)

    require(
        build_evidence["identity"] == TARGET,
        "Qt target build evidence identity differs",
    )
    require(
        build_evidence["qualification_component"]
        == build_qualification,
        "Qt target build qualification component differs",
    )
    require(
        build_evidence["inputs"]["plan_sha256"]
        == contract["plan"]["build_qualification"]["plan_sha256"],
        "Qt target build plan digest differs",
    )

    overlay_identity = overlay_evidence["identity"]
    require(
        overlay_identity["target"] == TARGET,
        "Qt runtime overlay target differs",
    )
    require(
        overlay_identity["base_image"] == expected_base,
        "Qt runtime overlay base image differs",
    )
    require(
        overlay_identity["release_sha256"] == release_sha256,
        "Qt runtime overlay release digest differs",
    )
    require(
        overlay_identity["runtime_qualification"]
        == runtime_qualification,
        "Qt runtime overlay qualification component differs",
    )

    identity = report["identity"]
    require(
        identity["target"] == TARGET
        and identity["tier"] == "native-release"
        and report["executor"] == {"kind": "native"},
        "Qt native release executor identity differs",
    )
    require(
        identity["base_image"] == expected_base
        and identity["release_sha256"] == release_sha256,
        "Qt native release base or release identity differs",
    )
    require(
        identity["runtime_qualification"] == runtime_qualification,
        "Qt native release qualification component differs",
    )
    require(
        identity["candidate"] == expected_candidate,
        "Qt native release candidate identity differs",
    )
    require(
        identity["input_rootfs_sha256"] == input_rootfs_sha256,
        "Qt native release rootfs digest differs",
    )
    require(
        identity["build_evidence_sha256"]
        == RUNTIME["canonical_sha256"](build_evidence),
        "Qt native release build evidence digest differs",
    )
    require(
        identity["overlay_evidence_sha256"]
        == RUNTIME["canonical_sha256"](overlay_evidence),
        "Qt native release overlay evidence digest differs",
    )
    return report


def validate(arguments):
    contract = QUALIFICATION["validate_release_contract"](
        arguments.release
    )
    release = contract["release"]
    candidate = RUNTIME["load_json"](arguments.candidate)
    candidate_schema = CANDIDATE["load_candidate_schema"](
        arguments.candidate_schema
    )
    candidate_sha256 = CANDIDATE["validate_candidate"](
        candidate,
        release,
        candidate_schema,
        expected_source_commit=arguments.expected_source_commit,
    )
    build_evidence = RUNTIME["load_schema"](
        arguments.build_evidence, "qt-target-build.schema.json"
    )
    overlay_evidence = RUNTIME["load_schema"](
        arguments.overlay_evidence, "qt-runtime-overlay.schema.json"
    )
    OVERLAY["validate_evidence"](overlay_evidence)
    report = RUNTIME["load_json"](arguments.report)
    RUNTIME["validate_evidence_document"](report)
    validate_native_binding(
        report,
        candidate,
        candidate_sha256,
        release,
        contract,
        build_evidence,
        overlay_evidence,
        arguments.expected_source_commit,
        arguments.expected_candidate_digest,
        arguments.input_rootfs_sha256,
    )
    print(
        "valid native AArch64 Qt qualification: %s (candidate %s)"
        % (arguments.report, candidate["digest"])
    )


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--candidate", type=Path, required=True)
    result.add_argument("--report", type=Path, required=True)
    result.add_argument("--build-evidence", type=Path, required=True)
    result.add_argument("--overlay-evidence", type=Path, required=True)
    result.add_argument("--expected-source-commit", required=True)
    result.add_argument("--expected-candidate-digest", required=True)
    result.add_argument("--input-rootfs-sha256", required=True)
    result.add_argument(
        "--release",
        type=Path,
        default=REPOSITORY / "config/release.json",
    )
    result.add_argument(
        "--candidate-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/candidate.schema.json",
    )
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        validate(arguments)
    except (KeyError, OSError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
