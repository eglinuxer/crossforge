"""Authorize one newly executed Python row from its original trusted CI job."""

import copy

from . import component_artifacts, component_catalog, python_qualification, registry_transfer
from .identity import content_sha256, digest_value, exact_fields, require


def document(source, row, producer, execution, entry):
    return validate(source, {"schema_version": 1, "kind": "crossforge-python-row-ci-handoff", "row": row,
        "producer": copy.deepcopy(producer), "execution": copy.deepcopy(execution),
        "components": {row: copy.deepcopy(entry)}})


def validate(source, value):
    exact_fields(value, ("schema_version", "kind", "row", "producer", "execution", "components"), "Python row CI handoff")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
            value["kind"] == "crossforge-python-row-ci-handoff", "unsupported Python row CI handoff schema")
    producer = component_artifacts.validate_producer(value["producer"])
    require(producer["kind"] == "github-actions", "Python row CI handoff requires a clean GitHub producer")
    exact_fields(value["execution"], ("build", "host"), "Python row CI execution")
    require(all(type(item) is dict and item for item in value["execution"].values()),
            "Python row CI execution must include build and physical host identities")
    settings = python_qualification.spec(source, value["row"])
    exact_fields(value["components"], (value["row"],), "Python row CI component set")
    entry = value["components"][value["row"]]
    exact_fields(entry, ("reference", "receipt_sha256", "receipt"), "Python row CI component reference")
    receipt = component_artifacts.validate_receipt(entry["receipt"])
    digest_value(entry["receipt_sha256"], "Python row CI receipt SHA256")
    require(content_sha256(receipt) == entry["receipt_sha256"], "Python row CI receipt bytes differ")
    contract = receipt["contract"]
    inputs = contract["inputs"]
    require(contract["role"] == "qualification" and inputs["component"] == settings["component"] and
            inputs["targets"] == settings["targets"] and inputs["parameters"].get("qualification") == settings,
            "Python row CI qualification role, row or target differs")
    require(contract["producer"] == producer, "Python row CI cannot relabel another producer's execution")
    require(inputs["parameters"].get("execution") == value["execution"], "Python row CI execution environment differs")
    require([item["path"] for item in receipt["metadata"]] == python_qualification.metadata_paths(),
            "Python row CI qualification evidence set differs")
    repository, digest = registry_transfer.reference(entry["reference"])
    require(repository == component_catalog.REPOSITORY and digest == receipt["artifact"]["root_digest"],
            "Python row CI registry artifact differs")
    return value


def verify(source, value, trusted_sha256, source_commit, invocation):
    digest_value(trusted_sha256, "upstream Python row handoff SHA256")
    require(content_sha256(validate(source, value)) == trusted_sha256,
            "Python row CI handoff differs from upstream job output")
    require(value["producer"]["source_commit"] == source_commit and value["producer"]["invocation"] == invocation,
            "Python row CI handoff source/run/attempt differs")
    return value
