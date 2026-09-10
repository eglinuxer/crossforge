"""Same-run CI handoff, authenticated by an upstream job's independent digest.

This intentionally does not authorize cross-run reuse or trust arbitrary PR
producers. Durable signed catalogs are a separate qualification-reuse boundary.
"""

import copy

from . import component_artifacts, component_build, registry_transfer
from .identity import content_sha256, digest_value, exact_fields, require


REPOSITORY = "ghcr.io/eglinuxer/crossforge-components"
ROLES = ("toolchain-install", "gcc-test-context")


def document(producer, execution, components):
    return validate({"schema_version": 1, "kind": "crossforge-component-ci-handoff",
                     "producer": copy.deepcopy(producer), "build_execution": copy.deepcopy(execution),
                     "components": copy.deepcopy(components)})


def validate(value):
    exact_fields(value, ("schema_version", "kind", "producer", "build_execution", "components"), "CI component handoff")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
            value["kind"] == "crossforge-component-ci-handoff", "unsupported CI handoff schema")
    component_artifacts.validate_producer(value["producer"])
    require(value["producer"]["kind"] == "github-actions", "CI handoff must have a clean GitHub producer")
    require(type(value["build_execution"]) is dict and value["build_execution"], "CI build environment is missing")
    require(type(value["components"]) is dict and set(value["components"]) == set(ROLES), "CI handoff roles differ")
    for role in ROLES:
        component = value["components"][role]
        exact_fields(component, ("reference", "receipt_sha256", "receipt"), "CI component reference")
        receipt = component_artifacts.validate_receipt(component["receipt"])
        digest_value(component["receipt_sha256"], "CI receipt SHA256")
        require(content_sha256(receipt) == component["receipt_sha256"], "CI receipt bytes differ")
        spec = component_build.toolchain_spec("x86_64", role)
        require(receipt["contract"]["role"] == role and receipt["contract"]["inputs"]["component"] == spec["component"] and
                receipt["contract"]["inputs"]["targets"] == [spec["triple"]], "CI receipt role or target differs")
        require(receipt["contract"]["producer"] == value["producer"], "CI component producer differs")
        require(receipt["contract"]["inputs"]["parameters"].get("execution") == value["build_execution"],
                "CI component build environment differs")
        repo, digest = registry_transfer.reference(component["reference"])
        require(repo == REPOSITORY and digest == receipt["artifact"]["root_digest"], "CI registry artifact differs")
    return value


def verify(value, trusted_sha256, source_commit, invocation):
    digest_value(trusted_sha256, "upstream job handoff SHA256")
    require(content_sha256(validate(value)) == trusted_sha256, "CI handoff differs from upstream job output")
    require(value["producer"]["source_commit"] == source_commit and
            value["producer"]["invocation"] == invocation, "CI handoff source/run/attempt differs")
    return value
