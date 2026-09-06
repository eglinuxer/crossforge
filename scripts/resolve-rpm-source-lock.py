#!/usr/bin/env python3
"""Resolve, download, and authenticate the complete Rocky SRPM source lock."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/rpm-source-lock.schema.json"
REPOSITORIES = (
    {
        "id": "baseos",
        "baseurl": "https://download.rockylinux.org/pub/rocky/8.10/BaseOS/source/tree/",
    },
    {
        "id": "appstream",
        "baseurl": "https://download.rockylinux.org/pub/rocky/8.10/AppStream/source/tree/",
    },
    {
        "id": "powertools",
        "baseurl": "https://download.rockylinux.org/pub/rocky/8.10/PowerTools/source/tree/",
    },
)
REPOSITORY_ORDER = {
    record["id"]: index for index, record in enumerate(REPOSITORIES)
}
EXPECTED_DUPLICATES = {
    "libdrm-2.4.115-2.el8.src.rpm",
    "perl-Digest-1.17-395.el8.src.rpm",
    "perl-Digest-MD5-2.55-396.el8.src.rpm",
    "perl-IO-Socket-IP-0.39-5.el8.src.rpm",
    "perl-Module-Load-0.32-395.el8.src.rpm",
    "perl-URI-1.73-3.el8.src.rpm",
    "perl-libnet-3.11-3.el8.src.rpm",
}
MAX_SOURCE_SIZE = 1024 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def canonical_sha256(document):
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def load_document(path, schema_path):
    document = STRICT["load_json"](path)
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    return document


def repository_for_url(url):
    parsed = urlparse(url)
    require(
        parsed.scheme == "https"
        and parsed.hostname == "download.rockylinux.org"
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment,
        "Rocky SRPM URL is outside the source repository",
    )
    matches = [
        record for record in REPOSITORIES if url.startswith(record["baseurl"])
    ]
    require(len(matches) == 1, "Rocky SRPM URL repository differs")
    return matches[0]["id"]


def parse_locations(text, required):
    result = {}
    for line_number, raw in enumerate(text.splitlines(), 1):
        url = raw.strip()
        require(url == raw and url, "invalid source URL at line %d" % line_number)
        repository = repository_for_url(url)
        filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        require(
            filename.endswith(".src.rpm") and "/" not in filename,
            "invalid Rocky SRPM URL filename",
        )
        if filename not in required:
            continue
        aliases = result.setdefault(filename, {})
        require(repository not in aliases, "Rocky source repository repeats an SRPM")
        aliases[repository] = url
    require(set(result) == set(required), "Rocky SRPM location set is incomplete")
    duplicates = {name for name, aliases in result.items() if len(aliases) > 1}
    require(duplicates == EXPECTED_DUPLICATES, "Rocky SRPM alias set differs")
    require(
        all(len(aliases) in (1, 2) for aliases in result.values()),
        "Rocky SRPM has too many repository aliases",
    )
    return {
        name: [
            {"repository": repository, "url": aliases[repository]}
            for repository in sorted(aliases, key=REPOSITORY_ORDER.get)
        ]
        for name, aliases in result.items()
    }


def download(url, destination, attempts=5):
    request = urllib.request.Request(
        url, headers={"User-Agent": "crossforge-rpm-source-lock/1"}
    )
    last_error = None
    for attempt in range(1, attempts + 1):
        temporary = destination.with_name(
            ".%s.%d.tmp" % (destination.name, attempt)
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                final = response.geturl()
                repository_for_url(final)
                require(
                    unquote(urlparse(final).path.rsplit("/", 1)[-1])
                    == destination.name.split("--", 1)[-1],
                    "Rocky SRPM redirect filename differs",
                )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    require(
                        content_length.isdigit()
                        and 0 < int(content_length) <= MAX_SOURCE_SIZE,
                        "Rocky SRPM content length is invalid",
                    )
                with temporary.open("wb") as stream:
                    received = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        received += len(chunk)
                        require(
                            received <= MAX_SOURCE_SIZE,
                            "Rocky SRPM exceeds the size limit",
                        )
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            require(temporary.stat().st_size > 0, "downloaded Rocky SRPM is empty")
            os.chmod(str(temporary), 0o644)
            os.replace(str(temporary), str(destination))
            return destination
        except (OSError, urllib.error.URLError, ValidationError) as error:
            last_error = error
            try:
                temporary.unlink()
            except OSError:
                pass
            if attempt < attempts:
                time.sleep(2)
    raise ValidationError("failed to download %s: %s" % (url, last_error))


def run(command, label):
    process = subprocess.run(
        [str(value) for value in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "%s failed: %s" % (label, (process.stdout + process.stderr)[-4000:]),
    )
    return process.stdout + process.stderr


def verify_source_rpm(path, expected_name, rpmkeys, rpm, fingerprint):
    signature = run(
        [rpmkeys, "--checksig", "--verbose", path],
        "Rocky SRPM signature verification",
    )
    key_id = fingerprint[-8:]
    require(
        "NOT OK" not in signature
        and "key ID %s: OK" % key_id in signature
        and "Payload SHA256 digest: OK" in signature,
        "Rocky SRPM signature identity differs: %s" % expected_name,
    )
    header = run(
        [
            rpm,
            "-qp",
            "--qf",
            "%{NAME}\\t%{VERSION}\\t%{RELEASE}\\t%{SOURCEPACKAGE}\\n",
            path,
        ],
        "Rocky SRPM header query",
    ).strip().split("\t")
    require(len(header) == 4 and header[3] == "1", "RPM is not a source package")
    require(
        "%s-%s-%s.src.rpm" % tuple(header[:3]) == expected_name,
        "Rocky SRPM header differs from filename: %s" % expected_name,
    )
    return {
        "name": header[0],
        "version": header[1],
        "release": header[2],
        "source_package": True,
    }


def resolve(
    release,
    requirements,
    locations_text,
    downloads,
    key_path,
    rpmkeys,
    rpm,
    jobs,
):
    require(
        requirements["status"] == "content-lock-pending"
        and requirements["summary"]["combined_source_rpms"] == 333,
        "RPM source requirements are not the expected pending set",
    )
    required = {record["source_rpm"] for record in requirements["sources"]}
    requirement_records = {
        record["source_rpm"]: record for record in requirements["sources"]
    }
    locations = parse_locations(locations_text, required)
    downloads.mkdir(parents=True, exist_ok=False)
    tasks = []
    for source_rpm in sorted(required):
        for alias in locations[source_rpm]:
            destination = downloads / (
                "%s--%s" % (alias["repository"], source_rpm)
            )
            tasks.append((alias["url"], destination))
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {
            executor.submit(download, url, destination): (url, destination)
            for url, destination in tasks
        }
        for future in concurrent.futures.as_completed(futures):
            future.result()

    trust = release["trust"]["rocky_rpm_key"]
    require(
        key_path.is_file()
        and not key_path.is_symlink()
        and sha256_file(key_path) == trust["sha256"],
        "Rocky signing key differs from release",
    )
    records = []
    for source_rpm in sorted(required):
        aliases = locations[source_rpm]
        paths = [
            downloads / ("%s--%s" % (alias["repository"], source_rpm))
            for alias in aliases
        ]
        digests = {sha256_file(path) for path in paths}
        sizes = {path.stat().st_size for path in paths}
        require(
            len(digests) == 1 and len(sizes) == 1,
            "Rocky SRPM aliases differ: %s" % source_rpm,
        )
        selected = aliases[0]
        selected_path = paths[0]
        verify_source_rpm(
            selected_path,
            source_rpm,
            rpmkeys,
            rpm,
            trust["fingerprint"],
        )
        source_record = {
            "source_rpm": source_rpm,
            "repository": selected["repository"],
            "url": selected["url"],
            "aliases": aliases[1:],
            "size": next(iter(sizes)),
            "sha256": next(iter(digests)),
            "header_verified": True,
            "signature_verified": True,
        }
        requirement_content = requirement_records[source_rpm]["content"]
        if requirement_content["status"] == "locked":
            require(
                requirement_content
                == {
                    "status": "locked",
                    "url": source_record["url"],
                    "sha256": source_record["sha256"],
                    "size": source_record["size"],
                    "signature": {
                        "key_sha256": trust["sha256"],
                        "fingerprint": trust["fingerprint"],
                    },
                },
                "existing GTS SRPM content lock differs: %s" % source_rpm,
            )
        records.append(source_record)
    return {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-rpm-source-lock",
        "status": "locked",
        "input_sha256": requirements["input_sha256"],
        "requirements": {
            "file": "evidence/sources/rpm-source-requirements.json",
            "canonical_sha256": canonical_sha256(requirements),
        },
        "repositories": list(REPOSITORIES),
        "trust": {
            "key_file": trust["file"],
            "key_sha256": trust["sha256"],
            "fingerprint": trust["fingerprint"],
        },
        "sources": records,
    }


def write_document(path, document, schema_path):
    schema = STRICT["load_json"](schema_path)
    require(schema.get("$id") == SCHEMA_ID, "RPM source lock schema differs")
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    require(not path.exists() and not path.is_symlink(), "RPM source lock output exists")
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
    parser.add_argument("--locations", type=Path, required=True)
    parser.add_argument("--downloads", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
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
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/rpm-source-lock.schema.json",
    )
    parser.add_argument(
        "--key",
        type=Path,
        default=REPOSITORY / "keys/RPM-GPG-KEY-rockyofficial",
    )
    parser.add_argument("--rpmkeys", type=Path, default=Path("/usr/bin/rpmkeys"))
    parser.add_argument("--rpm", type=Path, default=Path("/usr/bin/rpm"))
    arguments = parser.parse_args(argv)
    try:
        require(1 <= arguments.jobs <= 32, "source download jobs are out of range")
        release = load_document(arguments.release, arguments.release_schema)
        requirements = load_document(
            arguments.requirements, arguments.requirements_schema
        )
        document = resolve(
            release,
            requirements,
            arguments.locations.read_text(encoding="utf-8"),
            arguments.downloads,
            arguments.key,
            arguments.rpmkeys,
            arguments.rpm,
            arguments.jobs,
        )
        write_document(arguments.output, document, arguments.schema)
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print("locked %d Rocky SRPMs: %s" % (len(document["sources"]), arguments.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
