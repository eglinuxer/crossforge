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


def load_locked_file(repository, relative_path, description):
    require(
        isinstance(relative_path, str)
        and relative_path
        and not relative_path.startswith("/")
        and ".." not in Path(relative_path).parts,
        "invalid %s path" % description,
    )
    root = repository.resolve()
    expected = root / relative_path
    try:
        path = expected.resolve()
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise EvidenceError("%s path escaped repository" % description) from error
    require(path == expected and path.is_file(), "%s is missing or non-canonical" % description)
    try:
        return path.read_bytes()
    except OSError as error:
        raise EvidenceError("%s: %s" % (path, error)) from error


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

    zstd = config["python"]["zstd"]
    zstd_source = zstd["source"]
    zstd_signature = zstd_source["signature"]
    zstd_key = zstd_signature["key"]
    zstd_git = zstd_source["git"]
    zstd_license = zstd["license"]
    require(zstd["version"] == "1.5.7", "zstd version policy mismatch")
    expected_zstd_url = (
        "https://github.com/facebook/zstd/releases/download/v1.5.7/"
        "zstd-1.5.7.tar.gz"
    )
    require(
        zstd_source["status"] == "locked"
        and zstd_source["url"] == expected_zstd_url
        and zstd_source["sha256"]
        == "eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3"
        and zstd_source["size"] == 2434947,
        "zstd release archive identity mismatch",
    )
    signature_payload = load_evidence(repository, zstd_signature["evidence"])
    require(
        zstd_signature["url"] == expected_zstd_url + ".sig"
        and len(signature_payload) == zstd_signature["size"] == 858
        and hashlib.sha256(signature_payload).hexdigest()
        == zstd_signature["sha256"]
        == "24425933fb954f4608ae9383bc37ad8398e50364c1ec30bbdb5adbfe88209fb1"
        and signature_payload.startswith(b"-----BEGIN PGP SIGNATURE-----\n")
        and signature_payload.rstrip().endswith(b"-----END PGP SIGNATURE-----"),
        "zstd detached signature evidence mismatch",
    )
    key_payload = load_locked_file(repository, zstd_key["file"], "zstd release key")
    require(
        hashlib.sha256(key_payload).hexdigest()
        == zstd_key["sha256"]
        == "7ef8dd39f90db88f1f95e9a57db783cfc96eba95c9a8f91d52f6ca99d98fc13d"
        and zstd_key["fingerprint"]
        == "4ef4ac63455fc9f4545d9b7def8fe99528b52ffd"
        and key_payload.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
        and key_payload.rstrip().endswith(b"-----END PGP PUBLIC KEY BLOCK-----"),
        "zstd release key identity mismatch",
    )
    zstd_tag_payload = load_evidence(repository, zstd_git["tag_evidence"])
    zstd_commit_payload = load_evidence(repository, zstd_git["commit_evidence"])
    require(
        zstd_git["repository"] == "https://github.com/facebook/zstd.git"
        and zstd_git["tag"] == "v1.5.7"
        and git_object_id("tag", zstd_tag_payload)
        == zstd_git["tag_object"]
        == "ac66b19e6bd6b83238bf008eecc1298105298532"
        and git_object_id("commit", zstd_commit_payload)
        == zstd_git["commit"]
        == "f8745da6ff1ad1e7bab384bd1f9d742439278e99",
        "zstd Git identity mismatch",
    )
    zstd_tag_headers = git_headers(zstd_tag_payload)
    require(
        single_header(zstd_tag_headers, "object") == zstd_git["commit"]
        and single_header(zstd_tag_headers, "type") == "commit"
        and single_header(zstd_tag_headers, "tag") == zstd_git["tag"]
        and b"-----BEGIN PGP SIGNATURE-----" in zstd_tag_payload
        and b"-----END PGP SIGNATURE-----" in zstd_tag_payload,
        "zstd signed tag evidence mismatch",
    )
    require(
        zstd_commit_payload.startswith(b"tree ")
        and b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n" in zstd_commit_payload
        and b" -----END PGP SIGNATURE-----\n" in zstd_commit_payload,
        "zstd commit evidence is malformed",
    )
    require(
        zstd_license
        == {
            "expression": "BSD-3-Clause",
            "license_file": "LICENSE",
            "license_sha256": "7055266497633c9025b777c78eb7235af13922117480ed5c674677adc381c9d8",
            "copying_file": "COPYING",
            "copying_sha256": "f9c375a1be4a41f7b70301dd83c91cb89e41567478859b77eef375a52d782505",
        },
        "zstd selected license identity mismatch",
    )

    vcpkg = config["vcpkg"]
    vcpkg_release = vcpkg["release"]
    vcpkg_tool = vcpkg["tool"]
    vcpkg_signature = vcpkg_tool["signature"]
    vcpkg_key = vcpkg_signature["key"]
    vcpkg_tag_payload = load_evidence(
        repository, vcpkg_release["tag_evidence"]
    )
    vcpkg_commit_payload = load_evidence(
        repository, vcpkg_release["commit_evidence"]
    )
    tool_commit_payload = load_evidence(
        repository, vcpkg_tool["commit_evidence"]
    )
    require(
        vcpkg["repository"] == "https://github.com/microsoft/vcpkg.git"
        and vcpkg_release["status"] == "locked"
        and vcpkg_release["tag"] == "2026.07.29"
        and git_object_id("tag", vcpkg_tag_payload)
        == vcpkg_release["tag_object"]
        == "c76c06644034521fb761a39f8f52d8e87d1103d5"
        and git_object_id("commit", vcpkg_commit_payload)
        == vcpkg_release["commit"]
        == "9e593bb18ea69cc5095e012465dcd675a822ed0d",
        "vcpkg registry Git identity mismatch",
    )
    vcpkg_tag_headers = git_headers(vcpkg_tag_payload)
    require(
        single_header(vcpkg_tag_headers, "object")
        == vcpkg_release["commit"]
        and single_header(vcpkg_tag_headers, "type") == "commit"
        and single_header(vcpkg_tag_headers, "tag")
        == vcpkg_release["tag"]
        and b"-----BEGIN SSH SIGNATURE-----" in vcpkg_tag_payload
        and b"-----END SSH SIGNATURE-----" in vcpkg_tag_payload
        and b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n"
        in vcpkg_commit_payload,
        "vcpkg signed release evidence mismatch",
    )
    require(
        vcpkg_commit_payload.startswith(b"tree "),
        "vcpkg release commit evidence is malformed",
    )
    require(
        vcpkg_tool["status"] == "locked"
        and vcpkg_tool["repository"]
        == "https://github.com/microsoft/vcpkg-tool.git"
        and vcpkg_tool["tag"] == "2026-07-27"
        and git_object_id("commit", tool_commit_payload)
        == vcpkg_tool["commit"]
        == "98d7cb0cf1f4686a3e43aa5672b6230c1d56bce8"
        and b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n"
        in tool_commit_payload
        and vcpkg_tool["url"]
        == "https://github.com/microsoft/vcpkg-tool/releases/download/"
        "2026-07-27/vcpkg-glibc"
        and vcpkg_tool["sha256"]
        == "7e97ef6bcd58f74d079f40d086b801a0222c5d15e4ea0d8d507a538033493d04"
        and vcpkg_tool["sha512"]
        == "be59d1fdf3725d2fb4bd1c0b435266726aaff2f87cb2503b775f44fb9b392ab4"
        "74e6370d7e90e9d07b2c584b7deacc0670f98b477905c2e0d5cba5e01cee93dc"
        and vcpkg_tool["size"] == 8548168,
        "vcpkg-tool release identity mismatch",
    )
    require(
        tool_commit_payload.startswith(b"tree "),
        "vcpkg-tool commit evidence is malformed",
    )
    signature_payload = load_evidence(
        repository, vcpkg_signature["evidence"]
    )
    require(
        vcpkg_signature["url"] == vcpkg_tool["url"] + ".sig"
        and len(signature_payload) == vcpkg_signature["size"] == 481
        and hashlib.sha256(signature_payload).hexdigest()
        == vcpkg_signature["sha256"]
        == "8b7176edd2699187c021ab72ae2c2713bafb2b1daabf4c320eaa05c13b2e80c7"
        and signature_payload.startswith(b"-----BEGIN PGP SIGNATURE-----\n")
        and signature_payload.rstrip().endswith(
            b"-----END PGP SIGNATURE-----"
        ),
        "vcpkg-tool detached signature evidence mismatch",
    )
    key_payload = load_locked_file(
        repository, vcpkg_key["file"], "Microsoft release key"
    )
    require(
        hashlib.sha256(key_payload).hexdigest()
        == vcpkg_key["sha256"]
        == "2fa9c05d591a1582a9aba276272478c262e95ad00acf60eaee1644d93941e3c6"
        and vcpkg_key["fingerprint"]
        == "bc528686b50d79e339d3721ceb3e94adbe1229cf"
        and key_payload.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
        and key_payload.rstrip().endswith(
            b"-----END PGP PUBLIC KEY BLOCK-----"
        ),
        "Microsoft vcpkg-tool signing key identity mismatch",
    )
    require(
        vcpkg["registry_license"]
        == {
            "expression": "MIT",
            "license_file": "LICENSE.txt",
            "license_sha256": "1ee376fc340e0aa6ad6a3581c94126e741468705096ac92263048a21daa86460",
            "notice_file": "NOTICE.txt",
            "notice_sha256": "e46407f44d1f439e1f62fdfd1479418cf221d90e8d8fd27bfa4a362e23065c87",
        },
        "vcpkg registry license identity mismatch",
    )
    tool_license = vcpkg_tool["license"]
    tool_license_payload = load_locked_file(
        repository, tool_license["license_file"], "vcpkg-tool license"
    )
    tool_notice_payload = load_locked_file(
        repository, tool_license["notice_file"], "vcpkg-tool notice"
    )
    require(
        tool_license["expression"] == "MIT"
        and hashlib.sha256(tool_license_payload).hexdigest()
        == tool_license["license_sha256"]
        == "16e3c9cdb4fa14a8627bc4b5ef0237773a79c3ad1d012c288f37e29573e116cc"
        and hashlib.sha256(tool_notice_payload).hexdigest()
        == tool_license["notice_sha256"]
        == "6b9a0fc7d06f94019adc9705b92d0b2e53509f23f9d43744bbd663aa2c1597d5",
        "vcpkg-tool license identity mismatch",
    )

    ninja = config["host_tools"]["ninja"]
    ninja_binary = ninja["binary"]
    ninja_source = ninja["source"]
    ninja_license = ninja["license"]
    ninja_commit_payload = load_evidence(
        repository, ninja["commit_evidence"]
    )
    _ninja_tag_payload, ninja_tag = evidence_json(
        repository,
        ninja["tag_evidence"],
        "sha256:" + ninja["tag_evidence_sha256"],
        ninja["tag_evidence_size"],
    )
    ninja_release_config = ninja["release"]
    _ninja_release_payload, ninja_release = evidence_json(
        repository,
        ninja_release_config["evidence"],
        "sha256:" + ninja_release_config["evidence_sha256"],
        ninja_release_config["evidence_size"],
    )
    ninja_asset = one(
        [
            asset
            for asset in ninja_release.get("assets", [])
            if asset.get("name")
            == ninja_binary["archive_member"] + "-linux.zip"
        ],
        "Ninja linux release asset",
    )
    require(
        ninja["version"] == "1.13.2"
        and ninja["repository"] == "https://github.com/ninja-build/ninja.git"
        and ninja["tag"] == "v1.13.2"
        and git_object_id("commit", ninja_commit_payload)
        == ninja["commit"]
        == "3441b633c2fe2c494e958780ba0f4227b1327634"
        and ninja_commit_payload.startswith(b"tree ")
        and ninja_tag.get("ref") == "refs/tags/v1.13.2"
        and ninja_tag.get("object")
        == {
            "sha": ninja["commit"],
            "type": "commit",
            "url": "https://api.github.com/repos/ninja-build/ninja/git/commits/"
            + ninja["commit"],
        },
        "Ninja Git identity mismatch",
    )
    require(
        ninja_release_config["immutable"] is False
        and ninja_release.get("tag_name") == ninja["tag"]
        and ninja_release.get("immutable") is False
        and ninja_release.get("draft") is False
        and ninja_release.get("prerelease") is False
        and ninja_asset.get("size") == ninja_binary["size"]
        and ninja_asset.get("digest")
        == "sha256:" + ninja_binary["sha256"]
        and ninja_asset.get("browser_download_url") == ninja_binary["url"],
        "Ninja release asset evidence mismatch",
    )
    require(
        ninja_binary
        == {
            "status": "locked",
            "url": "https://github.com/ninja-build/ninja/releases/download/"
            "v1.13.2/ninja-linux.zip",
            "sha256": "5749cbc4e668273514150a80e387a957f933c6ed3f5f11e03fb30955e2bbead6",
            "sha512": "714b900cf10b7ecb1b641c91f4ef696250c64984e5955a8088e4a538d6e8077f"
            "43e55f6da47efcedbe316c68d51a9e98feff51734eb0eac1b17aa85af5698753",
            "size": 134040,
            "archive_member": "ninja",
            "extracted_sha256": "607e668f90dd6cd82e1a42ae572647ad1b1fd43063964295b9547836d8c15d99",
            "extracted_sha512": "baa28f9bb5519c19f6294956d216a7e384b5919e304412f4fb854d3c434c6ab0"
            "005aa0410f2b25c2ec082a6a630f0289933a818576a8f0a2b17d5564438a1dc9",
            "extracted_size": 290928,
        }
        and ninja_source
        == {
            "status": "locked",
            "url": "https://github.com/ninja-build/ninja/archive/"
            "3441b633c2fe2c494e958780ba0f4227b1327634.tar.gz",
            "sha256": "bccc6197cd8c3ac2a439e26d6bf41506fe49c430cf3d593269a15379f24266ee",
            "sha512": "7c7480c91f5c4d41c51dd5caeebea8b18049ae89e794afdfbc889897a86eaa15"
            "b1f7cb6a3d99330da3f60bb48173494c2b7617c1c0a748fe1d94e66007766bba",
            "size": 292638,
            "archive_root": "ninja-3441b633c2fe2c494e958780ba0f4227b1327634",
        }
        and ninja_license
        == {
            "expression": "Apache-2.0",
            "source_file": "COPYING",
            "sha256": "eb7e9ab9690124c5c9f42bdc81383d886a3dede26345b6ed15bbad7caf81f7ea",
            "size": 11358,
        },
        "Ninja locked material identity mismatch",
    )

    cmake = config["host_tools"]["cmake"]
    cmake_binary = cmake["binary"]
    require(
        cmake["version"] == "4.4.0"
        and cmake_binary
        == {
            "status": "locked",
            "url": "https://github.com/Kitware/CMake/releases/download/"
            "v4.4.0/cmake-4.4.0-linux-x86_64.tar.gz",
            "sha256": "3864eb649b4466ae126a64bbde1657adad78efbbaa068bf38201de5cf1b5349f",
            "sha512": "3df4aaa128a438ed48dcac7065fd355ff538eed8f394491298d0db63a891d671d"
            "a247c8fa262e4fa6bf99429d630abab317d5a0248168fe203d1ca4978dab4da",
            "size": 64838835,
            "archive_root": "cmake-4.4.0-linux-x86_64",
        }
        and [item["path"] for item in cmake["payloads"]]
        == ["bin/cmake", "bin/cpack", "bin/ctest"]
        and cmake["license"]
        == {
            "expression": "BSD-3-Clause",
            "path": "doc/cmake/LICENSE.rst",
            "sha256": "4382e7c1879ac90e3f101a395d23846fa4dbcaa1eed7265b43681e348754825d",
            "size": 1498,
        },
        "CMake locked material identity mismatch",
    )

    python_signers = {
        "3.9": ("lukasz@langa.pl", "https://github.com/login/oauth"),
        "3.10": ("pablogsal@python.org", "https://accounts.google.com"),
        "3.11": ("pablogsal@python.org", "https://accounts.google.com"),
        "3.12": ("thomas@python.org", "https://accounts.google.com"),
        "3.13": ("thomas@python.org", "https://accounts.google.com"),
        "3.14": ("hugo@python.org", "https://github.com/login/oauth"),
    }
    python_patch_policy = {
        "3.9.25": {
            "adapter": "legacy",
            "file": "patches/cpython/3.9/0001-gh-115382-isolate-target-sysconfig.patch",
            "sha256": "e4d5629748d9737c891f47eb38cb3a5722c3b71afc5e28b5cede80ae5b66cf77",
            "layout_marker": b"to the 3.9 source\nlayout",
        },
        "3.10.21": {
            "adapter": "legacy",
            "file": "patches/cpython/3.10/0001-gh-115382-isolate-target-sysconfig.patch",
            "sha256": "af23410fcaef3bb630dc0b986b5de52a542f3e1945c2493261a92500357773d3",
            "layout_marker": b"to the 3.10 source\nlayout",
        },
        "3.11.16": {
            "adapter": "transition",
            "file": "patches/cpython/3.11/0001-gh-115382-isolate-target-sysconfig.patch",
            "sha256": "072dacfcc57b06bc1e5382726990627593a36e1f08232cb790db42ae334a49aa",
            "layout_marker": b"to the 3.11 source\nlayout",
        },
        "3.12.14": {
            "adapter": "modern",
            "file": "patches/cpython/3.12/0001-gh-115382-isolate-target-sysconfig.patch",
            "sha256": "ff3a8e2695b4c66d0f60e6c73ac0028221ef803a308ff4e81393a54c9404dd33",
            "layout_marker": b"to the 3.12 source\nlayout",
        },
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
        patch_policy = python_patch_policy.get(version)
        if minor in ("3.9", "3.10", "3.11", "3.12"):
            require(
                patch_policy is not None,
                "CPython %s has no audited isolation patch policy" % version,
            )
        if patch_policy is not None:
            require(
                version_entry["adapter"] == patch_policy["adapter"]
                and len(patches) == 1,
                "CPython %s adapter requires exactly one patch" % minor,
            )
            require(
                patches[0]["file"] == patch_policy["file"],
                "CPython %s patch path mismatch" % minor,
            )
            require(
                patches[0]["sha256"] == patch_policy["sha256"],
                "CPython %s patch digest policy mismatch" % minor,
            )
        else:
            require(not patches, "unexpected CPython patch for %s" % version)
        for patch in patches:
            patch_payload = load_cpython_patch(repository, patch["file"])
            require(
                hashlib.sha256(patch_payload).hexdigest() == patch["sha256"],
                "%s: digest mismatch" % patch["file"],
            )
            expected_files = (
                (
                    b"Lib/distutils/sysconfig.py",
                    b"Lib/sysconfig.py",
                    b"configure",
                    b"configure.ac",
                )
                if minor == "3.9"
                else (
                    b"Lib/sysconfig.py",
                    b"configure",
                    b"configure.ac",
                )
            )
            diff_headers = [
                line
                for line in patch_payload.splitlines()
                if line.startswith(b"diff --git ")
            ]
            require(
                diff_headers
                == [
                    b"diff --git a/" + name + b" b/" + name
                    for name in expected_files
                ],
                "CPython isolation patch changes an unexpected file set",
            )
            require(
                [
                    line
                    for line in patch_payload.splitlines()
                    if line.startswith(b"--- ")
                ]
                == [b"--- a/" + name for name in expected_files]
                and [
                    line
                    for line in patch_payload.splitlines()
                    if line.startswith(b"+++ ")
                ]
                == [b"+++ b/" + name for name in expected_files],
                "CPython isolation patch has unexpected old/new file headers",
            )
            require(
                b"https://github.com/python/cpython/issues/115382" in patch_payload
                and b"909d5ac2959ea88e1d3b38f35676a1c7e5dd44f6" in patch_payload
                and b"+    if (path := os.environ.get('_PYTHON_SYSCONFIGDATA_PATH')):"
                in patch_payload
                and b"PYTHONPATH=$(srcdir)/Lib" in patch_payload
                and b"-    _temp = __import__(name, globals(), locals(), ['build_time_vars'], 0)"
                in patch_payload
                and b"+        _temp = __import__(name, globals(), locals(), ['build_time_vars'], 0)"
                in patch_payload
                and patch_policy is not None
                and patch_policy["layout_marker"] in patch_payload,
                "CPython %s patch is missing gh-115382 isolation semantics"
                % minor,
            )
            if minor == "3.9":
                require(
                    b"from sysconfig import _init_posix as sysconfig_init_posix"
                    in patch_payload
                    and b"+    sysconfig_init_posix(config_vars)"
                    in patch_payload
                    and b"+    _config_vars = config_vars" in patch_payload,
                    "CPython 3.9 patch lacks isolated distutils delegation",
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
        "zstd_tag_object": zstd_git["tag_object"],
        "zstd_commit": zstd_git["commit"],
        "zstd_signature_sha256": zstd_signature["sha256"],
        "vcpkg_tag_object": vcpkg_release["tag_object"],
        "vcpkg_commit": vcpkg_release["commit"],
        "vcpkg_tool_commit": vcpkg_tool["commit"],
        "vcpkg_tool_signature_sha256": vcpkg_signature["sha256"],
        "ninja_commit": ninja["commit"],
        "ninja_binary_sha256": ninja_binary["sha256"],
        "cmake_binary_sha256": cmake_binary["sha256"],
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
        "CPython Sigstore bundles %s; patches %d; zstd %s; vcpkg %s; "
        "Ninja %s; CMake %s"
        % (
            result["rocky_index_sha256"],
            result["qemu_manifest_sha256"],
            result["qemu_commit"],
            result["python_sigstore_status"],
            result["python_patches"],
            result["zstd_commit"],
            result["vcpkg_commit"],
            result["ninja_commit"],
            config["host_tools"]["cmake"]["version"],
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvidenceError, KeyError, TypeError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
