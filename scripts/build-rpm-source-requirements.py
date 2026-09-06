#!/usr/bin/env python3
"""Build and validate the exact RPM source bundle requirement set."""

import argparse
import hashlib
import json
import os
import runpy
import sys
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
RPM = runpy.run_path(str(REPOSITORY / "scripts/validate-rpm-lock.py"))
ValidationError = STRICT["ValidationError"]
SCHEMA_ID = (
    "https://crossforge.dev/schemas/rpm-source-requirements.schema.json"
)
EXPECTED_LOCK_PATHS = {
    "locks/host-build-common-el8-x86_64.json",
    "locks/host-gcc-build-el8-x86_64.json",
    "locks/host-gcc-test-el8-x86_64.json",
    "locks/host-python-build-el8-x86_64.json",
    "locks/host-qt-build-el8-x86_64.json",
    "locks/host-runtime-el8-x86_64.json",
    "locks/qt-runtime-el8-aarch64.json",
    "locks/qt-runtime-el8-x86_64.json",
    "locks/qt-target-el8-aarch64.json",
    "locks/qt-target-el8-x86_64.json",
    "locks/sysroot-el8-aarch64.json",
    "locks/sysroot-el8-x86_64.json",
}


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def canonical_sha256(document):
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_schema_document(path, schema_path):
    document = STRICT["load_json"](path)
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    return document


def release_context(release_path, release_schema):
    release = load_schema_document(release_path, release_schema)
    expected = {}
    for pin in release["host_locks"].values():
        expected[pin["lock_file"]] = pin["canonical_sha256"]
    for target in release["targets"]:
        pin = target["sysroot"]
        expected[pin["lock_file"]] = pin["canonical_sha256"]
    for plan_name, release_pin, schema_name in (
        (
            "config/qt-qualification.json",
            release["qt"]["qualification"]["plan"],
            "qt-qualification-plan.schema.json",
        ),
        (
            "config/qt-runtime-qualification.json",
            release["qt"]["runtime_qualification"]["plan"],
            "qt-runtime-qualification-plan.schema.json",
        ),
    ):
        plan = load_schema_document(
            REPOSITORY / plan_name,
            REPOSITORY / "config/schemas" / schema_name,
        )
        require(
            release_pin["file"] == plan_name
            and release_pin["canonical_sha256"]
            == canonical_sha256(plan)
            and plan["status"] == "locked",
            "Qt RPM source plan differs from release: %s" % plan_name,
        )
        for pin in plan["locks"]:
            expected[pin["lock_file"]] = pin["canonical_sha256"]
    require(set(expected) == EXPECTED_LOCK_PATHS, "RPM source lock set differs")
    return release, expected


def validate_base_map(base_map, release, expected_lock_sha256):
    require(
        base_map["base_image"]
        == {
            "index_digest": release["base_image"]["digest"],
            "manifest_digest": release["base_image"]["manifests"][
                "amd64"
            ],
        }
        and base_map["arch"] == "x86_64"
        and base_map["host_runtime_lock_sha256"]
        == expected_lock_sha256,
        "Rocky base source map identity differs",
    )
    nevras = [record["nevra"] for record in base_map["packages"]]
    require(
        nevras == sorted(set(nevras)),
        "Rocky base source map packages are not canonical",
    )


def locked_source_content(release):
    result = {}
    for source in (release["gts"]["source"], release["binutils"]["source"]):
        name = PurePosixPath(urlparse(source["url"]).path).name
        require(name.endswith(".src.rpm"), "locked GTS source is not an SRPM")
        result[name] = {
            "status": "locked",
            "url": source["url"],
            "sha256": source["sha256"],
            "size": source["size"],
            "signature": {
                "key_sha256": release["trust"]["rocky_rpm_key"][
                    "sha256"
                ],
                "fingerprint": release["trust"]["rocky_rpm_key"][
                    "fingerprint"
                ],
            },
        }
    require(len(result) == 2, "locked GTS SRPM source set differs")
    return result


def build_document(release, expected_locks, base_map):
    host_runtime_path = release["host_locks"]["host-runtime"]["lock_file"]
    validate_base_map(
        base_map, release, expected_locks[host_runtime_path]
    )
    sources = {}

    def add(source_rpm, nevra, origin):
        record = sources.setdefault(
            source_rpm,
            {"binary_nevras": set(), "origins": set()},
        )
        record["binary_nevras"].add(nevra)
        record["origins"].add(origin)

    for package in base_map["packages"]:
        add(package["source_rpm"], package["nevra"], "base-image")

    lock_records = []
    locked_sources = set()
    binary_payloads = 0
    for relative_path, expected_sha256 in sorted(expected_locks.items()):
        lock_path = REPOSITORY / relative_path
        lock = RPM["load_json"](lock_path)
        RPM["validate_schema"](lock)
        require(
            canonical_sha256(lock) == expected_sha256,
            "RPM source input lock digest differs: %s" % relative_path,
        )
        RPM["validate_lock_semantics"](lock, validate_plan=False)
        lock_sources = set()
        for package in lock["packages"]:
            source_rpm = package["header"]["source_rpm"]
            lock_sources.add(source_rpm)
            locked_sources.add(source_rpm)
            add(source_rpm, package["nevra"], relative_path)
        binary_payloads += len(lock["packages"])
        lock_records.append(
            {
                "file": relative_path,
                "canonical_sha256": expected_sha256,
                "packages": len(lock["packages"]),
                "source_rpms": len(lock_sources),
            }
        )

    content = locked_source_content(release)
    require(
        set(content).issubset(sources),
        "locked GTS sources are absent from RPM requirements",
    )
    source_records = []
    for source_rpm in sorted(sources):
        record = sources[source_rpm]
        source_records.append(
            {
                "source_rpm": source_rpm,
                "binary_nevras": sorted(record["binary_nevras"]),
                "origins": sorted(record["origins"]),
                "content": content.get(source_rpm, {"status": "missing"}),
            }
        )
    base_sources = {
        record["source_rpm"] for record in base_map["packages"]
    }
    missing = len(source_records) - len(content)
    document = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-rpm-source-requirements",
        "status": "locked" if missing == 0 else "content-lock-pending",
        "release_sha256": canonical_sha256(release),
        "base_source_map": {
            "canonical_sha256": canonical_sha256(base_map),
            "packages": len(base_map["packages"]),
            "source_rpms": len(base_sources),
        },
        "rpm_locks": lock_records,
        "summary": {
            "base_packages": len(base_map["packages"]),
            "base_source_rpms": len(base_sources),
            "locked_binary_payloads": binary_payloads,
            "locked_source_rpms": len(locked_sources),
            "combined_source_rpms": len(source_records),
            "content_locked_source_rpms": len(content),
            "missing_content_locks": missing,
        },
        "sources": source_records,
        "checks": {
            "base_mapping_complete": True,
            "rpm_lock_mapping_complete": True,
            "content_lock_complete": missing == 0,
        },
    }
    return document


def validate_document(document, schema, require_complete=False):
    require(schema.get("$id") == SCHEMA_ID, "RPM source requirements schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    sources = document["sources"]
    require(
        [record["source_rpm"] for record in sources]
        == sorted({record["source_rpm"] for record in sources}),
        "RPM source requirements are not canonical",
    )
    missing = sum(
        record["content"]["status"] == "missing" for record in sources
    )
    require(
        document["summary"]["combined_source_rpms"] == len(sources)
        and document["summary"]["missing_content_locks"] == missing
        and document["summary"]["content_locked_source_rpms"]
        == len(sources) - missing
        and document["checks"]["content_lock_complete"]
        == (missing == 0)
        and document["status"]
        == ("locked" if missing == 0 else "content-lock-pending"),
        "RPM source requirement summary differs",
    )
    if require_complete:
        require(missing == 0, "RPM source content lock is incomplete")
    return document


def validate_expected(document, expected_path, schema):
    expected = STRICT["load_json"](expected_path)
    validate_document(expected, schema, require_complete=False)
    require(expected == document, "reviewed RPM source requirements differ")
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    require(
        expected_path.read_text(encoding="utf-8") == payload,
        "reviewed RPM source requirement bytes are not canonical",
    )


def write_document(path, document):
    require(not path.exists() and not path.is_symlink(), "RPM source requirements output exists")
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
    parser.add_argument("--base-source-map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument(
        "--release", type=Path, default=REPOSITORY / "config/release.json"
    )
    parser.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    parser.add_argument(
        "--base-source-map-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/rpm-source-map.schema.json",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY
        / "config/schemas/rpm-source-requirements.schema.json",
    )
    arguments = parser.parse_args(argv)
    try:
        release, expected_locks = release_context(
            arguments.release, arguments.release_schema
        )
        base_map = load_schema_document(
            arguments.base_source_map, arguments.base_source_map_schema
        )
        document = build_document(release, expected_locks, base_map)
        schema = STRICT["load_json"](arguments.schema)
        validate_document(document, schema, arguments.require_complete)
        if arguments.expected is not None:
            validate_expected(document, arguments.expected, schema)
        write_document(arguments.output, document)
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "mapped %d RPM source requirements (%d content locks missing): %s"
        % (
            document["summary"]["combined_source_rpms"],
            document["summary"]["missing_content_locks"],
            arguments.output,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
