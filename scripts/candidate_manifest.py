#!/usr/bin/env python3
"""Create and validate a digest-only Crossforge candidate identity."""

import argparse
import hashlib
import json
import os
import re
import runpy
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_ID = "https://crossforge.dev/schemas/candidate.schema.json"
GIT_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
OCI_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ReleaseValidationError = STRICT["ValidationError"]


class CandidateError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise CandidateError(message)


def canonical_bytes(document):
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(document):
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def load_release(release_path, release_schema_path):
    release = STRICT["load_json"](release_path)
    schema = STRICT["load_json"](release_schema_path)
    require(
        isinstance(release, dict) and isinstance(schema, dict),
        "release and schema roots must be objects",
    )
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](release, schema, schema, "$")
    return release


def load_candidate_schema(schema_path):
    schema = STRICT["load_json"](schema_path)
    require(isinstance(schema, dict), "candidate schema root must be an object")
    STRICT["validate_schema_subset"](schema)
    require(schema.get("$id") == SCHEMA_ID, "candidate schema identity differs")
    return schema


def candidate_document(
    release, source_commit, digest, platform_manifest_digest
):
    return {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-candidate",
        "version": release["product"]["version"],
        "source_commit": source_commit,
        "release_sha256": canonical_sha256(release),
        "repository": release["product"]["image_repository"],
        "digest": digest,
        "platform": release["platforms"]["image"],
        "platform_manifest_digest": platform_manifest_digest,
    }


def candidate_tag(release, source_commit, run_id, run_attempt):
    require(GIT_SHA1_RE.match(source_commit or ""), "source commit is not a full Git SHA-1")
    require(RUN_ID_RE.match(run_id or ""), "run ID must be a positive decimal integer")
    require(
        RUN_ID_RE.match(run_attempt or ""),
        "run attempt must be a positive decimal integer",
    )
    tag_version = release["product"]["version"].replace("+", "_")
    tag = "candidate-v%s-g%s-r%s-a%s" % (
        tag_version,
        source_commit[:12],
        run_id,
        run_attempt,
    )
    require(OCI_TAG_RE.match(tag), "candidate tag is not a valid OCI tag")
    return tag


def validate_candidate(
    document, release, schema, expected_source_commit=None
):
    try:
        STRICT["validate"](document, schema, schema, "$")
    except ReleaseValidationError as error:
        raise CandidateError("candidate schema validation failed: %s" % error) from error
    require(
        document["version"] == release["product"]["version"],
        "candidate version differs from release",
    )
    require(
        document["release_sha256"] == canonical_sha256(release),
        "candidate release digest differs",
    )
    require(
        document["repository"] == release["product"]["image_repository"],
        "candidate image repository differs from release",
    )
    require(
        document["platform"] == release["platforms"]["image"],
        "candidate platform differs from release",
    )
    if expected_source_commit is not None:
        require(
            document["source_commit"] == expected_source_commit,
            "candidate source commit differs from expected commit",
        )
    return canonical_sha256(document)


def write_json_once(path, document):
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "candidate output must not be a symlink")
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as error:
            raise CandidateError("cannot read existing candidate output: %s" % path) from error
        require(
            existing == payload,
            "refusing to replace a different candidate manifest: %s" % path,
        )
        return False

    descriptor, temporary_text = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_text)
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


def add_release_arguments(parser):
    parser.add_argument(
        "--release", type=Path, default=REPOSITORY / "config/release.json"
    )
    parser.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )


def add_common_arguments(parser):
    add_release_arguments(parser)
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/candidate.schema.json",
    )


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = result.add_subparsers(dest="command")
    create = subparsers.add_parser("create", allow_abbrev=False)
    add_common_arguments(create)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--digest", required=True)
    create.add_argument("--platform-manifest-digest", required=True)
    create.add_argument("--output", type=Path, required=True)
    validate = subparsers.add_parser("validate", allow_abbrev=False)
    add_common_arguments(validate)
    validate.add_argument("candidate", type=Path)
    validate.add_argument("--expected-source-commit")
    tag = subparsers.add_parser("tag", allow_abbrev=False)
    add_release_arguments(tag)
    tag.add_argument("--source-commit", required=True)
    tag.add_argument("--run-id", required=True)
    tag.add_argument("--run-attempt", required=True)
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        require(
            arguments.command in ("create", "validate", "tag"),
            "a command is required",
        )
        release = load_release(arguments.release, arguments.release_schema)
        if arguments.command == "tag":
            print(
                candidate_tag(
                    release,
                    arguments.source_commit,
                    arguments.run_id,
                    arguments.run_attempt,
                )
            )
            return 0
        schema = load_candidate_schema(arguments.schema)
        if arguments.command == "create":
            document = candidate_document(
                release,
                arguments.source_commit,
                arguments.digest,
                arguments.platform_manifest_digest,
            )
            digest = validate_candidate(document, release, schema)
            state = "wrote" if write_json_once(arguments.output, document) else "current"
            print("%s candidate: %s (canonical sha256:%s)" % (state, arguments.output, digest))
            return 0

        document = STRICT["load_json"](arguments.candidate)
        require(isinstance(document, dict), "candidate root must be an object")
        digest = validate_candidate(
            document,
            release,
            schema,
            expected_source_commit=arguments.expected_source_commit,
        )
        print(
            "valid candidate: %s (canonical sha256:%s)"
            % (arguments.candidate, digest)
        )
        return 0
    except (CandidateError, ReleaseValidationError, KeyError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
