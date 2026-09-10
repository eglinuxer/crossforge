"""Authenticate cross-run receipt catalogs with the pinned upstream verifier.

A catalog authorizes receipt digests from one trusted producer. It does not
replace current input capture, OCI verification, or domain qualification checks.
The registry and downloaded catalog/bundle files are untrusted until verified.
"""

import base64
import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile

from . import component_artifacts, component_inputs, registry_transfer
from .identity import canonical_bytes, content_sha256, digest_value, exact_fields
from .identity import file_record, load_json, parse_json, require


REPOSITORY = "ghcr.io/eglinuxer/crossforge-components"
GITHUB_REPOSITORY = "eglinuxer/crossforge"
WORKFLOW = ".github/workflows/component-pilot.yml"
ISSUER = "https://token.actions.githubusercontent.com"
SIGNER = "https://github.com/" + GITHUB_REPOSITORY + "/" + WORKFLOW + "@refs/heads/main"
EVENT = "workflow_dispatch"


def _key(entry):
    contract = entry["receipt"]["contract"]
    return (contract["inputs"]["component"], contract["role"], component_inputs.identity(contract["inputs"]))


def validate(value):
    exact_fields(value, ("schema_version", "kind", "producer", "entries"), "component catalog")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
            value["kind"] == "crossforge-component-catalog", "unsupported component catalog schema")
    producer = component_artifacts.validate_producer(value["producer"])
    require(producer["kind"] == "github-actions" and producer["invocation"].startswith(
        "https://github.com/" + GITHUB_REPOSITORY + "/actions/runs/"), "catalog requires a trusted repository producer")
    require(type(value["entries"]) is list and 0 < len(value["entries"]) <= 128, "invalid catalog entry count")
    for entry in value["entries"]:
        exact_fields(entry, ("reference", "receipt_sha256", "receipt"), "catalog entry")
        receipt = component_artifacts.validate_receipt(entry["receipt"])
        digest_value(entry["receipt_sha256"], "catalog receipt SHA256")
        require(content_sha256(receipt) == entry["receipt_sha256"], "catalog receipt differs from its digest")
        require(receipt["contract"]["producer"] == producer, "catalog cannot relabel another producer's receipt")
        repository, digest = registry_transfer.reference(entry["reference"])
        require(repository == REPOSITORY and digest == receipt["artifact"]["root_digest"], "catalog registry artifact differs")
    keys = [_key(entry) for entry in value["entries"]]
    require(keys == sorted(set(keys)), "catalog entries must be sorted and unique by component, role, and inputs")
    return value


def document(producer, entries):
    # Validate before sorting so malformed input always fails at the boundary.
    value = {"schema_version": 1, "kind": "crossforge-component-catalog",
             "producer": copy.deepcopy(producer), "entries": copy.deepcopy(entries)}
    require(type(value["entries"]) is list, "catalog entries must be an array")
    for entry in value["entries"]:
        exact_fields(entry, ("reference", "receipt_sha256", "receipt"), "catalog entry")
        component_artifacts.validate_receipt(entry["receipt"])
    value["entries"].sort(key=_key)
    return validate(value)


def regular_bytes(path, maximum):
    path = Path(path).absolute()
    require(path.lstat().st_size <= maximum, "catalog input exceeds size limit: " + str(path))
    before = file_record(path.parent, path.name)
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    require(len(data) <= maximum and hashlib.sha256(data).hexdigest() == before["sha256"] and
            file_record(path.parent, path.name) == before, "catalog input changed while reading: " + str(path))
    return data


def verify(source, catalog, bundle, cosign, temporary_parent=None):
    """Verify exact local snapshots; there is no caller-supplied 'verified' flag.

    source and cosign come from the consumer's trusted checkout/tool setup, not
    the catalog producer. No signature, SCT, or transparency check is disabled.
    """
    data = regular_bytes(catalog, 64 * 1024 * 1024)
    value = validate(parse_json(data))
    require(data == canonical_bytes(value) + b"\n", "catalog must use the canonical signed encoding")
    bundle_data = regular_bytes(bundle, 16 * 1024 * 1024)
    release = load_json(Path(source) / "config/release.json")
    policy = release["sigstore"]
    cosign = Path(cosign).absolute()
    binary = file_record(cosign.parent, cosign.name)
    require(binary["sha256"] == policy["verifier"]["binary"]["sha256"] and int(binary["mode"], 8) & 0o111,
            "catalog verifier differs from the pinned executable")
    encoded_root = regular_bytes(Path(source) / policy["trust"]["trusted_root_evidence"], 1024 * 1024)
    root = base64.b64decode(b"".join(encoded_root.split()), validate=True)
    require(hashlib.sha256(root).hexdigest() == policy["trust"]["trusted_root_sha256"],
            "catalog trust root differs from consumer policy")
    with tempfile.TemporaryDirectory(prefix="component-catalog-", dir=temporary_parent) as temporary:
        directory = Path(temporary)
        blob = directory / "catalog.json"
        signature = directory / "catalog.sigstore.json"
        trusted_root = directory / "trusted_root.json"
        blob.write_bytes(data)
        signature.write_bytes(bundle_data)
        trusted_root.write_bytes(root)
        command = [str(cosign), "verify-blob", "--bundle", str(signature), "--trusted-root", str(trusted_root),
                   "--certificate-identity", SIGNER, "--certificate-oidc-issuer", ISSUER,
                   "--certificate-github-workflow-repository", GITHUB_REPOSITORY,
                   "--certificate-github-workflow-ref", "refs/heads/main",
                   "--certificate-github-workflow-trigger", EVENT,
                   "--certificate-github-workflow-sha", value["producer"]["source_commit"], str(blob)]
        subprocess.run(command, check=True, stdout=sys.stderr, stderr=sys.stderr)
        require(blob.read_bytes() == data and signature.read_bytes() == bundle_data and trusted_root.read_bytes() == root,
                "catalog verification inputs changed during verification")
    require(file_record(cosign.parent, cosign.name) == binary, "catalog verifier changed during verification")
    return value, {"kind": "crossforge-component-catalog-authentication", "schema_version": 1,
        "catalog_sha256": hashlib.sha256(data).hexdigest(), "bundle_sha256": hashlib.sha256(bundle_data).hexdigest(),
        "producer": copy.deepcopy(value["producer"]), "signer": SIGNER, "issuer": ISSUER,
        "event": EVENT, "verifier_sha256": binary["sha256"], "trusted_root_sha256": hashlib.sha256(root).hexdigest()}


def select(source, catalog, bundle, cosign, expected_inputs, role, temporary_parent=None):
    component_inputs.validate(expected_inputs)
    require(role in component_artifacts.ROLES and expected_inputs["scope"] == (
        "qualification" if role == "qualification" else "build"), "catalog selection role or scope differs")
    value, authentication = verify(source, catalog, bundle, cosign, temporary_parent)
    key = (expected_inputs["component"], role, component_inputs.identity(expected_inputs))
    matches = [entry for entry in value["entries"] if _key(entry) == key]
    if not matches:
        return {"status": "missing", "authentication": authentication, "entry": None}
    entry = matches[0]
    component_inputs.require_match(entry["receipt"]["contract"]["inputs"], expected_inputs)
    return {"status": "authenticated-reference", "authentication": authentication, "entry": copy.deepcopy(entry)}
