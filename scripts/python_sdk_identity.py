#!/usr/bin/env python3
"""Compute the canonical content identity of a CPython SDK tree."""

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


class IdentityError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise IdentityError(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sdk_tree_identity(prefix):
    require(prefix.is_dir() and not prefix.is_symlink(), "SDK root is invalid: %s" % prefix)
    resolved_prefix = prefix.resolve()
    entries = []
    for path in sorted(prefix.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(prefix).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            resolved = path.resolve()
            require(
                resolved == resolved_prefix
                or str(resolved).startswith(str(resolved_prefix) + os.sep),
                "SDK symlink escapes target prefix: %s" % path,
            )
            entries.append(
                {"path": relative, "type": "symlink", "target": os.readlink(path)}
            )
        elif stat.S_ISREG(metadata.st_mode):
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "size": metadata.st_size,
                    "sha256": sha256_file(path),
                }
            )
        elif stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": relative, "type": "directory"})
        else:
            raise IdentityError("unsupported SDK file type: %s" % path)
    return {
        "entries": len(entries),
        "canonical_sha256": hashlib.sha256(canonical_bytes(entries)).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    arguments = parser.parse_args()
    try:
        identity = sdk_tree_identity(arguments.root)
    except (IdentityError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(json.dumps(identity, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
