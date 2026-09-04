#!/usr/bin/env python3
"""Validate the pinned Qt archive, checksum evidence, layout, and licenses."""

import argparse
import base64
import binascii
import hashlib
import json
import os
import runpy
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
COMPONENT = runpy.run_path(str(Path(__file__).with_name("release_component.py")))
STRICT = runpy.run_path(str(Path(__file__).with_name("validate-release.py")))
ComponentError = COMPONENT["ComponentError"]
SchemaError = STRICT["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/qt-source-manifest.schema.json"
VERSION = "6.8.4"
ARCHIVE_NAME = "qt-everywhere-opensource-src-6.8.4.tar.xz"
CHECKSUM_NAME = ARCHIVE_NAME + ".sha256"
TOP_DIRECTORY = "qt-everywhere-src-6.8.4"
EXPECTED_FILES = (
    "LICENSES/BSD-3-Clause.txt",
    "LICENSES/GFDL-1.3-no-invariants-only.txt",
    "LICENSES/GPL-2.0-only.txt",
    "LICENSES/GPL-3.0-only.txt",
    "LICENSES/LGPL-3.0-only.txt",
    "LICENSES/LicenseRef-Qt-Commercial.txt",
    "LICENSES/Qt-GPL-exception-1.0.txt",
    "configure",
    "qtbase/CMakeLists.txt",
    "qtdeclarative/CMakeLists.txt",
    "qtmultimedia/CMakeLists.txt",
    "qtquick3d/CMakeLists.txt",
    "qtshadertools/CMakeLists.txt",
    "qttools/CMakeLists.txt",
    "qtwayland/CMakeLists.txt",
    "qtwebengine/CMakeLists.txt",
)
MAX_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
MAX_UNPACKED_SIZE = 16 * 1024 * 1024 * 1024


class QtSourceError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise QtSourceError(message)


def sha256_file(path, maximum=None):
    path = Path(path)
    try:
        information = path.lstat()
    except OSError as error:
        raise QtSourceError("cannot inspect %s: %s" % (path, error)) from error
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
            "sources/qt",
            "build",
            component_sha256,
            pointer,
            expected_type,
        )
    except ComponentError as error:
        raise QtSourceError(str(error)) from error


def load_policy(component_path, component_sha256):
    try:
        document = COMPONENT["load_component"](
            component_path,
            "sources/qt",
            "build",
            component_sha256,
        )
    except ComponentError as error:
        raise QtSourceError(str(error)) from error
    require(
        material(document, component_sha256, "/qt/version", "string") == VERSION,
        "Qt version differs",
    )
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
            "/qt/source/%s" % field,
            expected_type,
        )
    checksum = {}
    for field, expected_type in (
        ("url", "string"),
        ("sha256", "string"),
        ("size", "integer"),
        ("evidence", "string"),
        ("authentication", "string"),
    ):
        checksum[field] = material(
            document,
            component_sha256,
            "/qt/source/checksum/%s" % field,
            expected_type,
        )
    layout = {
        "top_directory": material(
            document,
            component_sha256,
            "/qt/source/layout/top_directory",
            "string",
        ),
        "member_count": material(
            document,
            component_sha256,
            "/qt/source/layout/member_count",
            "integer",
        ),
        "files": [],
    }
    for index in range(len(EXPECTED_FILES)):
        base = "/qt/source/layout/files/%d" % index
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
    require(source["status"] == "locked", "Qt source is not locked")
    require(layout["top_directory"] == TOP_DIRECTORY, "Qt top directory differs")
    require(
        [record["file"] for record in layout["files"]] == list(EXPECTED_FILES),
        "Qt source marker or license set differs",
    )
    require(
        checksum["authentication"] == "hash-pinned-https-sidecar-no-signature",
        "Qt source authentication boundary differs",
    )
    return document, source, checksum, layout


def decode_checksum_evidence(path, checksum, archive_sha256):
    path = Path(path)
    expected_path = REPOSITORY / checksum["evidence"]
    require(path.resolve() == expected_path.resolve(), "Qt checksum evidence path differs")
    encoded = path.read_bytes()
    require(
        encoded and encoded == encoded.strip() + b"\n" and len(encoded.splitlines()) == 1,
        "Qt checksum evidence envelope is not canonical base64",
    )
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise QtSourceError("Qt checksum evidence is invalid base64") from error
    require(len(decoded) == checksum["size"], "Qt checksum evidence size differs")
    require(
        hashlib.sha256(decoded).hexdigest() == checksum["sha256"],
        "Qt checksum evidence digest differs",
    )
    expected = ("%s  %s\n" % (archive_sha256, ARCHIVE_NAME)).encode("ascii")
    require(decoded == expected, "Qt checksum sidecar content differs")
    return {
        "file": CHECKSUM_NAME,
        "sha256": checksum["sha256"],
        "size": checksum["size"],
        "authentication": checksum["authentication"],
        "evidence": checksum["evidence"],
        "evidence_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def normalized_link_target(member):
    link = PurePosixPath(member.linkname)
    require(not link.is_absolute(), "Qt archive contains an absolute link")
    candidate = PurePosixPath(member.name).parent / link if member.issym() else link
    parts = []
    for part in candidate.parts:
        if part in ("", "."):
            continue
        if part == "..":
            require(parts, "Qt archive link escapes the archive root")
            parts.pop()
        else:
            parts.append(part)
    require(parts and parts[0] == TOP_DIRECTORY, "Qt archive link escapes top directory")


def inspect_archive(path, source, layout):
    size, digest = sha256_file(path, MAX_ARCHIVE_SIZE)
    require(size == source["size"], "Qt source archive size differs")
    require(digest == source["sha256"], "Qt source archive digest differs")
    selected_names = {
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
                    "Qt archive contains an unsafe member path",
                )
                require(member.name not in names, "Qt archive member is duplicated")
                names.add(member.name)
                tops.add(pure.parts[0])
                require(
                    member.isfile()
                    or member.isdir()
                    or member.issym()
                    or member.islnk(),
                    "Qt archive contains a special file",
                )
                if member.issym() or member.islnk():
                    normalized_link_target(member)
                if member.isfile():
                    require(member.size >= 0, "Qt archive member size is negative")
                    unpacked_size += member.size
                    require(
                        unpacked_size <= MAX_UNPACKED_SIZE,
                        "Qt archive exceeds the unpacked-size limit",
                    )
                expected = selected_names.get(member.name)
                if expected is None:
                    continue
                require(member.isfile() and member.size > 0, "Qt marker is not a file")
                stream = archive.extractfile(member)
                require(stream is not None, "cannot read Qt source marker")
                payload = stream.read()
                require(
                    len(payload) == member.size
                    and hashlib.sha256(payload).hexdigest() == expected["sha256"],
                    "Qt source marker digest differs: %s" % expected["file"],
                )
                expected_mode = 0o775 if expected["file"] == "configure" else 0o664
                require(
                    member.mode == expected_mode,
                    "Qt source marker mode differs: %s" % expected["file"],
                )
                selected[expected["file"]] = {
                    "file": expected["file"],
                    "sha256": expected["sha256"],
                    "size": member.size,
                    "mode": "%04o" % expected_mode,
                }
    except (OSError, tarfile.TarError) as error:
        raise QtSourceError("cannot inspect Qt source archive: %s" % error) from error
    require(tops == {TOP_DIRECTORY}, "Qt archive top directory differs")
    require(count == layout["member_count"], "Qt archive member count differs")
    require(set(selected) == set(EXPECTED_FILES), "Qt source markers are incomplete")
    return {
        "file": ARCHIVE_NAME,
        "sha256": digest,
        "size": size,
    }, [selected[name] for name in EXPECTED_FILES]


def write_json_once(path, document):
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "Qt manifest output must not be a symlink")
    if path.exists():
        require(
            path.is_file() and path.read_text(encoding="utf-8") == payload,
            "refusing to replace a different Qt source manifest",
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


def validate_manifest(document, schema_path):
    schema = STRICT["load_json"](schema_path)
    require(schema.get("$id") == SCHEMA_ID, "Qt source manifest schema differs")
    try:
        STRICT["validate_schema_subset"](schema)
        STRICT["validate"](document, schema, schema, "$")
    except SchemaError as error:
        raise QtSourceError("Qt source manifest schema failed: %s" % error) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--component-sha256", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--checksum-evidence", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY / "config/schemas/qt-source-manifest.schema.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        _document, source, checksum, layout = load_policy(
            arguments.component, arguments.component_sha256
        )
        archive, files = inspect_archive(arguments.archive, source, layout)
        checksum_identity = decode_checksum_evidence(
            arguments.checksum_evidence, checksum, archive["sha256"]
        )
        manifest = {
            "$schema": SCHEMA_ID,
            "schema_version": 1,
            "kind": "crossforge-qt-source",
            "version": VERSION,
            "source_component": {
                "component": "sources/qt",
                "canonical_sha256": arguments.component_sha256,
            },
            "archive": archive,
            "checksum": checksum_identity,
            "top_directory": layout["top_directory"],
            "member_count": layout["member_count"],
            "files": files,
        }
        validate_manifest(manifest, arguments.schema)
        state = "wrote" if write_json_once(arguments.output, manifest) else "current"
        print("%s Qt source manifest: %s" % (state, arguments.output))
        return 0
    except (ComponentError, OSError, QtSourceError, SchemaError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
