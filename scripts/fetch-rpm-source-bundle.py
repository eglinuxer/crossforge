#!/usr/bin/env python3
"""Fetch or verify the complete content-locked Rocky SRPM bundle."""

import argparse
import concurrent.futures
import hashlib
import os
import runpy
import shutil
import stat
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
VALIDATOR = runpy.run_path(
    str(REPOSITORY / "scripts/validate-rpm-source-lock.py")
)
BUILD = VALIDATOR["BUILD"]
RESOLVE = VALIDATOR["RESOLVE"]
ValidationError = VALIDATOR["ValidationError"]


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def file_identity(path):
    information = os.lstat(str(path))
    require(stat.S_ISREG(information.st_mode), "SRPM bundle entry is not regular")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
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


def load_context(arguments):
    release = BUILD["load_schema_document"](
        arguments.release, arguments.release_schema
    )
    requirements = BUILD["load_schema_document"](
        arguments.requirements, arguments.requirements_schema
    )
    lock = BUILD["load_schema_document"](
        arguments.lock, arguments.lock_schema
    )
    VALIDATOR["validate_lock"](release, requirements, lock)
    return release, lock


def fetch_bundle(lock, output, jobs):
    require(1 <= jobs <= 32, "SRPM download jobs are out of range")
    require(not output.exists() and not output.is_symlink(), "SRPM output exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".%s." % output.name, dir=str(output.parent))
    )
    try:
        tasks = []
        for record in lock["sources"]:
            destination = temporary / record["source_rpm"]
            tasks.append((record, destination))
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = {
                executor.submit(RESOLVE["download"], record["url"], destination): (
                    record,
                    destination,
                )
                for record, destination in tasks
            }
            for future in concurrent.futures.as_completed(futures):
                record, destination = futures[future]
                future.result()
                require(
                    file_identity(destination)
                    == {"sha256": record["sha256"], "size": record["size"]},
                    "downloaded SRPM identity differs: %s"
                    % record["source_rpm"],
                )
        require(
            sorted(path.name for path in temporary.iterdir())
            == sorted(record["source_rpm"] for record in lock["sources"]),
            "downloaded SRPM bundle membership differs",
        )
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(str(temporary), ignore_errors=True)
        raise
    return {"sources": len(lock["sources"]), "bytes": sum(r["size"] for r in lock["sources"])}


def verify_bundle(release, lock, bundle, rpmkeys, rpm):
    require(bundle.is_dir() and not bundle.is_symlink(), "SRPM bundle is missing")
    expected_names = [record["source_rpm"] for record in lock["sources"]]
    observed_names = sorted(path.name for path in bundle.iterdir())
    require(observed_names == sorted(expected_names), "SRPM bundle membership differs")
    trust = release["trust"]["rocky_rpm_key"]
    total = 0
    for record in lock["sources"]:
        path = bundle / record["source_rpm"]
        identity = file_identity(path)
        require(
            identity
            == {"sha256": record["sha256"], "size": record["size"]},
            "SRPM bundle identity differs: %s" % record["source_rpm"],
        )
        RESOLVE["verify_source_rpm"](
            path,
            record["source_rpm"],
            rpmkeys,
            rpm,
            trust["fingerprint"],
        )
        total += identity["size"]
    require(total == sum(r["size"] for r in lock["sources"]), "SRPM byte total differs")
    return {"sources": len(lock["sources"]), "bytes": total}


def add_common_arguments(parser):
    parser.add_argument("--bundle", type=Path, required=True)
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
        default=REPOSITORY / "evidence/sources/rpm-source-requirements.json",
    )
    parser.add_argument(
        "--requirements-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/rpm-source-requirements.schema.json",
    )
    parser.add_argument(
        "--lock", type=Path, default=REPOSITORY / "locks/rpm-source-el8.json"
    )
    parser.add_argument(
        "--lock-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/rpm-source-lock.schema.json",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    fetch = commands.add_parser("fetch", allow_abbrev=False)
    add_common_arguments(fetch)
    fetch.add_argument("--jobs", type=int, default=8)
    verify = commands.add_parser("verify", allow_abbrev=False)
    add_common_arguments(verify)
    verify.add_argument("--rpmkeys", type=Path, default=Path("/usr/bin/rpmkeys"))
    verify.add_argument("--rpm", type=Path, default=Path("/usr/bin/rpm"))
    arguments = parser.parse_args(argv)
    try:
        require(arguments.command in ("fetch", "verify"), "source bundle command is required")
        release, lock = load_context(arguments)
        if arguments.command == "fetch":
            summary = fetch_bundle(lock, arguments.bundle, arguments.jobs)
            action = "fetched"
        else:
            summary = verify_bundle(
                release, lock, arguments.bundle, arguments.rpmkeys, arguments.rpm
            )
            action = "verified"
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "%s RPM source bundle: %d SRPMs, %d bytes"
        % (action, summary["sources"], summary["bytes"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
