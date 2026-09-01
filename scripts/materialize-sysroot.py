#!/usr/bin/env python3
"""Fetch and verify generic RPM locks, and install target sysroots offline."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-rpm-lock.py"))
ValidationError = VALIDATOR["ValidationError"]
load_json = VALIDATOR["load_json"]
canonical_sha256 = VALIDATOR["canonical_sha256"]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reject_symlink_components(path, label):
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            raise ValidationError("%s contains a symlink component: %s" % (label, current))


def safe_rpm_filename(location, nevra):
    if not isinstance(location, str) or "\\" in location:
        raise ValidationError("unsafe RPM location for %s" % nevra)
    relative = PurePosixPath(location)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or str(relative) != location
        or not relative.name.endswith(".rpm")
    ):
        raise ValidationError("unsafe RPM location for %s" % nevra)
    return relative.name


def normalize_lock(lock, transaction):
    role = transaction["identity"]["role"]
    arch = transaction["identity"]["arch"]
    forward = sorted(
        [
            item
            for item in transaction["items"]
            if item["action"] in ("install", "upgrade")
        ],
        key=lambda item: item["nevra"],
    )
    packages = lock["packages"]
    if [item["nevra"] for item in forward] != [item["nevra"] for item in packages]:
        raise ValidationError("RPM lock and transaction forward items differ")
    repositories = {item["id"]: item for item in transaction["repositories"]}
    normalized = []
    for item, package in zip(forward, packages):
        repository = repositories[item["repo_id"]]
        expected_header = {
            "name": item["name"],
            "epoch": item["epoch"],
            "version": item["version"],
            "release": item["release"],
            "arch": item["arch"],
            "nevra": item["nevra"],
            "source_rpm": item["source_rpm"],
        }
        if package["header"] != expected_header:
            raise ValidationError("verified RPM header differs from transaction")
        if package["received_sha256"] != item["repository_checksum"]["value"]:
            raise ValidationError("received RPM differs from repository checksum")
        if package["signature"]["fingerprint"] != repository["gpg_key"]["fingerprint"]:
            raise ValidationError("RPM signature differs from repository trust root")
        normalized.append(
            {
                "item": item,
                "lock": package,
                "filename": safe_rpm_filename(item["location"], item["nevra"]),
            }
        )
    filenames = [item["filename"] for item in normalized]
    if len(filenames) != len(set(filenames)):
        raise ValidationError("RPM transaction contains duplicate filenames")
    key_identities = {
        (
            repository["gpg_key"]["sha256"],
            repository["gpg_key"]["fingerprint"],
        )
        for repository in repositories.values()
    }
    if len(key_identities) != 1:
        raise ValidationError("RPM transaction uses multiple trust roots")
    key_sha256, fingerprint = next(iter(key_identities))
    return {
        "lock": lock,
        "transaction": transaction,
        "role": role,
        "arch": arch,
        "packages": normalized,
        "result_packages": transaction["manifests"]["result"]["packages"],
        "key_sha256": key_sha256,
        "fingerprint": fingerprint,
    }


def load_lock(path):
    lock = load_json(path)
    VALIDATOR["validate_document"](lock)
    if lock["kind"] != "rpm-lock":
        raise ValidationError("a verified generic RPM lock is required")
    transaction = VALIDATOR["validate_release_binding"](
        lock, path, REPOSITORY / "config/release.json"
    )
    return normalize_lock(lock, transaction)


def package_filename(package):
    return package["filename"]


def verify_file(package, path):
    reject_symlink_components(path, "RPM payload")
    if path.is_symlink() or not path.is_file():
        raise ValidationError("missing locked RPM: %s" % path)
    if path.stat().st_size != package["item"]["size"]:
        raise ValidationError("size mismatch for %s" % path.name)
    if sha256_file(path) != package["lock"]["received_sha256"]:
        raise ValidationError("SHA256 mismatch for %s" % path.name)


def bundle_files(directory):
    reject_symlink_components(directory, "RPM bundle")
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError("RPM bundle is not a regular directory: %s" % directory)
    files = {}
    for path in directory.iterdir():
        if path.is_symlink() or not stat.S_ISREG(os.lstat(str(path)).st_mode):
            raise ValidationError("RPM bundle contains a non-regular entry: %s" % path)
        files[path.name] = path
    return files


def verify_bundle(context, directory):
    expected = {package_filename(package): package for package in context["packages"]}
    if len(expected) != len(context["packages"]):
        raise ValidationError("RPM lock contains duplicate filenames")
    actual = bundle_files(directory)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        raise ValidationError("bundle is missing RPMs: %s" % ", ".join(missing))
    if extra:
        raise ValidationError("bundle contains unlocked files: %s" % ", ".join(extra))
    for filename, package in expected.items():
        verify_file(package, actual[filename])


def download_package(package, directory):
    destination = directory / package_filename(package)
    if os.path.lexists(str(destination)):
        if destination.is_symlink():
            raise ValidationError("refusing symlink RPM destination: %s" % destination)
        verify_file(package, destination)
        return "cached %s" % destination.name

    item = package["item"]
    request = urllib.request.Request(
        item["url"], headers={"User-Agent": "crossforge-rpm-fetch/1"}
    )
    partial = None
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
                if length != item["size"]:
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
                    if size > item["size"]:
                        raise ValidationError(
                            "oversized download for %s" % destination.name
                        )
        if size != item["size"] or digest.hexdigest() != package["lock"]["received_sha256"]:
            raise ValidationError("downloaded content mismatch for %s" % destination.name)
        os.replace(str(partial), str(destination))
    except Exception:
        if partial is not None and partial.exists():
            partial.unlink()
        raise
    return "fetched %s" % destination.name


def require_output_directory(directory):
    reject_symlink_components(directory, "RPM output directory")
    if os.path.lexists(str(directory)) and not directory.is_dir():
        raise ValidationError("RPM output is not a directory: %s" % directory)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValidationError("refusing symlink RPM output directory: %s" % directory)


def fetch(context, directory, jobs):
    require_output_directory(directory)
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [
            executor.submit(download_package, package, directory)
            for package in context["packages"]
        ]
        messages = [future.result() for future in futures]
    verify_bundle(context, directory)
    for message in sorted(messages):
        print(message)


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
        detail = (process.stdout + process.stderr).strip()
        raise ValidationError("%s failed: %s" % (label, detail))
    return process.stdout, process.stderr


def verify_key_and_headers(context, directory, key):
    reject_symlink_components(key, "RPM signing key")
    if key.is_symlink() or not key.is_file():
        raise ValidationError("RPM signing key is not a regular file")
    if sha256_file(key) != context["key_sha256"]:
        raise ValidationError("RPM signing key SHA256 differs from lock")
    temporary = Path(tempfile.mkdtemp(prefix="crossforge-sysroot-rpmdb.", dir="/tmp"))
    try:
        command(["rpm", "--dbpath", temporary, "--initdb"], "RPM database init")
        command(["rpm", "--dbpath", temporary, "--import", key], "RPM key import")
        verified = []
        for package in context["packages"]:
            path = directory / package_filename(package)
            stdout, stderr = command(
                ["rpmkeys", "--dbpath", temporary, "--checksig", "--verbose", path],
                "signature verification for %s" % path.name,
            )
            output = (stdout + stderr).lower()
            fingerprint = context["fingerprint"]
            signature_lines = [line for line in output.splitlines() if "signature" in line]
            if (
                not signature_lines
                or (fingerprint not in output and fingerprint[-8:] not in output)
                or any(not line.rstrip().endswith(": ok") for line in signature_lines)
            ):
                raise ValidationError("invalid Rocky signature on %s" % path.name)
            query = (
                "%{NAME}\\t%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}\\t"
                "%{ARCH}\\t%{SOURCERPM}\\t%{SIZE}\\n"
            )
            stdout, _stderr = command(
                ["rpm", "--dbpath", temporary, "-qp", "--qf", query, path],
                "header query for %s" % path.name,
            )
            header = package["lock"]["header"]
            expected = [
                header["name"],
                str(header["epoch"]),
                header["version"],
                header["release"],
                header["arch"],
                header["source_rpm"],
                str(package["item"]["install_size"]),
            ]
            lines = stdout.rstrip("\n").splitlines()
            if len(lines) != 1 or lines[0].split("\t") != expected:
                raise ValidationError("RPM header differs from lock for %s" % path.name)
            verified.append(package)
    finally:
        shutil.rmtree(str(temporary))
    if len([item for item in verified if item["item"]["name"] == "filesystem"]) != 1:
        raise ValidationError("target sysroot must contain exactly one filesystem RPM")
    verified.sort(
        key=lambda item: (
            item["item"]["name"] != "filesystem",
            item["item"]["nevra"],
        )
    )
    return verified


def require_empty_destination(destination):
    reject_symlink_components(destination, "sysroot destination")
    resolved = Path(os.path.realpath(os.path.abspath(str(destination))))
    if str(resolved) == "/":
        raise ValidationError("refusing filesystem root as sysroot destination")
    if os.path.lexists(str(resolved)):
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise ValidationError("sysroot destination is not an empty directory: %s" % resolved)
    else:
        resolved.mkdir(parents=True)
    return resolved


def preseed_usrmerge_symlinks(context, bundle, destination):
    filesystem = [
        package for package in context["packages"]
        if package["item"]["name"] == "filesystem"
    ]
    if len(filesystem) != 1:
        raise ValidationError("target sysroot must contain exactly one filesystem RPM")
    path = bundle / package_filename(filesystem[0])
    stdout, _stderr = command(
        ["rpm", "-qp", "--dump", path], "filesystem RPM manifest query"
    )
    links = {}
    for line in stdout.splitlines():
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


def root_inventory(destination):
    query = "%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\n"
    stdout, _stderr = command(
        ["rpm", "--root", destination, "-qa", "--qf", query],
        "installed sysroot RPM inventory",
    )
    values = sorted(line for line in stdout.splitlines() if line)
    if len(values) != len(set(values)):
        raise ValidationError("installed sysroot RPM inventory contains duplicates")
    return values


def path_is_within(path, directory):
    path = os.path.realpath(str(path))
    directory = os.path.realpath(str(directory))
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


def write_metadata(destination, context):
    metadata = destination / "usr/share/crossforge"
    reject_symlink_components(metadata, "sysroot metadata directory")
    if not path_is_within(metadata, destination):
        raise ValidationError("sysroot metadata path escapes the destination")
    metadata.mkdir(parents=True, exist_ok=True)
    for name, document in (
        ("sysroot-lock.json", context["lock"]),
        ("sysroot-transaction.json", context["transaction"]),
    ):
        (metadata / name).write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def install(context, bundle, key, destination):
    if context["role"] != "target-sysroot":
        raise ValidationError("only target-sysroot RPM locks may be installed here")
    verify_bundle(context, bundle)
    verified = verify_key_and_headers(context, bundle, key)
    destination = require_empty_destination(destination)
    command(["rpm", "--root", destination, "--initdb"], "sysroot RPM database init")
    preseed_usrmerge_symlinks(context, bundle, destination)
    arguments = [
        "rpm",
        "--root",
        destination,
        "-U",
        "--noscripts",
        "--notriggers",
        "--excludedocs",
        "--nocaps",
        "--nocontexts",
    ]
    if context["arch"] != os.uname().machine:
        arguments.append("--ignorearch")
    arguments.extend(bundle / package_filename(package) for package in verified)
    command(arguments, "locked target sysroot RPM transaction")
    actual_inventory = root_inventory(destination)
    if actual_inventory != context["result_packages"]:
        missing = sorted(set(context["result_packages"]) - set(actual_inventory))
        extra = sorted(set(actual_inventory) - set(context["result_packages"]))
        raise ValidationError(
            "installed sysroot inventory differs from transaction "
            "(missing=%s; extra=%s)"
            % (",".join(missing[:10]), ",".join(extra[:10]))
        )
    required_paths = [
        "usr/include/features.h",
        "usr/lib64/crt1.o",
        "usr/lib64/libc.so",
        "usr/lib64/libstdc++.so.6",
        "lib64/libgcc_s.so.1",
    ]
    missing_paths = [
        path for path in required_paths if not (destination / path).exists()
    ]
    if missing_paths:
        raise ValidationError(
            "assembled sysroot is missing: %s" % ", ".join(missing_paths)
        )
    if (destination / "usr/include/c++").exists():
        raise ValidationError("EL8 libstdc++ headers leaked into the sysroot")
    write_metadata(destination, context)
    print(
        "installed %d locked RPMs into %s (lock sha256:%s)"
        % (len(verified), destination, canonical_sha256(context["lock"]))
    )


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
        context = load_lock(arguments.lock)
        if arguments.operation == "fetch":
            if arguments.jobs < 1 or arguments.jobs > 32:
                raise ValidationError("--jobs must be between 1 and 32")
            fetch(context, arguments.output, arguments.jobs)
        elif arguments.operation == "verify":
            verify_bundle(context, arguments.bundle)
            print(
                "verified %d locked RPMs for %s"
                % (len(context["packages"]), context["role"])
            )
        else:
            install(context, arguments.bundle, arguments.key, arguments.destination)
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
