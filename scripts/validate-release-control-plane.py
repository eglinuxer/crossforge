#!/usr/bin/env python3
"""Validate fail-closed GitHub controls required for release channel writes."""

import argparse
import json
import os
import re
import runpy
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_ID = "https://crossforge.dev/schemas/release-control-plane.schema.json"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]+$")
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]


class ControlPlaneError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ControlPlaneError(message)


def load_object(path, label):
    document = STRICT["load_json"](path)
    require(isinstance(document, dict), "%s must contain an object" % label)
    return document


def validate_control_plane(
    immutable,
    private_reporting,
    environment,
    branch_policies,
    actions_permissions,
    repository,
    reviewer,
):
    require(REPOSITORY_RE.match(repository or ""), "GitHub repository is invalid")
    require(LOGIN_RE.match(reviewer or ""), "required reviewer login is invalid")
    require(immutable.get("enabled") is True, "immutable releases are not enabled")
    require(
        private_reporting.get("enabled") is True,
        "private vulnerability reporting is not enabled",
    )
    require(environment.get("name") == "production", "release environment differs")
    require(
        environment.get("deployment_branch_policy")
        == {"protected_branches": False, "custom_branch_policies": True},
        "production deployment branch policy differs",
    )
    rules = environment.get("protection_rules")
    require(isinstance(rules, list), "production protection rules are absent")
    review_rules = [rule for rule in rules if rule.get("type") == "required_reviewers"]
    require(len(review_rules) == 1, "required reviewer rule is not unique")
    review_rule = review_rules[0]
    require(
        review_rule.get("prevent_self_review") is False,
        "single-maintainer self review policy differs",
    )
    reviewers = review_rule.get("reviewers")
    require(isinstance(reviewers, list), "required reviewers are absent")
    reviewer_logins = [
        record.get("reviewer", {}).get("login")
        for record in reviewers
        if record.get("type") == "User"
    ]
    require(
        reviewer_logins == [reviewer] and len(reviewers) == 1,
        "production required reviewer differs",
    )
    require(
        len([rule for rule in rules if rule.get("type") == "branch_policy"]) == 1,
        "production branch protection rule differs",
    )
    policies = branch_policies.get("branch_policies")
    require(
        branch_policies.get("total_count") == 1
        and isinstance(policies, list)
        and len(policies) == 1,
        "production deployment branch set differs",
    )
    policy = policies[0]
    require(
        policy.get("name") == "main"
        and policy.get("type", "branch") == "branch",
        "production deployment is not restricted to main",
    )
    require(
        actions_permissions.get("default_workflow_permissions") == "read"
        and actions_permissions.get("can_approve_pull_request_reviews") is False,
        "default Actions token permissions are not least privilege",
    )
    return {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-release-control-plane",
        "repository": repository,
        "environment": {
            "name": "production",
            "required_reviewer": reviewer,
            "prevent_self_review": False,
            "deployment_branches": ["main"],
        },
        "actions": {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        "checks": {
            "immutable_releases": True,
            "private_vulnerability_reporting": True,
            "required_review": True,
            "main_only": True,
            "least_privilege_actions": True,
        },
    }


def validate_schema(document, path):
    schema = STRICT["load_json"](path)
    STRICT["validate_schema_subset"](schema)
    require(schema.get("$id") == SCHEMA_ID, "control-plane schema differs")
    try:
        STRICT["validate"](document, schema, schema, "$")
    except ValidationError as error:
        raise ControlPlaneError(
            "control-plane report schema failed: %s" % error
        ) from error
    return document


def write_json_once(path, document):
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "control-plane output is a symlink")
    if path.exists():
        require(
            path.read_text(encoding="utf-8") == payload,
            "control-plane output differs",
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


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--immutable-releases", type=Path, required=True)
    result.add_argument("--private-vulnerability-reporting", type=Path, required=True)
    result.add_argument("--environment", type=Path, required=True)
    result.add_argument("--deployment-branch-policies", type=Path, required=True)
    result.add_argument("--actions-permissions", type=Path, required=True)
    result.add_argument("--repository", required=True)
    result.add_argument("--required-reviewer", required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release-control-plane.schema.json",
    )
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        report = validate_control_plane(
            load_object(arguments.immutable_releases, "immutable release setting"),
            load_object(
                arguments.private_vulnerability_reporting,
                "private vulnerability reporting setting",
            ),
            load_object(arguments.environment, "production environment"),
            load_object(
                arguments.deployment_branch_policies,
                "production deployment branch policies",
            ),
            load_object(arguments.actions_permissions, "Actions permissions"),
            arguments.repository,
            arguments.required_reviewer,
        )
        validate_schema(report, arguments.schema)
        state = "wrote" if write_json_once(arguments.output, report) else "current"
        print("%s release control-plane report: %s" % (state, arguments.output))
        return 0
    except (ControlPlaneError, KeyError, OSError, TypeError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
