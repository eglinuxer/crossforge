#!/usr/bin/env python3
"""Validate checked-in OCI/Git evidence, CPython patches, and Sigstore bundles."""

import argparse
import base64
import binascii
import hashlib
import json
import sys
from pathlib import Path


class EvidenceError(ValueError):
    pass


OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
SLSA_V1 = "https://slsa.dev/provenance/v1"


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError("%s: %s" % (path, error)) from error


def load_evidence(repository, relative_path):
    root = repository.resolve()
    path = (repository / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise EvidenceError("evidence path escaped repository: %s" % relative_path) from error
    require(path.suffix == ".b64", "evidence must use an exact-byte base64 envelope")
    try:
        encoded = b"".join(path.read_bytes().split())
        return base64.b64decode(encoded, validate=True)
    except (OSError, binascii.Error) as error:
        raise EvidenceError("%s: %s" % (path, error)) from error


def load_cpython_patch(repository, relative_path):
    require(
        isinstance(relative_path, str)
        and relative_path.startswith("patches/cpython/")
        and relative_path.endswith(".patch"),
        "invalid CPython patch path: %r" % relative_path,
    )
    root = repository.resolve()
    path = (repository / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise EvidenceError(
            "CPython patch path escaped repository: %s" % relative_path
        ) from error
    require(
        path.as_posix() == (root / relative_path).as_posix(),
        "CPython patch path is not canonical: %s" % relative_path,
    )
    try:
        payload = path.read_bytes()
        payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise EvidenceError("%s: %s" % (path, error)) from error
    require(payload, "CPython patch is empty: %s" % relative_path)
    return payload


def sha256(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def evidence_json(repository, relative_path, expected_digest, expected_size=None):
    payload = load_evidence(repository, relative_path)
    require(sha256(payload) == expected_digest, "%s: digest mismatch" % relative_path)
    if expected_size is not None:
        require(len(payload) == expected_size, "%s: size mismatch" % relative_path)
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError("%s: %s" % (relative_path, error)) from error
    return payload, document


def one(values, description):
    values = list(values)
    require(len(values) == 1, "expected one %s, found %d" % (description, len(values)))
    return values[0]


def platform_manifest(index, architecture, variant=None):
    return one(
        [
            descriptor
            for descriptor in index.get("manifests", [])
            if descriptor.get("platform", {}).get("os") == "linux"
            and descriptor.get("platform", {}).get("architecture") == architecture
            and descriptor.get("platform", {}).get("variant") == variant
        ],
        "linux/%s manifest" % architecture,
    )


def git_object_id(kind, payload):
    header = ("%s %d\0" % (kind, len(payload))).encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def git_headers(payload):
    header, separator, _message = payload.partition(b"\n\n")
    require(separator, "Git object has no header/message boundary")
    result = {}
    try:
        lines = header.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise EvidenceError("Git object headers are not UTF-8") from error
    for line in lines:
        key, separator, value = line.partition(" ")
        require(separator and key and value, "malformed Git object header")
        result.setdefault(key, []).append(value)
    return result


def single_header(headers, key):
    values = headers.get(key, [])
    require(len(values) == 1, "Git object must have exactly one %s header" % key)
    return values[0]


def validate_evidence(config, repository):
    base = config["base_image"]
    _rocky_payload, rocky_index = evidence_json(
        repository,
        base["index_evidence"],
        base["digest"],
    )
    require(rocky_index.get("schemaVersion") == 2, "Rocky OCI schema mismatch")
    require(
        rocky_index.get("mediaType") == OCI_INDEX,
        "Rocky evidence is not an OCI index",
    )
    rocky_amd64 = platform_manifest(rocky_index, "amd64")
    rocky_arm64 = platform_manifest(rocky_index, "arm64", "v8")
    require(
        rocky_amd64.get("digest") == base["manifests"]["amd64"],
        "Rocky amd64 child manifest mismatch",
    )
    require(
        rocky_arm64.get("digest") == base["manifests"]["arm64"],
        "Rocky arm64 child manifest mismatch",
    )

    qemu = config["qemu"]
    executor = qemu["executor"]
    provenance_config = executor["provenance"]
    source = executor["source"]
    qemu_index_payload, qemu_index = evidence_json(
        repository,
        executor["index_evidence"],
        executor["index_digest"],
    )
    require(
        qemu_index.get("mediaType") == OCI_INDEX,
        "QEMU evidence is not an OCI index",
    )
    index_annotations = qemu_index.get("annotations", {})
    require(
        index_annotations.get("org.opencontainers.image.revision")
        == provenance_config["builder_commit"]
        and index_annotations.get("org.opencontainers.image.version")
        == executor["tag"],
        "QEMU index annotations mismatch",
    )
    qemu_amd64 = platform_manifest(qemu_index, "amd64")
    qemu_manifest_payload, qemu_manifest = evidence_json(
        repository,
        executor["manifest_evidence"],
        executor["manifest_digest"],
        qemu_amd64.get("size"),
    )
    require(
        qemu_amd64.get("digest") == executor["manifest_digest"],
        "QEMU index amd64 manifest mismatch",
    )
    require(
        qemu_manifest.get("mediaType") == OCI_MANIFEST,
        "QEMU subject is not an OCI manifest",
    )
    manifest_annotations = qemu_manifest.get("annotations", {})
    require(
        manifest_annotations.get("org.opencontainers.image.revision")
        == provenance_config["builder_commit"],
        "QEMU manifest builder revision mismatch",
    )
    require(
        manifest_annotations.get("org.opencontainers.image.version") == executor["tag"],
        "QEMU manifest tag mismatch",
    )

    attestation_descriptor = one(
        [
            descriptor
            for descriptor in qemu_index.get("manifests", [])
            if descriptor.get("annotations", {}).get("vnd.docker.reference.type")
            == "attestation-manifest"
            and descriptor.get("annotations", {}).get(
                "vnd.docker.reference.digest"
            )
            == executor["manifest_digest"]
        ],
        "QEMU amd64 attestation manifest",
    )
    attestation_payload, attestation = evidence_json(
        repository,
        provenance_config["attestation_evidence"],
        provenance_config["attestation_manifest_digest"],
        attestation_descriptor.get("size"),
    )
    require(
        attestation_descriptor.get("digest")
        == provenance_config["attestation_manifest_digest"],
        "QEMU attestation descriptor mismatch",
    )
    require(
        attestation.get("mediaType") == OCI_MANIFEST,
        "QEMU attestation is not an OCI manifest",
    )
    require(
        attestation.get("artifactType")
        == "application/vnd.docker.attestation.manifest.v1+json",
        "QEMU attestation artifact type mismatch",
    )
    subject = attestation.get("subject", {})
    require(
        subject.get("digest") == executor["manifest_digest"]
        and subject.get("size") == len(qemu_manifest_payload),
        "QEMU attestation subject mismatch",
    )
    layer = one(attestation.get("layers", []), "QEMU provenance layer")
    require(
        layer.get("mediaType") == "application/vnd.in-toto+json",
        "invalid provenance media type",
    )
    require(
        layer.get("digest") == provenance_config["predicate_digest"]
        and layer.get("size") == provenance_config["predicate_size"],
        "QEMU provenance layer mismatch",
    )
    require(
        layer.get("annotations", {}).get("in-toto.io/predicate-type") == SLSA_V1,
        "QEMU provenance predicate annotation mismatch",
    )

    _predicate_payload, statement = evidence_json(
        repository,
        provenance_config["predicate_evidence"],
        provenance_config["predicate_digest"],
        provenance_config["predicate_size"],
    )
    require(
        statement.get("_type") == "https://in-toto.io/Statement/v0.1",
        "QEMU in-toto statement type mismatch",
    )
    require(statement.get("predicateType") == SLSA_V1, "QEMU predicate type mismatch")
    subject_digest = executor["manifest_digest"].split(":", 1)[1]
    one(
        [
            item
            for item in statement.get("subject", [])
            if item.get("digest", {}).get("sha256") == subject_digest
        ],
        "QEMU provenance subject",
    )
    build_definition = statement.get("predicate", {}).get("buildDefinition", {})
    external = build_definition.get("externalParameters", {})
    config_source = external.get("configSource", {})
    builder_uri = provenance_config["builder_repository"] + "#refs/heads/master"
    require(
        config_source.get("uri") == builder_uri
        and config_source.get("digest", {}).get("sha1")
        == provenance_config["builder_commit"]
        and config_source.get("path") == "Dockerfile",
        "QEMU builder config source mismatch",
    )
    one(
        [
            dependency
            for dependency in build_definition.get("resolvedDependencies", [])
            if dependency.get("uri") == builder_uri
            and dependency.get("digest", {}).get("sha1")
            == provenance_config["builder_commit"]
        ],
        "QEMU builder resolved dependency",
    )
    request_args = external.get("request", {}).get("args", {})
    source_repository = source["repository"]
    if source_repository.endswith(".git"):
        source_repository = source_repository[:-4]
    require(
        request_args.get("build-arg:QEMU_REPO") == source_repository,
        "QEMU source repository build argument mismatch",
    )
    require(
        request_args.get("build-arg:QEMU_VERSION") == source["tag"],
        "QEMU source tag build argument mismatch",
    )
    require(
        request_args.get("build-arg:DOCKER_META_VERSION") == executor["tag"],
        "QEMU image tag build argument mismatch",
    )
    require(
        request_args.get("build-arg:QEMU_PATCHES") == "cpu-max-arm"
        and request_args.get("build-arg:QEMU_PRESERVE_ARGV0") == "1",
        "QEMU patch build arguments mismatch",
    )
    llb_definition = build_definition.get("internalParameters", {}).get(
        "buildConfig", {}
    ).get("llbDefinition", [])
    clone_step = one(
        [
            step
            for step in llb_definition
            if "git clone $QEMU_REPO && cd qemu && git checkout $QEMU_VERSION"
            in step.get("op", {})
            .get("Op", {})
            .get("exec", {})
            .get("meta", {})
            .get("args", [])
        ],
        "QEMU Git checkout build step",
    )
    clone_environment = clone_step["op"]["Op"]["exec"]["meta"].get("env", [])
    require(
        "QEMU_VERSION=" + source["tag"] in clone_environment,
        "QEMU checkout tag mismatch",
    )
    require(
        "QEMU_REPO=" + source_repository in clone_environment,
        "QEMU checkout repository mismatch",
    )

    tag_payload = load_evidence(repository, source["tag_evidence"])
    commit_payload = load_evidence(repository, source["commit_evidence"])
    require(
        git_object_id("tag", tag_payload) == source["tag_object"],
        "QEMU tag object mismatch",
    )
    require(
        git_object_id("commit", commit_payload) == source["commit"],
        "QEMU commit object mismatch",
    )
    tag_headers = git_headers(tag_payload)
    require(single_header(tag_headers, "object") == source["commit"], "QEMU tag target mismatch")
    require(single_header(tag_headers, "type") == "commit", "QEMU tag target is not a commit")
    require(single_header(tag_headers, "tag") == source["tag"], "QEMU tag name mismatch")
    require(
        b"-----BEGIN PGP SIGNATURE-----" in tag_payload
        and b"-----END PGP SIGNATURE-----" in tag_payload,
        "QEMU annotated tag has no embedded signature",
    )
    git_headers(commit_payload)

    python_signers = {
        "3.9": ("lukasz@langa.pl", "https://github.com/login/oauth"),
        "3.10": ("pablogsal@python.org", "https://accounts.google.com"),
        "3.11": ("pablogsal@python.org", "https://accounts.google.com"),
        "3.12": ("thomas@python.org", "https://accounts.google.com"),
        "3.13": ("thomas@python.org", "https://accounts.google.com"),
        "3.14": ("hugo@python.org", "https://github.com/login/oauth"),
    }
    python_patch_count = 0
    for version_entry in config["python"]["versions"]:
        version = version_entry["version"]
        minor = ".".join(version.split(".")[:2])
        python_source = version_entry["source"]
        expected_url = (
            "https://www.python.org/ftp/python/%s/Python-%s.tar.xz"
            % (version, version)
        )
        require(python_source["status"] == "locked", "CPython source is not locked")
        require(python_source["url"] == expected_url, "CPython source URL mismatch")
        sigstore = python_source["sigstore"]
        require(
            sigstore["verification"] == "archived-unverified",
            "CPython Sigstore evidence must not claim unimplemented verification",
        )
        require(
            sigstore["bundle_url"] == expected_url + ".sigstore",
            "CPython Sigstore URL mismatch",
        )
        require(
            (sigstore["identity"], sigstore["oidc_issuer"])
            == python_signers[minor],
            "CPython Sigstore signer policy mismatch",
        )
        patches = version_entry["patches"]
        if minor == "3.11":
            require(
                version_entry["adapter"] == "transition" and len(patches) == 1,
                "CPython 3.11 transition adapter requires exactly one patch",
            )
            require(
                patches[0]["file"]
                == "patches/cpython/3.11/0001-gh-115382-isolate-target-sysconfig.patch",
                "CPython 3.11 transition patch path mismatch",
            )
        else:
            require(not patches, "unexpected CPython patch for %s" % version)
        for patch in patches:
            patch_payload = load_cpython_patch(repository, patch["file"])
            require(
                hashlib.sha256(patch_payload).hexdigest() == patch["sha256"],
                "%s: digest mismatch" % patch["file"],
            )
            diff_headers = [
                line
                for line in patch_payload.splitlines()
                if line.startswith(b"diff --git ")
            ]
            require(
                diff_headers
                == [
                    b"diff --git a/Lib/sysconfig.py b/Lib/sysconfig.py",
                    b"diff --git a/configure b/configure",
                    b"diff --git a/configure.ac b/configure.ac",
                ],
                "CPython transition patch changes an unexpected file set",
            )
            require(
                b"https://github.com/python/cpython/issues/115382" in patch_payload
                and b"909d5ac2959ea88e1d3b38f35676a1c7e5dd44f6" in patch_payload
                and b"+    if (path := os.environ.get('_PYTHON_SYSCONFIGDATA_PATH')):"
                in patch_payload
                and b"PYTHONPATH=$(srcdir)/Lib" in patch_payload,
                "CPython transition patch is missing gh-115382 isolation semantics",
            )
            python_patch_count += 1
        bundle_payload, bundle = evidence_json(
            repository,
            sigstore["bundle_evidence"],
            "sha256:" + sigstore["bundle_sha256"],
            sigstore["bundle_size"],
        )
        require(
            bundle.get("mediaType")
            == "application/vnd.dev.sigstore.bundle.v0.3+json",
            "CPython Sigstore bundle media type mismatch",
        )
        message_digest = bundle.get("messageSignature", {}).get(
            "messageDigest", {}
        )
        require(
            message_digest.get("algorithm") == "SHA2_256",
            "CPython Sigstore digest algorithm mismatch",
        )
        try:
            signed_digest = base64.b64decode(
                message_digest.get("digest", ""), validate=True
            ).hex()
        except (ValueError, binascii.Error) as error:
            raise EvidenceError("invalid CPython Sigstore message digest") from error
        require(
            signed_digest == python_source["sha256"],
            "CPython Sigstore message digest differs from source",
        )
        tlog_entries = bundle.get("verificationMaterial", {}).get(
            "tlogEntries", []
        )
        tlog_entry = one(tlog_entries, "CPython Sigstore transparency entry")
        try:
            body = json.loads(
                base64.b64decode(
                    tlog_entry["canonicalizedBody"], validate=True
                ).decode("utf-8"),
                object_pairs_hook=reject_duplicate_keys,
            )
        except (
            KeyError,
            ValueError,
            binascii.Error,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise EvidenceError("invalid CPython transparency entry") from error
        require(
            body.get("spec", {})
            .get("data", {})
            .get("hash", {})
            .get("value")
            == python_source["sha256"],
            "CPython transparency entry differs from source",
        )
        require(bundle_payload, "CPython Sigstore bundle is empty")

    return {
        "rocky_index_sha256": sha256(_rocky_payload),
        "qemu_index_sha256": sha256(qemu_index_payload),
        "qemu_manifest_sha256": sha256(qemu_manifest_payload),
        "qemu_attestation_sha256": sha256(attestation_payload),
        "qemu_tag_object": source["tag_object"],
        "qemu_commit": source["commit"],
        "python_sources": len(config["python"]["versions"]),
        "python_patches": python_patch_count,
        "python_sigstore_status": "archived-unverified",
    }


def main():
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=repository / "config/release.json",
    )
    arguments = parser.parse_args()
    config = load_json(arguments.config)
    result = validate_evidence(config, repository)
    print(
        "valid supply-chain evidence: Rocky %s; QEMU %s; source %s; "
        "CPython Sigstore bundles %s; patches %d"
        % (
            result["rocky_index_sha256"],
            result["qemu_manifest_sha256"],
            result["qemu_commit"],
            result["python_sigstore_status"],
            result["python_patches"],
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvidenceError, KeyError, TypeError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
