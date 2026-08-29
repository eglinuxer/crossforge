#!/usr/bin/env python3
"""Fetch, verify, and install a locked RPM sysroot without repository access."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-sysroot-lock.py"))
ValidationError = VALIDATOR["ValidationError"]
load_json = VALIDATOR["load_json"]
schema_for = VALIDATOR["schema_for"]
validate_document = VALIDATOR["validate_document"]
validate_release_binding = VALIDATOR["validate_release_binding"]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_lock(path):
    document = load_json(path)
    validate_document(document, schema_for(document))
    if document["kind"] != "sysroot-lock":
        raise ValidationError("a resolved sysroot lock is required")
    validate_release_binding(
        document, path, REPOSITORY / "config/release.json"
    )
    return document


def package_filename(package):
    return Path(package["location"]).name


def verify_file(package, path):
    if path.is_symlink() or not path.is_file():
        raise ValidationError("missing locked RPM: %s" % path)
    if path.stat().st_size != package["size"]:
        raise ValidationError("size mismatch for %s" % path.name)
    if sha256_file(path) != package["sha256"]:
        raise ValidationError("SHA256 mismatch for %s" % path.name)


def verify_bundle(lock, directory):
    if directory.is_symlink():
        raise ValidationError("refusing symlink RPM bundle: %s" % directory)
    expected = {package_filename(package): package for package in lock["packages"]}
    if len(expected) != len(lock["packages"]):
        raise ValidationError("lock contains duplicate RPM filenames")
    actual = {path.name for path in directory.glob("*.rpm") if path.is_file()}
    missing = sorted(set(expected) - actual)
    extra = sorted(actual - set(expected))
    if missing:
        raise ValidationError("bundle is missing RPMs: %s" % ", ".join(missing))
    if extra:
        raise ValidationError("bundle contains unlocked RPMs: %s" % ", ".join(extra))
    for filename, package in expected.items():
        verify_file(package, directory / filename)


def download_package(package, directory):
    destination = directory / package_filename(package)
    if destination.is_symlink():
        raise ValidationError("refusing symlink RPM destination: %s" % destination)
    if destination.exists():
        verify_file(package, destination)
        return "cached %s" % destination.name

    partial = None
    request = urllib.request.Request(
        package["url"], headers={"User-Agent": "crossforge-sysroot-fetch/1"}
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    length = int(content_length)
                except ValueError:
                    raise ValidationError("invalid HTTP content length")
                if length != package["size"]:
                    raise ValidationError(
                        "HTTP content length mismatch for %s" % destination.name
                    )
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=destination.name + ".",
                suffix=".part",
                dir=str(directory),
                delete=False,
            ) as output:
                partial = Path(output.name)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if size > package["size"]:
                        raise ValidationError(
                            "oversized download for %s" % destination.name
                        )
        if size != package["size"] or digest.hexdigest() != package["sha256"]:
            raise ValidationError("downloaded content mismatch for %s" % destination.name)
        os.replace(str(partial), str(destination))
    except Exception:
        if partial is not None and partial.exists():
            partial.unlink()
        raise
    return "fetched %s" % destination.name


def fetch(lock, directory, jobs):
    if directory.is_symlink():
        raise ValidationError("refusing symlink RPM output directory: %s" % directory)
    directory.mkdir(parents=True, exist_ok=True)
    filenames = [package_filename(package) for package in lock["packages"]]
    if len(filenames) != len(set(filenames)):
        raise ValidationError("lock contains duplicate RPM filenames")
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [
            executor.submit(download_package, package, directory)
            for package in lock["packages"]
        ]
        messages = [future.result() for future in futures]
    verify_bundle(lock, directory)
    for message in sorted(messages):
        print(message)


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


def verify_rpm_headers(lock, directory, rpm_database):
    fingerprint = lock["repositories"][0]["gpg_key"]["fingerprint"]
    packages = []
    for package in lock["packages"]:
        path = directory / package_filename(package)
        output = command(
            [
                "rpmkeys",
                "--dbpath",
                str(rpm_database),
                "--checksig",
                "--verbose",
                str(path),
            ],
            "signature verification for %s" % path.name,
        ).lower()
        signature_lines = [line for line in output.splitlines() if "signature" in line]
        if (
            not signature_lines
            or (fingerprint not in output and fingerprint[-8:] not in output)
            or any(not line.rstrip().endswith(": ok") for line in signature_lines)
        ):
            raise ValidationError("invalid Rocky signature on %s" % path.name)

        query = (
            "%{NAME}\\t%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}\\t"
            "%{ARCH}\\t%{SOURCERPM}\\n"
        )
        fields = command(
            [
                "rpm",
                "--dbpath",
                str(rpm_database),
                "-qp",
                "--qf",
                query,
                str(path),
            ],
            "header query for %s" % path.name,
        ).strip().split("\t")
        expected = [
            package["name"],
            str(package["epoch"]),
            package["version"],
            package["release"],
            package["arch"],
            package["source_rpm"],
        ]
        if fields != expected:
            raise ValidationError("RPM header differs from lock for %s" % path.name)
        packages.append((package["name"], str(path)))
    if not any(name == "filesystem" for name, _path in packages):
        raise ValidationError("locked transaction does not contain filesystem")
    packages.sort(key=lambda item: (item[0] != "filesystem", item[0], item[1]))
    return [path for _name, path in packages]


def require_empty_destination(destination):
    if destination.is_symlink():
        raise ValidationError("refusing symlink sysroot destination: %s" % destination)
    resolved = destination.resolve()
    if not resolved.is_absolute() or str(resolved) == "/":
        raise ValidationError("refusing unsafe sysroot destination: %s" % destination)
    if resolved.exists() and any(resolved.iterdir()):
        raise ValidationError("sysroot destination is not empty: %s" % resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def preseed_usrmerge_symlinks(lock, bundle, destination):
    filesystem = [
        package for package in lock["packages"] if package["name"] == "filesystem"
    ]
    if len(filesystem) != 1:
        raise ValidationError("lock must contain exactly one filesystem RPM")
    path = bundle / package_filename(filesystem[0])
    output = command(
        ["rpm", "-qp", "--dump", str(path)],
        "filesystem RPM manifest query",
    )
    links = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 11 and fields[4].startswith("012"):
            links[fields[0]] = fields[10]
    required = {
        "/bin": "usr/bin",
        "/lib": "usr/lib",
        "/lib64": "usr/lib64",
        "/sbin": "usr/sbin",
    }
    if any(links.get(name) != target for name, target in required.items()):
        raise ValidationError("filesystem RPM has unexpected usrmerge symlinks")
    for target in required.values():
        (destination / target).mkdir(parents=True, exist_ok=True)
    for name, target in required.items():
        os.symlink(target, str(destination / name.lstrip("/")))


def install(lock, bundle, key, destination):
    verify_bundle(lock, bundle)
    repository = lock["repositories"][0]
    if sha256_file(key) != repository["gpg_key"]["sha256"]:
        raise ValidationError("RPM signing key SHA256 differs from lock")

    destination = require_empty_destination(destination)
    rpm_database = destination / "var/lib/rpm"
    rpm_database.mkdir(parents=True, exist_ok=True)
    command(["rpm", "--dbpath", str(rpm_database), "--initdb"], "RPM database init")
    command(
        ["rpm", "--dbpath", str(rpm_database), "--import", str(key)],
        "Rocky signing key import",
    )
    packages = verify_rpm_headers(lock, bundle, rpm_database)
    preseed_usrmerge_symlinks(lock, bundle, destination)

    arguments = [
        "rpm",
        "--root",
        str(destination),
        "-U",
        "--noscripts",
        "--notriggers",
        "--excludedocs",
        "--nocaps",
        "--nocontexts",
    ]
    if lock["identity"]["arch"] != os.uname().machine:
        arguments.append("--ignorearch")
    command(arguments + packages, "locked RPM transaction")

    required_paths = [
        "usr/include/features.h",
        "usr/lib64/crt1.o",
        "usr/lib64/libc.so",
        "usr/lib64/libstdc++.so.6",
        "lib64/libgcc_s.so.1",
    ]
    missing = [path for path in required_paths if not (destination / path).exists()]
    if missing:
        raise ValidationError("assembled sysroot is missing: %s" % ", ".join(missing))
    if (destination / "usr/include/c++").exists():
        raise ValidationError("EL8 libstdc++ headers leaked into the sysroot")

    metadata = destination / "usr/share/crossforge"
    metadata.mkdir(parents=True, exist_ok=True)
    (metadata / "sysroot-lock.json").write_text(
        json.dumps(lock, indent=2) + "\n", encoding="utf-8"
    )
    print("installed %d locked RPMs into %s" % (len(packages), destination))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    subparsers = parser.add_subparsers(dest="operation")

    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("--output", type=Path, required=True)
    fetch_parser.add_argument("--jobs", type=int, default=8)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--bundle", type=Path, required=True)

    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--bundle", type=Path, required=True)
    install_parser.add_argument("--key", type=Path, required=True)
    install_parser.add_argument("--destination", type=Path, required=True)

    arguments = parser.parse_args()
    try:
        if arguments.operation is None:
            parser.error("an operation is required")
        lock = load_lock(arguments.lock)
        if arguments.operation == "fetch":
            if arguments.jobs < 1 or arguments.jobs > 32:
                raise ValidationError("--jobs must be between 1 and 32")
            fetch(lock, arguments.output, arguments.jobs)
        elif arguments.operation == "verify":
            verify_bundle(lock, arguments.bundle)
            print("verified %d locked RPMs" % len(lock["packages"]))
        else:
            install(lock, arguments.bundle, arguments.key, arguments.destination)
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
