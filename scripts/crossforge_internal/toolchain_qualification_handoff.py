"""Bind one newly executed toolchain/GCC gate to its original trusted CI job."""

import copy

from . import component_artifacts, component_catalog, component_qualification, registry_transfer
from .identity import content_sha256, digest_value, exact_fields, require


def document(source, arch, profile, producer, execution, entry):
    return validate(source, {"schema_version": 1, "kind": "crossforge-toolchain-qualification-ci-handoff",
        "arch": arch, "profile": profile, "producer": copy.deepcopy(producer),
        "execution": copy.deepcopy(execution), "entry": copy.deepcopy(entry)})


def validate(source, value):
    exact_fields(value, ("schema_version", "kind", "arch", "profile", "producer", "execution", "entry"),
                 "toolchain qualification CI handoff")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
            value["kind"] == "crossforge-toolchain-qualification-ci-handoff", "unsupported qualification handoff schema")
    producer = component_artifacts.validate_producer(value["producer"])
    require(producer["kind"] == "github-actions", "qualification handoff requires a clean GitHub producer")
    exact_fields(value["execution"], ("build", "host"), "qualification CI execution")
    require(all(type(item) is dict and item for item in value["execution"].values()),
            "qualification execution must include build and physical host identities")
    settings = component_qualification.spec(value["arch"], value["profile"])
    entry = value["entry"]
    exact_fields(entry, ("reference", "receipt_sha256", "receipt"), "qualification CI component reference")
    receipt = component_artifacts.validate_receipt(entry["receipt"])
    digest_value(entry["receipt_sha256"], "qualification CI receipt SHA256")
    require(content_sha256(receipt) == entry["receipt_sha256"], "qualification CI receipt bytes differ")
    contract, inputs = receipt["contract"], receipt["contract"]["inputs"]
    require(contract["role"] == "qualification" and inputs["component"] == settings["component"] and
            inputs["targets"] == [settings["triple"]] and inputs["parameters"].get("qualification") == settings,
            "qualification CI role, profile or target differs")
    require(contract["producer"] == producer, "qualification CI cannot relabel another producer's execution")
    require(inputs["parameters"].get("execution") == value["execution"], "qualification CI execution differs")
    copies = component_qualification.report_copies(source, settings)
    require(inputs["parameters"].get("report_copies") == copies, "qualification CI report policy differs")
    paths = sorted([component_artifacts.CONTRACT_PATH, component_qualification.RECORD_PATH,
                    component_qualification.PROGRESS_PATH] + list(copies.values()))
    require([item["path"] for item in receipt["metadata"]] == paths, "qualification CI evidence set differs")
    repository, digest = registry_transfer.reference(entry["reference"])
    require(repository == component_catalog.REPOSITORY and digest == receipt["artifact"]["root_digest"],
            "qualification CI registry artifact differs")
    return value


def verify(source, value, trusted_sha256, source_commit, invocation):
    digest_value(trusted_sha256, "upstream qualification handoff SHA256")
    require(content_sha256(validate(source, value)) == trusted_sha256,
            "qualification CI handoff differs from upstream job output")
    require(value["producer"]["source_commit"] == source_commit and value["producer"]["invocation"] == invocation,
            "qualification CI handoff source/run/attempt differs")
    return value
