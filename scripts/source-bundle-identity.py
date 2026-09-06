#!/usr/bin/env python3
"""Create a strict identity for one candidate-bound source archive."""

import argparse
import hashlib
import json
import os
import re
import runpy
import stat
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/source-bundle-identity.schema.json"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}\Z")
CHECKSUM_RE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9_.-]+)\n\Z")


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def file_identity(path):
    information = os.lstat(str(path))
    require(stat.S_ISREG(information.st_mode), "source archive is not regular")
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {"sha256": digest.hexdigest(), "size": size}


def create_identity(archive, checksum, manifest, source_commit):
    require(COMMIT_RE.match(source_commit), "source bundle commit is invalid")
    filename = "crossforge-source-%s.tar.zst" % source_commit
    require(archive.name == filename, "source archive filename differs")
    identity = file_identity(archive)
    require(checksum.is_file() and not checksum.is_symlink(), "source checksum is missing")
    try:
        checksum_text = checksum.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as error:
        raise ValidationError("cannot read source checksum: %s" % error)
    match = CHECKSUM_RE.match(checksum_text)
    require(
        match is not None
        and match.group(1) == identity["sha256"]
        and match.group(2) == filename,
        "source archive checksum differs",
    )
    source_manifest = STRICT["load_json"](manifest)
    require(
        source_manifest.get("kind") == "crossforge-source-bundle"
        and source_manifest.get("source_commit") == source_commit,
        "source bundle manifest identity differs",
    )
    return {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-source-bundle-identity",
        "source_commit": source_commit,
        "release_sha256": source_manifest["release_sha256"],
        "archive": dict({"file": filename}, **identity),
    }


def write_once(path, document):
    require(not path.exists() and not path.is_symlink(), "source identity output exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(path))
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--checksum", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/source-bundle-identity.schema.json",
    )
    arguments = parser.parse_args(argv)
    try:
        document = create_identity(
            arguments.archive,
            arguments.checksum,
            arguments.manifest,
            arguments.source_commit,
        )
        schema = STRICT["load_json"](arguments.schema)
        STRICT["validate_schema_subset"](schema)
        STRICT["validate"](document, schema, schema, "$")
        write_once(arguments.output, document)
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "source bundle identity: %s (%d bytes)"
        % (document["archive"]["sha256"], document["archive"]["size"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
