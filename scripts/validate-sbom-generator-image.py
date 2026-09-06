#!/usr/bin/env python3
"""Validate the pinned SBOM generator image against its SLSA source identity."""

import argparse
import json
import runpy
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
ATTESTATIONS = runpy.run_path(str(REPOSITORY / "scripts/image_attestations.py"))
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]
AttestationError = ATTESTATIONS["AttestationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/sbom-generator-image.schema.json"
CONFIG_SOURCE = (
    "https://github.com/docker/buildkit-syft-scanner.git#refs/tags/v1.12.0"
)


class GeneratorError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise GeneratorError(message)


def nested_values(value, key):
    result = []
    if isinstance(value, dict):
        for name, child in value.items():
            if name == key:
                result.append(child)
            result.extend(nested_values(child, key))
    elif isinstance(value, list):
        for child in value:
            result.extend(nested_values(child, key))
    return result


def one(records, label):
    require(len(records) == 1, "%s is not unique" % label)
    return records[0]


def validate(arguments):
    release = STRICT["load_json"](arguments.release)
    release_schema = STRICT["load_json"](arguments.release_schema)
    STRICT["validate_schema_subset"](release_schema)
    STRICT["validate"](release, release_schema, release_schema, "$")
    generator = release["sbom"]["generator"]
    source = generator["source"]
    index_payload = ATTESTATIONS["regular_bytes"](arguments.index, "generator index")
    require(
        ATTESTATIONS["sha256_bytes"](index_payload) == generator["digest"],
        "SBOM generator index digest differs",
    )
    index = ATTESTATIONS["json_bytes"](index_payload, "generator index")
    require(
        index.get("schemaVersion") == 2
        and index.get("mediaType") in ATTESTATIONS["INDEX_MEDIA_TYPES"],
        "SBOM generator index format differs",
    )
    manifests = index.get("manifests")
    require(isinstance(manifests, list), "SBOM generator manifest list is invalid")
    platform = one(
        [
            record
            for record in manifests
            if isinstance(record.get("platform"), dict)
            and record["platform"].get("os") == "linux"
            and record["platform"].get("architecture") == "amd64"
        ],
        "SBOM generator amd64 manifest",
    )
    platform_identity = ATTESTATIONS["digest_descriptor"](
        platform, "SBOM generator amd64 manifest"
    )
    require(
        platform_identity["digest"] == generator["manifest_digest"],
        "SBOM generator amd64 digest differs",
    )
    manifest_payload = ATTESTATIONS["regular_bytes"](
        arguments.manifest, "generator amd64 manifest"
    )
    ATTESTATIONS["validate_blob"](
        manifest_payload, platform, "generator amd64 manifest"
    )
    attestation = one(
        [
            record
            for record in manifests
            if record.get("annotations", {}).get("vnd.docker.reference.type")
            == "attestation-manifest"
            and record.get("annotations", {}).get("vnd.docker.reference.digest")
            == generator["manifest_digest"]
        ],
        "SBOM generator attestation manifest",
    )
    require(
        attestation.get("digest")
        == generator["provenance"]["attestation_manifest_digest"],
        "SBOM generator attestation digest differs",
    )
    attestation_payload = ATTESTATIONS["regular_bytes"](
        arguments.attestation_manifest, "generator attestation manifest"
    )
    ATTESTATIONS["validate_blob"](
        attestation_payload, attestation, "generator attestation manifest"
    )
    attestation_document = ATTESTATIONS["json_bytes"](
        attestation_payload, "generator attestation manifest"
    )
    require(
        attestation_document.get("artifactType")
        == ATTESTATIONS["ATTESTATION_ARTIFACT"],
        "SBOM generator attestation artifact type differs",
    )
    subject = attestation_document.get("subject")
    require(
        isinstance(subject, dict)
        and subject.get("digest") == generator["manifest_digest"]
        and subject.get("size") == platform_identity["size"],
        "SBOM generator attestation subject differs",
    )
    layers = attestation_document.get("layers")
    require(isinstance(layers, list), "SBOM generator attestation layers differ")
    provenance_descriptor = one(
        [
            layer
            for layer in layers
            if layer.get("annotations", {}).get("in-toto.io/predicate-type")
            == ATTESTATIONS["PROVENANCE"]
        ],
        "SBOM generator provenance",
    )
    require(
        provenance_descriptor.get("digest")
        == generator["provenance"]["predicate_digest"]
        and provenance_descriptor.get("mediaType") == ATTESTATIONS["IN_TOTO"],
        "SBOM generator provenance digest differs",
    )
    provenance_payload = ATTESTATIONS["regular_bytes"](
        arguments.provenance, "generator provenance"
    )
    ATTESTATIONS["validate_blob"](
        provenance_payload, provenance_descriptor, "generator provenance"
    )
    statement = ATTESTATIONS["json_bytes"](
        provenance_payload, "generator provenance"
    )
    require(
        statement.get("_type") == ATTESTATIONS["STATEMENT"]
        and statement.get("predicateType") == ATTESTATIONS["PROVENANCE"],
        "SBOM generator provenance statement differs",
    )
    ATTESTATIONS["statement_subject"](
        statement, generator["manifest_digest"], "SBOM generator provenance"
    )
    predicate = statement.get("predicate")
    definition = predicate.get("buildDefinition") if isinstance(predicate, dict) else None
    require(
        isinstance(definition, dict)
        and definition.get("buildType") == ATTESTATIONS["BUILDKIT"],
        "SBOM generator provenance build type differs",
    )
    external = definition.get("externalParameters")
    config_source = external.get("configSource") if isinstance(external, dict) else None
    request = external.get("request") if isinstance(external, dict) else None
    require(
        config_source
        == {
            "uri": CONFIG_SOURCE,
            "digest": {"sha1": source["tag_object"]},
            "path": "Dockerfile",
        },
        "SBOM generator provenance config source differs",
    )
    require(
        isinstance(request, dict)
        and request.get("args", {}).get("build-arg:GIT_REF")
        == "refs/tags/" + source["tag"],
        "SBOM generator provenance tag request differs",
    )
    require(
        set(nested_values(definition, "git.checksum")) == {source["commit"]},
        "SBOM generator provenance peeled commit differs",
    )
    run_details = predicate.get("runDetails")
    metadata = run_details.get("metadata") if isinstance(run_details, dict) else None
    require(
        isinstance(run_details, dict)
        and run_details.get("builder", {}).get("id")
        == generator["provenance"]["builder_id"],
        "SBOM generator provenance builder differs",
    )
    require(
        isinstance(metadata, dict)
        and metadata.get("buildkit_completeness", {}).get("request") is True
        and nested_values(definition, "llbDefinition"),
        "SBOM generator provenance is not max mode",
    )
    report = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-sbom-generator-image",
        "version": generator["version"],
        "repository": generator["repository"],
        "index_digest": generator["digest"],
        "manifest_digest": generator["manifest_digest"],
        "attestation_manifest_digest": generator["provenance"]
        ["attestation_manifest_digest"],
        "provenance_digest": generator["provenance"]["predicate_digest"],
        "source": {
            "tag": source["tag"],
            "tag_object": source["tag_object"],
            "commit": source["commit"],
        },
        "checks": {
            "index": True,
            "platform": True,
            "oci_subject": True,
            "slsa_v1": True,
            "source": True,
            "max_provenance": True,
        },
    }
    schema = STRICT["load_json"](arguments.schema)
    STRICT["validate_schema_subset"](schema)
    require(schema.get("$id") == SCHEMA_ID, "SBOM generator report schema differs")
    STRICT["validate"](report, schema, schema, "$")
    output = Path(arguments.output)
    state = (
        "wrote"
        if ATTESTATIONS["write_json_once"](output, report)
        else "current"
    )
    print("%s SBOM generator image evidence: %s" % (state, output))
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--index", type=Path, required=True)
    result.add_argument("--manifest", type=Path, required=True)
    result.add_argument("--attestation-manifest", type=Path, required=True)
    result.add_argument("--provenance", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument(
        "--release", type=Path, default=REPOSITORY / "config/release.json"
    )
    result.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    result.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/sbom-generator-image.schema.json",
    )
    return result


def main(argv=None):
    try:
        validate(parser().parse_args(argv))
        return 0
    except (
        AttestationError,
        GeneratorError,
        KeyError,
        OSError,
        TypeError,
        ValidationError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
