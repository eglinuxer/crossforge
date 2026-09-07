#!/usr/bin/env python3
"""Verify exported CI cache manifests are readable without registry credentials."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

CACHE_PREFIX = "ghcr.io/eglinuxer/crossforge-buildcache:main-"


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key: " + key)
        value[key] = item
    return value


def exported_references(document):
    if not isinstance(document, dict) or set(document) != {"target"}:
        raise ValueError("expected a CI Bake cache override")
    references = []
    for target in document["target"].values():
        for export in target.get("cache-to", []):
            reference = export.get("ref", "")
            if export.get("type") != "registry" or not re.fullmatch(
                re.escape(CACHE_PREFIX) + r"[a-zA-Z0-9_-]+", reference
            ):
                raise ValueError("unexpected CI cache export destination")
            references.append(reference)
    if not references or len(set(references)) != len(references):
        raise ValueError("expected distinct exported cache references")
    return sorted(references)


def anonymous_environment(config_root):
    # Preserve network routing/CA configuration, never registry, GitHub, or
    # Buildx credentials. An isolated Docker config has no credential helpers.
    allowed = {"PATH", "HOME", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["DOCKER_CONFIG"] = str(config_root)
    return environment


def inspect_reference(reference, buildx, environment):
    record = {"reference": reference, "status": "unavailable"}
    for attempt in range(1, 4):
        record["attempts"] = attempt
        record["status"] = "unavailable"
        try:
            result = subprocess.run(
                [str(buildx), "imagetools", "inspect", "--raw", reference],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=30, check=False,
            )
            if result.returncode:
                record["return_code"] = result.returncode
            else:
                manifest = json.loads(result.stdout, object_pairs_hook=unique_object)
                if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
                    raise ValueError("not an OCI/Docker manifest")
                record.update(status="readable", manifest_digest="sha256:" +
                              hashlib.sha256(result.stdout).hexdigest())
        except subprocess.TimeoutExpired:
            record["status"] = "timeout"
        except (ValueError, OSError):
            record["status"] = "inspection-error"
        if record["status"] == "readable":
            record.pop("return_code", None)
            return record
        if attempt < 3:
            time.sleep(2)
    return record


def check_access(references, buildx, output):
    # Invoke the pinned executable directly: changing DOCKER_CONFIG must not
    # accidentally select the runner's unpinned system Buildx plugin.
    with tempfile.TemporaryDirectory(prefix="crossforge-anonymous-cache-") as directory:
        config = Path(directory)
        (config / "config.json").write_text('{"auths": {}}\n', encoding="utf-8")
        environment = anonymous_environment(config)
        with ThreadPoolExecutor(max_workers=4) as executor:
            records = list(executor.map(
                lambda reference: inspect_reference(reference, buildx, environment),
                references,
            ))
    passed = bool(records) and all(record["status"] == "readable" for record in records)
    report = {
        "schema_version": 1, "kind": "crossforge-ci-cache-access",
        "status": "passed" if passed else "failed", "authentication": "anonymous",
        "source_commit": os.environ.get("GITHUB_SHA", ""), "caches": records,
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not passed:
        print("Exported CI caches are not all anonymously readable. Check GHCR "
              "package visibility/access and network availability. The first "
              "crossforge-buildcache package defaults to private; configure it "
              "as public in its GitHub package settings. Do not grant PRs "
              "registry credentials. See cache-access.json for failed references.",
              file=sys.stderr)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--cache-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--buildx", type=Path,
                        default=Path.home() / ".docker/cli-plugins/docker-buildx")
    args = parser.parse_args()
    document = json.loads(args.cache_config.read_text(encoding="utf-8"),
                          object_pairs_hook=unique_object)
    return check_access(exported_references(document), args.buildx, args.output)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        sys.exit(1)
