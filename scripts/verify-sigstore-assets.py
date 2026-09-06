#!/usr/bin/env python3
"""Verify Crossforge source bundles with a locked, TUF-authenticated Cosign."""

import argparse
import base64
import binascii
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
SUPPLY = runpy.run_path(
    str(REPOSITORY / "scripts/validate-supply-chain-evidence.py")
)
TUF = runpy.run_path(
    str(REPOSITORY / "scripts/verify-sigstore-tuf-root.py")
)
FETCH = runpy.run_path(
    str(REPOSITORY / "scripts/fetch-sigstore-assets.py")
)
REPORT = runpy.run_path(
    str(REPOSITORY / "scripts/validate-sigstore-report.py")
)
ValidationError = STRICT["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/sigstore-verification.schema.json"


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    return FETCH["sha256_file"](path)


def load_release(release_path, schema_path):
    return FETCH["load_release"](release_path, schema_path)


def load_downloads(root, expected):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), "Sigstore downloads root is invalid")
    manifest = STRICT["load_json"](root / "downloads.json")
    require(
        isinstance(manifest, dict)
        and set(manifest) == {"schema_version", "kind", "files"}
        and manifest["schema_version"] == 1
        and manifest["kind"] == "crossforge-sigstore-downloads"
        and manifest["files"] == expected,
        "Sigstore downloads manifest differs",
    )
    expected_names = {"downloads.json"}
    for record in expected:
        path = root / record["file"]
        FETCH["validate_download"](path, record)
        expected_names.add(record["file"])
    require(
        {path.name for path in root.iterdir()} == expected_names,
        "Sigstore downloads contain unexpected files",
    )
    return {record["file"]: root / record["file"] for record in expected}


def load_bundle(path):
    document = STRICT["load_json"](path)
    require(
        isinstance(document, dict)
        and document.get("mediaType")
        == "application/vnd.dev.sigstore.bundle.v0.3+json",
        "Sigstore bundle media type differs: %s" % path,
    )
    return document


def signed_timestamp_count(bundle):
    timestamps = (
        bundle.get("verificationMaterial", {})
        .get("timestampVerificationData", {})
        .get("rfc3161Timestamps", [])
    )
    require(isinstance(timestamps, list), "Sigstore timestamp list differs")
    return len(timestamps)


def run(command, label):
    environment = os.environ.copy()
    environment["COSIGN_YES"] = "true"
    process = subprocess.run(
        [str(value) for value in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=environment,
        timeout=180,
    )
    output = (process.stdout + process.stderr).strip()
    require(
        process.returncode == 0 and output == "Verified OK",
        "%s failed: %s"
        % (label, output[-4000:]),
    )
    require(
        "insecure" not in process.stderr.lower(),
        "%s used an insecure verification mode" % label,
    )


def verify_blob(
    cosign,
    bundle_path,
    trusted_root,
    artifact,
    identity=None,
    issuer=None,
    key=None,
    signed_timestamp=True,
):
    command = [
        cosign,
        "verify-blob",
        "--bundle",
        bundle_path,
        "--trusted-root",
        trusted_root,
    ]
    if key is not None:
        command.extend(["--key", key])
    else:
        require(identity and issuer, "keyless Sigstore policy is incomplete")
        command.extend(
            [
                "--certificate-identity",
                identity,
                "--certificate-oidc-issuer",
                issuer,
            ]
        )
    if signed_timestamp:
        command.append("--use-signed-timestamps")
    command.append(artifact)
    run(command, "Sigstore verification for %s" % Path(artifact).name)


def validate_cosign_version(cosign, version, commit):
    process = subprocess.run(
        [str(cosign), "version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=30,
    )
    output = process.stdout + process.stderr
    require(
        process.returncode == 0
        and "GitVersion:    v%s" % version in output
        and "GitCommit:     %s" % commit in output
        and "GitTreeState:  clean" in output
        and "Platform:      linux/amd64" in output,
        "Cosign binary identity differs",
    )


def write_json_once(path, document, schema_path):
    schema = STRICT["load_json"](schema_path)
    require(schema.get("$id") == SCHEMA_ID, "Sigstore evidence schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    require(not path.exists() and not path.is_symlink(), "Sigstore evidence output exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(path))
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def verify(arguments):
    release = load_release(arguments.release, arguments.release_schema)
    supply = SUPPLY["validate_evidence"](release, REPOSITORY)
    downloads = load_downloads(
        arguments.downloads, FETCH["asset_plan"](release)
    )
    verifier = release["sigstore"]["verifier"]
    trust = release["sigstore"]["trust"]
    exceptions = {
        record["artifact"]
        for record in verifier["policy"]["signed_timestamps"]["exceptions"]
    }
    require(
        exceptions == {"Python-3.9.25.tar.xz"},
        "Sigstore timestamp exception set differs",
    )

    with tempfile.TemporaryDirectory(
        prefix="crossforge-sigstore-verify-"
    ) as directory:
        temporary = Path(directory)
        trusted_root = TUF["decode_base64_evidence"](
            REPOSITORY / trust["trusted_root_evidence"],
            temporary / "trusted_root.json",
            "Sigstore trusted root",
        )
        artifact_key = TUF["decode_base64_evidence"](
            REPOSITORY / trust["artifact_key_evidence"],
            temporary / "artifact.pub",
            "Sigstore artifact key",
        )
        cosign = downloads["cosign"]
        kms_path = downloads["cosign-kms.sigstore.json"]
        kms_bundle = load_bundle(kms_path)
        try:
            kms_signature = base64.b64decode(
                kms_bundle["messageSignature"]["signature"], validate=True
            )
        except (KeyError, binascii.Error, ValueError) as error:
            raise ValidationError("Cosign KMS signature differs") from error
        require(
            TUF["verify_ecdsa_sha256"](
                artifact_key.read_text(encoding="utf-8"),
                kms_signature,
                cosign.read_bytes(),
            ),
            "Cosign bootstrap signature is invalid",
        )
        validate_cosign_version(
            cosign, verifier["version"], verifier["git_commit"]
        )
        require(
            signed_timestamp_count(kms_bundle) >= 1,
            "Cosign KMS bundle lacks a signed timestamp",
        )
        verify_blob(
            cosign,
            kms_path,
            trusted_root,
            cosign,
            key=artifact_key,
            signed_timestamp=True,
        )

        results = []
        for entry in release["python"]["versions"]:
            source = entry["source"]
            artifact_name = "Python-%s.tar.xz" % entry["version"]
            bundle_name = "cpython-%s.sigstore.json" % entry["version"]
            bundle_path = TUF["decode_base64_evidence"](
                REPOSITORY / source["sigstore"]["bundle_evidence"],
                temporary / bundle_name,
                "CPython %s Sigstore bundle" % entry["version"],
            )
            require(
                bundle_path.stat().st_size
                == source["sigstore"]["bundle_size"]
                and sha256_file(bundle_path)
                == source["sigstore"]["bundle_sha256"],
                "CPython %s Sigstore bundle identity differs"
                % entry["version"],
            )
            bundle = load_bundle(bundle_path)
            use_timestamp = artifact_name not in exceptions
            require(
                signed_timestamp_count(bundle) >= (1 if use_timestamp else 0)
                and (
                    use_timestamp
                    or signed_timestamp_count(bundle) == 0
                ),
                "CPython %s signed timestamp policy differs"
                % entry["version"],
            )
            verify_blob(
                cosign,
                bundle_path,
                trusted_root,
                downloads[artifact_name],
                identity=source["sigstore"]["identity"],
                issuer=source["sigstore"]["oidc_issuer"],
                signed_timestamp=use_timestamp,
            )
            results.append(
                {
                    "artifact": artifact_name,
                    "artifact_sha256": source["sha256"],
                    "bundle_sha256": source["sigstore"]["bundle_sha256"],
                    "identity": source["sigstore"]["identity"],
                    "issuer": source["sigstore"]["oidc_issuer"],
                    "signed_timestamp": use_timestamp,
                    "verified": True,
                }
            )

        nfpm = release["nfpm"]
        nfpm_bundle_path = downloads["nfpm-checksums.sigstore.json"]
        nfpm_bundle = load_bundle(nfpm_bundle_path)
        require(
            signed_timestamp_count(nfpm_bundle) >= 1,
            "nFPM Sigstore bundle lacks a signed timestamp",
        )
        verify_blob(
            cosign,
            nfpm_bundle_path,
            trusted_root,
            downloads["nfpm-checksums.txt"],
            identity=nfpm["sigstore"]["expected_identity"],
            issuer=nfpm["sigstore"]["expected_issuer"],
            signed_timestamp=True,
        )
        results.append(
            {
                "artifact": "nfpm-checksums.txt",
                "artifact_sha256": nfpm["checksums"]["sha256"],
                "bundle_sha256": nfpm["sigstore"]["sha256"],
                "identity": nfpm["sigstore"]["expected_identity"],
                "issuer": nfpm["sigstore"]["expected_issuer"],
                "signed_timestamp": True,
                "verified": True,
            }
        )

    results.sort(key=lambda record: record["artifact"])
    document = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-sigstore-source-verification",
        "status": "verified",
        "release_sha256": sha256_bytes(
            json.dumps(
                release,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "verifier": {
            "version": verifier["version"],
            "git_commit": verifier["git_commit"],
            "binary_sha256": verifier["binary"]["sha256"],
            "kms_bundle_sha256": verifier["kms_bundle"]["sha256"],
            "tuf_root_version": supply["sigstore_tuf_root_version"],
            "tuf_targets_version": supply[
                "sigstore_tuf_targets_version"
            ],
            "trusted_root_sha256": supply[
                "sigstore_trusted_root_sha256"
            ],
            "artifact_key_sha256": supply[
                "sigstore_artifact_key_sha256"
            ],
        },
        "artifacts": results,
        "checks": {
            "cosign_bootstrap_signature": True,
            "cosign_bundle": True,
            "certificate_chain": True,
            "certificate_identity": True,
            "sct": verifier["policy"]["require_sct"],
            "transparency_log": verifier["policy"]["require_tlog"],
            "inclusion_proof": True,
            "signed_timestamps_when_present": True,
            "offline": True,
        },
    }
    REPORT["validate_report_document"](
        document, release, STRICT["load_json"](arguments.evidence_schema)
    )
    write_json_once(arguments.output, document, arguments.evidence_schema)
    print(
        "verified %d Sigstore source bundles with Cosign %s: %s"
        % (len(results), verifier["version"], arguments.output)
    )
    return document


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--downloads", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument(
        "--release",
        type=Path,
        default=REPOSITORY / "config/release.json",
    )
    result.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    result.add_argument(
        "--evidence-schema",
        type=Path,
        default=REPOSITORY
        / "config/schemas/sigstore-verification.schema.json",
    )
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        verify(arguments)
    except (
        KeyError,
        OSError,
        subprocess.SubprocessError,
        SUPPLY["EvidenceError"],
        TUF["TUFError"],
        ValidationError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
