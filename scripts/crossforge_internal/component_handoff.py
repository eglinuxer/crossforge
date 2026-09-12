"""Same-run CI handoff, authenticated by an upstream job's independent digest.

This intentionally does not authorize cross-run reuse or trust arbitrary PR
producers. Durable signed catalogs are a separate qualification-reuse boundary.
"""

import copy

from . import component_artifacts, component_build, registry_transfer
from .identity import content_sha256, digest_value, exact_fields, require


REPOSITORY = "ghcr.io/eglinuxer/crossforge-components"
ROLES = ("toolchain-install", "gcc-test-context")


def document(producer, execution, components, architecture=None):
    value = {"schema_version": 1 if architecture is None else 2, "kind": "crossforge-component-ci-handoff",
             "producer": copy.deepcopy(producer), "build_execution": copy.deepcopy(execution),
             "components": copy.deepcopy(components)}
    if architecture is not None:
        value["architecture"] = architecture
    return validate(value)


def validate(value):
    require(type(value) is dict, "CI component handoff must be an object")
    version = value.get("schema_version")
    exact_fields(value, ("schema_version", "kind", "producer", "build_execution", "components") +
                 (("architecture",) if type(version) is int and version == 2 else ()), "CI component handoff")
    require(type(version) is int and version in (1, 2) and
            value["kind"] == "crossforge-component-ci-handoff", "unsupported CI handoff schema")
    component_artifacts.validate_producer(value["producer"])
    require(value["producer"]["kind"] == "github-actions", "CI handoff must have a clean GitHub producer")
    require(type(value["build_execution"]) is dict and value["build_execution"], "CI build environment is missing")
    architecture = "x86_64" if version == 1 else value["architecture"]
    require(architecture in ("x86_64", "aarch64"), "unsupported CI handoff architecture")
    require(type(value["components"]) is dict and value["components"] and
            not set(value["components"]) - set(ROLES), "CI handoff roles differ")
    if version == 1:
        require(set(value["components"]) == set(ROLES), "legacy CI handoff requires both pilot roles")
    for role in value["components"]:
        component = value["components"][role]
        exact_fields(component, ("reference", "receipt_sha256", "receipt"), "CI component reference")
        receipt = component_artifacts.validate_receipt(component["receipt"])
        digest_value(component["receipt_sha256"], "CI receipt SHA256")
        require(content_sha256(receipt) == component["receipt_sha256"], "CI receipt bytes differ")
        spec = component_build.toolchain_spec(architecture, role)
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
