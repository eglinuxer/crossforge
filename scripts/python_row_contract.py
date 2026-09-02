#!/usr/bin/env python3
"""Define and validate the deliberately implemented CPython rows."""

import argparse
import re
import sys


class ContractError(RuntimeError):
    pass


# This is implementation state, not version-source metadata. Exact patch
# versions, support status, sources, and patches remain canonical in
# config/release.json. Order controls phase aggregation and is intentional.
IMPLEMENTED_ROWS = (
    {
        "minor": "3.13",
        "row": "cp313",
        "adapter": "modern",
        "gil_policy": "zero",
        "sysconfig_isolation": True,
        "zstd": False,
        "introduced_phase": 5,
    },
    {
        "minor": "3.11",
        "row": "cp311",
        "adapter": "transition",
        "gil_policy": "absent",
        "sysconfig_isolation": True,
        "zstd": False,
        "introduced_phase": 6,
    },
    {
        "minor": "3.12",
        "row": "cp312",
        "adapter": "modern",
        "gil_policy": "absent",
        "sysconfig_isolation": True,
        "zstd": False,
        "introduced_phase": 7,
    },
    {
        "minor": "3.14",
        "row": "cp314",
        "adapter": "modern",
        "gil_policy": "zero",
        "sysconfig_isolation": True,
        "zstd": True,
        "introduced_phase": 8,
    },
)
RECORD_FIELDS = (
    "minor",
    "row",
    "adapter",
    "gil_policy",
    "sysconfig_isolation",
    "zstd",
    "introduced_phase",
)
RECORD_KEYS = set(RECORD_FIELDS)
EXACT_VERSION = re.compile(r"3\.[0-9]+\.[0-9]+\Z")


def require(condition, message):
    if not condition:
        raise ContractError(message)


def copy_record(record):
    return {name: record[name] for name in RECORD_FIELDS}


def validate_contract_table():
    require(
        isinstance(IMPLEMENTED_ROWS, tuple) and IMPLEMENTED_ROWS,
        "implemented CPython table is empty",
    )
    minors = []
    rows = []
    for record in IMPLEMENTED_ROWS:
        require(set(record) == RECORD_KEYS, "implemented CPython record fields differ")
        require(
            re.fullmatch(r"3\.[0-9]+", record["minor"]) is not None,
            "implemented CPython minor is invalid",
        )
        require(
            record["row"] == "cp" + record["minor"].replace(".", ""),
            "implemented CPython row/minor mismatch",
        )
        require(
            record["adapter"] in ("legacy", "transition", "modern"),
            "implemented CPython adapter is invalid",
        )
        require(
            record["gil_policy"] in ("absent", "zero"),
            "implemented CPython GIL policy is invalid",
        )
        require(
            record["sysconfig_isolation"] is True,
            "implemented CPython row lacks sysconfig isolation",
        )
        require(
            type(record["zstd"]) is bool,
            "implemented CPython zstd policy is invalid",
        )
        require(
            type(record["introduced_phase"]) is int
            and record["introduced_phase"] > 0,
            "implemented CPython phase is invalid",
        )
        minors.append(record["minor"])
        rows.append(record["row"])
    require(len(minors) == len(set(minors)), "implemented CPython minor is duplicated")
    require(len(rows) == len(set(rows)), "implemented CPython row is duplicated")
    phases = [record["introduced_phase"] for record in IMPLEMENTED_ROWS]
    require(phases == sorted(phases), "implemented CPython phases are not ordered")
    first_phase = phases[0]
    require(
        len(
            [
                record
                for record in IMPLEMENTED_ROWS
                if record["introduced_phase"] <= first_phase
            ]
        )
        > 0,
        "first implemented CPython phase has no rows",
    )


def contract_for_version(version):
    require(
        isinstance(version, str) and EXACT_VERSION.fullmatch(version) is not None,
        "CPython version must be an exact patch release",
    )
    minor = version.rsplit(".", 1)[0]
    matches = [record for record in IMPLEMENTED_ROWS if record["minor"] == minor]
    require(len(matches) == 1, "CPython version is not implemented: %s" % version)
    return copy_record(matches[0])


def contract_for_row(row):
    require(isinstance(row, str), "CPython row must be text")
    matches = [record for record in IMPLEMENTED_ROWS if record["row"] == row]
    require(len(matches) == 1, "CPython row is not implemented: %s" % row)
    return copy_record(matches[0])


def bind_release(release, version=None, row=None, adapter=None):
    require(isinstance(release, dict), "release must be an object")
    require((version is None) != (row is None), "select CPython by version or row")
    if version is not None:
        contract = contract_for_version(version)
    else:
        contract = contract_for_row(row)
    if adapter is not None:
        require(adapter == contract["adapter"], "CPython adapter differs from implementation")
    try:
        versions = release["python"]["versions"]
    except (KeyError, TypeError) as error:
        raise ContractError("release has an invalid CPython version table") from error
    require(isinstance(versions, list), "release CPython versions must be an array")
    matches = []
    for entry in versions:
        require(isinstance(entry, dict), "release CPython entry must be an object")
        candidate = entry.get("version")
        require(
            isinstance(candidate, str)
            and EXACT_VERSION.fullmatch(candidate) is not None,
            "release CPython entry version is invalid",
        )
        if candidate.rsplit(".", 1)[0] != contract["minor"]:
            continue
        matches.append(entry)
    require(
        len(matches) == 1,
        "release must select one exact implemented CPython %s entry"
        % contract["minor"],
    )
    entry = matches[0]
    if version is not None:
        require(
            entry["version"] == version,
            "release does not select exact CPython version: %s" % version,
        )
    require(
        entry.get("adapter") == contract["adapter"],
        "release CPython adapter differs from implementation",
    )
    source = entry.get("source")
    require(
        isinstance(source, dict) and source.get("status") == "locked",
        "release CPython source is not locked",
    )
    return {"contract": contract, "entry": entry}


def contracts_for_phase(phase):
    require(type(phase) is int and phase > 0, "phase must be a positive integer")
    return tuple(
        copy_record(record)
        for record in IMPLEMENTED_ROWS
        if record["introduced_phase"] <= phase
    )


def rows_for_phase(phase):
    return tuple(record["row"] for record in contracts_for_phase(phase))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",))
    parser.add_argument("version")
    parser.add_argument("adapter")
    arguments = parser.parse_args()
    try:
        contract = contract_for_version(arguments.version)
        require(
            arguments.adapter == contract["adapter"],
            "CPython adapter differs from implementation",
        )
    except ContractError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print("valid: %s %s %s" % (contract["row"], arguments.version, arguments.adapter))
    return 0


validate_contract_table()
LATEST_PHASE = max(record["introduced_phase"] for record in IMPLEMENTED_ROWS)
LATEST_ROWS = rows_for_phase(LATEST_PHASE)


if __name__ == "__main__":
    raise SystemExit(main())
