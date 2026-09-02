#!/usr/bin/env python3
"""Fetch a content-locked source artifact from release.json."""

import argparse
import hashlib
import os
import runpy
import sys
import tempfile
import urllib.request
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
RELEASE_VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = RELEASE_VALIDATOR["ValidationError"]
load_json = RELEASE_VALIDATOR["load_json"]
validate = RELEASE_VALIDATOR["validate"]
validate_schema_subset = RELEASE_VALIDATOR["validate_schema_subset"]


def source_for(config, component, version=None):
    if component == "python":
        matches = [
            entry
            for entry in config["python"]["versions"]
            if entry["version"] == version
        ]
        if len(matches) != 1:
            raise ValidationError("Python source version is not unique: %s" % version)
        source = matches[0]["source"]
    else:
        if version is not None:
            raise ValidationError("--version is only valid for Python sources")
        source = config["gts" if component == "gcc" else "binutils"]["source"]
    if source["status"] != "locked":
        raise ValidationError("%s source is not locked" % component)
    if source["size"] <= 0:
        raise ValidationError("%s source size must be positive" % component)
    return source


def verify(path, source):
    if path.is_symlink() or not path.is_file():
        raise ValidationError("missing source artifact: %s" % path)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    if size != source["size"]:
        raise ValidationError("source size mismatch: %s" % path)
    if digest.hexdigest() != source["sha256"]:
        raise ValidationError("source SHA256 mismatch: %s" % path)


def fetch(source, output):
    if output.is_symlink():
        raise ValidationError("refusing symlink source output: %s" % output)
    if output.exists():
        verify(output, source)
        print("cached: %s" % output)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink():
        raise ValidationError("refusing symlink source directory: %s" % output.parent)
    partial = None
    request = urllib.request.Request(
        source["url"], headers={"User-Agent": "crossforge-source-fetch/1"}
    )
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    length = int(content_length)
                except ValueError:
                    raise ValidationError("invalid HTTP content length")
                if length != source["size"]:
                    raise ValidationError(
                        "HTTP content length differs from release.json"
                    )
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=output.name + ".",
                suffix=".part",
                dir=str(output.parent),
                delete=False,
            ) as stream:
                partial = Path(stream.name)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
                    size += len(chunk)
                    if size > source["size"]:
                        raise ValidationError("source download exceeds locked size")
        verify(partial, source)
        os.replace(str(partial), str(output))
    except Exception:
        if partial is not None and partial.exists():
            partial.unlink()
        raise
    print("fetched: %s" % output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("gcc", "binutils", "python"))
    parser.add_argument("--version")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=REPOSITORY / "config/release.json"
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    arguments = parser.parse_args()
    try:
        config = load_json(arguments.config)
        schema = load_json(arguments.schema)
        validate_schema_subset(schema)
        validate(config, schema, schema, "$")
        if arguments.component == "python" and not arguments.version:
            raise ValidationError("Python source fetch requires --version")
        fetch(
            source_for(config, arguments.component, arguments.version),
            arguments.output,
        )
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
