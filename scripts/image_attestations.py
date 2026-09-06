#!/usr/bin/env python3
"""Validate digest-bound BuildKit max-provenance and SPDX attestations."""

import argparse
import hashlib
import json
import os
import re
import runpy
import stat
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_ID = "https://crossforge.dev/schemas/image-attestations.schema.json"
INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
ATTESTATION_ARTIFACT = "application/vnd.docker.attestation.manifest.v1+json"
IN_TOTO = "application/vnd.in-toto+json"
PROVENANCE = "https://slsa.dev/provenance/v0.2"
SPDX = "https://spdx.dev/Document"
STATEMENT = "https://in-toto.io/Statement/v0.1"
BUILDKIT = "https://mobyproject.org/buildkit@v1"
BUILDKIT_METADATA = "https://mobyproject.org/buildkit@v1#metadata"
EMPTY_CONFIG_DIGEST = "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
MAX_FILE_SIZE = 256 * 1024 * 1024
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)

STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]


class AttestationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise AttestationError(message)


def sha256_bytes(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def regular_bytes(path, label):
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise AttestationError("cannot inspect %s: %s" % (label, error))
    require(stat.S_ISREG(metadata.st_mode), "%s is not a regular file" % label)
    require(not path.is_symlink(), "%s is a symlink" % label)
    require(0 < metadata.st_size <= MAX_FILE_SIZE, "%s size is invalid" % label)
    return path.read_bytes()


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def reject_nonfinite(value):
    raise AttestationError("non-finite JSON value: %s" % value)


def json_bytes(payload, label):
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite,
        )
    except (UnicodeError, ValueError) as error:
        raise AttestationError("cannot parse %s: %s" % (label, error))
    require(isinstance(document, dict), "%s must contain an object" % label)
    return document


def digest_descriptor(descriptor, label):
    require(isinstance(descriptor, dict), "%s descriptor is invalid" % label)
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    require(DIGEST_RE.match(digest or ""), "%s digest is invalid" % label)
    require(type(size) is int and 0 < size <= MAX_FILE_SIZE, "%s size is invalid" % label)
    return {"digest": digest, "size": size}


def validate_blob(payload, descriptor, label):
    identity = digest_descriptor(descriptor, label)
    require(len(payload) == identity["size"], "%s byte size differs" % label)
    require(sha256_bytes(payload) == identity["digest"], "%s digest differs" % label)
    return identity


def statement_subject(statement, platform_digest, label):
    subjects = statement.get("subject")
    require(isinstance(subjects, list) and subjects, "%s subject is absent" % label)
    expected = platform_digest.split(":", 1)[1]
    require(
        all(
            isinstance(subject, dict)
            and isinstance(subject.get("name"), str)
            and subject.get("name")
            and subject.get("digest") == {"sha256": expected}
            for subject in subjects
        ),
        "%s subject digest differs" % label,
    )


def validate_provenance(statement, platform_digest, source_commit, expected_target):
    require(statement.get("_type") == STATEMENT, "provenance statement type differs")
    require(statement.get("predicateType") == PROVENANCE, "provenance predicate type differs")
    statement_subject(statement, platform_digest, "provenance")
    predicate = statement.get("predicate")
    require(isinstance(predicate, dict), "provenance predicate is invalid")
    require(predicate.get("buildType") == BUILDKIT, "provenance build type differs")
    invocation = predicate.get("invocation")
    require(isinstance(invocation, dict), "provenance invocation is absent")
    parameters = invocation.get("parameters")
    args = parameters.get("args") if isinstance(parameters, dict) else None
    require(
        isinstance(args, dict) and args.get("target") == expected_target,
        "provenance build target differs",
    )
    build_config = predicate.get("buildConfig")
    require(
        isinstance(build_config, dict)
        and isinstance(build_config.get("llbDefinition"), list)
        and build_config["llbDefinition"],
        "provenance is not max mode",
    )
    metadata = predicate.get("metadata")
    require(isinstance(metadata, dict), "provenance metadata is absent")
    completeness = metadata.get("completeness")
    require(
        isinstance(completeness, dict)
        and completeness.get("parameters") is True
        and completeness.get("environment") is True,
        "provenance completeness differs",
    )
    buildkit_metadata = metadata.get(BUILDKIT_METADATA)
    vcs = buildkit_metadata.get("vcs") if isinstance(buildkit_metadata, dict) else None
    require(
        isinstance(vcs, dict) and vcs.get("revision") == source_commit,
        "provenance source revision differs",
    )


def validate_spdx(statement, platform_digest):
    require(statement.get("_type") == STATEMENT, "SBOM statement type differs")
    require(statement.get("predicateType") == SPDX, "SBOM predicate type differs")
    statement_subject(statement, platform_digest, "SBOM")
    predicate = statement.get("predicate")
    require(
        isinstance(predicate, dict)
        and isinstance(predicate.get("spdxVersion"), str)
        and predicate["spdxVersion"].startswith("SPDX-2.")
        and predicate.get("SPDXID") == "SPDXRef-DOCUMENT"
        and predicate.get("dataLicense") == "CC0-1.0"
        and isinstance(predicate.get("creationInfo"), dict),
        "SBOM is not an SPDX document",
    )


def create_report(arguments):
    require(arguments.image_kind in ("sdk-candidate", "source-bundle"), "image kind differs")
    require(REPOSITORY_RE.match(arguments.repository or ""), "repository is invalid")
    require(COMMIT_RE.match(arguments.source_commit or ""), "source commit is invalid")
    require(DIGEST_RE.match(arguments.expected_index_digest or ""), "index digest is invalid")
    require(DIGEST_RE.match(arguments.platform_manifest_digest or ""), "platform digest is invalid")
    index_payload = regular_bytes(arguments.index, "OCI index")
    require(
        sha256_bytes(index_payload) == arguments.expected_index_digest,
        "OCI index digest differs",
    )
    index = json_bytes(index_payload, "OCI index")
    require(index.get("schemaVersion") == 2, "OCI index schema differs")
    require(index.get("mediaType") in INDEX_MEDIA_TYPES, "OCI index media type differs")
    manifests = index.get("manifests")
    require(isinstance(manifests, list), "OCI index manifests are invalid")
    platform_matches = [
        descriptor
        for descriptor in manifests
        if isinstance(descriptor.get("platform"), dict)
        and descriptor["platform"].get("os") == "linux"
        and descriptor["platform"].get("architecture") == "amd64"
    ]
    require(len(platform_matches) == 1, "OCI index platform descriptor differs")
    platform = platform_matches[0]
    platform_identity = digest_descriptor(platform, "platform manifest")
    require(
        platform_identity["digest"] == arguments.platform_manifest_digest,
        "platform manifest digest differs",
    )
    attestation_matches = [
        descriptor
        for descriptor in manifests
        if isinstance(descriptor.get("platform"), dict)
        and descriptor["platform"].get("os") == "unknown"
        and descriptor["platform"].get("architecture") == "unknown"
        and descriptor.get("annotations", {}).get("vnd.docker.reference.type")
        == "attestation-manifest"
        and descriptor.get("annotations", {}).get("vnd.docker.reference.digest")
        == arguments.platform_manifest_digest
    ]
    require(
        len(manifests) == 2 and len(attestation_matches) == 1,
        "OCI index attestation descriptor set differs",
    )
    attestation_descriptor = attestation_matches[0]
    attestation_payload = regular_bytes(
        arguments.attestation_manifest, "attestation manifest"
    )
    attestation_identity = validate_blob(
        attestation_payload, attestation_descriptor, "attestation manifest"
    )
    manifest = json_bytes(attestation_payload, "attestation manifest")
    require(manifest.get("schemaVersion") == 2, "attestation manifest schema differs")
    require(manifest.get("mediaType") == OCI_MANIFEST, "attestation manifest media type differs")
    require(manifest.get("artifactType") == ATTESTATION_ARTIFACT, "attestation artifact type differs")
    require(
        manifest.get("config")
        == {
            "mediaType": "application/vnd.oci.empty.v1+json",
            "digest": EMPTY_CONFIG_DIGEST,
            "size": 2,
            "data": "e30=",
        },
        "attestation empty config differs",
    )
    subject = manifest.get("subject")
    require(isinstance(subject, dict), "attestation manifest subject is absent")
    require(
        subject.get("mediaType") == platform.get("mediaType")
        and subject.get("digest") == platform_identity["digest"]
        and subject.get("size") == platform_identity["size"],
        "attestation manifest subject differs",
    )
    if "platform" in subject:
        require(
            isinstance(subject["platform"], dict)
            and subject["platform"].get("os") == "linux"
            and subject["platform"].get("architecture") == "amd64",
            "attestation manifest subject platform differs",
        )
    layers = manifest.get("layers")
    require(isinstance(layers, list) and len(layers) == 2, "attestation layers differ")
    by_type = {}
    for layer in layers:
        predicate_type = layer.get("annotations", {}).get("in-toto.io/predicate-type")
        require(predicate_type in (PROVENANCE, SPDX), "unknown attestation predicate")
        require(predicate_type not in by_type, "duplicate attestation predicate")
        require(layer.get("mediaType") == IN_TOTO, "attestation layer media type differs")
        by_type[predicate_type] = layer
    require(set(by_type) == {PROVENANCE, SPDX}, "required attestations are absent")
    payload_paths = {PROVENANCE: arguments.provenance, SPDX: arguments.sbom}
    records = []
    for predicate_type in sorted(by_type):
        payload = regular_bytes(payload_paths[predicate_type], predicate_type)
        identity = validate_blob(payload, by_type[predicate_type], predicate_type)
        statement = json_bytes(payload, predicate_type)
        require(
            statement.get("predicateType") == predicate_type,
            "attestation descriptor and statement predicate differ",
        )
        if predicate_type == PROVENANCE:
            validate_provenance(
                statement,
                arguments.platform_manifest_digest,
                arguments.source_commit,
                arguments.image_kind,
            )
        else:
            validate_spdx(statement, arguments.platform_manifest_digest)
        records.append(
            {
                "predicate_type": predicate_type,
                "digest": identity["digest"],
                "size": identity["size"],
                "statement_type": statement["_type"],
            }
        )
    return {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-public-image-attestations",
        "image_kind": arguments.image_kind,
        "source_commit": arguments.source_commit,
        "repository": arguments.repository,
        "index_digest": arguments.expected_index_digest,
        "platform_manifest_digest": arguments.platform_manifest_digest,
        "attestation_manifest": attestation_identity,
        "attestations": records,
        "checks": {
            "oci_artifact": True,
            "subject_bound": True,
            "blob_digests": True,
            "max_provenance": True,
            "source_revision": True,
            "spdx_document": True,
        },
    }


def validate_schema(document, schema_path):
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    require(schema.get("$id") == SCHEMA_ID, "attestation schema identity differs")
    try:
        STRICT["validate"](document, schema, schema, "$")
    except ValidationError as error:
        raise AttestationError("attestation report schema failed: %s" % error)
    return document


def write_json_once(path, document):
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "attestation report output is a symlink")
    if path.exists():
        require(path.read_text(encoding="utf-8") == payload, "attestation report differs")
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return True


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for name in (
        "index",
        "attestation-manifest",
        "provenance",
        "sbom",
    ):
        result.add_argument("--" + name, type=Path, required=True)
    result.add_argument("--expected-index-digest", required=True)
    result.add_argument("--platform-manifest-digest", required=True)
    result.add_argument(
        "--image-kind", choices=("sdk-candidate", "source-bundle"), required=True
    )
    result.add_argument("--repository", required=True)
    result.add_argument("--source-commit", required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/image-attestations.schema.json",
    )
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        report = create_report(arguments)
        validate_schema(report, arguments.schema)
        state = "wrote" if write_json_once(arguments.output, report) else "current"
        print("%s public image attestation report: %s" % (state, arguments.output))
        return 0
    except (AttestationError, KeyError, OSError, TypeError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
