#!/usr/bin/env python3
"""Fetch only version-database Git trees missing from a pinned vcpkg clone."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


OID_RE = re.compile(r"^[0-9a-f]{40}\Z")
EXPECTED_VERSION_FILES = 3054
EXPECTED_VERSION_TREES = 39823
EXPECTED_VERSION_TREE_SHA256 = (
    "e593f4bc1905ff51ddc990ffee7a04ed81ae7472ed300d8884f1ba506e94363e"
)


class FetchError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise FetchError(message)


def run(arguments, repository, input_text=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=str(repository),
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "command failed (%s):\n%s"
        % (
            " ".join(str(argument) for argument in arguments),
            process.stdout + process.stderr,
        ),
    )
    return process.stdout


def version_trees(repository):
    paths = sorted((repository / "versions").glob("*/*.json"))
    require(
        len(paths) == EXPECTED_VERSION_FILES,
        "vcpkg version-file inventory differs",
    )
    trees = set()
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise FetchError("invalid vcpkg version file: %s" % path) from error
        versions = document.get("versions") if isinstance(document, dict) else None
        require(isinstance(versions, list) and versions, "empty vcpkg version file")
        for record in versions:
            tree = record.get("git-tree") if isinstance(record, dict) else None
            require(isinstance(tree, str) and OID_RE.match(tree), "invalid Git tree")
            trees.add(tree)
    require(
        len(trees) == EXPECTED_VERSION_TREES,
        "vcpkg version-tree inventory differs",
    )
    trees = sorted(trees)
    digest = hashlib.sha256(("\n".join(trees) + "\n").encode("ascii")).hexdigest()
    require(
        digest == EXPECTED_VERSION_TREE_SHA256,
        "vcpkg version-tree digest differs",
    )
    return trees


def missing_trees(repository, trees):
    output = run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        repository,
        "\n".join(trees) + "\n",
    )
    lines = output.splitlines()
    require(len(lines) == len(trees), "vcpkg Git batch output differs")
    missing = []
    for tree, line in zip(trees, lines):
        if line == tree + " tree":
            continue
        require(line == tree + " missing", "vcpkg Git object is not a tree")
        missing.append(tree)
    return missing


def fetch(repository, tag, commit):
    require(repository.is_dir() and (repository / ".git").is_dir(), "invalid clone")
    require(OID_RE.match(commit), "invalid release commit")
    observed = run(["git", "rev-parse", tag + "^{}"], repository).strip()
    require(observed == commit, "vcpkg release tag moved")
    run(["git", "checkout", "--detach", "--force", commit], repository)
    trees = version_trees(repository)
    missing = missing_trees(repository, trees)
    if missing:
        run(["git", "fetch", "--no-tags", "origin"] + missing, repository)
    require(
        not missing_trees(repository, trees),
        "vcpkg registry still has missing version trees",
    )
    print(
        "fetched vcpkg history closure: %d files, %d trees, %d fetched"
        % (EXPECTED_VERSION_FILES, len(trees), len(missing))
    )
    return len(missing)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    arguments = parser.parse_args()
    fetch(arguments.repository, arguments.tag, arguments.commit)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FetchError, OSError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
