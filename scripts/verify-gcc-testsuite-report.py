#!/usr/bin/env python3
"""Verify a release-bound GCC full qualification report."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path


KNOWN_STATUSES = {
    "PASS",
    "FAIL",
    "XFAIL",
    "XPASS",
    "KFAIL",
    "KPASS",
    "UNRESOLVED",
    "UNTESTED",
    "UNSUPPORTED",
    "ERROR",
    "WARNING",
}


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def reject_nonfinite(value):
    raise VerificationError("non-finite JSON number: %s" % value)


def load_json(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "invalid JSON input: %s" % path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(
                stream,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_nonfinite,
            )
    except (OSError, UnicodeError, ValueError) as error:
        raise VerificationError("cannot load %s: %s" % (path, error)) from error
    require(isinstance(value, dict), "%s must contain an object" % path)
    return value


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256(value, label):
    require(
        isinstance(value, str) and re.match(r"^[0-9a-f]{64}$", value),
        "%s must be a lowercase SHA256" % label,
    )
    return value


def exact_keys(value, expected, label):
    require(
        isinstance(value, dict) and set(value) == set(expected),
        "%s fields differ" % label,
    )


def result_key(record):
    return (
        record.get("suite"),
        record.get("status"),
        record.get("test"),
        record.get("count"),
    )


def verify_documents(
    report, release, plan, baseline, expected_component_sha256
):
    exact_keys(
        report,
        {
            "schema_version",
            "kind",
            "status",
            "profile",
            "target",
            "runtime_tier",
            "plan_sha256",
            "baseline_sha256",
            "status_counts",
            "results",
            "unexpected",
            "summaries",
            "materials",
        },
        "GCC report",
    )
    require(
        report["schema_version"] == 1
        and report["kind"] == "gcc-testsuite-report"
        and report["status"] == "passed"
        and report["profile"] == "full"
        and report["target"] == "x86_64-unknown-linux-gnu"
        and report["runtime_tier"] == "host-direct",
        "GCC full qualification identity differs",
    )
    full = release.get("gcc_testsuite", {}).get("full", {})
    records = full.get("baselines", [])
    require(
        full.get("profile") == "full"
        and len(records) == 1
        and records[0].get("target") == report["target"]
        and records[0].get("runtime_tier") == report["runtime_tier"],
        "release GCC full contract differs",
    )
    plan_sha256 = sha256(
        full.get("plan", {}).get("canonical_sha256"), "release full plan"
    )
    baseline_sha256 = sha256(
        records[0].get("canonical_sha256"), "release full baseline"
    )
    require(
        canonical_sha256(plan) == plan_sha256
        and report["plan_sha256"] == plan_sha256,
        "GCC report plan differs",
    )
    require(
        plan.get("schema_version") == 1
        and plan.get("kind") == "gcc-testsuite-plan"
        and plan.get("profile") == "full",
        "GCC full plan identity differs",
    )
    planned_suites = [record.get("id") for record in plan.get("suites", [])]
    require(
        planned_suites
        == ["g++.full", "gcc.full", "libgomp.full", "libstdc++.full"],
        "GCC full plan suite set differs",
    )
    unexpected_statuses = plan.get("unexpected_statuses")
    require(
        isinstance(unexpected_statuses, list)
        and unexpected_statuses
        and all(
            isinstance(status, str) and status in KNOWN_STATUSES
            for status in unexpected_statuses
        )
        and len(unexpected_statuses) == len(set(unexpected_statuses)),
        "GCC full plan unexpected statuses are invalid",
    )
    exact_keys(
        baseline,
        {
            "$schema",
            "schema_version",
            "kind",
            "profile",
            "plan_sha256",
            "target",
            "runtime_tier",
            "unexpected",
        },
        "GCC baseline",
    )
    require(
        report["baseline_sha256"] == baseline_sha256
        and canonical_sha256(baseline) == baseline_sha256,
        "GCC report baseline differs",
    )
    require(
        baseline["schema_version"] == 1
        and baseline["kind"] == "gcc-testsuite-baseline"
        and baseline["profile"] == "full"
        and baseline["plan_sha256"] == plan_sha256
        and baseline["target"] == report["target"]
        and baseline["runtime_tier"] == report["runtime_tier"],
        "GCC full baseline identity differs",
    )
    require(
        baseline.get("unexpected") == report["unexpected"],
        "GCC report unexpected results differ from the baseline",
    )
    results = report["results"]
    require(
        isinstance(results, list) and results,
        "GCC report results are absent",
    )
    counts = Counter()
    for record in results:
        exact_keys(record, {"suite", "status", "test", "count"}, "GCC result")
        require(
            isinstance(record["suite"], str)
            and isinstance(record["status"], str)
            and isinstance(record["test"], str)
            and isinstance(record["count"], int)
            and not isinstance(record["count"], bool)
            and record["count"] >= 1,
            "GCC result record is invalid",
        )
        require(
            record["suite"] in planned_suites
            and record["status"] in KNOWN_STATUSES,
            "GCC result identity is not in the full plan",
        )
        counts[record["status"]] += record["count"]
    require(
        results == sorted(results, key=result_key),
        "GCC report results are unsorted",
    )
    require(
        report["status_counts"]
        == {status: counts[status] for status in sorted(counts)},
        "GCC report status counts are not derived from results",
    )
    require(counts.get("PASS", 0) > 0, "GCC report contains no PASS results")
    unexpected_status_set = set(unexpected_statuses)
    derived_unexpected = [
        record
        for record in results
        if record["status"] in unexpected_status_set
    ]
    require(
        report["unexpected"] == derived_unexpected,
        "GCC report unexpected results are not derived from results",
    )
    summaries = report["summaries"]
    require(
        isinstance(summaries, list)
        and [item.get("suite") for item in summaries]
        == planned_suites,
        "GCC report summary set differs",
    )
    for summary in summaries:
        exact_keys(summary, {"suite", "sha256", "size"}, "GCC summary")
        sha256(summary["sha256"], "GCC summary")
        require(
            isinstance(summary["size"], int)
            and not isinstance(summary["size"], bool)
            and summary["size"] > 0,
            "GCC summary size is invalid",
        )
    expected_component_sha256 = sha256(
        expected_component_sha256, "expected qualification component"
    )
    require(
        report.get("materials", {}).get("qualification_component")
        == {
            "component": "toolchain/gcc-testsuite-qualification",
            "canonical_sha256": expected_component_sha256,
        },
        "GCC qualification component differs",
    )
    return {
        "status": "passed",
        "plan_sha256": plan_sha256,
        "baseline_sha256": baseline_sha256,
        "results": len(results),
        "unexpected": len(report["unexpected"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--component-sha256", required=True)
    arguments = parser.parse_args()
    result = verify_documents(
        load_json(arguments.report),
        load_json(arguments.release),
        load_json(arguments.plan),
        load_json(arguments.baseline),
        arguments.component_sha256,
    )
    print(
        "verified GCC full qualification: %d records, %d unexpected"
        % (result["results"], result["unexpected"])
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, VerificationError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
