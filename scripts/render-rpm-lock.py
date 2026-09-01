#!/usr/bin/env python3
"""Bind a canonical DNF transaction to verified RPM payload bytes."""

import argparse
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
RESOLVER = runpy.run_path(str(REPOSITORY / "scripts/resolve-rpm-transaction.py"))
ValidationError = STRICT["ValidationError"]


class LockError(RuntimeError):
    pass


def canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(arguments, label):
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    process = subprocess.run(
        [str(argument) for argument in arguments],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if process.returncode != 0:
        raise LockError(
            "%s failed: %s" % (label, (process.stdout + process.stderr).strip())
        )
    return process.stdout


def load_validated(path, schema_path):
    document = STRICT["load_json"](path)
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    return document


def repository_path(reference, label):
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise LockError("unsafe %s: %s" % (label, reference))
    path = (REPOSITORY / relative).resolve()
    if REPOSITORY not in path.parents:
        raise LockError("%s escapes the repository" % label)
    return path


def regular_file(path, label):
    if path.is_symlink() or not path.is_file():
        raise LockError("%s is not a regular file: %s" % (label, path))


def verify_plan(transaction):
    reference = transaction["plan"]
    path = repository_path(reference["file"], "plan reference")
    plan = load_validated(path, REPOSITORY / "config/schemas/rpm-plan.schema.json")
    if canonical_sha256(plan) != reference["canonical_sha256"]:
        raise LockError("plan canonical SHA256 differs from the transaction")
    if plan["identity"] != transaction["identity"]:
        raise LockError("plan identity differs from the transaction")
    if plan["base"] != transaction["base"]:
        raise LockError("plan base differs from the transaction")
    plan_policy = dict(plan["solver_policy"])
    transaction_policy = dict(transaction["solver_policy"])
    plan_policy["enabled_modules"] = sorted(plan_policy["enabled_modules"])
    transaction_policy["enabled_modules"] = sorted(
        transaction_policy["enabled_modules"]
    )
    if plan_policy != transaction_policy:
        raise LockError("plan solver policy differs from the transaction")
    return plan


def verify_release_binding(transaction, key):
    release = load_validated(
        REPOSITORY / "config/release.json",
        REPOSITORY / "config/schemas/release.schema.json",
    )
    base = release["base_image"]
    expected_image = "%s:%s" % (base["repository"], base["tag"])
    resolver = transaction["resolver"]
    if resolver["image"] != expected_image or resolver["image_digest"] != base["digest"]:
        raise LockError("resolver image differs from release.json")
    trust = release["trust"]["rocky_rpm_key"]
    regular_file(key, "RPM signing key")
    if sha256_file(key) != trust["sha256"]:
        raise LockError("RPM key SHA256 differs from release.json")
    fingerprint = RESOLVER["key_fingerprint"](key)
    if fingerprint != trust["fingerprint"]:
        raise LockError("RPM key fingerprint differs from release.json")
    return trust


def metadata_path(metadata_root, repository_id, location):
    relative = Path(location)
    if relative.is_absolute() or ".." in relative.parts:
        raise LockError("unsafe repository metadata location: %s" % location)
    path = (metadata_root / repository_id / relative).resolve()
    root = (metadata_root / repository_id).resolve()
    if root != path and root not in path.parents:
        raise LockError("repository metadata escaped its directory")
    return path


def verify_repository_metadata(transaction, metadata_root, key, trust):
    for repository in transaction["repositories"]:
        if repository["gpg_key"] != {
            "sha256": trust["sha256"],
            "fingerprint": trust["fingerprint"],
        }:
            raise LockError("repository trust root differs from release.json")
        repomd = repository["repomd"]
        repomd_path = metadata_path(
            metadata_root, repository["id"], repomd["location"]
        )
        signature_path = metadata_path(
            metadata_root, repository["id"], repomd["signature"]["location"]
        )
        for path, expected, label in (
            (repomd_path, repomd, "repomd"),
            (signature_path, repomd["signature"], "repomd signature"),
        ):
            regular_file(path, label)
            if path.stat().st_size != expected["size"] or sha256_file(path) != expected["sha256"]:
                raise LockError("%s content differs from the transaction" % label)
        with tempfile.TemporaryDirectory(prefix="crossforge-lock-gpg-") as temporary:
            RESOLVER["verify_detached_signature"](
                key,
                trust["fingerprint"],
                signature_path,
                repomd_path,
                Path(temporary),
            )
        revision, records = RESOLVER["parse_repomd"](repomd_path)
        if revision != repomd["revision"] or records != repository["metadata"]:
            raise LockError("repomd records differ from the transaction")
        for record in records:
            path = metadata_path(metadata_root, repository["id"], record["location"])
            regular_file(path, "repository metadata")
            if path.stat().st_size != record["size"]:
                raise LockError("metadata size differs from repomd: %s" % path)
            if sha256_file(path) != record["checksum"]["value"]:
                raise LockError("metadata SHA256 differs from repomd: %s" % path)
            RESOLVER["verify_open_metadata"](path, record)


def rpm_database(key):
    temporary = tempfile.TemporaryDirectory(prefix="crossforge-lock-rpmdb-")
    database = Path(temporary.name) / "rpmdb"
    database.mkdir()
    command(["rpm", "--dbpath", database, "--initdb"], "RPM database init")
    command(["rpm", "--dbpath", database, "--import", key], "RPM key import")
    return temporary, database


def forward_items(transaction):
    result = []
    for item in transaction["items"]:
        if item["action"] in ("install", "upgrade"):
            if any(
                item[field] is None
                for field in (
                    "repo_id",
                    "location",
                    "url",
                    "repository_checksum",
                    "size",
                    "install_size",
                    "source_rpm",
                )
            ):
                raise LockError("forward transaction item lacks repository identity")
            result.append(item)
        elif item["action"] != "remove":
            raise LockError("unsupported transaction action: %s" % item["action"])
    return sorted(result, key=lambda item: item["nevra"])


def rpm_filename(item):
    location = Path(item["location"])
    if location.is_absolute() or ".." in location.parts or not location.name.endswith(".rpm"):
        raise LockError("unsafe RPM location: %s" % item["location"])
    return location.name


def verify_rpm(item, path, database, fingerprint):
    regular_file(path, "RPM payload")
    if path.stat().st_size != item["size"]:
        raise LockError("RPM size differs from transaction: %s" % item["nevra"])
    digest = sha256_file(path)
    checksum = item["repository_checksum"]
    if checksum["algorithm"] != "sha256" or digest != checksum["value"]:
        raise LockError("RPM SHA256 differs from repository metadata: %s" % item["nevra"])
    signature_output = command(
        ["rpmkeys", "--dbpath", database, "--checksig", "--verbose", path],
        "signature verification for %s" % item["nevra"],
    ).lower()
    signature_lines = [
        line for line in signature_output.splitlines() if "signature" in line
    ]
    if (
        not signature_lines
        or (fingerprint not in signature_output and fingerprint[-8:] not in signature_output)
        or any(not line.rstrip().endswith(": ok") for line in signature_lines)
    ):
        raise LockError("RPM is not signed by the locked Rocky key: %s" % item["nevra"])
    query = (
        "%{NAME}\\t%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}\\t"
        "%{ARCH}\\t%{SOURCERPM}\\t%{SIZE}\\n"
    )
    fields = command(
        ["rpm", "--dbpath", database, "-qp", "--qf", query, path],
        "header query for %s" % item["nevra"],
    ).strip().split("\t")
    if len(fields) != 7:
        raise LockError("unexpected RPM header: %s" % item["nevra"])
    expected = [
        item["name"],
        str(item["epoch"]),
        item["version"],
        item["release"],
        item["arch"],
        item["source_rpm"],
        str(item["install_size"]),
    ]
    if fields != expected:
        raise LockError("RPM header differs from transaction: %s" % item["nevra"])
    return {
        "nevra": item["nevra"],
        "received_sha256": digest,
        "header": {
            "name": item["name"],
            "epoch": item["epoch"],
            "version": item["version"],
            "release": item["release"],
            "arch": item["arch"],
            "nevra": item["nevra"],
            "source_rpm": item["source_rpm"],
        },
        "signature": {"status": "verified", "fingerprint": fingerprint},
    }


def render(arguments):
    transaction = load_validated(
        arguments.transaction,
        REPOSITORY / "config/schemas/rpm-transaction.schema.json",
    )
    verify_plan(transaction)
    trust = verify_release_binding(transaction, arguments.key)
    verify_repository_metadata(transaction, arguments.metadata_dir, arguments.key, trust)
    items = forward_items(transaction)
    expected_filenames = [rpm_filename(item) for item in items]
    if len(expected_filenames) != len(set(expected_filenames)):
        raise LockError("transaction contains duplicate RPM filenames")
    if arguments.rpm_dir.is_symlink() or not arguments.rpm_dir.is_dir():
        raise LockError("RPM directory is not a regular directory")
    actual = sorted(
        path.name
        for path in arguments.rpm_dir.iterdir()
        if path.is_file() and not path.is_symlink()
    )
    if actual != sorted(expected_filenames):
        raise LockError("RPM directory differs from the exact DNF transaction")
    temporary, database = rpm_database(arguments.key)
    try:
        packages = [
            verify_rpm(
                item,
                arguments.rpm_dir / rpm_filename(item),
                database,
                trust["fingerprint"],
            )
            for item in items
        ]
    finally:
        temporary.cleanup()
    packages.sort(key=lambda item: item["nevra"])
    lock = {
        "$schema": "https://crossforge.dev/schemas/rpm-lock.schema.json",
        "schema_version": 1,
        "kind": "rpm-lock",
        "transaction": {
            "file": arguments.transaction_reference,
            "canonical_sha256": canonical_sha256(transaction),
        },
        "packages": packages,
    }
    schema = STRICT["load_json"](REPOSITORY / "config/schemas/rpm-lock.schema.json")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](lock, schema, schema, "$")
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transaction", type=Path, required=True)
    parser.add_argument("--transaction-reference", required=True)
    parser.add_argument("--rpm-dir", type=Path, required=True)
    parser.add_argument("--metadata-dir", type=Path, required=True)
    parser.add_argument(
        "--key",
        type=Path,
        default=REPOSITORY / "keys/RPM-GPG-KEY-rockyofficial",
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        reference = Path(arguments.transaction_reference)
        if reference.is_absolute() or ".." in reference.parts:
            raise LockError("unsafe transaction reference")
        lock = render(arguments)
        text = json.dumps(lock, indent=2, ensure_ascii=False) + "\n"
        RESOLVER["atomic_write_text"](arguments.output, text)
    except (OSError, ValidationError, LockError, RESOLVER["ResolutionError"]) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "locked: %s (%d RPM(s); canonical sha256:%s)"
        % (arguments.output, len(lock["packages"]), canonical_sha256(lock))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
