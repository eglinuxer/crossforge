#!/usr/bin/env python3
"""Authenticate and safely prepare the locked Ninja host-tool payload."""

import argparse
import base64
import binascii
import hashlib
import json
import os
import runpy
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
COMPONENT_NAME = "sources/ninja"


class PreparationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise PreparationError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_identity(path):
    sha256 = hashlib.sha256()
    sha512 = hashlib.sha512()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha256.update(block)
            sha512.update(block)
            size += len(block)
    return {
        "sha256": sha256.hexdigest(),
        "sha512": sha512.hexdigest(),
        "size": size,
    }


def bytes_identity(payload):
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "sha512": hashlib.sha512(payload).hexdigest(),
        "size": len(payload),
    }


def git_object_id(kind, payload):
    header = ("%s %d\0" % (kind, len(payload))).encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def material_map(document):
    result = {}
    for record in document["materials"]:
        path = record["path"]
        require(path not in result, "Ninja component repeats material %s" % path)
        result[path] = record["value"]
    return result


def load_identity(component_path, expected_sha256):
    reader = runpy.run_path(str(SCRIPT_DIRECTORY / "release_component.py"))
    try:
        document = reader["load_component"](
            component_path, COMPONENT_NAME, "build", expected_sha256
        )
    except reader["ComponentError"] as error:
        raise PreparationError("invalid Ninja source component: %s" % error) from error
    require(document["dependencies"] == [], "Ninja source component has dependencies")
    values = material_map(document)
    fields = {
        "/host_tools/ninja/version": str,
        "/host_tools/ninja/repository": str,
        "/host_tools/ninja/tag": str,
        "/host_tools/ninja/tag_evidence": str,
        "/host_tools/ninja/tag_evidence_sha256": str,
        "/host_tools/ninja/tag_evidence_size": int,
        "/host_tools/ninja/commit": str,
        "/host_tools/ninja/commit_evidence": str,
        "/host_tools/ninja/release/immutable": bool,
        "/host_tools/ninja/release/evidence": str,
        "/host_tools/ninja/release/evidence_sha256": str,
        "/host_tools/ninja/release/evidence_size": int,
        "/host_tools/ninja/binary/status": str,
        "/host_tools/ninja/binary/url": str,
        "/host_tools/ninja/binary/sha256": str,
        "/host_tools/ninja/binary/sha512": str,
        "/host_tools/ninja/binary/size": int,
        "/host_tools/ninja/binary/archive_member": str,
        "/host_tools/ninja/binary/extracted_sha256": str,
        "/host_tools/ninja/binary/extracted_sha512": str,
        "/host_tools/ninja/binary/extracted_size": int,
        "/host_tools/ninja/source/status": str,
        "/host_tools/ninja/source/url": str,
        "/host_tools/ninja/source/sha256": str,
        "/host_tools/ninja/source/sha512": str,
        "/host_tools/ninja/source/size": int,
        "/host_tools/ninja/source/archive_root": str,
        "/host_tools/ninja/license/expression": str,
        "/host_tools/ninja/license/source_file": str,
        "/host_tools/ninja/license/sha256": str,
        "/host_tools/ninja/license/size": int,
    }
    require(set(values) == set(fields), "Ninja source material set differs")
    for path, expected_type in fields.items():
        require(
            type(values[path]) is expected_type,
            "Ninja source material type differs: %s" % path,
        )
    require(
        values["/host_tools/ninja/version"] == "1.13.2"
        and values["/host_tools/ninja/tag"] == "v1.13.2"
        and values["/host_tools/ninja/repository"]
        == "https://github.com/ninja-build/ninja.git"
        and values["/host_tools/ninja/binary/status"] == "locked"
        and values["/host_tools/ninja/source/status"] == "locked"
        and values["/host_tools/ninja/release/immutable"] is False
        and values["/host_tools/ninja/license/expression"] == "Apache-2.0",
        "Ninja locked release policy differs",
    )
    return document, values


def safe_repository_file(repository, relative, label):
    require(type(relative) is str, "%s path must be text" % label)
    logical = PurePosixPath(relative)
    require(
        not logical.is_absolute()
        and logical.parts
        and all(part not in ("", ".", "..") for part in logical.parts)
        and logical.as_posix() == relative,
        "unsafe %s path" % label,
    )
    root = repository.resolve()
    candidate = root.joinpath(*logical.parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise PreparationError("%s escaped repository" % label) from error
    require(
        resolved == candidate and resolved.is_file() and not resolved.is_symlink(),
        "missing or non-canonical %s" % label,
    )
    return resolved


def evidence_bytes(repository, relative, label):
    path = safe_repository_file(repository, relative, label)
    require(path.suffix == ".b64", "%s is not a base64 envelope" % label)
    try:
        return base64.b64decode(b"".join(path.read_bytes().split()), validate=True)
    except (OSError, binascii.Error, ValueError) as error:
        raise PreparationError("invalid %s" % label) from error


def evidence_json(repository, relative, sha256, size, label):
    payload = evidence_bytes(repository, relative, label)
    require(
        bytes_identity(payload)["sha256"] == sha256 and len(payload) == size,
        "%s identity differs" % label,
    )
    try:
        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise PreparationError("invalid JSON %s" % label) from error
    require(isinstance(document, dict), "%s must contain an object" % label)
    return document


def verify_evidence(identity, repository):
    commit = evidence_bytes(
        repository,
        identity["/host_tools/ninja/commit_evidence"],
        "Ninja commit evidence",
    )
    require(
        git_object_id("commit", commit)
        == identity["/host_tools/ninja/commit"]
        and commit.startswith(b"tree "),
        "Ninja commit evidence differs",
    )
    tag = evidence_json(
        repository,
        identity["/host_tools/ninja/tag_evidence"],
        identity["/host_tools/ninja/tag_evidence_sha256"],
        identity["/host_tools/ninja/tag_evidence_size"],
        "Ninja tag-ref evidence",
    )
    require(
        tag.get("ref") == "refs/tags/" + identity["/host_tools/ninja/tag"]
        and tag.get("object", {}).get("type") == "commit"
        and tag.get("object", {}).get("sha")
        == identity["/host_tools/ninja/commit"],
        "Ninja lightweight tag mapping differs",
    )
    release = evidence_json(
        repository,
        identity["/host_tools/ninja/release/evidence"],
        identity["/host_tools/ninja/release/evidence_sha256"],
        identity["/host_tools/ninja/release/evidence_size"],
        "Ninja release evidence",
    )
    assets = [
        asset
        for asset in release.get("assets", [])
        if asset.get("name") == "ninja-linux.zip"
    ]
    require(len(assets) == 1, "Ninja release asset inventory differs")
    asset = assets[0]
    require(
        release.get("tag_name") == identity["/host_tools/ninja/tag"]
        and release.get("immutable") is False
        and release.get("draft") is False
        and release.get("prerelease") is False
        and asset.get("size") == identity["/host_tools/ninja/binary/size"]
        and asset.get("digest")
        == "sha256:" + identity["/host_tools/ninja/binary/sha256"]
        and asset.get("browser_download_url")
        == identity["/host_tools/ninja/binary/url"],
        "Ninja release evidence differs",
    )


def verify_archives(binary_archive, source_archive, identity):
    binary_identity = file_identity(binary_archive)
    source_identity = file_identity(source_archive)
    require(
        binary_identity
        == {
            "sha256": identity["/host_tools/ninja/binary/sha256"],
            "sha512": identity["/host_tools/ninja/binary/sha512"],
            "size": identity["/host_tools/ninja/binary/size"],
        },
        "Ninja binary archive identity differs",
    )
    require(
        source_identity
        == {
            "sha256": identity["/host_tools/ninja/source/sha256"],
            "sha512": identity["/host_tools/ninja/source/sha512"],
            "size": identity["/host_tools/ninja/source/size"],
        },
        "Ninja source archive identity differs",
    )
    with zipfile.ZipFile(str(binary_archive), "r") as archive:
        records = archive.infolist()
        require(len(records) == 1, "Ninja binary archive must contain one file")
        member = records[0]
        require(
            member.filename == identity["/host_tools/ninja/binary/archive_member"]
            and not member.is_dir()
            and member.flag_bits & 1 == 0
            and member.file_size
            == identity["/host_tools/ninja/binary/extracted_size"],
            "Ninja binary archive member differs",
        )
        binary = archive.read(member)
    extracted_identity = bytes_identity(binary)
    require(
        extracted_identity
        == {
            "sha256": identity["/host_tools/ninja/binary/extracted_sha256"],
            "sha512": identity["/host_tools/ninja/binary/extracted_sha512"],
            "size": identity["/host_tools/ninja/binary/extracted_size"],
        },
        "Ninja extracted binary identity differs",
    )
    license_member = "%s/%s" % (
        identity["/host_tools/ninja/source/archive_root"],
        identity["/host_tools/ninja/license/source_file"],
    )
    with tarfile.open(str(source_archive), "r:gz") as archive:
        try:
            member = archive.getmember(license_member)
        except KeyError as error:
            raise PreparationError("Ninja source license is missing") from error
        require(member.isfile(), "Ninja source license is not a regular file")
        stream = archive.extractfile(member)
        require(stream is not None, "cannot read Ninja source license")
        with stream:
            license_payload = stream.read()
    require(
        bytes_identity(license_payload)["sha256"]
        == identity["/host_tools/ninja/license/sha256"]
        and len(license_payload) == identity["/host_tools/ninja/license/size"],
        "Ninja source license identity differs",
    )
    return binary, license_payload, binary_identity, source_identity


def prepare(
    component_path,
    component_sha256,
    binary_archive,
    source_archive,
    repository,
    output,
):
    component, identity = load_identity(component_path, component_sha256)
    verify_evidence(identity, repository)
    binary, license_payload, binary_archive_identity, source_archive_identity = (
        verify_archives(binary_archive, source_archive, identity)
    )
    require(not output.exists() and not output.is_symlink(), "Ninja output exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".%s." % output.name, dir=str(output.parent))
    )
    try:
        prepared = temporary / "prepared"
        materials = temporary / "materials"
        prepared.mkdir()
        materials.mkdir()
        binary_path = prepared / "ninja"
        binary_path.write_bytes(binary)
        os.chmod(str(binary_path), 0o755)
        (prepared / "COPYING").write_bytes(license_payload)
        os.chmod(str(prepared / "COPYING"), 0o644)
        shutil.copy2(str(binary_archive), str(materials / "ninja-linux.zip"))
        shutil.copy2(str(source_archive), str(materials / "ninja-source.tar.gz"))
        manifest = {
            "schema_version": 1,
            "kind": "crossforge-ninja-source",
            "component": {
                "name": COMPONENT_NAME,
                "canonical_sha256": canonical_sha256(component),
            },
            "release": {
                "version": identity["/host_tools/ninja/version"],
                "tag": identity["/host_tools/ninja/tag"],
                "commit": identity["/host_tools/ninja/commit"],
                "tag_evidence_sha256": identity[
                    "/host_tools/ninja/tag_evidence_sha256"
                ],
                "release_evidence_sha256": identity[
                    "/host_tools/ninja/release/evidence_sha256"
                ],
            },
            "binary_archive": binary_archive_identity,
            "binary": bytes_identity(binary),
            "source_archive": source_archive_identity,
            "license": {
                "expression": identity["/host_tools/ninja/license/expression"],
                "sha256": hashlib.sha256(license_payload).hexdigest(),
                "size": len(license_payload),
            },
        }
        (temporary / "source.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(str(temporary), ignore_errors=True)
        raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--component-sha256", required=True)
    parser.add_argument("--binary-archive", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    prepare(
        arguments.component,
        arguments.component_sha256,
        arguments.binary_archive,
        arguments.source_archive,
        arguments.repository,
        arguments.output,
    )
    print("prepared locked Ninja host tool: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        KeyError,
        OSError,
        PreparationError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
        tarfile.TarError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
