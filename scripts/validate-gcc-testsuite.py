#!/usr/bin/env python3
"""Validate GCC testsuite policy/baselines and normalize DejaGNU summaries."""

import argparse
import hashlib
import json
import re
import runpy
import sys
from collections import Counter
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]
load_json = STRICT["load_json"]
PLAN_SCHEMA = REPOSITORY / "config/schemas/gcc-testsuite-plan.schema.json"
BASELINE_SCHEMA = REPOSITORY / "config/schemas/gcc-testsuite-baseline.schema.json"
RESULT_RE = re.compile(
    r"^(PASS|FAIL|XFAIL|XPASS|KFAIL|KPASS|UNRESOLVED|UNTESTED|"
    r"UNSUPPORTED|ERROR|WARNING):\s+(.+)$"
)
STATUS_PREFIX_RE = re.compile(r"^([A-Z][A-Z0-9_-]*):\s*(.*)$")
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
EXPECTED_UNEXPECTED = [
    "ERROR",
    "FAIL",
    "KFAIL",
    "KPASS",
    "UNRESOLVED",
    "WARNING",
    "XPASS",
]
TARGET_CONTRACT = [
    {
        "arch": "x86_64",
        "triple": "x86_64-unknown-linux-gnu",
        "runtime_tiers": [
            {
                "name": "host-direct",
                "board": {
                    "name": "crossforge-x86_64",
                    "file": "tests/gcc/boards/crossforge-x86_64.exp",
                    "sha256": "e58d22f29110d15e91d89443ee16bde73d871543d3c5e294a2d6126f584e5c05",
                },
            }
        ],
    },
    {
        "arch": "aarch64",
        "triple": "aarch64-unknown-linux-gnu",
        "runtime_tiers": [
            {
                "name": "locked-sysroot",
                "board": {
                    "name": "crossforge-aarch64-locked",
                    "file": "tests/gcc/boards/crossforge-aarch64-locked.exp",
                    "sha256": "d48843e17cfa0ad8ae5f8a4190df0b3d5c68a3a31bc74f9dc9e2e9a81c36ff21",
                },
            },
            {
                "name": "clean-rocky",
                "board": {
                    "name": "crossforge-aarch64-clean",
                    "file": "tests/gcc/boards/crossforge-aarch64-clean.exp",
                    "sha256": "81dda3ea4668a5a273bace5a5b3204450f7e9300f45de8cd8f634cc53d59c7ae",
                },
            },
        ],
    },
]
SMOKE_SUITE_CONTRACT = [
    {
        "id": "gcc.execute",
        "make_target": "check-gcc",
        "sum_file": "gcc/testsuite/gcc/gcc.sum",
        "runtestflags": ["execute.exp=20000112-1.c"],
    }
]
FULL_SUITE_CONTRACT = [
    {
        "id": "g++.full",
        "make_target": "check-g++",
        "make_directory": "gcc",
        "sum_file": "gcc/testsuite/g++/g++.sum",
        "runtestflags": [],
    },
    {
        "id": "gcc.full",
        "make_target": "check-gcc",
        "make_directory": "gcc",
        "sum_file": "gcc/testsuite/gcc/gcc.sum",
        "runtestflags": [],
    },
    {
        "id": "libgomp.full",
        "make_target": "check-target-libgomp",
        "sum_file": "{target}/libgomp/testsuite/libgomp.sum",
        "runtestflags": [],
    },
    {
        "id": "libstdc++.full",
        "driver": "runtest-installed",
        "tool": "libstdc++",
        "timeout_seconds": 7200,
        "sum_file": "libstdc++.sum",
        "runtestflags": [],
    },
]
SUITE_CONTRACTS = {
    "full": FULL_SUITE_CONTRACT,
    "smoke": SMOKE_SUITE_CONTRACT,
}
PROFILE_JOBS = {
    "full": 4,
    "smoke": 1,
}
PROFILE_HOST_GCC_MAJOR = {
    "full": "8",
    "smoke": None,
}
FULL_SOURCE_PATCH_CONTRACT = [
    {
        "id": "libstdcxx-dumpfullversion",
        "suite": "libstdc++.full",
        "file": "patches/gcc/0001-libstdcxx-use-dumpfullversion.patch",
        "sha256": "2b4494622a40ccbe7ae450bab6993482677bde81cccb3ad4ef940060372dbf1d",
        "strip": 1,
        "targets": [
            {
                "file": "libstdc++-v3/testsuite/lib/dts.exp",
                "before_sha256": "882fda388274fe4576d0fd7438e05cf0d9a58521fde0f362391b48a8a36aa8b9",
                "after_sha256": "3862b02f6b329f6be7fe31cef254c7680c60e3b72c52faacd9b0eae483a3b593",
            }
        ],
    },
    {
        "id": "libstdcxx-el8-pr93672-hang",
        "suite": "libstdc++.full",
        "file": "patches/gcc/0004-libstdcxx-skip-el8-pr93672-hang.patch",
        "sha256": "55ebd4164f14710bd662305ed806499c620a73e065505aa091e5908eb8022625",
        "strip": 1,
        "targets": [
            {
                "file": "libstdc++-v3/testsuite/27_io/basic_istream/ignore/char/93672.cc",
                "before_sha256": "42570e6e201b64830d44e3c0c993408f6e9966b5091f1d3f00567bfa38e78020",
                "after_sha256": "6e2d606511f1c00eb69e00778275953ec7d82293c33a637a906006a1b7ea3ae6",
            }
        ],
    },
    {
        "id": "testsuite-gcov-under-test",
        "suite": "g++.full",
        "file": "patches/gcc/0002-testsuite-honor-gcov-under-test.patch",
        "sha256": "302280973e6e9c5b47219da36e1b90d2a06ba4a1b250313ab340799364dd335e",
        "strip": 1,
        "targets": [
            {
                "file": "gcc/testsuite/g++.dg/gcov/gcov.exp",
                "before_sha256": "f0656bfcddeae61ef20dd25aa599d2fa6fe284e058d9ed350b3803eb578db8b5",
                "after_sha256": "7b394237a6bee05dac22ed20325473c7d1f507bc4b453bb658660d2700a42d32",
            }
        ],
    },
    {
        "id": "gcc-testsuite-gcov-under-test",
        "suite": "gcc.full",
        "file": "patches/gcc/0005-gcc-testsuite-honor-gcov-under-test.patch",
        "sha256": "dadb6bcd96c250d1b7d381c05f4efec48fb60fe09db147a8cf0bcfff99f9bac6",
        "strip": 1,
        "targets": [
            {
                "file": "gcc/testsuite/gcc.misc-tests/gcov.exp",
                "before_sha256": "d66fad345baa8fb4134af67e9172c2562c2a73865af00ea458a0b57da2204200",
                "after_sha256": "b604c19b36e99601a7b16f9d99557d65ec48669f5968162b5befeadb18c7aca8",
            },
        ],
    },
    {
        "id": "testsuite-cross-gcc-ar",
        "suite": "gcc.full",
        "file": "patches/gcc/0003-testsuite-honor-cross-gcc-ar.patch",
        "sha256": "38a9f473bcb11ba773e7ba78f13ed5dd8332c50aa9533e47ad3b5fd4a27fdb46",
        "strip": 1,
        "targets": [
            {
                "file": "gcc/testsuite/lib/lto.exp",
                "before_sha256": "a8c523bad8d33994d41354d021b6bd248a2f785c217b9a5e01a05203cb3b9a03",
                "after_sha256": "e95feeaf2771859c3f5bb1cea791b2337b0845e48ec1ad453f6d8da68d8234ef",
            }
        ],
    },
]


def canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_file(value, label):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValidationError("%s is not a safe repository path" % label)
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or str(relative) != value:
        raise ValidationError("%s is not a safe repository path" % label)
    path = (REPOSITORY / Path(*relative.parts)).resolve()
    if REPOSITORY not in path.parents:
        raise ValidationError("%s escapes the repository" % label)
    if path.is_symlink() or not path.is_file():
        raise ValidationError("%s is missing or not a regular file" % label)
    return path


def validate_schema(document, path):
    schema = load_json(path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")


def result_key(record):
    return (record["suite"], record["status"], record["test"], record["count"])


def validate_plan(plan):
    validate_schema(plan, PLAN_SCHEMA)
    if plan["profile"] not in SUITE_CONTRACTS:
        raise ValidationError("unknown GCC testsuite profile")
    if plan["unexpected_statuses"] != EXPECTED_UNEXPECTED:
        raise ValidationError("GCC testsuite unexpected status policy differs")
    if plan["targets"] != TARGET_CONTRACT:
        raise ValidationError("GCC testsuite target/runtime matrix differs")
    if plan["suites"] != SUITE_CONTRACTS[plan["profile"]]:
        raise ValidationError(
            "GCC testsuite %s suite contract differs" % plan["profile"]
        )
    if plan.get("jobs", 1) != PROFILE_JOBS[plan["profile"]]:
        raise ValidationError(
            "GCC testsuite %s parallelism differs" % plan["profile"]
        )
    if plan.get("host_gcc_major") != PROFILE_HOST_GCC_MAJOR[plan["profile"]]:
        raise ValidationError(
            "GCC testsuite %s host compiler policy differs" % plan["profile"]
        )
    expected_patches = (
        FULL_SOURCE_PATCH_CONTRACT if plan["profile"] == "full" else None
    )
    if plan.get("source_patches") != expected_patches:
        raise ValidationError(
            "GCC testsuite %s source patch contract differs" % plan["profile"]
        )
    for patch in plan.get("source_patches", []):
        patch_path = repository_file(patch["file"], "GCC testsuite source patch")
        if file_sha256(patch_path) != patch["sha256"]:
            raise ValidationError("GCC testsuite source patch digest differs")
    site_path = repository_file(plan["site"]["file"], "GCC testsuite site file")
    if file_sha256(site_path) != plan["site"]["sha256"]:
        raise ValidationError("GCC testsuite site file digest differs")
    for target in plan["targets"]:
        for tier in target["runtime_tiers"]:
            board = tier["board"]
            board_path = repository_file(board["file"], "GCC testsuite board file")
            if file_sha256(board_path) != board["sha256"]:
                raise ValidationError("GCC testsuite board file digest differs")
    suite_ids = [suite["id"] for suite in plan["suites"]]
    if suite_ids != sorted(suite_ids) or len(suite_ids) != len(set(suite_ids)):
        raise ValidationError("GCC testsuite suite ids must be sorted and unique")
    sum_files = []
    for suite in plan["suites"]:
        for target in plan["targets"]:
            resolve_summary_path(suite["sum_file"], target["triple"])
        sum_files.append(suite["sum_file"])
    if len(sum_files) != len(set(sum_files)):
        raise ValidationError("GCC testsuite summary paths must be unique")
    return plan


def resolve_summary_path(template, target):
    if template.count("{target}") > 1 or (
        "{" in template.replace("{target}", "")
        or "}" in template.replace("{target}", "")
    ):
        raise ValidationError("GCC testsuite summary path has an unknown template")
    value = template.replace("{target}", target)
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or str(relative) != value
    ):
        raise ValidationError("GCC testsuite summary path is unsafe")
    return relative


def validate_baseline(baseline, plan, target, runtime_tier):
    validate_schema(baseline, BASELINE_SCHEMA)
    if baseline["profile"] != plan["profile"]:
        raise ValidationError("GCC testsuite baseline profile differs")
    if baseline["plan_sha256"] != canonical_sha256(plan):
        raise ValidationError("GCC testsuite baseline plan digest differs")
    if baseline["target"] != target or baseline["runtime_tier"] != runtime_tier:
        raise ValidationError("GCC testsuite baseline target/runtime differs")
    unexpected = baseline["unexpected"]
    if unexpected != sorted(unexpected, key=result_key):
        raise ValidationError("GCC testsuite baseline records are not sorted")
    keys = [result_key(record) for record in unexpected]
    if len(keys) != len(set(keys)):
        raise ValidationError("GCC testsuite baseline records are duplicated")
    suite_ids = {suite["id"] for suite in plan["suites"]}
    allowed_statuses = set(plan["unexpected_statuses"])
    for record in unexpected:
        if record["suite"] not in suite_ids or record["status"] not in allowed_statuses:
            raise ValidationError("GCC testsuite baseline record is outside the plan")
    return baseline


def validate_profile_contract(release, contract, expected_pairs):
    plan_path = repository_file(contract["plan"]["file"], "GCC testsuite plan")
    plan = validate_plan(load_json(plan_path))
    plan_sha256 = canonical_sha256(plan)
    if plan_sha256 != contract["plan"]["canonical_sha256"]:
        raise ValidationError("release GCC testsuite plan digest differs")
    if plan["profile"] != contract["profile"]:
        raise ValidationError("release GCC testsuite profile differs")
    if plan["gcc_version"] != release["gts"]["gcc_version"]:
        raise ValidationError("GCC testsuite compiler version differs from release")
    records = contract["baselines"]
    pairs = [(record["target"], record["runtime_tier"]) for record in records]
    if pairs != expected_pairs:
        raise ValidationError("release GCC testsuite baselines differ from the matrix")
    baselines = {}
    for record in records:
        path = repository_file(record["file"], "GCC testsuite baseline")
        baseline = validate_baseline(
            load_json(path), plan, record["target"], record["runtime_tier"]
        )
        if canonical_sha256(baseline) != record["canonical_sha256"]:
            raise ValidationError("release GCC testsuite baseline digest differs")
        baselines[(record["target"], record["runtime_tier"])] = {
            "document": baseline,
            "path": path,
            "canonical_sha256": record["canonical_sha256"],
        }
    return {
        "plan": plan,
        "plan_path": plan_path,
        "plan_sha256": plan_sha256,
        "baselines": baselines,
    }


def validate_release_contract(release_path):
    release = load_json(release_path)
    release_schema = load_json(REPOSITORY / "config/schemas/release.schema.json")
    STRICT["validate_schema_subset"](release_schema)
    STRICT["validate"](release, release_schema, release_schema, "$")
    smoke_contract = release["gcc_testsuite"]
    smoke_pairs = [
        (target["triple"], tier["name"])
        for target in TARGET_CONTRACT
        for tier in target["runtime_tiers"]
    ]
    profiles = {
        "smoke": validate_profile_contract(
            release, smoke_contract, smoke_pairs
        ),
        "full": validate_profile_contract(
            release,
            smoke_contract["full"],
            [("x86_64-unknown-linux-gnu", "host-direct")],
        ),
    }
    return {
        "release": release,
        "profiles": profiles,
        "plan": profiles["smoke"]["plan"],
        "plan_path": profiles["smoke"]["plan_path"],
        "plan_sha256": profiles["smoke"]["plan_sha256"],
        "baselines": profiles["smoke"]["baselines"],
    }


def parse_summary(suite, path, unexpected_statuses):
    if path.is_symlink() or not path.is_file():
        raise ValidationError("GCC testsuite summary is missing: %s" % path)
    counts = Counter()
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        for number, raw in enumerate(stream, 1):
            line = raw.rstrip("\r\n")
            match = RESULT_RE.match(line)
            if match is not None:
                status, test = match.groups()
                counts[(status, test)] += 1
                continue
            prefix = STATUS_PREFIX_RE.match(line)
            if prefix is not None and prefix.group(1) not in KNOWN_STATUSES:
                raise ValidationError(
                    "unknown DejaGNU status at %s:%d: %s"
                    % (path, number, prefix.group(1))
                )
    if not counts:
        raise ValidationError("GCC testsuite summary contains no result records")
    records = [
        {
            "suite": suite,
            "status": status,
            "test": test,
            "count": count,
        }
        for (status, test), count in counts.items()
    ]
    records.sort(key=result_key)
    unexpected = [
        record for record in records if record["status"] in unexpected_statuses
    ]
    return records, unexpected


def normalize_summaries(plan, baseline, summaries, materials):
    all_records = []
    unexpected = []
    raw = []
    expected_suites = [suite["id"] for suite in plan["suites"]]
    if sorted(summaries) != sorted(expected_suites):
        raise ValidationError("GCC testsuite summary set differs from the plan")
    for suite in expected_suites:
        path = Path(summaries[suite])
        records, failures = parse_summary(
            suite, path, set(plan["unexpected_statuses"])
        )
        all_records.extend(records)
        unexpected.extend(failures)
        raw.append(
            {
                "suite": suite,
                "sha256": file_sha256(path),
                "size": path.stat().st_size,
            }
        )
    all_records.sort(key=result_key)
    unexpected.sort(key=result_key)
    expected = baseline["unexpected"]
    if unexpected != expected:
        expected_keys = {result_key(record): record for record in expected}
        observed_keys = {result_key(record): record for record in unexpected}
        added = [observed_keys[key] for key in sorted(set(observed_keys) - set(expected_keys))]
        resolved = [expected_keys[key] for key in sorted(set(expected_keys) - set(observed_keys))]
        raise ValidationError(
            "GCC testsuite baseline mismatch: added=%s resolved=%s"
            % (json.dumps(added, sort_keys=True), json.dumps(resolved, sort_keys=True))
        )
    if not any(record["status"] == "PASS" for record in all_records):
        raise ValidationError("GCC testsuite produced no passing test")
    status_counts = Counter()
    for record in all_records:
        status_counts[record["status"]] += record["count"]
    return {
        "schema_version": 1,
        "kind": "gcc-testsuite-report",
        "status": "passed",
        "profile": plan["profile"],
        "target": baseline["target"],
        "runtime_tier": baseline["runtime_tier"],
        "plan_sha256": canonical_sha256(plan),
        "baseline_sha256": canonical_sha256(baseline),
        "status_counts": {
            status: status_counts[status] for status in sorted(status_counts)
        },
        "results": all_records,
        "unexpected": unexpected,
        "summaries": raw,
        "materials": materials,
    }


def observe_summaries(plan, target, runtime_tier, summaries, materials):
    all_records = []
    unexpected = []
    raw = []
    expected_suites = [suite["id"] for suite in plan["suites"]]
    if sorted(summaries) != sorted(expected_suites):
        raise ValidationError("GCC testsuite summary set differs from the plan")
    for suite in expected_suites:
        path = Path(summaries[suite])
        records, failures = parse_summary(
            suite, path, set(plan["unexpected_statuses"])
        )
        all_records.extend(records)
        unexpected.extend(failures)
        raw.append(
            {
                "suite": suite,
                "sha256": file_sha256(path),
                "size": path.stat().st_size,
            }
        )
    all_records.sort(key=result_key)
    unexpected.sort(key=result_key)
    if not any(record["status"] == "PASS" for record in all_records):
        raise ValidationError("GCC testsuite observation produced no passing test")
    status_counts = Counter()
    for record in all_records:
        status_counts[record["status"]] += record["count"]
    plan_sha256 = canonical_sha256(plan)
    candidate = {
        "$schema": "https://crossforge.dev/schemas/gcc-testsuite-baseline.schema.json",
        "schema_version": 1,
        "kind": "gcc-testsuite-baseline",
        "profile": plan["profile"],
        "plan_sha256": plan_sha256,
        "target": target,
        "runtime_tier": runtime_tier,
        "unexpected": unexpected,
    }
    validate_baseline(candidate, plan, target, runtime_tier)
    return (
        {
            "schema_version": 1,
            "kind": "gcc-testsuite-observation",
            "status": "observed",
            "profile": plan["profile"],
            "target": target,
            "runtime_tier": runtime_tier,
            "plan_sha256": plan_sha256,
            "candidate_baseline_sha256": canonical_sha256(candidate),
            "status_counts": {
                status: status_counts[status] for status in sorted(status_counts)
            },
            "results": all_records,
            "unexpected": unexpected,
            "summaries": raw,
            "materials": materials,
        },
        candidate,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--release", type=Path, default=REPOSITORY / "config/release.json")
    parser.add_argument("--plan", type=Path, action="append", default=[])
    arguments = parser.parse_args()
    try:
        contract = validate_release_contract(arguments.release)
        plans = [validate_plan(load_json(path)) for path in arguments.plan]
    except (OSError, UnicodeError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    for profile in ("smoke", "full"):
        selected = contract["profiles"][profile]
        print(
            "valid GCC testsuite %s contract: %s (%d runtime baseline(s))"
            % (
                profile,
                selected["plan_sha256"],
                len(selected["baselines"]),
            )
        )
    for plan in plans:
        print(
            "valid GCC testsuite %s plan: %s"
            % (plan["profile"], canonical_sha256(plan))
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
