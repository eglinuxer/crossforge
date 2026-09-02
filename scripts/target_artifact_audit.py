#!/usr/bin/env python3
"""Validate canonical target-artifact guard records."""

import hashlib
import json
import os
from pathlib import Path


EXEC_OPERATIONS = (
    "execve",
    "execv",
    "execvp",
    "execvpe",
    "execl",
    "execlp",
    "execle",
    "fexecve",
    "execveat",
    "posix_spawn",
    "posix_spawnp",
)
LOADER_OPERATIONS = ("dlopen", "dlmopen")
RECORD_KEYS = {"operation", "path"}


class AuditError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise AuditError(message)


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(records):
    return hashlib.sha256(canonical_bytes(records)).hexdigest()


def path_is_below(path, root):
    return path == root or path.startswith(root + os.sep)


def validate_records(records, build_directory, prefix):
    require(isinstance(records, list) and records, "target-artifact audit is empty")
    build_root = str(build_directory)
    prefix_root = str(prefix)
    executable_canary = str(build_directory / "target-exec-canary")
    loader_canary = str(build_directory / "target-dlopen-canary.so")
    normalized = []
    for index, record in enumerate(records):
        require(
            isinstance(record, dict) and set(record) == RECORD_KEYS,
            "invalid target-artifact audit record %d" % index,
        )
        operation = record["operation"]
        path = record["path"]
        require(
            operation in EXEC_OPERATIONS + LOADER_OPERATIONS,
            "unknown target-artifact audit operation: %s" % operation,
        )
        require(
            isinstance(path, str)
            and path.startswith("/")
            and os.path.normpath(path) == path,
            "target-artifact audit path is not canonical: %r" % path,
        )
        require(
            path_is_below(path, build_root) or path_is_below(path, prefix_root),
            "target-artifact audit path escaped guarded roots",
        )
        normalized.append({"operation": operation, "path": path})

    for operation in EXEC_OPERATIONS:
        paths = [item["path"] for item in normalized if item["operation"] == operation]
        require(
            paths.count(executable_canary) == 1,
            "%s target-execution canary was not denied exactly once" % operation,
        )
        for path in paths:
            require(
                path == executable_canary or Path(path).name.startswith("conftest"),
                "cross-build attempted to execute a target artifact: %s %s"
                % (operation, path),
            )
    for operation in LOADER_OPERATIONS:
        paths = [item["path"] for item in normalized if item["operation"] == operation]
        require(
            paths == [loader_canary],
            "cross-build %s audit differs from its canary" % operation,
        )
    return {
        "records": normalized,
        "denied_execution_attempts": sum(
            1 for item in normalized if item["operation"] in EXEC_OPERATIONS
        ),
        "denied_loader_attempts": sum(
            1 for item in normalized if item["operation"] in LOADER_OPERATIONS
        ),
    }


def parse_lines(lines, build_directory, prefix):
    records = []
    for line in lines:
        fields = line.split("\t")
        require(len(fields) == 2, "invalid target-artifact audit line")
        records.append({"operation": fields[0], "path": fields[1]})
    return validate_records(records, build_directory, prefix)
