"""Pin the exact authenticated raw components and qualified rows for SDK retries.

This selection record grants no trust. Every retry still authenticates catalogs
and verifies the original receipts, installed bytes and execution evidence.
"""

import copy
import re
import subprocess

from . import component_artifacts, component_recovery, registry_transfer
from .identity import content_sha256, digest_value, exact_fields, require


def revision(source):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(source)).decode().strip()
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "SDK recovery source commit must be complete")
    require(not subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=str(source)),
            "SDK recovery requires a clean source checkout")
    return commit


def context(source, graph, root, execution):
    return component_recovery.context(source, graph, "sdk-assembly", [root], execution, revision(source))


def validate_row_pin(value, row):
    exact_fields(value, component_recovery.PIN_FIELDS, "SDK row recovery pin")
    require(value["component"] == "qualification/python-" + row and value["role"] == "qualification",
            "SDK recovery row identity differs")
    for field in ("inputs_sha256", "receipt_sha256"):
        digest_value(value[field], "SDK recovery " + field)
    for field in ("catalog_reference", "reference"):
        repository, _ = registry_transfer.reference(value[field])
        require(repository == component_recovery.REPOSITORY, "SDK row recovery reference is outside the internal registry")
    component_artifacts.validate_producer(value["producer"])
    require(value["producer"]["kind"] == "github-actions", "SDK row recovery requires an authenticated GitHub producer")
    return value


def row_pin(result, row):
    require(result.get("status") == "verified-qualified-row", "cannot recover an unverified or missing Python row")
    value = {key: copy.deepcopy(result.get(key)) for key in ("component", "role", "inputs_sha256", "reference", "producer")}
    value.update(catalog_reference=result.get("catalog", {}).get("reference"),
                 receipt_sha256=result.get("subject", {}).get("receipt_sha256"))
    return validate_row_pin(value, row)


def verify_row_selection(expected, result, row):
    require(content_sha256(validate_row_pin(expected, row)) == content_sha256(row_pin(result, row)),
            "resolved Python row differs from its original recovery pin")
    return result


def validate(value, expected_raw, rows):
    exact_fields(value, ("schema_version", "kind", "raw", "rows"), "SDK recovery document")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
            value["kind"] == "crossforge-sdk-component-recovery", "unsupported SDK recovery schema")
    component_recovery.validate(value["raw"], expected_raw)
    current = value["raw"]["context"]
    require(current["stage"] == "sdk-assembly" and current["source_commit"] is not None,
            "SDK recovery requires a source commit and SDK stage")
    exact_fields(value["rows"], rows, "SDK recovery rows")
    for row in rows:
        validate_row_pin(value["rows"][row], row)
    return value


def document(current, raw, qualified, expected_raw, rows):
    value = {"schema_version": 1, "kind": "crossforge-sdk-component-recovery",
             "raw": component_recovery.document(current, raw, expected_raw),
             "rows": {row: row_pin(result, row) for row, result in qualified.items()}}
    return validate(value, expected_raw, rows)


def verify(value, trusted_sha256, current, expected_raw, rows):
    digest_value(trusted_sha256, "independent SDK recovery SHA256")
    validate(value, expected_raw, rows)
    require(content_sha256(value) == trusted_sha256, "SDK recovery document differs from the selected SHA256")
    # The whole document is independently pinned; reuse the original raw schema
    # and its context verifier without expanding its allowed component roles.
    raw = component_recovery.verify(value["raw"], content_sha256(value["raw"]), current, expected_raw)
    return {"raw": raw, "rows": copy.deepcopy(value["rows"])}
