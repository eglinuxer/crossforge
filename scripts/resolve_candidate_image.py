#!/usr/bin/env python3
"""Resolve immutable image digests from Buildx metadata and a raw OCI index."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


class ImageIdentityError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ImageIdentityError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, ValueError) as error:
        raise ImageIdentityError("cannot read JSON %s: %s" % (path, error)) from error


def buildx_digest(metadata, target):
    require(isinstance(metadata, dict), "Buildx metadata root must be an object")
    record = metadata.get(target)
    if record is None and "containerimage.digest" in metadata:
        record = metadata
    require(isinstance(record, dict), "Buildx metadata has no target %s" % target)
    digest = record.get("containerimage.digest")
    require(
        isinstance(digest, str) and DIGEST_RE.match(digest),
        "Buildx target has no valid containerimage.digest",
    )
    return digest


def parse_platform(value):
    parts = value.split("/") if isinstance(value, str) else []
    require(
        len(parts) == 2 and all(parts),
        "platform must have os/architecture form",
    )
    return parts[0], parts[1]


def platform_manifest_digest(raw_index, expected_digest, platform):
    require(DIGEST_RE.match(expected_digest or ""), "expected index digest is invalid")
    observed_digest = "sha256:" + hashlib.sha256(raw_index).hexdigest()
    require(observed_digest == expected_digest, "raw OCI index digest differs")
    try:
        index = json.loads(
            raw_index.decode("utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise ImageIdentityError("raw OCI index is invalid JSON: %s" % error) from error
    require(isinstance(index, dict), "OCI index root must be an object")
    require(index.get("schemaVersion") == 2, "OCI index schemaVersion differs")
    require(
        index.get("mediaType") in INDEX_MEDIA_TYPES,
        "candidate image is not an OCI index or Docker manifest list",
    )
    manifests = index.get("manifests")
    require(isinstance(manifests, list), "OCI index manifests must be an array")
    operating_system, architecture = parse_platform(platform)
    matches = []
    for descriptor in manifests:
        require(isinstance(descriptor, dict), "OCI descriptor must be an object")
        descriptor_platform = descriptor.get("platform")
        if not isinstance(descriptor_platform, dict):
            continue
        if (
            descriptor_platform.get("os") == operating_system
            and descriptor_platform.get("architecture") == architecture
        ):
            digest = descriptor.get("digest")
            require(
                isinstance(digest, str) and DIGEST_RE.match(digest),
                "platform descriptor digest is invalid",
            )
            matches.append(digest)
    require(
        len(matches) == 1,
        "OCI index must contain exactly one %s manifest, found %d"
        % (platform, len(matches)),
    )
    return matches[0]


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = result.add_subparsers(dest="command")
    metadata = subparsers.add_parser("buildx-digest", allow_abbrev=False)
    metadata.add_argument("--metadata", type=Path, required=True)
    metadata.add_argument("--target", default="sdk-candidate")
    platform = subparsers.add_parser("platform-digest", allow_abbrev=False)
    platform.add_argument("--index", type=Path, required=True)
    platform.add_argument("--expected-digest", required=True)
    platform.add_argument("--platform", default="linux/amd64")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        require(
            arguments.command in ("buildx-digest", "platform-digest"),
            "a command is required",
        )
        if arguments.command == "buildx-digest":
            print(buildx_digest(load_json(arguments.metadata), arguments.target))
        else:
            raw_index = arguments.index.read_bytes()
            print(
                platform_manifest_digest(
                    raw_index, arguments.expected_digest, arguments.platform
                )
            )
        return 0
    except (ImageIdentityError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
