#!/usr/bin/env python3
"""Validate and manifest the complete Crossforge source bundle tree."""

import argparse
import hashlib
import json
import os
import re
import runpy
import stat
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
RPM = runpy.run_path(str(REPOSITORY / "scripts/validate-rpm-source-lock.py"))
BUILD = RPM["BUILD"]
RESOLVE = RPM["RESOLVE"]
STRICT = BUILD["STRICT"]
ValidationError = RPM["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/source-bundle-manifest.schema.json"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}\Z")


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def file_identity(path):
    information = os.lstat(str(path))
    require(stat.S_ISREG(information.st_mode), "source bundle entry is not regular")
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {"sha256": digest.hexdigest(), "size": size}


def expected_entries(release, lock, source_commit):
    require(COMMIT_RE.match(source_commit), "source bundle commit is invalid")
    require(source_commit != "0" * 40, "source bundle commit is unbound")
    entries = {}

    def add(path, scope, kind, component, origin, sha256=None, size=None):
        require(path not in entries, "source bundle path is duplicated: %s" % path)
        entries[path] = {
            "scope": scope,
            "kind": kind,
            "component": component,
            "origin": origin,
            "sha256": sha256,
            "size": size,
        }

    for record in lock["sources"]:
        add(
            "sources/product/rpm/" + record["source_rpm"],
            "product",
            "source-rpm",
            "rpm/source-el8",
            record["url"],
            record["sha256"],
            record["size"],
        )

    for entry in release["python"]["versions"]:
        source = entry["source"]
        add(
            "sources/product/cpython/Python-%s.tar.xz" % entry["version"],
            "product",
            "source-archive",
            "python/cp%s-source"
            % "".join(entry["version"].split(".")[:2]),
            source["url"],
            source["sha256"],
            source["size"],
        )
        sigstore = source["sigstore"]
        add(
            "verification/cpython/Python-%s.tar.xz.sigstore" % entry["version"],
            "verification",
            "sigstore-bundle",
            "python/qualification",
            sigstore["bundle_url"],
            sigstore["bundle_sha256"],
            sigstore["bundle_size"],
        )

    zstd = release["python"]["zstd"]
    zstd_source = zstd["source"]
    add(
        "sources/product/zstd/zstd-%s.tar.gz" % zstd["version"],
        "product",
        "source-archive",
        "sources/zstd",
        zstd_source["url"],
        zstd_source["sha256"],
        zstd_source["size"],
    )
    add_signature_and_key(
        add,
        "zstd",
        zstd_source["signature"],
        "zstd-%s.tar.gz.sig" % zstd["version"],
        "sources/zstd",
    )

    cmake = release["host_tools"]["cmake"]
    cmake_source = cmake["source"]
    add_source(
        add,
        "sources/product/cmake/cmake-%s.tar.gz" % cmake["version"],
        cmake_source,
        "sources/cmake",
    )
    checksums = cmake_source["checksums"]
    add(
        "verification/cmake/cmake-4.4.0-SHA-256.txt",
        "verification",
        "checksum",
        "sources/cmake",
        checksums["url"],
        checksums["sha256"],
        checksums["size"],
    )
    add_signature_and_key(
        add,
        "cmake",
        checksums["signature"],
        "cmake-4.4.0-SHA-256.txt.asc",
        "sources/cmake",
    )

    ninja = release["host_tools"]["ninja"]
    add_source(
        add,
        "sources/product/ninja/ninja-%s.tar.gz" % ninja["version"],
        ninja["source"],
        "sources/ninja",
    )

    vcpkg = release["vcpkg"]
    tool_source = vcpkg["tool"]["source"]
    add_source(
        add,
        "sources/product/vcpkg/vcpkg-tool-%s.tar.gz"
        % vcpkg["tool"]["commit"],
        tool_source,
        "sources/vcpkg",
    )
    add(
        "sources/product/vcpkg/vcpkg-registry-%s.tar.gz"
        % vcpkg["release"]["tag"],
        "product",
        "registry-snapshot",
        "sources/vcpkg",
        "generated:verified-complete-git-history",
    )
    add_signature_and_key(
        add,
        "vcpkg",
        vcpkg["tool"]["signature"],
        "vcpkg-glibc.sig",
        "sources/vcpkg",
    )

    nfpm = release["nfpm"]
    add_source(
        add,
        "sources/product/nfpm/nfpm-%s.tar.gz" % nfpm["version"],
        nfpm["source"]["archive"],
        "sources/nfpm",
    )
    for name, policy, kind in (
        ("checksums.txt", nfpm["checksums"], "checksum"),
        ("checksums.txt.sigstore.json", nfpm["sigstore"], "sigstore-bundle"),
    ):
        add(
            "verification/nfpm/" + name,
            "verification",
            kind,
            "sources/nfpm",
            policy["url"],
            policy["sha256"],
            policy["size"],
        )

    qemu = release["qemu"]["executor"]
    qemu_source = qemu["source"]["archive"]
    add_source(
        add,
        "sources/product/qemu/qemu-%s.tar.xz" % release["qemu"]["version"],
        qemu_source,
        "supply/qemu-source",
    )
    add_signature_and_key(
        add,
        "qemu",
        qemu_source["signature"],
        "qemu-10.2.3.tar.xz.sig",
        "supply/qemu-source",
    )
    builder = qemu["provenance"]["builder_source"]
    add_source(
        add,
        "sources/product/qemu/binfmt-%s.tar.gz"
        % qemu["provenance"]["builder_commit"],
        builder,
        "supply/qemu-source",
    )

    qt = release["qt"]
    add_source(
        add,
        "sources/qualification/qt/qt-everywhere-opensource-src-%s.tar.xz"
        % qt["version"],
        qt["source"],
        "sources/qt",
        scope="qualification",
    )
    qt_checksum = qt["source"]["checksum"]
    add(
        "verification/qt/qt-everywhere-opensource-src-6.8.4.tar.xz.sha256",
        "verification",
        "checksum",
        "sources/qt",
        qt_checksum["url"],
        qt_checksum["sha256"],
        qt_checksum["size"],
    )
    for name, component in (
        ("ffmpeg", "sources/ffmpeg"),
        ("xcb_util_cursor", "sources/xcb-util-cursor"),
    ):
        dependency = qt["dependencies"][name]
        archive_name = (
            "ffmpeg-%s.tar.xz" % dependency["version"]
            if name == "ffmpeg"
            else "xcb-util-cursor-%s.tar.xz" % dependency["version"]
        )
        directory = "ffmpeg" if name == "ffmpeg" else "xcb-util-cursor"
        add_source(
            add,
            "sources/qualification/%s/%s" % (directory, archive_name),
            dependency["source"],
            component,
            scope="qualification",
        )
        add_signature_and_key(
            add,
            directory,
            dependency["source"]["signature"],
            archive_name + (".asc" if name == "ffmpeg" else ".sig"),
            component,
        )

    add(
        "sources/product/crossforge/crossforge-%s.tar.gz" % source_commit,
        "product",
        "project-snapshot",
        "product/release",
        "generated:clean-candidate-source-tree",
    )

    rocky_key = release["trust"]["rocky_rpm_key"]
    add_key(add, "rocky", rocky_key, "rpm/source-el8")

    metadata = {
        "metadata/rpm-source-el8.json": ("source-lock", "rpm/source-el8"),
        "metadata/qemu-source.json": ("source-manifest", "supply/qemu-source"),
        "metadata/cmake-source.json": ("source-manifest", "sources/cmake"),
        "metadata/vcpkg-source.json": ("source-manifest", "sources/vcpkg"),
        "metadata/ninja-source.json": ("source-manifest", "sources/ninja"),
        "metadata/nfpm-source.json": ("source-manifest", "sources/nfpm"),
        "metadata/zstd-source.json": ("source-manifest", "sources/zstd"),
        "metadata/qt-source.json": ("source-manifest", "sources/qt"),
        "metadata/ffmpeg-source.json": ("source-manifest", "sources/ffmpeg"),
        "metadata/xcb-util-cursor-source.json": (
            "source-manifest",
            "sources/xcb-util-cursor",
        ),
    }
    for path, (kind, component) in metadata.items():
        add(path, "metadata", kind, component, "derived:qualified-source-stage")
    return entries


def add_source(add, path, policy, component, scope="product"):
    add(
        path,
        scope,
        "source-archive",
        component,
        policy["url"],
        policy["sha256"],
        policy["size"],
    )


def add_key(add, directory, key, component):
    filename = Path(key["file"]).name
    add(
        "verification/%s/%s" % (directory, filename),
        "verification",
        "public-key",
        component,
        key.get("retrieval_url", "repository:" + key["file"]),
        key["sha256"],
        key.get("size"),
    )


def add_signature_and_key(add, directory, signature, filename, component):
    add(
        "verification/%s/%s" % (directory, filename),
        "verification",
        "signature",
        component,
        signature["url"],
        signature["sha256"],
        signature["size"],
    )
    add_key(add, directory, signature["key"], component)


def validate_files(root, expected):
    require(root.is_dir() and not root.is_symlink(), "source bundle root is missing")
    observed = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        information = os.lstat(str(path))
        require(
            stat.S_ISDIR(information.st_mode) or stat.S_ISREG(information.st_mode),
            "source bundle contains a special entry: %s" % relative,
        )
        if stat.S_ISREG(information.st_mode):
            observed.append(relative)
    require(sorted(observed) == sorted(expected), "source bundle file membership differs")
    result = []
    for path in sorted(expected):
        policy = expected[path]
        identity = file_identity(root / path)
        if policy["sha256"] is not None:
            require(identity["sha256"] == policy["sha256"], "source digest differs: %s" % path)
        if policy["size"] is not None:
            require(identity["size"] == policy["size"], "source size differs: %s" % path)
        require(identity["size"] > 0, "source bundle entry is empty: %s" % path)
        result.append(
            {
                "path": path,
                "scope": policy["scope"],
                "kind": policy["kind"],
                "component": policy["component"],
                "sha256": identity["sha256"],
                "size": identity["size"],
                "origin": policy["origin"],
            }
        )
    return result


def validate_metadata(root, lock, entries):
    lock_path = root / "metadata/rpm-source-el8.json"
    observed_lock = STRICT["load_json"](lock_path)
    require(observed_lock == lock, "bundled RPM source lock differs")
    expected_kinds = {
        "metadata/qemu-source.json": "crossforge-qemu-source",
        "metadata/cmake-source.json": "crossforge-cmake-source",
        "metadata/vcpkg-source.json": "crossforge-vcpkg-source",
        "metadata/ninja-source.json": "crossforge-ninja-source",
        "metadata/nfpm-source.json": "crossforge-nfpm-source",
        "metadata/zstd-source.json": "crossforge-zstd-source",
        "metadata/qt-source.json": "crossforge-qt-source",
        "metadata/ffmpeg-source.json": "crossforge-ffmpeg-source",
        "metadata/xcb-util-cursor-source.json": "crossforge-xcb-util-cursor-source",
    }
    for path, kind in expected_kinds.items():
        document = STRICT["load_json"](root / path)
        require(document.get("kind") == kind, "source-stage manifest kind differs: %s" % path)
    require(any(entry["kind"] == "source-lock" for entry in entries), "source lock entry is absent")


def assemble(release, lock, root, source_commit, schema):
    expected = expected_entries(release, lock, source_commit)
    entries = validate_files(root, expected)
    validate_metadata(root, lock, entries)
    summary = {
        "entries": len(entries),
        "bytes": sum(entry["size"] for entry in entries),
        "source_rpms": sum(entry["kind"] == "source-rpm" for entry in entries),
        "source_archives": sum(
            entry["kind"]
            in ("source-archive", "project-snapshot", "registry-snapshot")
            for entry in entries
        ),
        "verification_materials": sum(
            entry["scope"] == "verification" for entry in entries
        ),
        "metadata_files": sum(entry["scope"] == "metadata" for entry in entries),
    }
    document = {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-source-bundle",
        "release_sha256": BUILD["canonical_sha256"](release),
        "source_commit": source_commit,
        "rpm_source_lock": {
            "file": "metadata/rpm-source-el8.json",
            "canonical_sha256": RESOLVE["canonical_sha256"](lock),
            "source_rpms": 333,
            "source_bytes": sum(record["size"] for record in lock["sources"]),
        },
        "entries": entries,
        "summary": summary,
    }
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")
    require(summary == {
        "entries": 384,
        "bytes": summary["bytes"],
        "source_rpms": 333,
        "source_archives": 18,
        "verification_materials": 23,
        "metadata_files": 10,
    }, "source bundle summary differs")
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--release", type=Path, default=REPOSITORY / "config/release.json"
    )
    parser.add_argument(
        "--release-schema", type=Path, default=REPOSITORY / "config/schemas/release.schema.json"
    )
    parser.add_argument("--lock", type=Path, default=REPOSITORY / "locks/rpm-source-el8.json")
    parser.add_argument(
        "--lock-schema", type=Path, default=REPOSITORY / "config/schemas/rpm-source-lock.schema.json"
    )
    parser.add_argument(
        "--schema", type=Path, default=REPOSITORY / "config/schemas/source-bundle-manifest.schema.json"
    )
    arguments = parser.parse_args(argv)
    try:
        release = BUILD["load_schema_document"](arguments.release, arguments.release_schema)
        lock = BUILD["load_schema_document"](arguments.lock, arguments.lock_schema)
        RPM["validate_lock"](
            release,
            BUILD["load_schema_document"](
                REPOSITORY / "evidence/sources/rpm-source-requirements.json",
                REPOSITORY / "config/schemas/rpm-source-requirements.schema.json",
            ),
            lock,
        )
        schema = STRICT["load_json"](arguments.schema)
        document = assemble(
            release, lock, arguments.root, arguments.source_commit, schema
        )
        require(
            not arguments.output.exists() and not arguments.output.is_symlink(),
            "source bundle manifest output exists",
        )
        arguments.output.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(str(arguments.output), 0o644)
    except (KeyError, OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "assembled source bundle manifest: %d entries, %d bytes"
        % (document["summary"]["entries"], document["summary"]["bytes"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
