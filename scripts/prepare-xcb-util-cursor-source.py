#!/usr/bin/env python3
"""Authenticate and inspect the locked xcb-util-cursor source archive."""

import argparse
import base64
import binascii
import hashlib
import json
import os
import runpy
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
COMPONENT = runpy.run_path(str(Path(__file__).with_name("release_component.py")))
STRICT = runpy.run_path(str(Path(__file__).with_name("validate-release.py")))
ComponentError = COMPONENT["ComponentError"]
SchemaError = STRICT["ValidationError"]
COMPONENT_NAME = "sources/xcb-util-cursor"
SCHEMA_ID = (
    "https://crossforge.dev/schemas/xcb-util-cursor-source-manifest.schema.json"
)
VERSION = "0.1.6"
ARCHIVE_NAME = "xcb-util-cursor-0.1.6.tar.xz"
SIGNATURE_NAME = ARCHIVE_NAME + ".sig"
TOP_DIRECTORY = "xcb-util-cursor-0.1.6"
EXPECTED_FILES = (
    "COPYING",
    "configure",
    "cursor/Makefile.in",
    "cursor/xcb-cursor.pc.in",
)
MAX_ARCHIVE_SIZE = 4 * 1024 * 1024
MAX_UNPACKED_SIZE = 32 * 1024 * 1024


class XcbCursorSourceError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise XcbCursorSourceError(message)


def sha256_file(path, maximum=None):
    path = Path(path)
    try:
        information = path.lstat()
    except OSError as error:
        raise XcbCursorSourceError("cannot inspect %s: %s" % (path, error)) from error
    require(
        stat.S_ISREG(information.st_mode) and not path.is_symlink(),
        "input is not a regular file: %s" % path,
    )
    if maximum is not None:
        require(
            0 < information.st_size <= maximum,
            "input file size is outside the safety limit: %s" % path,
        )
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return information.st_size, digest.hexdigest()


def material(document, component_sha256, pointer, expected_type):
    try:
        return COMPONENT["material_value"](
            document,
            COMPONENT_NAME,
            "build",
            component_sha256,
            pointer,
            expected_type,
        )
    except ComponentError as error:
        raise XcbCursorSourceError(str(error)) from error


def load_policy(component_path, component_sha256):
    try:
        document = COMPONENT["load_component"](
            component_path, COMPONENT_NAME, "build", component_sha256
        )
    except ComponentError as error:
        raise XcbCursorSourceError(str(error)) from error
    prefix = "/qt/dependencies/xcb_util_cursor"
    version = material(document, component_sha256, prefix + "/version", "string")
    source = {}
    for field, expected_type in (
        ("status", "string"),
        ("url", "string"),
        ("sha256", "string"),
        ("size", "integer"),
    ):
        source[field] = material(
            document,
            component_sha256,
            prefix + "/source/" + field,
            expected_type,
        )
    signature = {}
    for field, expected_type in (
        ("url", "string"),
        ("sha256", "string"),
        ("size", "integer"),
        ("evidence", "string"),
    ):
        signature[field] = material(
            document,
            component_sha256,
            prefix + "/source/signature/" + field,
            expected_type,
        )
    key = {}
    for field in ("file", "retrieval_url", "sha256", "fingerprint"):
        key[field] = material(
            document,
            component_sha256,
            prefix + "/source/signature/key/" + field,
            "string",
        )
    signature["key"] = key
    license_identity = {}
    for field in ("expression", "file", "sha256"):
        license_identity[field] = material(
            document,
            component_sha256,
            prefix + "/license/" + field,
            "string",
        )
    layout = {
        "top_directory": material(
            document,
            component_sha256,
            prefix + "/layout/top_directory",
            "string",
        ),
        "member_count": material(
            document,
            component_sha256,
            prefix + "/layout/member_count",
            "integer",
        ),
        "files": [],
    }
    for index in range(len(EXPECTED_FILES)):
        base = prefix + "/layout/files/%d" % index
        layout["files"].append(
            {
                "file": material(
                    document, component_sha256, base + "/file", "string"
                ),
                "sha256": material(
                    document, component_sha256, base + "/sha256", "string"
                ),
            }
        )
    require(version == VERSION, "xcb-util-cursor version differs")
    require(source["status"] == "locked", "xcb-util-cursor source is not locked")
    require(
        signature["url"] == source["url"] + ".sig",
        "xcb-util-cursor signature URL differs",
    )
    require(
        layout["top_directory"] == TOP_DIRECTORY,
        "xcb-util-cursor top directory differs",
    )
    require(
        [record["file"] for record in layout["files"]]
        == list(EXPECTED_FILES),
        "xcb-util-cursor marker order or set differs",
    )
    require(
        license_identity["expression"] == "MIT"
        and license_identity["file"] == "COPYING"
        and license_identity["sha256"] == layout["files"][0]["sha256"],
        "xcb-util-cursor license identity differs",
    )
    expected_paths = {
        prefix + "/version",
        prefix + "/source/status",
        prefix + "/source/url",
        prefix + "/source/sha256",
        prefix + "/source/size",
        prefix + "/source/signature/url",
        prefix + "/source/signature/sha256",
        prefix + "/source/signature/size",
        prefix + "/source/signature/evidence",
        prefix + "/source/signature/key/file",
        prefix + "/source/signature/key/retrieval_url",
        prefix + "/source/signature/key/sha256",
        prefix + "/source/signature/key/fingerprint",
        prefix + "/license/expression",
        prefix + "/license/file",
        prefix + "/license/sha256",
        prefix + "/layout/top_directory",
        prefix + "/layout/member_count",
    }
    for index in range(len(EXPECTED_FILES)):
        expected_paths.add(prefix + "/layout/files/%d/file" % index)
        expected_paths.add(prefix + "/layout/files/%d/sha256" % index)
    require(
        {record["path"] for record in document["materials"]} == expected_paths,
        "xcb-util-cursor source component material set differs",
    )
    return document, source, signature, license_identity, layout


def repository_file(relative, description):
    require(
        isinstance(relative, str)
        and relative
        and not relative.startswith("/")
        and ".." not in PurePosixPath(relative).parts,
        "invalid %s path" % description,
    )
    expected = REPOSITORY / relative
    try:
        resolved = expected.resolve()
        resolved.relative_to(REPOSITORY.resolve())
    except (OSError, RuntimeError, ValueError) as error:
        raise XcbCursorSourceError("%s escapes repository" % description) from error
    require(
        resolved == expected and resolved.is_file() and not resolved.is_symlink(),
        "%s is missing or non-canonical" % description,
    )
    return resolved


def decode_signature_evidence(path, signature):
    path = Path(path)
    expected = repository_file(signature["evidence"], "signature evidence")
    require(path.resolve() == expected, "signature evidence path differs")
    encoded = path.read_bytes()
    require(
        encoded and encoded == encoded.strip() + b"\n" and len(encoded.splitlines()) == 1,
        "signature evidence envelope is not canonical base64",
    )
    try:
        payload = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise XcbCursorSourceError("signature evidence is invalid base64") from error
    require(len(payload) == signature["size"], "signature evidence size differs")
    require(
        hashlib.sha256(payload).hexdigest() == signature["sha256"],
        "signature evidence digest differs",
    )
    return payload, hashlib.sha256(encoded).hexdigest()


def verify_signature(archive, signature_path, signature, evidence_payload):
    signature_path = Path(signature_path)
    size, digest = sha256_file(signature_path, 4096)
    require(
        (size, digest) == (signature["size"], signature["sha256"]),
        "xcb-util-cursor detached signature identity differs",
    )
    require(
        signature_path.read_bytes() == evidence_payload,
        "detached signature differs from archived evidence",
    )
    key = repository_file(signature["key"]["file"], "release key")
    key_size, key_digest = sha256_file(key, 64 * 1024)
    require(key_size > 0 and key_digest == signature["key"]["sha256"], "release key digest differs")
    expected_fingerprint = signature["key"]["fingerprint"]
    with tempfile.TemporaryDirectory(prefix="crossforge-xcb-cursor-gpg-") as temporary:
        home = Path(temporary)
        os.chmod(str(home), 0o700)
        imported = subprocess.run(
            [
                "gpg",
                "--batch",
                "--no-autostart",
                "--homedir",
                str(home),
                "--import",
                str(key),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        listed = subprocess.run(
            [
                "gpg",
                "--batch",
                "--no-autostart",
                "--homedir",
                str(home),
                "--with-colons",
                "--list-keys",
                "--fingerprint",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        fingerprints = [
            line.split(":")[9].lower()
            for line in listed.stdout.splitlines()
            if line.startswith("fpr:")
        ]
        require(
            imported.returncode == 0
            and listed.returncode == 0
            and fingerprints
            and fingerprints[0] == expected_fingerprint,
            "cannot import release key or primary fingerprint differs",
        )
        verified = subprocess.run(
            [
                "gpg",
                "--batch",
                "--no-autostart",
                "--no-auto-key-retrieve",
                "--homedir",
                str(home),
                "--status-fd",
                "1",
                "--verify",
                str(signature_path),
                str(archive),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        valid = [
            line.split()[2].lower()
            for line in verified.stdout.splitlines()
            if line.startswith("[GNUPG:] VALIDSIG ")
        ]
        require(
            verified.returncode == 0 and valid == [expected_fingerprint],
            "xcb-util-cursor detached signature is not valid",
        )
    return {
        "file": SIGNATURE_NAME,
        "sha256": digest,
        "size": size,
        "evidence": signature["evidence"],
        "key": {
            "file": signature["key"]["file"],
            "sha256": key_digest,
            "fingerprint": expected_fingerprint,
            "retrieval_url": signature["key"]["retrieval_url"],
        },
    }


def inspect_archive(path, source, layout):
    size, digest = sha256_file(path, MAX_ARCHIVE_SIZE)
    require(
        (size, digest) == (source["size"], source["sha256"]),
        "xcb-util-cursor source archive identity differs",
    )
    expected = {
        "%s/%s" % (TOP_DIRECTORY, record["file"]): record
        for record in layout["files"]
    }
    selected = {}
    names = set()
    tops = set()
    count = 0
    unpacked_size = 0
    try:
        with tarfile.open(str(path), "r:xz") as archive:
            for member in archive:
                count += 1
                pure = PurePosixPath(member.name)
                require(
                    not pure.is_absolute()
                    and pure.parts
                    and all(part not in ("", ".", "..") for part in pure.parts),
                    "xcb-util-cursor archive contains an unsafe member path",
                )
                require(member.name not in names, "xcb-util-cursor archive member is duplicated")
                names.add(member.name)
                tops.add(pure.parts[0])
                require(
                    member.isfile() or member.isdir(),
                    "xcb-util-cursor archive contains a link or special file",
                )
                if member.isfile():
                    require(member.size >= 0, "xcb-util-cursor member size is negative")
                    unpacked_size += member.size
                    require(
                        unpacked_size <= MAX_UNPACKED_SIZE,
                        "xcb-util-cursor archive exceeds the unpacked-size limit",
                    )
                marker = expected.get(member.name)
                if marker is None:
                    continue
                require(member.isfile() and member.size > 0, "xcb-util-cursor marker is not a file")
                stream = archive.extractfile(member)
                require(stream is not None, "cannot read xcb-util-cursor marker")
                payload = stream.read()
                require(
                    len(payload) == member.size
                    and hashlib.sha256(payload).hexdigest() == marker["sha256"],
                    "xcb-util-cursor marker digest differs: %s" % marker["file"],
                )
                selected[marker["file"]] = {
                    "file": marker["file"],
                    "sha256": marker["sha256"],
                    "size": member.size,
                    "mode": "%04o" % member.mode,
                }
    except (OSError, tarfile.TarError) as error:
        raise XcbCursorSourceError("cannot inspect source archive: %s" % error) from error
    require(tops == {TOP_DIRECTORY}, "xcb-util-cursor archive top directory differs")
    require(count == layout["member_count"], "xcb-util-cursor archive member count differs")
    require(set(selected) == set(EXPECTED_FILES), "xcb-util-cursor source markers are incomplete")
    return {
        "file": ARCHIVE_NAME,
        "sha256": digest,
        "size": size,
    }, [selected[name] for name in EXPECTED_FILES]


def validate_manifest(document, schema_path):
    schema = STRICT["load_json"](schema_path)
    require(schema.get("$id") == SCHEMA_ID, "source manifest schema differs")
    try:
        STRICT["validate_schema_subset"](schema)
        STRICT["validate"](document, schema, schema, "$")
    except SchemaError as error:
        raise XcbCursorSourceError("source manifest schema failed: %s" % error) from error


def write_json_once(path, document):
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "source manifest output must not be a symlink")
    if path.exists():
        require(
            path.is_file() and path.read_text(encoding="utf-8") == payload,
            "refusing to replace a different source manifest",
        )
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--component-sha256", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--signature-evidence", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY
        / "config/schemas/xcb-util-cursor-source-manifest.schema.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        _document, source, signature, license_identity, layout = load_policy(
            arguments.component, arguments.component_sha256
        )
        archive, files = inspect_archive(arguments.archive, source, layout)
        evidence_payload, evidence_sha256 = decode_signature_evidence(
            arguments.signature_evidence, signature
        )
        signature_identity = verify_signature(
            arguments.archive, arguments.signature, signature, evidence_payload
        )
        signature_identity["evidence_sha256"] = evidence_sha256
        manifest = {
            "$schema": SCHEMA_ID,
            "schema_version": 1,
            "kind": "crossforge-xcb-util-cursor-source",
            "version": VERSION,
            "source_component": {
                "component": COMPONENT_NAME,
                "canonical_sha256": arguments.component_sha256,
            },
            "archive": archive,
            "signature": signature_identity,
            "license": license_identity,
            "top_directory": layout["top_directory"],
            "member_count": layout["member_count"],
            "files": files,
        }
        validate_manifest(manifest, arguments.schema)
        state = "wrote" if write_json_once(arguments.output, manifest) else "current"
        print("%s xcb-util-cursor source manifest: %s" % (state, arguments.output))
        return 0
    except (ComponentError, OSError, XcbCursorSourceError, SchemaError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
