#!/usr/bin/env python3
"""Create and validate evidence for digest-only stable OCI promotion."""

import argparse
import copy
import json
import os
import re
import runpy
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_ID = "https://crossforge.dev/schemas/release-promotion.schema.json"
CANDIDATE_WORKFLOW = ".github/workflows/candidate.yml"
GIT_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
OCI_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
CANDIDATE = runpy.run_path(str(REPOSITORY / "scripts/candidate_manifest.py"))
ValidationError = STRICT["ValidationError"]
CandidateError = CANDIDATE["CandidateError"]


class PromotionError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PromotionError(message)


def positive_integer(value, label):
    require(type(value) is int and value > 0, "%s must be a positive integer" % label)
    return value


def load_release(path, schema_path):
    return CANDIDATE["load_release"](path, schema_path)


def load_schema(path):
    schema = STRICT["load_json"](path)
    require(isinstance(schema, dict), "promotion schema root must be an object")
    STRICT["validate_schema_subset"](schema)
    require(schema.get("$id") == SCHEMA_ID, "promotion schema identity differs")
    return schema


def validate_candidate_run(document, expected_repository, expected_run_id):
    require(isinstance(document, dict), "candidate run metadata root must be an object")
    require(
        GITHUB_REPOSITORY_RE.match(expected_repository or ""),
        "expected GitHub repository is invalid",
    )
    expected_run_id = positive_integer(expected_run_id, "candidate run ID")
    require(document.get("id") == expected_run_id, "candidate run ID differs")
    attempt = positive_integer(document.get("run_attempt"), "candidate run attempt")
    require(document.get("event") == "workflow_dispatch", "candidate run was not manual")
    require(document.get("status") == "completed", "candidate run is not complete")
    require(document.get("conclusion") == "success", "candidate run did not succeed")
    require(document.get("head_branch") == "main", "candidate run branch differs")
    require(document.get("path") == CANDIDATE_WORKFLOW, "candidate workflow path differs")
    head_sha = document.get("head_sha")
    require(GIT_SHA1_RE.match(head_sha or ""), "candidate run head SHA is invalid")
    repository = document.get("repository")
    head_repository = document.get("head_repository")
    require(
        isinstance(repository, dict)
        and repository.get("full_name") == expected_repository,
        "candidate run repository differs",
    )
    require(
        isinstance(head_repository, dict)
        and head_repository.get("full_name") == expected_repository,
        "candidate run head repository differs",
    )
    expected_url = "https://github.com/%s/actions/runs/%d" % (
        expected_repository,
        expected_run_id,
    )
    require(document.get("html_url") == expected_url, "candidate run URL differs")
    return {
        "id": expected_run_id,
        "attempt": attempt,
        "workflow_path": CANDIDATE_WORKFLOW,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": head_sha,
        "repository": expected_repository,
        "url": expected_url,
    }


def version_tag(version):
    tag = "v" + version.replace("+", "_")
    require(OCI_TAG_RE.match(tag), "release version cannot form an OCI tag")
    return tag


def image_evidence(repository, digest, platform_digest, version, channel, source=False):
    prefix = "source-" if source else ""
    release_tag = prefix + version_tag(version)
    channel_tag = prefix + channel
    require(OCI_TAG_RE.match(channel_tag), "stable channel cannot form an OCI tag")
    return {
        "repository": repository,
        "digest": digest,
        "platform_manifest_digest": platform_digest,
        "version_reference": "%s:%s" % (repository, release_tag),
        "channel_reference": "%s:%s" % (repository, channel_tag),
    }


def artifact_names(run_id, attempt):
    suffix = "%d-%d" % (run_id, attempt)
    return sorted(
        [
            "candidate-identity-" + suffix,
            "native-aarch64-probes-" + suffix,
            "native-aarch64-evidence-" + suffix,
            "candidate-signature-" + suffix,
        ]
    )


def promotion_document(
    release,
    candidate,
    candidate_sha256,
    candidate_run,
    promotion_repository,
    promotion_run_id,
    promotion_run_attempt,
    promotion_workflow_sha,
):
    require(
        candidate["source_commit"] == candidate_run["head_sha"],
        "candidate source commit differs from candidate run",
    )
    require(
        promotion_repository == candidate_run["repository"],
        "promotion repository differs from candidate run",
    )
    positive_integer(promotion_run_id, "promotion run ID")
    positive_integer(promotion_run_attempt, "promotion run attempt")
    require(
        GIT_SHA1_RE.match(promotion_workflow_sha or ""),
        "promotion workflow SHA is invalid",
    )
    product = release["product"]
    source = candidate["source_bundle"]
    return {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-release-promotion",
        "candidate_run": copy.deepcopy(candidate_run),
        "promotion_run": {
            "id": promotion_run_id,
            "attempt": promotion_run_attempt,
            "workflow_sha": promotion_workflow_sha,
            "repository": promotion_repository,
        },
        "release": {
            "version": product["version"],
            "stable_channel": product["stable_channel"],
            "canonical_sha256": CANDIDATE["canonical_sha256"](release),
        },
        "candidate_manifest_sha256": candidate_sha256,
        "candidate": image_evidence(
            candidate["repository"],
            candidate["digest"],
            candidate["platform_manifest_digest"],
            product["version"],
            product["stable_channel"],
        ),
        "source_bundle": image_evidence(
            source["repository"],
            source["digest"],
            source["platform_manifest_digest"],
            product["version"],
            product["stable_channel"],
            source=True,
        ),
        "artifacts": artifact_names(candidate_run["id"], candidate_run["attempt"]),
        "verification": {
            "candidate_evidence": "revalidated",
            "native_aarch64": "revalidated",
            "qt_native_aarch64": "revalidated",
            "signatures": "verified-with-pinned-trusted-root",
            "registry": "anonymous-exact-digest",
        },
    }


def validate_document(document, release, schema):
    try:
        STRICT["validate"](document, schema, schema, "$")
    except ValidationError as error:
        raise PromotionError("promotion schema validation failed: %s" % error) from error
    expected_release_sha256 = CANDIDATE["canonical_sha256"](release)
    require(
        document["release"]["canonical_sha256"] == expected_release_sha256,
        "promotion release digest differs",
    )
    product = release["product"]
    require(
        document["release"]["version"] == product["version"]
        and document["release"]["stable_channel"] == product["stable_channel"],
        "promotion release identity differs",
    )
    run = document["candidate_run"]
    require(
        document["artifacts"] == artifact_names(run["id"], run["attempt"]),
        "promotion artifact set differs",
    )
    for name, source in (("candidate", False), ("source_bundle", True)):
        image = document[name]
        expected = image_evidence(
            product["image_repository"],
            image["digest"],
            image["platform_manifest_digest"],
            product["version"],
            product["stable_channel"],
            source=source,
        )
        require(image == expected, "%s promotion references differ" % name)
    return document


def write_json_once(path, document):
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "promotion output must not be a symlink")
    if path.exists():
        require(
            path.read_text(encoding="utf-8") == payload,
            "refusing to replace different promotion evidence: %s" % path,
        )
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


def add_common(parser):
    parser.add_argument(
        "--release", type=Path, default=REPOSITORY / "config/release.json"
    )
    parser.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release-promotion.schema.json",
    )


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = result.add_subparsers(dest="command")
    create = commands.add_parser("create", allow_abbrev=False)
    add_common(create)
    create.add_argument("--candidate", type=Path, required=True)
    create.add_argument("--candidate-run", type=Path, required=True)
    create.add_argument("--expected-github-repository", required=True)
    create.add_argument("--expected-candidate-run-id", type=int, required=True)
    create.add_argument("--promotion-run-id", type=int, required=True)
    create.add_argument("--promotion-run-attempt", type=int, required=True)
    create.add_argument("--promotion-workflow-sha", required=True)
    create.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate", allow_abbrev=False)
    add_common(validate)
    validate.add_argument("evidence", type=Path)
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        require(arguments.command in ("create", "validate"), "a command is required")
        release = load_release(arguments.release, arguments.release_schema)
        schema = load_schema(arguments.schema)
        if arguments.command == "validate":
            document = STRICT["load_json"](arguments.evidence)
            validate_document(document, release, schema)
            print("valid release promotion evidence: %s" % arguments.evidence)
            return 0
        run_document = STRICT["load_json"](arguments.candidate_run)
        candidate_run = validate_candidate_run(
            run_document,
            arguments.expected_github_repository,
            arguments.expected_candidate_run_id,
        )
        candidate = STRICT["load_json"](arguments.candidate)
        candidate_schema = CANDIDATE["load_candidate_schema"](
            REPOSITORY / "config/schemas/candidate.schema.json"
        )
        candidate_sha256 = CANDIDATE["validate_candidate"](
            candidate,
            release,
            candidate_schema,
            expected_source_commit=candidate_run["head_sha"],
        )
        document = promotion_document(
            release,
            candidate,
            candidate_sha256,
            candidate_run,
            arguments.expected_github_repository,
            arguments.promotion_run_id,
            arguments.promotion_run_attempt,
            arguments.promotion_workflow_sha,
        )
        validate_document(document, release, schema)
        state = "wrote" if write_json_once(arguments.output, document) else "current"
        print("%s release promotion evidence: %s" % (state, arguments.output))
        return 0
    except (
        CandidateError,
        KeyError,
        OSError,
        PromotionError,
        TypeError,
        ValidationError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
