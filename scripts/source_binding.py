#!/usr/bin/env python3
"""Create or validate the public source identity embedded in an SDK candidate."""

import argparse
import hashlib
import json
import os
import re
import runpy
import sys
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/source-binding.schema.json"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def canonical_sha256(document):
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_release(path, schema_path):
    release = STRICT["load_json"](path)
    schema = STRICT["load_json"](schema_path)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](release, schema, schema, "$")
    return release


def source_binding(
    release,
    source_commit,
    digest,
    platform_manifest_digest,
    archive_sha256,
    archive_size,
):
    require(COMMIT_RE.match(source_commit), "source binding commit is invalid")
    require(DIGEST_RE.match(digest), "source OCI digest is invalid")
    require(
        DIGEST_RE.match(platform_manifest_digest),
        "source platform manifest digest is invalid",
    )
    require(SHA256_RE.match(archive_sha256), "source archive SHA256 is invalid")
    require(
        type(archive_size) is int and archive_size > 1456725209,
        "source archive size is invalid",
    )
    return {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-source-binding",
        "source_commit": source_commit,
        "release_sha256": canonical_sha256(release),
        "repository": release["product"]["image_repository"],
        "digest": digest,
        "platform": release["platforms"]["image"],
        "platform_manifest_digest": platform_manifest_digest,
        "archive": {
            "file": "crossforge-source-%s.tar.zst" % source_commit,
            "sha256": archive_sha256,
            "size": archive_size,
        },
    }


def validate_binding(document, release, schema, expected_source_commit=None):
    STRICT["validate_schema_subset"](schema)
    require(schema.get("$id") == SCHEMA_ID, "source binding schema differs")
    STRICT["validate"](document, schema, schema, "$")
    require(
        document["release_sha256"] == canonical_sha256(release),
        "source binding release digest differs",
    )
    require(
        document["repository"] == release["product"]["image_repository"]
        and document["platform"] == release["platforms"]["image"],
        "source binding repository or platform differs",
    )
    require(
        document["archive"]["file"]
        == "crossforge-source-%s.tar.zst" % document["source_commit"],
        "source binding archive filename differs",
    )
    if expected_source_commit is not None:
        require(
            document["source_commit"] == expected_source_commit,
            "source binding commit differs",
        )
    return canonical_sha256(document)


def source_offer(document):
    reference = document["repository"] + "@" + document["digest"]
    return (
        "Crossforge corresponding source\n"
        "\n"
        "This SDK candidate is bound to a public, immutable source OCI image.\n"
        "Source OCI: %s\n"
        "Source platform manifest: %s\n"
        "Archive: %s\n"
        "Archive SHA256: %s\n"
        "Archive bytes: %d\n"
        "Source commit: %s\n"
        "\n"
        "Pull by digest, copy the archive from the container root, and verify\n"
        "its SHA256 and size before extraction. MANIFEST.json inside the archive\n"
        "maps every source and verification material to its component and origin.\n"
        % (
            reference,
            document["platform_manifest_digest"],
            document["archive"]["file"],
            document["archive"]["sha256"],
            document["archive"]["size"],
            document["source_commit"],
        )
    )


def write_once(path, payload, binary=False):
    require(not path.exists() and not path.is_symlink(), "source binding output exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        mode = "wb" if binary else "w"
        options = {} if binary else {"encoding": "utf-8"}
        with os.fdopen(descriptor, mode, **options) as stream:
            stream.write(payload)
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


def add_common(parser):
    parser.add_argument(
        "--release", type=Path, default=REPOSITORY / "config/release.json"
    )
    parser.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/source-binding.schema.json",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    create = commands.add_parser("create", allow_abbrev=False)
    add_common(create)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--digest", required=True)
    create.add_argument("--platform-manifest-digest", required=True)
    create.add_argument("--archive-sha256", required=True)
    create.add_argument("--archive-size", required=True, type=int)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--offer", type=Path)
    validate = commands.add_parser("validate", allow_abbrev=False)
    add_common(validate)
    validate.add_argument("binding", type=Path)
    validate.add_argument("--expected-source-commit")
    arguments = parser.parse_args(argv)
    try:
        require(arguments.command in ("create", "validate"), "source binding command is required")
        release = load_release(arguments.release, arguments.release_schema)
        schema = STRICT["load_json"](arguments.schema)
        if arguments.command == "create":
            document = source_binding(
                release,
                arguments.source_commit,
                arguments.digest,
                arguments.platform_manifest_digest,
                arguments.archive_sha256,
                arguments.archive_size,
            )
            digest = validate_binding(document, release, schema, arguments.source_commit)
            write_once(
                arguments.output,
                json.dumps(document, indent=2, sort_keys=True) + "\n",
            )
            if arguments.offer is not None:
                write_once(arguments.offer, source_offer(document))
            action = "wrote"
        else:
            document = STRICT["load_json"](arguments.binding)
            digest = validate_binding(
                document,
                release,
                schema,
                arguments.expected_source_commit,
            )
            action = "valid"
    except (KeyError, OSError, TypeError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print("%s source binding (canonical sha256:%s)" % (action, digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
