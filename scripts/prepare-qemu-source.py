#!/usr/bin/env python3
"""Authenticate and export the official QEMU source archive offline."""

import argparse
import base64
import hashlib
import json
import os
import posixpath
import runpy
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
SIGNATURE = runpy.run_path(str(REPOSITORY / "scripts/source_signature.py"))
ValidationError = STRICT["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/qemu-source-manifest.schema.json"


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def canonical_sha256(document):
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(payload)


def file_identity(path):
    require(path.is_file() and not path.is_symlink(), "missing QEMU source input")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


def safe_repository_file(repository, relative, label):
    require(
        isinstance(relative, str)
        and relative
        and not relative.startswith("/")
        and ".." not in relative.split("/"),
        "%s path is unsafe" % label,
    )
    root = repository.resolve()
    path = repository / relative
    require(path.is_file() and not path.is_symlink(), "%s is missing" % label)
    try:
        path.resolve().relative_to(root)
    except ValueError:
        raise ValidationError("%s escaped repository" % label)
    return path


def verify_signature(gpg, archive, signature, key, policy):
    fingerprint = policy["key"]["fingerprint"]
    require(
        policy["verification"]
        == {
            "status": "cryptographically-valid-expired-key",
            "signature_time": "2026-05-27T22:12:30Z",
            "exception": "upstream-release-key-expired-before-signing",
        },
        "QEMU expired-key exception differs",
    )
    try:
        return SIGNATURE["verify_expired_key_signature"](
            gpg,
            archive,
            signature,
            key,
            {
                "key_sha256": policy["key"]["sha256"],
                "primary_fingerprint": fingerprint,
                "signing_fingerprint": fingerprint,
                "signing_key_expires_at": policy["key"]["expires_at"],
                "signature_time": policy["verification"]["signature_time"],
                "status": policy["verification"]["status"],
                "exception": policy["verification"]["exception"],
            },
        )
    except SIGNATURE["SignatureError"] as error:
        raise ValidationError("QEMU source signature: %s" % error)


def verify_archive(archive_path, policy):
    identity = file_identity(archive_path)
    require(
        identity == {"sha256": policy["sha256"], "size": policy["size"]},
        "QEMU source archive identity differs",
    )
    layout = policy["layout"]
    selected = {}
    seen = set()
    external_symlinks = []
    members = 0
    try:
        with tarfile.open(str(archive_path), "r:xz") as archive:
            for member in archive:
                members += 1
                name = member.name
                require(
                    name
                    and name not in seen
                    and not name.startswith("/")
                    and ".." not in name.split("/")
                    and name.split("/", 1)[0] == layout["top_directory"],
                    "QEMU source archive contains an unsafe path",
                )
                seen.add(name)
                require(
                    member.isfile() or member.isdir() or member.issym(),
                    "QEMU source archive contains a special entry",
                )
                if member.issym():
                    resolved_link = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), member.linkname)
                    )
                    internal = (
                        member.linkname
                        and not member.linkname.startswith("/")
                        and (
                            resolved_link == layout["top_directory"]
                            or resolved_link.startswith(
                                layout["top_directory"] + "/"
                            )
                        )
                    )
                    if not internal:
                        external_symlinks.append(
                            {"path": name, "target": member.linkname}
                        )
                relative = name[len(layout["top_directory"]) + 1 :]
                if relative in ("VERSION", "COPYING"):
                    require(member.isfile(), "QEMU source marker is not a file")
                    stream = archive.extractfile(member)
                    require(stream is not None, "QEMU source marker is unreadable")
                    selected[relative] = sha256_bytes(stream.read())
    except (KeyError, OSError, tarfile.TarError) as error:
        raise ValidationError("cannot inspect QEMU source archive: %s" % error)
    require(members == layout["member_count"], "QEMU source member count differs")
    require(
        external_symlinks == layout["reviewed_external_symlinks"],
        "QEMU source external symlink set differs",
    )
    require(
        selected
        == {
            "VERSION": layout["version_sha256"],
            "COPYING": layout["license_sha256"],
        },
        "QEMU source marker or license differs",
    )
    return identity


def prepare(release_path, release_schema, archive, repository, output, gpg):
    release = STRICT["load_json"](release_path)
    schema = STRICT["load_json"](release_schema)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](release, schema, schema, "$")
    source = release["qemu"]["executor"]["source"]
    policy = source["archive"]
    signature_policy = policy["signature"]
    signature_evidence = safe_repository_file(
        repository, signature_policy["evidence"], "QEMU signature evidence"
    )
    try:
        signature = base64.b64decode(
            b"".join(signature_evidence.read_bytes().split()), validate=True
        )
    except (ValueError, TypeError) as error:
        raise ValidationError("invalid QEMU signature evidence: %s" % error)
    require(
        len(signature) == signature_policy["size"]
        and sha256_bytes(signature) == signature_policy["sha256"],
        "QEMU signature evidence identity differs",
    )
    key = safe_repository_file(
        repository, signature_policy["key"]["file"], "QEMU release key"
    )
    archive_identity = verify_archive(archive, policy)
    require(not output.exists() and not output.is_symlink(), "QEMU output exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".%s." % output.name, dir=str(output.parent))
    )
    try:
        materials = temporary / "materials"
        materials.mkdir()
        signature_path = materials / "qemu-10.2.3.tar.xz.sig"
        signature_path.write_bytes(signature)
        os.chmod(str(signature_path), 0o644)
        verify_signature(gpg, archive, signature_path, key, signature_policy)
        shutil.copyfile(str(archive), str(materials / "qemu-10.2.3.tar.xz"))
        shutil.copyfile(str(key), str(materials / "QEMU-RELEASE-KEY.asc"))
        for path in (
            materials / "qemu-10.2.3.tar.xz",
            materials / "QEMU-RELEASE-KEY.asc",
        ):
            os.chmod(str(path), 0o644)
        manifest = {
            "$schema": SCHEMA_ID,
            "schema_version": 1,
            "kind": "crossforge-qemu-source",
            "release_sha256": canonical_sha256(release),
            "source": {
                "version": release["qemu"]["version"],
                "tag": source["tag"],
                "commit": source["commit"],
                "archive": dict({"file": "qemu-10.2.3.tar.xz"}, **archive_identity),
                "signature": {
                    "file": "qemu-10.2.3.tar.xz.sig",
                    "sha256": signature_policy["sha256"],
                    "size": signature_policy["size"],
                    "verification": signature_policy["verification"]["status"],
                    "signature_time": signature_policy["verification"]["signature_time"],
                    "exception": signature_policy["verification"]["exception"],
                },
                "key": {
                    "file": "QEMU-RELEASE-KEY.asc",
                    "sha256": signature_policy["key"]["sha256"],
                    "fingerprint": signature_policy["key"]["fingerprint"],
                    "expires_at": signature_policy["key"]["expires_at"],
                },
                "layout": policy["layout"],
            },
        }
        manifest_path = temporary / "source-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(str(manifest_path), 0o644)
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(str(temporary), ignore_errors=True)
        raise
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release", type=Path, default=REPOSITORY / "config/release.json")
    parser.add_argument(
        "--release-schema",
        type=Path,
        default=REPOSITORY / "config/schemas/release.schema.json",
    )
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--gpg", type=Path, default=Path("/usr/bin/gpg"))
    arguments = parser.parse_args(argv)
    try:
        manifest = prepare(
            arguments.release,
            arguments.release_schema,
            arguments.archive,
            arguments.repository,
            arguments.output,
            arguments.gpg,
        )
        manifest_schema = STRICT["load_json"](
            arguments.repository / "config/schemas/qemu-source-manifest.schema.json"
        )
        STRICT["validate_schema_subset"](manifest_schema)
        STRICT["validate"](manifest, manifest_schema, manifest_schema, "$")
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print("prepared authenticated QEMU source: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
