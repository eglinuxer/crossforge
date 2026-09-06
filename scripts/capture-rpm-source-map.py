#!/usr/bin/env python3
"""Bind a fixed Rocky image RPMDB to exact source RPM names."""

import argparse
import hashlib
import json
import os
import runpy
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
RPM = runpy.run_path(str(REPOSITORY / "scripts/validate-rpm-lock.py"))
ValidationError = STRICT["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/rpm-source-map.schema.json"


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def canonical_sha256(document):
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_release(path, schema_path):
    release = STRICT["load_json"](path)
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](release, schema, schema, "$")
    return release


def load_host_runtime_context(release, lock_path):
    pin = release["host_locks"]["host-runtime"]
    expected_path = (REPOSITORY / pin["lock_file"]).resolve()
    require(
        Path(lock_path).resolve() == expected_path,
        "host runtime lock path differs from release",
    )
    lock = RPM["load_json"](lock_path)
    RPM["validate_schema"](lock)
    require(
        pin["status"] == "locked"
        and canonical_sha256(lock) == pin["canonical_sha256"],
        "host runtime lock digest differs from release",
    )
    transaction = RPM["validate_lock_semantics"](
        lock, validate_plan=False
    )
    require(
        transaction["identity"]["role"] == "host-runtime"
        and transaction["identity"]["arch"] == "x86_64"
        and transaction["resolver"]["image_digest"]
        == release["base_image"]["digest"],
        "host runtime transaction base identity differs",
    )
    return lock, transaction


def parse_rpmdb(text):
    records = []
    seen = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        fields = line.split("\t")
        require(
            len(fields) == 2
            and all(fields)
            and fields[1] != "(none)"
            and fields[1].endswith(".src.rpm")
            and "/" not in fields[1]
            and "\\" not in fields[1],
            "invalid RPM source mapping at line %d" % line_number,
        )
        require(fields[0] not in seen, "RPMDB repeats a package NEVRA")
        seen.add(fields[0])
        records.append({"nevra": fields[0], "source_rpm": fields[1]})
    require(records, "RPM source mapping is empty")
    records.sort(key=lambda record: record["nevra"])
    return records


def build_document(release, lock, transaction, rpmdb_text):
    packages = parse_rpmdb(rpmdb_text)
    expected = transaction["manifests"]["base"]["packages"]
    observed = [record["nevra"] for record in packages]
    require(
        observed == expected,
        "fixed Rocky RPMDB differs from host runtime base manifest",
    )
    return {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-rpm-source-map",
        "base_image": {
            "index_digest": release["base_image"]["digest"],
            "manifest_digest": release["base_image"]["manifests"][
                "amd64"
            ],
        },
        "arch": "x86_64",
        "host_runtime_lock_sha256": canonical_sha256(lock),
        "packages": packages,
    }


def write_document(path, document, schema_path):
    schema = STRICT["load_json"](schema_path)
    require(schema.get("$id") == SCHEMA_ID, "RPM source map schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    require(not path.exists() and not path.is_symlink(), "RPM source map output exists")
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
    parser.add_argument("--rpmdb", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--release",
        type=Path,
        default=REPOSITORY / "config/release.json",
    )
    parser.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=REPOSITORY / "locks/host-runtime-el8-x86_64.json",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/rpm-source-map.schema.json",
    )
    arguments = parser.parse_args(argv)
    try:
        release = load_release(arguments.release, arguments.release_schema)
        lock, transaction = load_host_runtime_context(
            release, arguments.lock
        )
        rpmdb_text = arguments.rpmdb.read_text(encoding="utf-8")
        document = build_document(
            release, lock, transaction, rpmdb_text
        )
        write_document(arguments.output, document, arguments.schema)
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "captured %d Rocky RPM source mappings: %s"
        % (len(document["packages"]), arguments.output)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
