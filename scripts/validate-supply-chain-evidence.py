#!/usr/bin/env python3
"""Validate checked-in OCI/Git evidence, CPython patches, and Sigstore bundles."""

import argparse
import base64
import binascii
import hashlib
import json
import runpy
import sys
from pathlib import Path


class EvidenceError(ValueError):
    pass


OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
SLSA_V1 = "https://slsa.dev/provenance/v1"
REPOSITORY = Path(__file__).resolve().parents[1]
TUF = runpy.run_path(
    str(REPOSITORY / "scripts/verify-sigstore-tuf-root.py")
)


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
    sigstore_trust = config["sigstore"]["trust"]
    sigstore_verifier = config["sigstore"]["verifier"]
    require(
        config["sigstore"]["signing"]
        == {
            "status": "required",
            "identity": "https://github.com/eglinuxer/crossforge/"
            ".github/workflows/candidate.yml@refs/heads/main",
            "oidc_issuer": "https://token.actions.githubusercontent.com",
            "transparency_log": True,
        },
        "candidate Sigstore signing policy differs",
    )
    require(
        sigstore_verifier["status"] == "locked"
        and sigstore_verifier["policy"]["require_tlog"] is True
        and sigstore_verifier["policy"]["require_sct"] is True
        and sigstore_verifier["policy"]["signed_timestamps"]
        == {
            "default": True,
            "exceptions": [
                {
                    "artifact": "Python-3.9.25.tar.xz",
                    "reason": "upstream-bundle-omits-rfc3161",
                }
            ],
        },
        "Sigstore verifier policy differs",
    )
    require(
        config["nfpm"]["sigstore"]["status"] == "verified",
        "nFPM Sigstore verification status differs",
    )
    try:
        tuf = TUF["verify_arguments"](
            argparse.Namespace(
                root_directory=repository
                / sigstore_trust["root_directory"],
                initial_version=sigstore_trust[
                    "initial_root_version"
                ],
                final_version=sigstore_trust["final_root_version"],
                initial_root_sha256=sigstore_trust[
                    "initial_root_sha256"
                ],
                targets=repository
                / sigstore_trust["targets_evidence"],
                targets_sha256=sigstore_trust["targets_sha256"],
                trusted_root=repository
                / sigstore_trust["trusted_root_evidence"],
                trusted_root_sha256=sigstore_trust[
                    "trusted_root_sha256"
                ],
                base64_envelopes=True,
            )
        )
    except (OSError, TUF["TUFError"]) as error:
        raise EvidenceError("invalid Sigstore TUF trust root: %s" % error) from error
    require(
        tuf["targets_version"] == sigstore_trust["targets_version"],
        "Sigstore TUF targets version differs from release",
    )
    targets_payload = load_evidence(
        repository, sigstore_trust["targets_evidence"]
    )
    try:
        targets = json.loads(
            targets_payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise EvidenceError("invalid Sigstore TUF targets") from error
    artifact_key = load_evidence(
        repository, sigstore_trust["artifact_key_evidence"]
    )
    artifact_record = targets["signed"]["targets"].get("artifact.pub")
    require(
        artifact_record is not None
        and artifact_record["length"] == len(artifact_key)
        and artifact_record["hashes"]["sha256"]
        == hashlib.sha256(artifact_key).hexdigest()
        == sigstore_trust["artifact_key_sha256"],
        "Sigstore artifact key differs from authenticated TUF target",
    )
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
    builder_source = provenance_config["builder_source"]
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
    require(
        builder_source
        == {
            "status": "locked",
            "url": "https://github.com/tonistiigi/binfmt/archive/"
            "e29e7d72c9672c8c8bf846655ab149b50e1a62bd.tar.gz",
            "sha256": (
                "1b50178686d461b5ca300aaa889ad9c3ff195a2d33c0cec688180db70d6d4764"
            ),
            "size": 1616824,
            "archive_root": (
                "binfmt-e29e7d72c9672c8c8bf846655ab149b50e1a62bd"
            ),
            "member_count": 764,
            "dockerfile_sha256": (
                "ebb8708a04e004bf32adc83c2dac518e909f82c20ae89e62cbeb4cc2e8a5ebf7"
            ),
            "configure_sha256": (
                "9a3a08e3311fa64a275641f335d34dd1a89f4f2a01b4ae5a2192869b6a2828ba"
            ),
            "license_sha256": (
                "bba3332a1e2ec03031b587452cd9254bd7ab6ec701aef20b12e642f47f423dd6"
            ),
            "patches": {
                "cpu_max_arm_sha256": (
                    "cbfd75a619b10c616f6e19afc469ed400060eeaa29a54e09a8ba08f5a82c8684"
                ),
                "preserve_argv0_sha256": (
                    "886df00c35f5afc4bc47e8fc96cb42956d8e4be0c35f74b8041f43468b44fed4"
                ),
            },
        }
        and builder_source["archive_root"]
        == "binfmt-" + provenance_config["builder_commit"],
        "QEMU binfmt builder source identity differs",
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

    qemu_archive = source["archive"]
    qemu_signature = qemu_archive["signature"]
    qemu_key = qemu_signature["key"]
    require(
        qemu_archive["status"] == "locked"
        and qemu_archive["url"]
        == "https://download.qemu.org/qemu-10.2.3.tar.xz"
        and qemu_archive["sha256"]
        == "2aa0e420e4ea89ea34a833f4c4eced96a35b51a9ee8568b232692729b60b064d"
        and qemu_archive["size"] == 141095748
        and qemu_archive["layout"]
        == {
            "top_directory": "qemu-10.2.3",
            "member_count": 84628,
            "version_sha256": (
                "b9bba35d8e16f369d55d674718b3ce7b0cd86c60681f153bb885a596eeabea4b"
            ),
            "license_sha256": (
                "dd3ce02338c3a48abb6ba59b48809f7108a8bd242cb0cc8be90daafa30707c28"
            ),
            "reviewed_external_symlinks": [
                {
                    "path": (
                        "qemu-10.2.3/roms/edk2/EmulatorPkg/Unix/Host/"
                        "X11IncludeHack"
                    ),
                    "target": "/opt/X11/include",
                }
            ],
        },
        "QEMU official source archive policy differs",
    )
    require(
        qemu_signature["url"]
        == "https://download.qemu.org/qemu-10.2.3.tar.xz.sig"
        and qemu_signature["sha256"]
        == "cdd27f047ef822ce837309c849a07009b21c666689850b04b5d84cdfc08fbbe9"
        and qemu_signature["size"] == 310
        and qemu_signature["evidence"]
        == "evidence/gpg/qemu-10.2.3.tar.xz.sig.b64"
        and qemu_signature["verification"]
        == {
            "status": "cryptographically-valid-expired-key",
            "signature_time": "2026-05-27T22:12:30Z",
            "exception": "upstream-release-key-expired-before-signing",
        },
        "QEMU official source signature policy differs",
    )
    require(
        qemu_key
        == {
            "file": "keys/QEMU-RELEASE-KEY.asc",
            "retrieval_url": (
                "https://keys.openpgp.org/vks/v1/by-fingerprint/"
                "CEACC9E15534EBABB82D3FA03353C9CEF108B584"
            ),
            "sha256": (
                "0ce28d0b02f2e36286be047e1c76558421c8b6324f729462a753cf6cb20fe368"
            ),
            "fingerprint": "ceacc9e15534ebabb82d3fa03353c9cef108b584",
            "expires_at": "2026-05-11T15:13:07Z",
        }
        and qemu_signature["verification"]["signature_time"]
        > qemu_key["expires_at"],
        "QEMU expired release-key exception is not exact",
    )
    qemu_signature_payload = load_evidence(
        repository, qemu_signature["evidence"]
    )
    qemu_key_payload = load_locked_file(
        repository, qemu_key["file"], "QEMU release key"
    )
    require(
        len(qemu_signature_payload) == qemu_signature["size"]
        and hashlib.sha256(qemu_signature_payload).hexdigest()
        == qemu_signature["sha256"]
        and hashlib.sha256(qemu_key_payload).hexdigest() == qemu_key["sha256"],
        "QEMU official source signature evidence differs",
    )

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

    qt = config["qt"]
    qt_source = qt["source"]
    qt_checksum = qt_source["checksum"]
    expected_qt_url = (
        "https://download.qt.io/archive/qt/6.8/6.8.4/single/"
        "qt-everywhere-opensource-src-6.8.4.tar.xz"
    )
    require(
        qt["version"] == "6.8.4"
        and qt_source["status"] == "locked"
        and qt_source["url"] == expected_qt_url
        and qt_source["sha256"]
        == "1da37a32a583e7856d6fc13357c8ff6ad3ef7b877b8d276713b85026426d5246"
        and qt_source["size"] == 994798840
        and qt_checksum["url"] == expected_qt_url + ".sha256"
        and qt_checksum["sha256"]
        == "f208721e3239cba3d21312295e7d991f378e83e79e51e55fe2ffb6c05726bb0a"
        and qt_checksum["size"] == 108
        and qt_checksum["authentication"]
        == "hash-pinned-https-sidecar-no-signature",
        "Qt source or checksum identity mismatch",
    )
    qt_evidence_envelope = load_locked_file(
        repository, qt_checksum["evidence"], "Qt checksum evidence"
    )
    require(
        qt_evidence_envelope
        and qt_evidence_envelope == qt_evidence_envelope.strip() + b"\n"
        and len(qt_evidence_envelope.splitlines()) == 1,
        "Qt checksum evidence envelope is not canonical",
    )
    try:
        qt_checksum_payload = base64.b64decode(
            qt_evidence_envelope.strip(), validate=True
        )
    except (ValueError, binascii.Error) as error:
        raise EvidenceError("invalid Qt checksum evidence") from error
    require(
        len(qt_checksum_payload) == qt_checksum["size"]
        and hashlib.sha256(qt_checksum_payload).hexdigest()
        == qt_checksum["sha256"]
        and qt_checksum_payload
        == (
            "%s  qt-everywhere-opensource-src-6.8.4.tar.xz\n"
            % qt_source["sha256"]
        ).encode("ascii"),
        "Qt checksum evidence differs from the source identity",
    )

    ffmpeg = qt["dependencies"]["ffmpeg"]
    ffmpeg_source = ffmpeg["source"]
    ffmpeg_signature = ffmpeg_source["signature"]
    ffmpeg_key = ffmpeg_signature["key"]
    expected_ffmpeg_url = "https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz"
    require(
        ffmpeg["version"] == "7.1.1"
        and ffmpeg_source["status"] == "locked"
        and ffmpeg_source["url"] == expected_ffmpeg_url
        and ffmpeg_source["sha256"]
        == "733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"
        and ffmpeg_source["size"] == 11019500
        and ffmpeg_signature["url"] == expected_ffmpeg_url + ".asc"
        and ffmpeg_signature["sha256"]
        == "a52e92620b266ea341191a01b42a191e01c15a9f56e99b173582181781f5bc75"
        and ffmpeg_signature["size"] == 520,
        "FFmpeg source or signature identity mismatch",
    )
    ffmpeg_signature_payload = load_evidence(
        repository, ffmpeg_signature["evidence"]
    )
    require(
        len(ffmpeg_signature_payload) == ffmpeg_signature["size"]
        and hashlib.sha256(ffmpeg_signature_payload).hexdigest()
        == ffmpeg_signature["sha256"],
        "FFmpeg detached signature evidence mismatch",
    )
    ffmpeg_key_payload = load_locked_file(
        repository, ffmpeg_key["file"], "FFmpeg release key"
    )
    require(
        ffmpeg_key["retrieval_url"] == "https://ffmpeg.org/ffmpeg-devel.asc"
        and hashlib.sha256(ffmpeg_key_payload).hexdigest()
        == ffmpeg_key["sha256"]
        == "397b3becedcd5a98769967ff1ff8501ddc89f8368b8f766e4701377d7dbaabe5"
        and ffmpeg_key["fingerprint"]
        == "fcf986ea15e6e293a5644f10b4322f04d67658d8"
        and ffmpeg_key_payload.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
        and ffmpeg_key_payload.rstrip().endswith(
            b"-----END PGP PUBLIC KEY BLOCK-----"
        ),
        "FFmpeg release key identity mismatch",
    )
    require(
        ffmpeg["license"]
        == {
            "expression": (
                "LGPL-2.1-or-later AND BSD-3-Clause AND BSD-2-Clause AND "
                "BSD-Source-Code AND ISC AND MIT AND MPL-2.0"
            ),
            "file": "COPYING.LGPLv2.1",
            "sha256": "b634ab5640e258563c536e658cad87080553df6f34f62269a21d554844e58bfe",
        }
        and ffmpeg["layout"]
        == {
            "top_directory": "ffmpeg-7.1.1",
            "member_count": 8646,
            "files": [
                {
                    "file": "COPYING.LGPLv2.1",
                    "sha256": "b634ab5640e258563c536e658cad87080553df6f34f62269a21d554844e58bfe",
                },
                {
                    "file": "LICENSE.md",
                    "sha256": "cb48bf09a11f5fb576cddb0431c8f5ed0a60157a9ec942adffc13907cbe083f2",
                },
                {
                    "file": "configure",
                    "sha256": "e7c000ab52464fe5bf1e88b07ff2118875e79c056cd85ff0b877415f7d9deb54",
                },
                {
                    "file": "libavcodec/version_major.h",
                    "sha256": "1363595d85d4bec36318f4c33bd46ad7ab49f16a893c7b3585a0d420e385112c",
                },
                {
                    "file": "libavformat/version_major.h",
                    "sha256": "b50b3071ab4aa54ca6802b393a9de139d41a5cffd21a0dcaa7347a7c0bba7f5b",
                },
                {
                    "file": "libavutil/version.h",
                    "sha256": "d9889f49a84933fee0a83f36f59d2fce87e16f9b66c4c7bfce56a2da59fecc40",
                },
            ],
        },
        "FFmpeg license or archive layout identity mismatch",
    )

    xcb_cursor = qt["dependencies"]["xcb_util_cursor"]
    xcb_source = xcb_cursor["source"]
    xcb_signature = xcb_source["signature"]
    xcb_key = xcb_signature["key"]
    expected_xcb_url = (
        "https://xorg.freedesktop.org/archive/individual/lib/"
        "xcb-util-cursor-0.1.6.tar.xz"
    )
    require(
        xcb_cursor["version"] == "0.1.6"
        and xcb_source["status"] == "locked"
        and xcb_source["url"] == expected_xcb_url
        and xcb_source["sha256"]
        == "fdeb8bd127873519be5cc70dcd0d3b5d33b667877200f9925a59fdcad8f7a933"
        and xcb_source["size"] == 273084
        and xcb_signature["url"] == expected_xcb_url + ".sig"
        and xcb_signature["sha256"]
        == "6e1fd66c2182647c988f5c4b3a71615c7a62e1d6aef02928f807cdca39c2677a"
        and xcb_signature["size"] == 566,
        "xcb-util-cursor source or signature identity mismatch",
    )
    xcb_signature_payload = load_evidence(
        repository, xcb_signature["evidence"]
    )
    require(
        len(xcb_signature_payload) == xcb_signature["size"]
        and hashlib.sha256(xcb_signature_payload).hexdigest()
        == xcb_signature["sha256"],
        "xcb-util-cursor detached signature evidence mismatch",
    )
    xcb_key_payload = load_locked_file(
        repository, xcb_key["file"], "xcb-util-cursor release key"
    )
    require(
        xcb_key["retrieval_url"]
        == "https://gitlab.archlinux.org/archlinux/packaging/packages/"
        "xcb-util-cursor/-/raw/main/keys/pgp/"
        "3AB285232C46AE43D8E192F4DAB0F78EA6E7E2D2.asc"
        and hashlib.sha256(xcb_key_payload).hexdigest()
        == xcb_key["sha256"]
        == "5ec5e03a686fc6abfd6c0d6993e345692fb7be41d79ab257ad2bc7b2d2c817c7"
        and xcb_key["fingerprint"]
        == "3ab285232c46ae43d8e192f4dab0f78ea6e7e2d2"
        and xcb_key_payload.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
        and xcb_key_payload.rstrip().endswith(
            b"-----END PGP PUBLIC KEY BLOCK-----"
        ),
        "xcb-util-cursor release key identity mismatch",
    )
    require(
        xcb_cursor["license"]
        == {
            "expression": "MIT",
            "file": "COPYING",
            "sha256": "0dde91ae1d443105dc9e13cbaed6674c36683b7095836ad9ddfce26be270aad5",
        }
        and xcb_cursor["layout"]
        == {
            "top_directory": "xcb-util-cursor-0.1.6",
            "member_count": 40,
            "files": [
                {
                    "file": "COPYING",
                    "sha256": "0dde91ae1d443105dc9e13cbaed6674c36683b7095836ad9ddfce26be270aad5",
                },
                {
                    "file": "configure",
                    "sha256": "c894e83b88d111cba0e59afccf380d481120a484d572bbe7f56f4278507e088b",
                },
                {
                    "file": "cursor/Makefile.in",
                    "sha256": "7a656bb23b4177e833a8990d9d140abba02283240b394a1f13c4baf881f279c7",
                },
                {
                    "file": "cursor/xcb-cursor.pc.in",
                    "sha256": "8962f7ce570b2de7b3bf4015172fbd7152113cb073c4a1b5e13b85991431a9f7",
                },
            ],
        },
        "xcb-util-cursor license or archive layout identity mismatch",
    )

    vcpkg = config["vcpkg"]
    vcpkg_release = vcpkg["release"]
    vcpkg_tool = vcpkg["tool"]
    vcpkg_tool_source = vcpkg_tool["source"]
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
        vcpkg_tool_source
        == {
            "status": "locked",
            "url": "https://github.com/microsoft/vcpkg-tool/archive/"
            "98d7cb0cf1f4686a3e43aa5672b6230c1d56bce8.tar.gz",
            "sha256": (
                "5b0142bc8cd44e5ac7e7539257245be44ec6d695b863fc090f4e56589fed50dd"
            ),
            "size": 2990430,
            "archive_root": (
                "vcpkg-tool-98d7cb0cf1f4686a3e43aa5672b6230c1d56bce8"
            ),
            "member_count": 2457,
            "cmakelists_sha256": (
                "5d0a8e47501857e3883d427e702ed7904f57a55817de1fc55784604c9d5c32e1"
            ),
            "entrypoint_sha256": (
                "d62b4e2a8074b25b094ccf1d99730e6c6c43df8c744d114b94c3657359bccca7"
            ),
        },
        "vcpkg-tool corresponding source identity mismatch",
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
    cmake_source = cmake["source"]
    cmake_checksums = cmake_source["checksums"]
    cmake_signature = cmake_checksums["signature"]
    cmake_key = cmake_signature["key"]
    require(
        cmake_source["status"] == "locked"
        and cmake_source["url"]
        == "https://github.com/Kitware/CMake/releases/download/v4.4.0/"
        "cmake-4.4.0.tar.gz"
        and cmake_source["sha256"]
        == "65757f442fdd242e27f1728fc26dc0cba4164f7a0791a5c788631c00080369bc"
        and cmake_source["size"] == 13275398
        and cmake_source["layout"]
        == {
            "top_directory": "cmake-4.4.0",
            "member_count": 33033,
            "license_sha256": cmake["license"]["sha256"],
            "readme_sha256": (
                "2877e91220f07dc5ce53fefb23789066c1801c8b06bcc9aedb30787cc12ba7b9"
            ),
            "cmakelists_sha256": (
                "90a565ebddf9e0f8cac5484032580260a4a2b57d455f61d8afedbedf8b48c731"
            ),
        },
        "CMake corresponding source identity differs",
    )
    require(
        cmake_checksums["url"]
        == "https://github.com/Kitware/CMake/releases/download/v4.4.0/"
        "cmake-4.4.0-SHA-256.txt"
        and cmake_checksums["sha256"]
        == "90b67ae9ab545d351bff676b4318e5d52d315fe6b9c62e49ca2b6a219f92052d"
        and cmake_checksums["size"] == 2015
        and cmake_checksums["entries"] == 21
        and cmake_checksums["evidence"]
        == "evidence/checksums/cmake-4.4.0-SHA-256.txt.b64",
        "CMake checksum manifest identity differs",
    )
    require(
        cmake_signature["url"]
        == "https://github.com/Kitware/CMake/releases/download/v4.4.0/"
        "cmake-4.4.0-SHA-256.txt.asc"
        and cmake_signature["sha256"]
        == "792e7895c2fa618363bc0e3c9ee7b5a97fd03995630ae386270c27f9576f0589"
        and cmake_signature["size"] == 833
        and cmake_signature["evidence"]
        == "evidence/gpg/cmake-4.4.0-SHA-256.txt.asc.b64"
        and cmake_signature["verification"]
        == {
            "status": "cryptographically-valid-expired-key",
            "signature_time": "2026-07-09T18:21:38Z",
            "exception": "upstream-signing-subkey-expired-before-signing",
        },
        "CMake checksum signature policy differs",
    )
    require(
        cmake_key
        == {
            "file": "keys/CMAKE-RELEASE-KEY.asc",
            "retrieval_url": (
                "https://keys.openpgp.org/vks/v1/by-fingerprint/"
                "C6C265324BBEBDC350B513D02D2CEF1034921684"
            ),
            "sha256": (
                "ba1517003c9dbee2139dcaea10e5db2792c8a7372a350f90b578c83805d923c9"
            ),
            "size": 25388,
            "primary_fingerprint": "cba23971357c2e6590d9efd3ec8fef3a7bfb4eda",
            "signing_fingerprint": "c6c265324bbebdc350b513d02d2cef1034921684",
            "signing_key_expires_at": "2024-08-12T16:30:38Z",
        }
        and cmake_signature["verification"]["signature_time"]
        > cmake_key["signing_key_expires_at"],
        "CMake expired signing-subkey exception is not exact",
    )
    cmake_checksum_payload = load_evidence(
        repository, cmake_checksums["evidence"]
    )
    cmake_signature_payload = load_evidence(
        repository, cmake_signature["evidence"]
    )
    cmake_key_payload = load_locked_file(
        repository, cmake_key["file"], "CMake release key"
    )
    require(
        len(cmake_checksum_payload) == cmake_checksums["size"]
        and hashlib.sha256(cmake_checksum_payload).hexdigest()
        == cmake_checksums["sha256"]
        and len(cmake_signature_payload) == cmake_signature["size"]
        and hashlib.sha256(cmake_signature_payload).hexdigest()
        == cmake_signature["sha256"]
        and len(cmake_key_payload) == cmake_key["size"]
        and hashlib.sha256(cmake_key_payload).hexdigest() == cmake_key["sha256"]
        and (
            cmake_source["sha256"] + "  cmake-4.4.0.tar.gz\n"
        ).encode("ascii")
        in cmake_checksum_payload
        and (
            cmake_binary["sha256"]
            + "  cmake-4.4.0-linux-x86_64.tar.gz\n"
        ).encode("ascii")
        in cmake_checksum_payload,
        "CMake corresponding source evidence differs",
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
            sigstore["verification"] == "verified",
            "CPython Sigstore verification status differs",
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

    sbom_generator = config["sbom"]["generator"]
    sbom_source = sbom_generator["source"]
    sbom_key_path = (repository / sbom_source["key"]["file"]).resolve()
    try:
        sbom_key_path.relative_to(repository.resolve())
    except ValueError as error:
        raise EvidenceError("SBOM generator key escaped repository") from error
    sbom_key = sbom_key_path.read_bytes()
    require(
        hashlib.sha256(sbom_key).hexdigest() == sbom_source["key"]["sha256"],
        "SBOM generator key digest differs",
    )

    return {
        "sigstore_tuf_root_version": tuf["final_root_version"],
        "sigstore_tuf_targets_version": tuf["targets_version"],
        "sigstore_trusted_root_sha256": tuf["trusted_root_sha256"],
        "sigstore_artifact_key_sha256": hashlib.sha256(
            artifact_key
        ).hexdigest(),
        "sigstore_verifier_version": sigstore_verifier["version"],
        "nfpm_sigstore_status": config["nfpm"]["sigstore"]["status"],
        "rocky_index_sha256": sha256(_rocky_payload),
        "qemu_index_sha256": sha256(qemu_index_payload),
        "qemu_manifest_sha256": sha256(qemu_manifest_payload),
        "qemu_attestation_sha256": sha256(attestation_payload),
        "qemu_tag_object": source["tag_object"],
        "qemu_commit": source["commit"],
        "qemu_source_archive_sha256": qemu_archive["sha256"],
        "qemu_builder_source_sha256": builder_source["sha256"],
        "qemu_source_signature_status": qemu_signature["verification"]["status"],
        "python_sources": len(config["python"]["versions"]),
        "python_patches": python_patch_count,
        "python_sigstore_status": "verified",
        "zstd_tag_object": zstd_git["tag_object"],
        "zstd_commit": zstd_git["commit"],
        "zstd_signature_sha256": zstd_signature["sha256"],
        "qt_source_sha256": qt_source["sha256"],
        "qt_checksum_sha256": qt_checksum["sha256"],
        "ffmpeg_source_sha256": ffmpeg_source["sha256"],
        "ffmpeg_signature_sha256": ffmpeg_signature["sha256"],
        "xcb_util_cursor_source_sha256": xcb_source["sha256"],
        "xcb_util_cursor_signature_sha256": xcb_signature["sha256"],
        "vcpkg_tag_object": vcpkg_release["tag_object"],
        "vcpkg_commit": vcpkg_release["commit"],
        "vcpkg_tool_commit": vcpkg_tool["commit"],
        "vcpkg_tool_source_sha256": vcpkg_tool_source["sha256"],
        "vcpkg_tool_signature_sha256": vcpkg_signature["sha256"],
        "ninja_commit": ninja["commit"],
        "ninja_binary_sha256": ninja_binary["sha256"],
        "cmake_binary_sha256": cmake_binary["sha256"],
        "cmake_source_sha256": cmake_source["sha256"],
        "cmake_source_signature_status": cmake_signature["verification"]["status"],
        "sbom_generator_digest": sbom_generator["digest"],
        "sbom_generator_source_sha256": sbom_source["sha256"],
        "sbom_generator_key_sha256": sbom_source["key"]["sha256"],
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
        "valid supply-chain evidence: Sigstore TUF root %d/targets %d; "
        "Rocky %s; QEMU %s; source %s (%s), commit %s; "
        "CPython/nFPM Sigstore bundles %s/%s via Cosign %s; "
        "patches %d; zstd %s; vcpkg %s; "
        "Qt %s + FFmpeg %s + xcb-util-cursor %s; Ninja %s; "
        "CMake %s + source %s (%s); SBOM generator %s (%s)"
        % (
            result["sigstore_tuf_root_version"],
            result["sigstore_tuf_targets_version"],
            result["rocky_index_sha256"],
            result["qemu_manifest_sha256"],
            result["qemu_source_archive_sha256"],
            result["qemu_source_signature_status"],
            result["qemu_commit"],
            result["python_sigstore_status"],
            result["nfpm_sigstore_status"],
            result["sigstore_verifier_version"],
            result["python_patches"],
            result["zstd_commit"],
            result["vcpkg_commit"],
            config["qt"]["version"],
            config["qt"]["dependencies"]["ffmpeg"]["version"],
            config["qt"]["dependencies"]["xcb_util_cursor"]["version"],
            result["ninja_commit"],
            config["host_tools"]["cmake"]["version"],
            result["cmake_source_sha256"],
            result["cmake_source_signature_status"],
            config["sbom"]["generator"]["version"],
            result["sbom_generator_digest"],
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvidenceError, KeyError, TypeError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
