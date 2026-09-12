"""Fixed component selections for retrying one CI stage on the same source.

The caller supplies the recovery document's independent canonical SHA256.
Selections still go through catalog authentication and actual OCI verification;
this document neither grants producer trust nor asserts a qualification result.
"""

import copy
import re

from . import bake_materials, component_artifacts, component_build, registry_transfer
from .identity import content_sha256, digest_value, exact_fields, require


REPOSITORY = "ghcr.io/eglinuxer/crossforge-components"
PIN_FIELDS = ("component", "role", "inputs_sha256", "catalog_reference", "reference", "receipt_sha256", "producer")
CONTEXT_FIELDS = ("stage", "targets", "source_commit", "source_inventory_sha256")
NAME = re.compile(r"[a-zA-Z0-9_-]+\Z")


def context(source, graph, stage, roots, execution, source_commit=None):
    inventories = {root: bake_materials.source_closure(source, graph, root, execution) for root in roots}
    value = {"stage": stage, "targets": list(roots), "source_commit": source_commit or None,
             "source_inventory_sha256": content_sha256(inventories)}
    return validate_context(value)


def validate_context(value):
    exact_fields(value, CONTEXT_FIELDS, "component recovery context")
    require(type(value["stage"]) is str and NAME.fullmatch(value["stage"]), "recovery CI stage is invalid")
    roots = value["targets"]
    require(type(roots) is list and roots and all(type(name) is str and NAME.fullmatch(name) for name in roots),
            "recovery roots are invalid")
    require(roots == sorted(set(roots)), "recovery roots must be unique and sorted")
    commit = value["source_commit"]
    require(commit is None or type(commit) is str and re.fullmatch(r"[0-9a-f]{40}", commit),
            "recovery source commit must be complete")
    digest_value(value["source_inventory_sha256"], "recovery source inventory SHA256")
    return value


def validate_pin(value):
    exact_fields(value, PIN_FIELDS, "component recovery pin")
    require(type(value["component"]) is str and re.fullmatch(r"(?:toolchain|python)/[a-z0-9_-]+", value["component"]),
            "recovery component name is invalid")
    require(value["role"] in ("toolchain-install", "gcc-test-context", "python-install", "python-test-context"),
            "recovery role is not a raw build component")
    for field in ("inputs_sha256", "receipt_sha256"):
        digest_value(value[field], "recovery " + field)
    for field in ("catalog_reference", "reference"):
        repository, _ = registry_transfer.reference(value[field])
        require(repository == REPOSITORY, "recovery reference is outside the internal registry")
    component_artifacts.validate_producer(value["producer"])
    require(value["producer"]["kind"] == "github-actions", "recovery requires an authenticated GitHub producer")
    return value


def pin(result):
    require(result.get("status") == "verified-build-component", "cannot recover an unverified or missing component")
    value = {key: copy.deepcopy(result.get(key)) for key in ("component", "role", "inputs_sha256", "reference", "producer")}
    value.update(catalog_reference=result.get("catalog", {}).get("reference"),
                 receipt_sha256=result.get("subject", {}).get("receipt_sha256"))
    return validate_pin(value)


def requirements(source, graph, with_python):
    from . import component_resolution, python_components, python_handoff
    result = {}
    for arch, role in component_resolution.toolchain_edges(graph):
        settings = component_build.toolchain_spec(arch, role)
        result[arch + "-" + role] = {"component": settings["component"], "role": role}
    if with_python:
        parts = {name: (arch, kind) for name, arch, kind in python_handoff.PARTS}
        for row, names in component_resolution.python_requirements(source, graph).items():
            for name in names:
                settings = python_components.spec(source, row, *parts[name])
                result[row + "-" + name] = {key: settings[key] for key in ("component", "role")}
    return result


def validate(value, expected=None):
    exact_fields(value, ("schema_version", "kind", "context", "components"), "component recovery document")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
            value["kind"] == "crossforge-ci-component-recovery", "unsupported component recovery schema")
    validate_context(value["context"])
    require(type(value["components"]) is dict and all(type(name) is str and NAME.fullmatch(name)
            for name in value["components"]), "recovery component labels are invalid")
    for item in value["components"].values():
        validate_pin(item)
    if expected is not None:
        exact_fields(value["components"], expected, "required recovery components")
        for name, identity in expected.items():
            require({key: value["components"][name][key] for key in ("component", "role")} == identity,
                    "recovery component identity differs: " + name)
    return value


def document(current_context, results, expected):
    value = {"schema_version": 1, "kind": "crossforge-ci-component-recovery",
             "context": copy.deepcopy(current_context), "components": {name: pin(result) for name, result in results.items()}}
    return validate(value, expected)


def verify(value, trusted_sha256, current_context, expected):
    digest_value(trusted_sha256, "independent recovery document SHA256")
    validate(value, expected)
    require(content_sha256(value) == trusted_sha256, "recovery document differs from the selected SHA256")
    require(content_sha256(value["context"]) == content_sha256(validate_context(current_context)),
            "recovery source, stage, roots or execution inputs differ")
    return copy.deepcopy(value["components"])


def verify_selection(expected, result):
    require(content_sha256(validate_pin(expected)) == content_sha256(pin(result)),
            "resolved component differs from its original recovery pin")
    return result
