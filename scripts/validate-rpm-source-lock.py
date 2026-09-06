#!/usr/bin/env python3
"""Validate the checked Rocky SRPM source lock against release inputs."""

import argparse
import runpy
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse


REPOSITORY = Path(__file__).resolve().parents[1]
BUILD = runpy.run_path(
    str(REPOSITORY / "scripts/build-rpm-source-requirements.py")
)
RESOLVE = runpy.run_path(
    str(REPOSITORY / "scripts/resolve-rpm-source-lock.py")
)
STRICT = BUILD["STRICT"]
ValidationError = STRICT["ValidationError"]


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def validate_lock(release, requirements, lock):
    pin = release["source_bundle"]["rpm"]
    require(pin["status"] == "locked", "RPM source bundle is not locked")
    require(
        pin["requirements"]
        == {
            "file": "evidence/sources/rpm-source-requirements.json",
            "canonical_sha256": BUILD["canonical_sha256"](requirements),
        },
        "RPM source requirement pin differs",
    )
    require(
        pin["lock"]
        == {
            "file": "locks/rpm-source-el8.json",
            "canonical_sha256": RESOLVE["canonical_sha256"](lock),
        },
        "RPM source lock pin differs",
    )
    expected_locks = BUILD["expected_lock_pins"](release)
    expected_input_sha256 = BUILD["canonical_sha256"](
        BUILD["source_input_identity"](release, expected_locks)
    )
    require(
        requirements["input_sha256"]
        == lock["input_sha256"]
        == expected_input_sha256,
        "RPM source input identity differs",
    )
    require(
        lock["requirements"] == pin["requirements"],
        "RPM source lock requirement identity differs",
    )
    require(
        lock["repositories"] == list(RESOLVE["REPOSITORIES"]),
        "RPM source repository policy differs",
    )
    trust = release["trust"]["rocky_rpm_key"]
    require(
        lock["trust"]
        == {
            "key_file": trust["file"],
            "key_sha256": trust["sha256"],
            "fingerprint": trust["fingerprint"],
        },
        "RPM source lock trust differs",
    )
    required = [record["source_rpm"] for record in requirements["sources"]]
    sources = lock["sources"]
    require(
        [record["source_rpm"] for record in sources] == required,
        "RPM source lock membership or order differs",
    )
    duplicate_names = set()
    urls = set()
    total_size = 0
    requirements_by_name = {
        record["source_rpm"]: record for record in requirements["sources"]
    }
    for record in sources:
        name = record["source_rpm"]
        repository = RESOLVE["repository_for_url"](record["url"])
        require(
            repository == record["repository"]
            and unquote(urlparse(record["url"]).path.rsplit("/", 1)[-1])
            == name,
            "RPM source primary URL differs: %s" % name,
        )
        locations = [
            {"repository": record["repository"], "url": record["url"]}
        ] + record["aliases"]
        require(
            [item["repository"] for item in locations]
            == sorted(
                {item["repository"] for item in locations},
                key=RESOLVE["REPOSITORY_ORDER"].get,
            )
            and all(
                RESOLVE["repository_for_url"](item["url"])
                == item["repository"]
                and unquote(
                    urlparse(item["url"]).path.rsplit("/", 1)[-1]
                )
                == name
                for item in locations
            ),
            "RPM source aliases differ: %s" % name,
        )
        if record["aliases"]:
            duplicate_names.add(name)
        for item in locations:
            require(item["url"] not in urls, "RPM source lock repeats a URL")
            urls.add(item["url"])
        require(
            record["header_verified"] is True
            and record["signature_verified"] is True,
            "RPM source verification status differs: %s" % name,
        )
        content = requirements_by_name[name]["content"]
        if content["status"] == "locked":
            require(
                content["url"] == record["url"]
                and content["sha256"] == record["sha256"]
                and content["size"] == record["size"],
                "existing GTS source lock differs: %s" % name,
            )
        total_size += record["size"]
    require(
        duplicate_names == RESOLVE["EXPECTED_DUPLICATES"],
        "RPM source duplicate alias set differs",
    )
    require(
        len(sources) == pin["source_rpms"]
        and total_size == pin["source_bytes"],
        "RPM source lock count or byte total differs",
    )
    return {"sources": len(sources), "bytes": total_size}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--release", type=Path, default=REPOSITORY / "config/release.json"
    )
    parser.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=REPOSITORY
        / "evidence/sources/rpm-source-requirements.json",
    )
    parser.add_argument(
        "--requirements-schema",
        type=Path,
        default=REPOSITORY
        / "config/schemas/rpm-source-requirements.schema.json",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=REPOSITORY / "locks/rpm-source-el8.json",
    )
    parser.add_argument(
        "--lock-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/rpm-source-lock.schema.json",
    )
    arguments = parser.parse_args(argv)
    try:
        release = BUILD["load_schema_document"](
            arguments.release, arguments.release_schema
        )
        requirements = BUILD["load_schema_document"](
            arguments.requirements, arguments.requirements_schema
        )
        lock = BUILD["load_schema_document"](
            arguments.lock, arguments.lock_schema
        )
        summary = validate_lock(release, requirements, lock)
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "valid RPM source lock: %d SRPMs, %d bytes (canonical sha256:%s)"
        % (
            summary["sources"],
            summary["bytes"],
            RESOLVE["canonical_sha256"](lock),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
