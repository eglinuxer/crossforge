"""Preserve successful raw-component producers across same-run partial retries.

Independent outputs identify the original handoff and signed catalog bytes.
These checks neither sign anything nor replace the pinned catalog verifier.
"""

import hashlib
from pathlib import Path
import re

from . import component_catalog
from .identity import canonical_bytes, digest_value, exact_fields, parse_json, require


INVOCATION = re.compile(r"https://github\.com/eglinuxer/crossforge/actions/runs/([1-9][0-9]*)/attempts/([1-9][0-9]*)\Z")
ENSURE_OUTPUTS = ("produced", "handoff-sha256", "artifact-id", "producer-invocation")
SIGN_OUTPUTS = ("artifact-id", "catalog-sha256", "bundle-sha256", "producer-invocation", "signer-invocation")


def invocation(value):
    require(type(value) is str, "component invocation must be a string")
    match = INVOCATION.fullmatch(value)
    require(match is not None, "component retry requires an exact trusted run and attempt")
    return tuple(int(part) for part in match.groups())


def prior_invocation(selected, current):
    original_run, original_attempt = invocation(selected)
    current_run, current_attempt = invocation(current)
    require(original_run == current_run and original_attempt <= current_attempt,
            "component producer belongs to another run or a future attempt")
    return selected


def upstream(needs, stage, current):
    require(stage in ("sign", "store"), "unsupported component retry stage")
    job = "ensure" if stage == "sign" else "sign"
    exact_fields(needs, (job,), "component predecessor jobs")
    exact_fields(needs[job], ("result", "outputs"), "component predecessor")
    require(needs[job]["result"] == "success", "component predecessor did not succeed")
    output = needs[job]["outputs"]
    exact_fields(output, ENSURE_OUTPUTS if stage == "sign" else SIGN_OUTPUTS, "component predecessor outputs")
    require(type(output["artifact-id"]) is str and re.fullmatch(r"[1-9][0-9]*", output["artifact-id"]),
            "component artifact ID must be a positive decimal")
    prior_invocation(output["producer-invocation"], current["invocation"])
    if stage == "sign":
        require(output["produced"] == "true", "no new raw components were produced")
        digest_value(output["handoff-sha256"], "original producer handoff SHA256")
    else:
        for name in ("catalog-sha256", "bundle-sha256"):
            digest_value(output[name], "original " + name)
        prior_invocation(output["signer-invocation"], current["invocation"])
        prior_invocation(output["producer-invocation"], output["signer-invocation"])
    return output


def catalog_metadata(directory, current, producer_invocation):
    """Observe exact bytes; workflow verification remains a separate gate."""
    prior_invocation(producer_invocation, current["invocation"])
    directory = Path(directory)
    require({path.name for path in directory.iterdir()} == {"catalog.json", "catalog.sigstore.json", "authentication.json"},
            "signed catalog artifact file set differs")
    data = component_catalog.regular_bytes(directory / "catalog.json", 64 * 1024 * 1024)
    bundle = component_catalog.regular_bytes(directory / "catalog.sigstore.json", 16 * 1024 * 1024)
    component_catalog.regular_bytes(directory / "authentication.json", 1024 * 1024)
    catalog = component_catalog.validate(parse_json(data))
    require(catalog["schema_version"] in (2, 3), "component retry supports raw production catalogs only")
    require(data == canonical_bytes(catalog) + b"\n", "component retry catalog encoding differs")
    require(catalog["producer"]["source_commit"] == current["source_commit"] and
            catalog["producer"]["invocation"] == producer_invocation,
            "catalog source or original component producer differs")
    return {"catalog-sha256": hashlib.sha256(data).hexdigest(), "bundle-sha256": hashlib.sha256(bundle).hexdigest(),
            "producer-invocation": producer_invocation}


def verify_catalog(directory, needs, current):
    original = upstream(needs, "store", current)
    actual = catalog_metadata(directory, current, original["producer-invocation"])
    require(all(actual[key] == original[key] for key in actual), "signed catalog differs from the original successful signer")
    return actual
