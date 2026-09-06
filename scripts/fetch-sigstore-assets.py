#!/usr/bin/env python3
"""Fetch the exact offline inputs for Sigstore source verification."""

import argparse
import hashlib
import json
import os
import runpy
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]
ALLOWED_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "www.python.org",
}


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def load_release(release_path, schema_path):
    release = STRICT["load_json"](release_path)
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](release, schema, schema, "$")
    return release


def asset_record(name, source, mode=0o644):
    url = source["url"]
    parsed = urlparse(url)
    require(
        parsed.scheme == "https"
        and parsed.hostname in ALLOWED_HOSTS
        and not parsed.username
        and not parsed.password
        and not parsed.fragment,
        "Sigstore asset URL is outside the allowlist: %s" % url,
    )
    require(
        isinstance(name, str)
        and name
        and "/" not in name
        and "\\" not in name
        and name not in (".", ".."),
        "Sigstore asset filename is unsafe",
    )
    return {
        "file": name,
        "url": url,
        "sha256": source["sha256"],
        "size": source["size"],
        "mode": mode,
    }


def asset_plan(release):
    verifier = release["sigstore"]["verifier"]
    records = [
        asset_record("cosign", verifier["binary"], 0o755),
        asset_record("cosign-kms.sigstore.json", verifier["kms_bundle"]),
    ]
    for entry in release["python"]["versions"]:
        source = entry["source"]
        records.append(
            asset_record("Python-%s.tar.xz" % entry["version"], source)
        )
    records.extend(
        [
            asset_record("nfpm-checksums.txt", release["nfpm"]["checksums"]),
            asset_record(
                "nfpm-checksums.sigstore.json",
                release["nfpm"]["sigstore"],
            ),
        ]
    )
    names = [record["file"] for record in records]
    urls = [record["url"] for record in records]
    require(
        len(names) == len(set(names))
        and len(urls) == len(set(urls)),
        "Sigstore asset plan contains duplicates",
    )
    records.sort(key=lambda record: record["file"])
    return records


def validate_download(path, record):
    require(
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size == record["size"]
        and sha256_file(path) == record["sha256"],
        "Sigstore asset content differs: %s" % record["file"],
    )
    require(
        stat.S_IMODE(path.stat().st_mode) == record["mode"],
        "Sigstore asset mode differs: %s" % record["file"],
    )


def download(record, destination, attempts=5):
    request = urllib.request.Request(
        record["url"],
        headers={"User-Agent": "crossforge-sigstore-fetch/1"},
    )
    last_error = None
    for attempt in range(1, attempts + 1):
        temporary = destination.with_name(
            ".%s.%d.tmp" % (destination.name, attempt)
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                final_url = urlparse(response.geturl())
                require(
                    final_url.scheme == "https"
                    and final_url.hostname in ALLOWED_HOSTS
                    and not final_url.username
                    and not final_url.password,
                    "Sigstore asset redirected outside the allowlist",
                )
                with temporary.open("wb") as stream:
                    shutil.copyfileobj(response, stream, 1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.chmod(str(temporary), record["mode"])
            validate_download(temporary, record)
            os.replace(str(temporary), str(destination))
            return
        except (OSError, urllib.error.URLError, ValidationError) as error:
            last_error = error
            try:
                temporary.unlink()
            except OSError:
                pass
            if attempt < attempts:
                time.sleep(2)
    raise ValidationError(
        "failed to fetch Sigstore asset %s: %s"
        % (record["file"], last_error)
    )


def fetch(release, output):
    output = Path(output)
    require(
        not output.exists() and not output.is_symlink(),
        "Sigstore download output already exists",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=".%s." % output.name, dir=str(output.parent)
        )
    )
    try:
        records = asset_plan(release)
        for record in records:
            download(record, temporary / record["file"])
        manifest = {
            "schema_version": 1,
            "kind": "crossforge-sigstore-downloads",
            "files": records,
        }
        manifest_path = temporary / "downloads.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(str(manifest_path), 0o644)
        os.replace(str(temporary), str(output))
    except BaseException:
        shutil.rmtree(str(temporary), ignore_errors=True)
        raise
    print("fetched %d Sigstore assets: %s" % (len(records), output))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--release",
        type=Path,
        default=REPOSITORY / "config/release.json",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        release = load_release(arguments.release, arguments.schema)
        fetch(release, arguments.output)
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
