#!/usr/bin/env python3
"""Render an offline sysroot lock from a completed DNF download transaction."""

import argparse
import hashlib
import json
import os
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-sysroot-lock.py"))
ValidationError = VALIDATOR["ValidationError"]
load_json = VALIDATOR["load_json"]
validate_document = VALIDATOR["validate_document"]
STRICT_JSON = VALIDATOR["STRICT_JSON"]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_metadata(repomd_path):
    namespace = {"repo": "http://linux.duke.edu/metadata/repo"}
    try:
        root = ElementTree.parse(str(repomd_path)).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ValidationError("cannot read repomd.xml: %s" % error)

    result = []
    open_checksums = {}
    for metadata_type in ("primary", "primary_db"):
        matches = [
            item
            for item in root.findall("repo:data", namespace)
            if item.get("type") == metadata_type
        ]
        if len(matches) != 1:
            raise ValidationError(
                "repomd.xml must contain exactly one %s record" % metadata_type
            )
        item = matches[0]
        checksum = item.find("repo:checksum", namespace)
        open_checksum = item.find("repo:open-checksum", namespace)
        location = item.find("repo:location", namespace)
        if (
            checksum is None
            or checksum.get("type") != "sha256"
            or location is None
            or not checksum.text
            or open_checksum is None
            or open_checksum.get("type") != "sha256"
            or not open_checksum.text
            or not location.get("href")
        ):
            raise ValidationError("invalid %s metadata record" % metadata_type)
        result.append(
            {
                "type": metadata_type,
                "location": location.get("href"),
                "checksum": {"algorithm": "sha256", "value": checksum.text},
            }
        )
        open_checksums[metadata_type] = open_checksum.text
    return result, open_checksums


def key_fingerprint(key_path):
    process = subprocess.run(
        ["gpg", "--batch", "--show-keys", "--with-colons", str(key_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if process.returncode != 0:
        raise ValidationError("cannot inspect RPM signing key: %s" % process.stderr)
    fingerprints = [
        line.split(":")[9].lower()
        for line in process.stdout.splitlines()
        if line.startswith("fpr:")
    ]
    if len(fingerprints) != 1 or len(fingerprints[0]) != 40:
        raise ValidationError("RPM signing key must contain exactly one fingerprint")
    return fingerprints[0]


def command(arguments, label):
    process = subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if process.returncode != 0:
        detail = (process.stdout + process.stderr).strip()
        raise ValidationError("%s failed: %s" % (label, detail))
    return process.stdout


def verify_rpm(path, database, fingerprint):
    output = command(
        ["rpmkeys", "--dbpath", str(database), "--checksig", "--verbose", str(path)],
        "signature verification for %s" % path.name,
    ).lower()
    signature_lines = [line for line in output.splitlines() if "signature" in line]
    if (
        not signature_lines
        or (fingerprint not in output and fingerprint[-8:] not in output)
        or any(not line.rstrip().endswith(": ok") for line in signature_lines)
    ):
        raise ValidationError("%s is not signed by the locked Rocky key" % path.name)

    query = (
        "%{NAME}\\t%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}\\t"
        "%{ARCH}\\t%{SOURCERPM}\\n"
    )
    fields = command(
        [
            "rpm",
            "--dbpath",
            str(database),
            "-qp",
            "--qf",
            query,
            str(path),
        ],
        "header query for %s" % path.name,
    ).strip().split("\t")
    if len(fields) != 6:
        raise ValidationError("unexpected RPM header query for %s" % path.name)
    return fields


def package_rows(
    primary_database,
    rpm_directory,
    key_path,
    fingerprint,
    repo_id,
    baseurl,
    root_names,
):
    rpm_paths = sorted(rpm_directory.glob("*.rpm"))
    if not rpm_paths:
        raise ValidationError("RPM directory is empty: %s" % rpm_directory)

    connection = sqlite3.connect(str(primary_database))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM packages").fetchall()
    except sqlite3.DatabaseError as error:
        raise ValidationError("cannot read primary SQLite metadata: %s" % error)
    finally:
        connection.close()

    by_filename = {}
    for row in rows:
        filename = os.path.basename(row["location_href"])
        by_filename.setdefault(filename, []).append(row)

    with tempfile.TemporaryDirectory(prefix="crossforge-rpmdb-") as temporary:
        database = Path(temporary) / "rpmdb"
        database.mkdir()
        command(["rpm", "--dbpath", str(database), "--initdb"], "RPM database init")
        command(
            ["rpm", "--dbpath", str(database), "--import", str(key_path)],
            "Rocky key import",
        )

        packages = []
        for rpm_path in rpm_paths:
            candidates = by_filename.get(rpm_path.name, [])
            if len(candidates) != 1:
                raise ValidationError(
                    "%s has %d primary metadata matches"
                    % (rpm_path.name, len(candidates))
                )
            row = candidates[0]
            if row["checksum_type"] != "sha256":
                raise ValidationError("%s does not use SHA256 metadata" % rpm_path.name)
            digest = sha256_file(rpm_path)
            if digest != row["pkgId"]:
                raise ValidationError(
                    "%s SHA256 differs from primary metadata" % rpm_path.name
                )

            name, epoch, version, release, arch, source_rpm = verify_rpm(
                rpm_path, database, fingerprint
            )
            expected_header = (
                row["name"],
                str(int(row["epoch"] or 0)),
                row["version"],
                row["release"],
                row["arch"],
                row["rpm_sourcerpm"],
            )
            if (name, epoch, version, release, arch, source_rpm) != expected_header:
                raise ValidationError(
                    "%s header differs from primary metadata" % rpm_path.name
                )

            nevra = "%s-%s:%s-%s.%s" % (name, epoch, version, release, arch)
            packages.append(
                {
                    "name": name,
                    "epoch": int(epoch),
                    "version": version,
                    "release": release,
                    "arch": arch,
                    "nevra": nevra,
                    "repo_id": repo_id,
                    "location": row["location_href"],
                    "url": baseurl + row["location_href"],
                    "repository_checksum": {
                        "algorithm": "sha256",
                        "value": row["pkgId"],
                    },
                    "sha256": digest,
                    "size": rpm_path.stat().st_size,
                    "install_size": row["size_installed"],
                    "source_rpm": source_rpm,
                    "signing_key_fingerprint": fingerprint,
                    "reason": "root" if name in root_names else "dependency",
                }
            )
    return sorted(packages, key=lambda package: package["nevra"])


def render(arguments):
    release = load_json(arguments.release_config)
    release_schema = load_json(REPOSITORY / "config/schemas/release.schema.json")
    STRICT_JSON["validate_schema_subset"](release_schema)
    STRICT_JSON["validate"](release, release_schema, release_schema, "$")
    plan = load_json(arguments.plan)
    plan_schema = load_json(REPOSITORY / "config/schemas/sysroot-plan.schema.json")
    validate_document(plan, plan_schema)
    root_names = {root["name"] for root in plan["roots"]}
    target_matches = [
        target
        for target in release["targets"]
        if target["arch"] == plan["identity"]["arch"]
    ]
    if (
        len(target_matches) != 1
        or target_matches[0]["triple"] != plan["identity"]["target_triple"]
    ):
        raise ValidationError("sysroot plan is not a release.json target")

    if arguments.baseurl != plan["repositories"][0]["baseurl"]:
        raise ValidationError("--baseurl differs from the planning manifest")
    if arguments.repo_id != plan["repositories"][0]["id"]:
        raise ValidationError("--repo-id differs from the planning manifest")
    metadata, open_checksums = repository_metadata(arguments.repomd)
    primary_db = next(item for item in metadata if item["type"] == "primary_db")
    if sha256_file(arguments.primary_db_archive) != primary_db["checksum"]["value"]:
        raise ValidationError("primary SQLite archive differs from repomd.xml")
    if sha256_file(arguments.primary_db) != open_checksums["primary_db"]:
        raise ValidationError("expanded primary SQLite differs from repomd.xml")

    fingerprint = key_fingerprint(arguments.key)
    trust = release["trust"]["rocky_rpm_key"]
    if sha256_file(arguments.key) != trust["sha256"] or fingerprint != trust[
        "fingerprint"
    ]:
        raise ValidationError("RPM key differs from release.json trust root")
    base = release["base_image"]
    resolver_image = "%s:%s" % (base["repository"], base["tag"])
    resolver_digest = arguments.resolver_digest or base["digest"]
    if resolver_digest != base["digest"]:
        raise ValidationError("resolver digest differs from release.json")
    packages = package_rows(
        arguments.primary_db,
        arguments.rpm_dir,
        arguments.key,
        fingerprint,
        arguments.repo_id,
        arguments.baseurl,
        root_names,
    )
    document = {
        "$schema": "https://crossforge.dev/schemas/sysroot-lock.schema.json",
        "schema_version": 1,
        "kind": "sysroot-lock",
        "identity": plan["identity"],
        "solver": {
            "implementation": "dnf",
            "image": resolver_image,
            "image_digest": resolver_digest,
            "dnf_version": arguments.dnf_version,
            "libdnf_version": arguments.libdnf_version,
            "rpm_version": arguments.rpm_version,
            "allowed_arches": plan["solver_policy"]["allowed_arches"],
            "install_weak_deps": False,
            "best": True,
            "strict": True,
            "allow_erasing": False,
        },
        "repositories": [
            {
                "id": arguments.repo_id,
                "baseurl": arguments.baseurl,
                "repomd_sha256": sha256_file(arguments.repomd),
                "metadata": metadata,
                "gpg_key": {
                    "sha256": sha256_file(arguments.key),
                    "fingerprint": fingerprint,
                },
            }
        ],
        "roots": plan["roots"],
        "packages": packages,
    }
    lock_schema = load_json(REPOSITORY / "config/schemas/sysroot-lock.schema.json")
    validate_document(document, lock_schema)
    return json.dumps(document, indent=2, sort_keys=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        type=Path,
        default=REPOSITORY / "config/sysroots/el8-x86_64.plan.json",
    )
    parser.add_argument(
        "--release-config",
        type=Path,
        default=REPOSITORY / "config/release.json",
    )
    parser.add_argument("--repomd", type=Path, required=True)
    parser.add_argument("--primary-db-archive", type=Path, required=True)
    parser.add_argument("--primary-db", type=Path, required=True)
    parser.add_argument("--rpm-dir", type=Path, required=True)
    parser.add_argument(
        "--key",
        type=Path,
        default=REPOSITORY / "keys/RPM-GPG-KEY-rockyofficial",
    )
    parser.add_argument("--repo-id", default="baseos")
    parser.add_argument(
        "--baseurl",
        default="https://download.rockylinux.org/pub/rocky/8.10/BaseOS/x86_64/os/",
    )
    parser.add_argument(
        "--resolver-digest",
        default=None,
    )
    parser.add_argument("--dnf-version", required=True)
    parser.add_argument("--libdnf-version", required=True)
    parser.add_argument("--rpm-version", required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        rendered = render(arguments)
        if arguments.output:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(rendered, encoding="utf-8")
            print("wrote: %s" % arguments.output)
        else:
            sys.stdout.write(rendered)
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
