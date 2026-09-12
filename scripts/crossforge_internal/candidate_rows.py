"""Consume signed Python rows and retain their original candidate lineage.

Only resolve() establishes trust: it authenticates the catalog and verifies the
OCI bytes, installed files and original execution with the existing row verifier.
The durable records below are checked checkpoint contents, not a new trust root.
Recovering a published SDK requires its independently selected immutable GitHub
checkpoint; these records never authorize rebuilding from unverified row pins.
"""

import copy
import hashlib
import json
from pathlib import Path

from . import component_artifacts, component_catalog, component_inputs, component_resolution
from . import python_components, python_handoff, python_qualification, python_row_resolution
from . import python_sdk, python_sdk_recovery
from .identity import content_sha256, digest_value, exact_fields, load_json, require


def validate(rows):
    require(type(rows) is dict, "candidate reused rows must be an object")
    for row, value in rows.items():
        exact_fields(value, ("pin", "receipt", "qualification", "authentication"), "candidate reused row")
        pin = python_sdk_recovery.validate_row_pin(value["pin"], row)
        receipt = component_artifacts.validate_receipt(value["receipt"])
        contract, artifact = receipt["contract"], receipt["artifact"]
        require(content_sha256(receipt) == pin["receipt_sha256"] and contract["producer"] == pin["producer"] and
                contract["role"] == pin["role"] and contract["inputs"]["component"] == pin["component"] and
                component_inputs.identity(contract["inputs"]) == pin["inputs_sha256"] and
                pin["reference"] == component_catalog.REPOSITORY + "@" + artifact["root_digest"],
                "candidate row receipt differs from its original selection")
        require([item["path"] for item in receipt["metadata"]] == python_qualification.metadata_paths(),
                "candidate row evidence set differs")
        prior = value["qualification"]
        exact_fields(prior, ("schema_version", "kind", "mode", "inputs_sha256", "producer", "started_at",
                            "completed_at", "vertices", "coverage"), "candidate prior row execution")
        require(type(prior["schema_version"]) is int and prior["schema_version"] == 1 and
                prior["kind"] == "crossforge-component-qualification" and prior["mode"] == "executed" and
                prior["inputs_sha256"] == pin["inputs_sha256"] and prior["producer"] == pin["producer"],
                "candidate cannot relabel a prior row qualification")
        metadata = {item["path"]: item for item in receipt["metadata"]}
        encoded = (json.dumps(prior, sort_keys=True, indent=2) + "\n").encode("utf-8")
        require(hashlib.sha256(encoded).hexdigest() == metadata[python_qualification.RECORD_PATH]["sha256"],
                "candidate prior row execution differs from sealed metadata")
        coverage = prior["coverage"]
        require(type(coverage) is dict and coverage.get("row") == row and coverage.get("status") == "passed" and
                coverage.get("manifest_sha256") == metadata["component/reports/row.json"]["sha256"],
                "candidate prior row coverage differs")
        authentication = value["authentication"]
        exact_fields(authentication, ("schema_version", "kind", "catalog_sha256", "bundle_sha256", "producer",
            "signer", "issuer", "event", "verifier_sha256", "trusted_root_sha256"), "candidate row authentication")
        require(type(authentication["schema_version"]) is int and authentication["schema_version"] == 1 and
                authentication["kind"] == "crossforge-component-catalog-authentication" and
                authentication["producer"] == pin["producer"] and authentication["issuer"] == component_catalog.ISSUER and
                authentication["signer"] == "https://github.com/" + component_catalog.GITHUB_REPOSITORY + "/" +
                    component_catalog.PYTHON_ROW_WORKFLOW + "@refs/heads/main" and
                authentication["event"] in ("push", "workflow_dispatch"), "candidate row authentication identity differs")
        for field in ("catalog_sha256", "bundle_sha256", "verifier_sha256", "trusted_root_sha256"):
            digest_value(authentication[field], "candidate row " + field)
    return rows


def dependency(row, value):
    return {"component": "qualification/python-" + row, "inputs_sha256": value["pin"]["inputs_sha256"],
            "artifact_digest": value["receipt"]["artifact"]["platform_digest"]}


def inputs(source, graph, row, execution, raw_bindings):
    settings = python_qualification.spec(source, row)
    bindings = {key: value for key, value in raw_bindings.items() if key.split(":", 1)[0] in settings["replay"]}
    resolved = copy.deepcopy(graph)
    for arch in python_components.ARCHES:
        build = "cpython-%s-%s-qualify-build" % (row, arch)
        runtime = "cpython-%s-%s-qualify" % (row, arch)
        subject = bindings[build + ":crossforge_toolchain"]
        require(subject["component"] == "toolchain/%s-install" % arch, "candidate row toolchain identity differs")
        reference = resolved["target"][build]["contexts"]["crossforge_toolchain"]
        contexts = resolved["target"][runtime]["contexts"]
        require(contexts["crossforge_sysroot"] in ("target:sysroot-" + arch, reference),
                "candidate row sysroot boundary differs")
        # The original row resolver obtains both runtime sysroots from the
        # verified toolchain installations. Match that seven-subject graph,
        # rather than recapturing the separate raw SDK sysroot producers.
        contexts["crossforge_sysroot"] = reference
        bindings[runtime + ":crossforge_sysroot"] = subject
    return python_qualification.inputs(source, resolved, settings, execution, bindings)


def check_inputs(source, execution, rows, raw):
    """Recheck durable lineage against current policy and selected raw inputs."""
    validate(rows)
    require(set(rows) <= set(python_sdk.matrix(source)), "candidate contains an unsupported reused row")
    policy = load_json(Path(source) / "config/release.json")["sigstore"]
    for row, value in rows.items():
        inputs = value["receipt"]["contract"]["inputs"]
        component_inputs.verify_files(inputs, source)
        require(inputs["parameters"].get("execution") == execution and
                inputs["parameters"].get("qualification") == python_qualification.spec(source, row),
                "candidate prior row policy or physical environment differs")
        # This also reads historical SDK checkpoints on a different worker.
        # Current inspection binaries are bound by inputs() during preparation
        # and each pre/post-build check, not substituted during image recovery.
        auth = value["authentication"]
        require(auth["verifier_sha256"] == policy["verifier"]["binary"]["sha256"] and
                auth["trusted_root_sha256"] == policy["trust"]["trusted_root_sha256"],
                "candidate prior row verifier or trust root differs")
        wanted = {"toolchain/%s-install" % arch for arch in python_components.ARCHES} | {
            python_components.spec(source, row, arch, kind)["component"] for _, arch, kind in python_handoff.PARTS}
        require({item["component"] for item in inputs["dependencies"]} == wanted,
                "candidate prior row must bind exactly seven raw subjects")
        by_component = {item["component"]: item for item in raw["components"].values()}
        for item in inputs["dependencies"]:
            require(item["component"] in by_component and
                    by_component[item["component"]]["inputs_sha256"] == item["inputs_sha256"],
                    "candidate prior row raw selection differs")
    return rows


def bind(source, original, resolved, raw_bindings, execution, results, cosign, data, evidence, builder, oras,
         docker_config=None):
    """Replace only rows accepted by the original authenticated row resolver."""
    graph, rows, bindings = copy.deepcopy(resolved), {}, {}
    for row in python_sdk.matrix(source):
        subjects = {arch + "-toolchain": results[arch + "-toolchain-install"]["subject"]
                    for arch in python_components.ARCHES}
        subjects.update({part: results[row + "-" + part]["subject"] for part, _, _ in python_handoff.PARTS})
        directory = Path(data) / row
        try:
            result = python_row_resolution.resolve(source, original, row, execution, subjects, cosign,
                directory, builder, oras, docker_config)
        finally:
            if directory.exists():
                component_resolution.preserve_evidence(directory, Path(evidence) / row)
        if result["status"] == "qualification-required":
            require(result.get("reason") == "catalog-index-absent", "candidate row fallback requires an absent index")
            continue
        require(result["status"] == "verified-qualified-row", "unsupported candidate row resolution")
        pin = python_sdk_recovery.row_pin(result, row)
        receipt = load_json(result["subject"]["receipt"])
        expected = inputs(source, resolved, row, execution, raw_bindings)
        component_inputs.require_match(receipt["contract"]["inputs"], expected)
        verified = result["verification"]
        require(verified["mode"] == "verified-prior-execution" and verified["receipt_sha256"] == pin["receipt_sha256"] and
                verified["reference"] == "oci-layout://%s@%s" %
                    (Path(result["subject"]["layout"]).resolve(), receipt["artifact"]["platform_digest"]),
                "candidate row verified artifact differs")
        rows[row] = {"pin": pin, "receipt": receipt, "qualification": copy.deepcopy(verified["qualification"]),
                     "authentication": copy.deepcopy(result["authentication"])}
        validate({row: rows[row]})
        target = "python-dev-append-" + row
        require(graph["target"][target]["contexts"]["crossforge_python_row"] == "target:python-row-" + row,
                "candidate row consumer boundary changed")
        graph["target"][target]["contexts"]["crossforge_python_row"] = verified["reference"]
        bindings[target + ":crossforge_python_row"] = dependency(row, rows[row])
    return graph, bindings, rows
