#!/usr/bin/env python3
"""Authenticate and export the source corresponding to the CMake host tool."""

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import runpy
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
SIGNATURE = runpy.run_path(str(REPOSITORY / "scripts/source_signature.py"))
COMPONENT = runpy.run_path(str(REPOSITORY / "scripts/release_component.py"))
ValidationError = STRICT["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/cmake-source-manifest.schema.json"
CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9_.+-]+)$")


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def file_identity(path, label):
    try:
        return SIGNATURE["file_identity"](path)
    except SIGNATURE["SignatureError"] as error:
        raise ValidationError("%s: %s" % (label, error))


def load_policy(path, expected_sha256):
    try:
        document = COMPONENT["load_component"](
            path, "sources/cmake", "build", expected_sha256
        )
    except COMPONENT["ComponentError"] as error:
        raise ValidationError(str(error))

    def value(pointer, expected_type):
        try:
            return COMPONENT["material_value"](
                document,
                "sources/cmake",
                "build",
                expected_sha256,
                pointer,
                expected_type,
            )
        except COMPONENT["ComponentError"] as error:
            raise ValidationError(str(error))

    base = "/host_tools/cmake"
    source_base = base + "/source"
    checksums_base = source_base + "/checksums"
    signature_base = checksums_base + "/signature"
    key_base = signature_base + "/key"
    verification_base = signature_base + "/verification"
    layout_base = source_base + "/layout"
    source = {
        "status": value(source_base + "/status", "string"),
        "url": value(source_base + "/url", "string"),
        "sha256": value(source_base + "/sha256", "string"),
        "size": value(source_base + "/size", "integer"),
        "layout": {
            field: value(layout_base + "/" + field, expected_type)
            for field, expected_type in (
                ("top_directory", "string"),
                ("member_count", "integer"),
                ("license_sha256", "string"),
                ("readme_sha256", "string"),
                ("cmakelists_sha256", "string"),
            )
        },
        "checksums": {
            field: value(checksums_base + "/" + field, expected_type)
            for field, expected_type in (
                ("url", "string"),
                ("sha256", "string"),
                ("size", "integer"),
                ("entries", "integer"),
                ("evidence", "string"),
            )
        },
    }
    source["checksums"]["signature"] = {
        "url": value(signature_base + "/url", "string"),
        "sha256": value(signature_base + "/sha256", "string"),
        "size": value(signature_base + "/size", "integer"),
        "evidence": value(signature_base + "/evidence", "string"),
        "key": {
            field: value(key_base + "/" + field, expected_type)
            for field, expected_type in (
                ("file", "string"),
                ("retrieval_url", "string"),
                ("sha256", "string"),
                ("size", "integer"),
                ("primary_fingerprint", "string"),
                ("signing_fingerprint", "string"),
                ("signing_key_expires_at", "string"),
            )
        },
        "verification": {
            field: value(verification_base + "/" + field, "string")
            for field in ("status", "signature_time", "exception")
        },
    }
    return {
        "component": document,
        "version": value(base + "/version", "string"),
        "binary": {"sha256": value(base + "/binary/sha256", "string")},
        "source": source,
        "license": {
            field: value(base + "/license/" + field, expected_type)
            for field, expected_type in (
                ("expression", "string"),
                ("path", "string"),
                ("sha256", "string"),
                ("size", "integer"),
            )
        },
    }


def safe_repository_file(repository, relative, label):
    require(
        isinstance(relative, str)
        and relative
        and not relative.startswith("/")
        and ".." not in relative.split("/"),
        "%s path is unsafe" % label,
    )
    root = repository.resolve()
    expected = repository / relative
    require(expected.is_file() and not expected.is_symlink(), "%s is missing" % label)
    try:
        resolved = expected.resolve()
        resolved.relative_to(root)
    except ValueError:
        raise ValidationError("%s escaped repository" % label)
    require(resolved == expected, "%s path is non-canonical" % label)
    return expected


def evidence_bytes(repository, relative, expected_sha256, expected_size, label):
    path = safe_repository_file(repository, relative, label)
    try:
        payload = base64.b64decode(b"".join(path.read_bytes().split()), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValidationError("invalid %s: %s" % (label, error))
    require(
        len(payload) == expected_size and sha256_bytes(payload) == expected_sha256,
        "%s identity differs" % label,
    )
    return payload


def parse_checksums(payload, policy, binary):
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValidationError("CMake checksum manifest is not ASCII") from error
    require(text.endswith("\n") and "\r" not in text, "CMake checksum line endings differ")
    result = {}
    for line in text.splitlines():
        match = CHECKSUM_LINE.match(line)
        require(match is not None, "CMake checksum line is malformed")
        digest, name = match.groups()
        require(name not in result, "CMake checksum manifest repeats an asset")
        result[name] = digest
    require(len(result) == policy["entries"], "CMake checksum asset count differs")
    require(
        result.get("cmake-4.4.0.tar.gz")
        == "65757f442fdd242e27f1728fc26dc0cba4164f7a0791a5c788631c00080369bc"
        == policy["source_sha256"]
        and result.get("cmake-4.4.0-linux-x86_64.tar.gz") == binary["sha256"],
        "CMake checksum manifest does not bind source and shipped binary",
    )
    return result


def verify_archive(archive_path, policy, installed_license):
    identity = file_identity(archive_path, "CMake source archive")
    require(
        identity == {"sha256": policy["sha256"], "size": policy["size"]},
        "CMake source archive identity differs",
    )
    layout = policy["layout"]
    selected = {}
    seen = set()
    members = 0
    try:
        with tarfile.open(str(archive_path), "r:gz") as archive:
            for member in archive:
                members += 1
                name = member.name
                require(
                    name
                    and name not in seen
                    and not name.startswith("/")
                    and ".." not in name.split("/")
                    and name.split("/", 1)[0] == layout["top_directory"],
                    "CMake source archive contains an unsafe path",
                )
                seen.add(name)
                require(
                    member.isfile() or member.isdir(),
                    "CMake source archive contains a special entry",
                )
                relative = name[len(layout["top_directory"]) + 1 :]
                if relative in ("LICENSE.rst", "README.rst", "CMakeLists.txt"):
                    require(member.isfile(), "CMake source marker is not a file")
                    stream = archive.extractfile(member)
                    require(stream is not None, "CMake source marker is unreadable")
                    selected[relative] = sha256_bytes(stream.read())
    except (KeyError, OSError, tarfile.TarError) as error:
        raise ValidationError("cannot inspect CMake source archive: %s" % error)
    require(members == layout["member_count"], "CMake source member count differs")
    require(
        selected
        == {
            "LICENSE.rst": layout["license_sha256"],
            "README.rst": layout["readme_sha256"],
            "CMakeLists.txt": layout["cmakelists_sha256"],
        },
        "CMake source marker or license differs",
    )
    require(
        layout["license_sha256"] == installed_license["sha256"]
        and installed_license
        == {
            "expression": "BSD-3-Clause",
            "path": "doc/cmake/LICENSE.rst",
            "sha256": layout["license_sha256"],
            "size": 1498,
        },
        "CMake source and installed license identities differ",
    )
    return identity


def verify_signature(gpg, checksums, signature, key, source_policy):
    signature_policy = source_policy["checksums"]["signature"]
    key_policy = signature_policy["key"]
    require(
        file_identity(key, "CMake release key")
        == {"sha256": key_policy["sha256"], "size": key_policy["size"]},
        "CMake release key identity differs",
    )
    verification = signature_policy["verification"]
    require(
        verification
        == {
            "status": "cryptographically-valid-expired-key",
            "signature_time": "2026-07-09T18:21:38Z",
            "exception": "upstream-signing-subkey-expired-before-signing",
        },
        "CMake expired-key exception differs",
    )
    try:
        return SIGNATURE["verify_expired_key_signature"](
            gpg,
            checksums,
            signature,
            key,
            {
                "key_sha256": key_policy["sha256"],
                "primary_fingerprint": key_policy["primary_fingerprint"],
                "signing_fingerprint": key_policy["signing_fingerprint"],
                "signing_key_expires_at": key_policy["signing_key_expires_at"],
                "signature_time": verification["signature_time"],
                "status": verification["status"],
                "exception": verification["exception"],
            },
        )
    except SIGNATURE["SignatureError"] as error:
        raise ValidationError("CMake checksum signature: %s" % error)


def prepare(component_path, component_sha256, archive, repository, output, gpg):
    tool = load_policy(component_path, component_sha256)
    source = tool["source"]
    checksums_policy = source["checksums"]
    signature_policy = checksums_policy["signature"]
    checksums_payload = evidence_bytes(
        repository,
        checksums_policy["evidence"],
        checksums_policy["sha256"],
        checksums_policy["size"],
        "CMake checksum evidence",
    )
    signature_payload = evidence_bytes(
        repository,
        signature_policy["evidence"],
        signature_policy["sha256"],
        signature_policy["size"],
        "CMake checksum signature evidence",
    )
    key = safe_repository_file(
        repository, signature_policy["key"]["file"], "CMake release key"
    )
    archive_identity = verify_archive(archive, source, tool["license"])
    parse_checksums(
        checksums_payload,
        {
            "entries": checksums_policy["entries"],
            "source_sha256": source["sha256"],
        },
        tool["binary"],
    )
    require(not output.exists() and not output.is_symlink(), "CMake source output exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".%s." % output.name, dir=str(output.parent))
    )
    try:
        materials = temporary / "materials"
        materials.mkdir()
        checksums = materials / "cmake-4.4.0-SHA-256.txt"
        signature = materials / "cmake-4.4.0-SHA-256.txt.asc"
        checksums.write_bytes(checksums_payload)
        signature.write_bytes(signature_payload)
        verification = verify_signature(gpg, checksums, signature, key, source)
        destinations = (
            (archive, materials / "cmake-4.4.0.tar.gz"),
            (key, materials / "CMAKE-RELEASE-KEY.asc"),
        )
        for original, destination in destinations:
            shutil.copyfile(str(original), str(destination))
        for path in materials.iterdir():
            os.chmod(str(path), 0o644)
        manifest = {
            "$schema": SCHEMA_ID,
            "schema_version": 1,
            "kind": "crossforge-cmake-source",
            "source_component": {
                "component": "sources/cmake",
                "canonical_sha256": component_sha256,
            },
            "source": {
                "version": tool["version"],
                "archive": dict(
                    {"file": "cmake-4.4.0.tar.gz"}, **archive_identity
                ),
                "checksums": {
                    "file": "cmake-4.4.0-SHA-256.txt",
                    "sha256": checksums_policy["sha256"],
                    "size": checksums_policy["size"],
                    "entries": checksums_policy["entries"],
                },
                "signature": {
                    "file": "cmake-4.4.0-SHA-256.txt.asc",
                    "sha256": signature_policy["sha256"],
                    "size": signature_policy["size"],
                    "verification": verification["status"],
                    "signature_time": verification["signature_time"],
                    "exception": verification["exception"],
                },
                "key": {
                    "file": "CMAKE-RELEASE-KEY.asc",
                    "sha256": signature_policy["key"]["sha256"],
                    "size": signature_policy["key"]["size"],
                    "primary_fingerprint": verification["primary_fingerprint"],
                    "signing_fingerprint": verification["signing_fingerprint"],
                    "signing_key_expires_at": verification[
                        "signing_key_expires_at"
                    ],
                },
                "layout": source["layout"],
            },
        }
        manifest_schema = STRICT["load_json"](
            repository / "config/schemas/cmake-source-manifest.schema.json"
        )
        STRICT["validate_schema_subset"](manifest_schema)
        STRICT["validate"](manifest, manifest_schema, manifest_schema, "$")
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
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--component-sha256", required=True)
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--gpg", type=Path, default=Path("/usr/bin/gpg"))
    arguments = parser.parse_args(argv)
    try:
        prepare(
            arguments.component,
            arguments.component_sha256,
            arguments.archive,
            arguments.repository,
            arguments.output,
            arguments.gpg,
        )
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print("prepared authenticated CMake source: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
