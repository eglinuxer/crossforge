#!/usr/bin/env python3
"""Validate an offline Sigstore qualification report against release.json."""

import argparse
import hashlib
import json
import runpy
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/sigstore-verification.schema.json"


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def canonical_sha256(document):
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def expected_artifacts(release):
    exceptions = {
        record["artifact"]
        for record in release["sigstore"]["verifier"]["policy"][
            "signed_timestamps"
        ]["exceptions"]
    }
    result = []
    for entry in release["python"]["versions"]:
        source = entry["source"]
        sigstore = source["sigstore"]
        artifact = "Python-%s.tar.xz" % entry["version"]
        require(
            sigstore["verification"] == "verified",
            "CPython Sigstore release status is not verified",
        )
        result.append(
            {
                "artifact": artifact,
                "artifact_sha256": source["sha256"],
                "bundle_sha256": sigstore["bundle_sha256"],
                "identity": sigstore["identity"],
                "issuer": sigstore["oidc_issuer"],
                "signed_timestamp": artifact not in exceptions,
                "verified": True,
            }
        )
    nfpm = release["nfpm"]
    require(
        nfpm["sigstore"]["status"] == "verified",
        "nFPM Sigstore release status is not verified",
    )
    result.append(
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
    result.sort(key=lambda record: record["artifact"])
    return result


def validate_report_document(document, release, schema):
    require(schema.get("$id") == SCHEMA_ID, "Sigstore report schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    verifier = release["sigstore"]["verifier"]
    trust = release["sigstore"]["trust"]
    require(
        document["release_sha256"] == canonical_sha256(release),
        "Sigstore report release digest differs",
    )
    require(
        document["verifier"]
        == {
            "version": verifier["version"],
            "git_commit": verifier["git_commit"],
            "binary_sha256": verifier["binary"]["sha256"],
            "kms_bundle_sha256": verifier["kms_bundle"]["sha256"],
            "tuf_root_version": trust["final_root_version"],
            "tuf_targets_version": trust["targets_version"],
            "trusted_root_sha256": trust["trusted_root_sha256"],
            "artifact_key_sha256": trust["artifact_key_sha256"],
        },
        "Sigstore report verifier identity differs",
    )
    require(
        document["artifacts"] == expected_artifacts(release),
        "Sigstore report artifact evidence differs",
    )
    return document


def load_release(path, schema_path):
    release = STRICT["load_json"](path)
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](release, schema, schema, "$")
    return release


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--release",
        type=Path,
        default=REPOSITORY / "config/release.json",
    )
    parser.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY
        / "config/schemas/sigstore-verification.schema.json",
    )
    arguments = parser.parse_args(argv)
    try:
        release = load_release(arguments.release, arguments.release_schema)
        report = STRICT["load_json"](arguments.report)
        schema = STRICT["load_json"](arguments.schema)
        validate_report_document(report, release, schema)
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print("valid Sigstore qualification report: %s" % arguments.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
