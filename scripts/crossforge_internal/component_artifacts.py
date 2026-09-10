"""Internal artifact contracts and receipts, separate from qualification policy.

A receipt binds an original producer and explicit inputs to observed OCI bytes.
It does not establish producer trust or prove that a qualification tier passed.
Consumers must obtain the expected receipt digest from their trust boundary.
"""

import copy
from datetime import datetime
from pathlib import Path
import re

from . import component_inputs
from .identity import (IdentityError, content_sha256, digest_value, exact_fields, file_record,
                       load_json, relative_path, require)


CONTRACT_SCHEMA = "https://crossforge.dev/schemas/component-artifact.schema.json"
RECEIPT_SCHEMA = "https://crossforge.dev/schemas/component-receipt.schema.json"
CONTRACT_PATH = "component/contract.json"
ROLES = {"toolchain-install", "gcc-test-context", "python-row", "qualification"}


def _timestamp(value):
    require(type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value),
            "producer timestamp must be UTC with second precision")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise IdentityError(str(error)) from error


def validate_producer(value):
    exact_fields(value, ("kind", "source_commit", "source_dirty", "invocation", "started_at"), "producer")
    require(value["kind"] in ("local", "github-actions"), "unsupported component producer")
    require(type(value["source_commit"]) is str and re.fullmatch(r"[0-9a-f]{40}", value["source_commit"]),
            "producer source commit is invalid")
    require(type(value["source_dirty"]) is bool, "producer source_dirty must be boolean")
    invocation = value["invocation"]
    require(type(invocation) is str and invocation and
            not any(ord(c) < 32 or ord(c) == 127 for c in invocation), "producer invocation is invalid")
    if value["kind"] == "github-actions":
        require(re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*", invocation),
                "GitHub producer invocation must identify an exact run and attempt")
        require(not value["source_dirty"], "GitHub producer source must be clean")
    else:
        require(re.fullmatch(r"urn:crossforge:local:[a-zA-Z0-9_.-]+", invocation),
                "local producer invocation must be a unique local run URN")
    _timestamp(value["started_at"])
    return value


def validate_contract(value):
    exact_fields(value, ("$schema", "schema_version", "kind", "role", "inputs", "producer"),
                 "component artifact contract")
    require(value["$schema"] == CONTRACT_SCHEMA and type(value["schema_version"]) is int and
            value["schema_version"] == 1 and value["kind"] == "crossforge-component-artifact",
            "unsupported component artifact schema")
    require(type(value["role"]) is str and value["role"] in ROLES, "unsupported artifact role")
    inputs = component_inputs.validate(value["inputs"])
    require(inputs["scope"] == ("qualification" if value["role"] == "qualification" else "build"),
            "artifact role and input scope differ")
    require(inputs["targets"], "artifact must declare its target triples")
    if value["role"] in ("toolchain-install", "gcc-test-context"):
        require(len(inputs["targets"]) == 1, "toolchain artifact must contain exactly one target")
    validate_producer(value["producer"])
    return value


def contract(role, inputs, producer):
    return validate_contract({"$schema": CONTRACT_SCHEMA, "schema_version": 1,
                              "kind": "crossforge-component-artifact", "role": role,
                              "inputs": copy.deepcopy(inputs), "producer": copy.deepcopy(producer)})


def validate_receipt(value):
    exact_fields(value, ("$schema", "schema_version", "kind", "contract", "artifact", "metadata"),
                 "component receipt")
    require(value["$schema"] == RECEIPT_SCHEMA and type(value["schema_version"]) is int and
            value["schema_version"] == 1 and value["kind"] == "crossforge-component-receipt",
            "unsupported component receipt schema")
    validate_contract(value["contract"])
    artifact = value["artifact"]
    exact_fields(artifact, ("root_digest", "platform_digest", "config_digest", "platform"), "artifact identity")
    require(artifact["platform"] == "linux/amd64", "unsupported artifact platform")
    for key in ("root_digest", "platform_digest", "config_digest"):
        digest_value(artifact[key], key, oci=True)
    records = value["metadata"]
    require(type(records) is list and records, "artifact metadata must be a nonempty array")
    for record in records:
        exact_fields(record, ("path", "sha256", "mode"), "metadata file")
        relative_path(record["path"])
        require(record["path"].startswith("component/"), "metadata must be under component/")
        digest_value(record["sha256"], "metadata SHA256")
        require(type(record["mode"]) is str and re.fullmatch(r"[0-7]{4}", record["mode"]),
                "metadata mode is invalid")
    paths = [record["path"] for record in records]
    require(paths == sorted(set(paths)) and CONTRACT_PATH in paths,
            "metadata paths must be sorted, unique, and contain the artifact contract")
    return value


def receipt(expected_contract, observation, metadata_root, metadata_paths):
    """Seal after BuildKit extracted these paths from the verified OCI digest."""
    validate_contract(expected_contract)
    metadata_root = Path(metadata_root)
    actual_contract = load_json(metadata_root / CONTRACT_PATH)
    require(content_sha256(validate_contract(actual_contract)) == content_sha256(expected_contract),
            "embedded component contract differs")
    require(type(metadata_paths) in (list, tuple) and all(type(p) is str for p in metadata_paths),
            "metadata paths must be an explicit array")
    require(len(metadata_paths) == len(set(metadata_paths)), "duplicate metadata path")
    require(observation.get("kind") == "crossforge-oci-layout-observation" and
            observation.get("schema_version") == 1, "expected verified OCI observation")
    value = {"$schema": RECEIPT_SCHEMA, "schema_version": 1,
             "kind": "crossforge-component-receipt", "contract": copy.deepcopy(actual_contract),
             "artifact": {key: observation[key] for key in (
                 "root_digest", "platform_digest", "config_digest", "platform")},
             "metadata": [file_record(metadata_root, p) for p in sorted(metadata_paths)]}
    return validate_receipt(value)


def verify_receipt(value, expected_receipt_sha256, expected_inputs, expected_role,
                   observation, metadata_root, required_metadata):
    """Verify a trusted receipt against independently planned inputs and bytes.

    metadata_root must be a fresh BuildKit extraction from observation's root.
    This function intentionally does not turn attached reports into qualification.
    """
    digest_value(expected_receipt_sha256, "trusted receipt SHA256")
    metadata_root = Path(metadata_root)
    require(content_sha256(validate_receipt(value)) == expected_receipt_sha256,
            "receipt digest differs from trusted reference")
    require(value["contract"]["role"] == expected_role, "component artifact role differs")
    component_inputs.require_match(value["contract"]["inputs"], expected_inputs)
    require(all(observation.get(key) == digest for key, digest in value["artifact"].items()),
            "observed OCI identity differs from receipt")
    require(type(required_metadata) in (list, tuple) and
            all(type(p) is str for p in required_metadata), "required metadata must be explicit")
    paths = [record["path"] for record in value["metadata"]]
    require(set(required_metadata) <= set(paths), "required component metadata is missing")
    for record in value["metadata"]:
        require(file_record(metadata_root, record["path"]) == record,
                "component metadata file differs: %s" % record["path"])
    embedded = validate_contract(load_json(metadata_root / CONTRACT_PATH))
    require(content_sha256(embedded) == content_sha256(value["contract"]),
            "embedded component contract differs from receipt")
    return value["artifact"]["platform_digest"]
