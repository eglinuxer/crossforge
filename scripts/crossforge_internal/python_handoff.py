"""Same-run handoff of newly built Python artifacts, without qualification."""

import copy

from . import component_artifacts, component_handoff, python_components, registry_transfer
from .identity import content_sha256, digest_value, exact_fields, require


PARTS = (("build", "build", "install"), ("x86_64-install", "x86_64", "install"),
         ("x86_64-test-context", "x86_64", "test-context"), ("aarch64-install", "aarch64", "install"),
         ("aarch64-test-context", "aarch64", "test-context"))


def specs(source, row):
    return {name: python_components.spec(source, row, arch, kind) for name, arch, kind in PARTS}


def document(source, row, producer, execution, components):
    return validate(source, {"schema_version": 1, "kind": "crossforge-python-ci-handoff", "row": row,
        "producer": copy.deepcopy(producer), "build_execution": copy.deepcopy(execution),
        "components": copy.deepcopy(components)})


def validate(source, value):
    exact_fields(value, ("schema_version", "kind", "row", "producer", "build_execution", "components"), "Python CI handoff")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
            value["kind"] == "crossforge-python-ci-handoff", "unsupported Python CI handoff schema")
    producer = component_artifacts.validate_producer(value["producer"])
    require(producer["kind"] == "github-actions", "Python CI handoff requires a clean GitHub producer")
    require(type(value["build_execution"]) is dict and value["build_execution"], "Python build environment is missing")
    selected = specs(source, value["row"])
    require(type(value["components"]) is dict and value["components"] and
            not set(value["components"]) - set(selected), "Python CI handoff component set differs")
    for name, entry in value["components"].items():
        exact_fields(entry, ("reference", "receipt_sha256", "receipt"), "Python CI component reference")
        receipt = component_artifacts.validate_receipt(entry["receipt"])
        digest_value(entry["receipt_sha256"], "Python CI receipt SHA256")
        require(content_sha256(receipt) == entry["receipt_sha256"], "Python CI receipt bytes differ")
        contract, settings = receipt["contract"], selected[name]
        inputs = contract["inputs"]
        require(contract["role"] == settings["role"] and inputs["component"] == settings["component"] and
                inputs["targets"] == settings["targets"] and inputs["parameters"].get("python_component") == settings,
                "Python CI component role, row or target differs")
        require(contract["producer"] == producer, "Python CI cannot relabel another producer's artifact")
        require(inputs["parameters"].get("execution") == value["build_execution"], "Python CI build environment differs")
        repository, digest = registry_transfer.reference(entry["reference"])
        require(repository == component_handoff.REPOSITORY and digest == receipt["artifact"]["root_digest"],
                "Python CI registry artifact differs")
    return value


def verify(source, value, trusted_sha256, source_commit, invocation):
    digest_value(trusted_sha256, "upstream Python handoff SHA256")
    require(content_sha256(validate(source, value)) == trusted_sha256, "Python CI handoff differs from upstream job output")
    require(value["producer"]["source_commit"] == source_commit and value["producer"]["invocation"] == invocation,
            "Python CI handoff source/run/attempt differs")
    return value
