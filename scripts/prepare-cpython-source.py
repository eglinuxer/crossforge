#!/usr/bin/env python3
"""Verify, extract, and adapt one implemented CPython source row."""

import argparse
import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
RELEASE_VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = RELEASE_VALIDATOR["ValidationError"]
load_json = RELEASE_VALIDATOR["load_json"]
validate = RELEASE_VALIDATOR["validate"]
validate_schema_subset = RELEASE_VALIDATOR["validate_schema_subset"]

# Rows are enabled deliberately after their two target qualifications pass. A
# source entry in release.json alone is not an implementation claim.
IMPLEMENTED_ROWS = {
    "cp311": ("3.11", "transition"),
    "cp313": ("3.13", "modern"),
}


class PreparationError(Exception):
    pass


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def row_for(config, row):
    contract = IMPLEMENTED_ROWS.get(row)
    if contract is None:
        raise PreparationError("CPython row is not implemented: %s" % row)
    expected_minor, expected_adapter = contract
    matches = []
    for entry in config["python"]["versions"]:
        version = entry["version"]
        if version.rsplit(".", 1)[0] == expected_minor:
            matches.append(entry)
    if len(matches) != 1:
        raise PreparationError("CPython row is not unique: %s" % row)
    entry = matches[0]
    if entry["adapter"] != expected_adapter:
        raise PreparationError(
            "CPython row adapter mismatch: %s/%s" % (row, entry["adapter"])
        )
    compact = expected_minor.replace(".", "")
    if row != "cp" + compact:
        raise PreparationError("CPython row name differs from version: %s" % row)
    if entry["source"]["status"] != "locked":
        raise PreparationError("CPython source is not locked: %s" % row)
    return entry


def verify_archive(archive, source):
    if archive.is_symlink() or not archive.is_file():
        raise PreparationError("missing CPython archive: %s" % archive)
    digest, size = sha256_file(archive)
    if size != source["size"]:
        raise PreparationError("CPython archive size mismatch: %s" % archive)
    if digest != source["sha256"]:
        raise PreparationError("CPython archive SHA256 mismatch: %s" % archive)


def checked_member_path(member, root):
    path = PurePosixPath(member.name)
    if path.is_absolute() or not path.parts or path.parts[0] != root:
        raise PreparationError("archive member is outside %s: %s" % (root, member.name))
    if any(part in ("", ".", "..") for part in path.parts):
        raise PreparationError("archive member has an unsafe path: %s" % member.name)
    if not (member.isdir() or member.isfile()):
        raise PreparationError("archive member has an unsupported type: %s" % member.name)
    return path


def extract_archive(archive, temporary, version):
    root = "Python-%s" % version
    seen = set()
    timestamps = []
    with tarfile.open(str(archive), mode="r:xz") as source:
        members = source.getmembers()
        if not members:
            raise PreparationError("CPython archive is empty")
        for member in members:
            relative = checked_member_path(member, root)
            key = str(relative)
            if key in seen:
                raise PreparationError("duplicate archive member: %s" % key)
            seen.add(key)
            destination = temporary.joinpath(*relative.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                extracted = source.extractfile(member)
                if extracted is None:
                    raise PreparationError("cannot read archive member: %s" % key)
                with extracted, destination.open("xb") as output:
                    shutil.copyfileobj(extracted, output, 1024 * 1024)
            os.chmod(str(destination), member.mode & 0o777)
            timestamps.append((destination, member.mtime))
    source_root = temporary / root
    if not (source_root / "configure").is_file():
        raise PreparationError("CPython archive has no configure script")
    for path, timestamp in reversed(timestamps):
        os.utime(str(path), (timestamp, timestamp), follow_symlinks=False)
    return source_root


def patch_path(repository, patch):
    relative = PurePosixPath(patch["file"])
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise PreparationError("unsafe CPython patch path: %s" % patch["file"])
    path = repository.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise PreparationError("missing CPython patch: %s" % path)
    digest, unused_size = sha256_file(path)
    if digest != patch["sha256"]:
        raise PreparationError("CPython patch SHA256 mismatch: %s" % path)
    return path


def apply_patches(source_root, patches, repository):
    applied = []
    for patch in patches:
        path = patch_path(repository, patch)
        result = subprocess.run(
            ["patch", "-p1", "--batch", "--forward", "--fuzz=0", "-i", str(path)],
            cwd=str(source_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        if result.returncode != 0:
            raise PreparationError(
                "failed to apply CPython patch %s:\n%s" % (patch["file"], result.stdout)
            )
        applied.append({"file": patch["file"], "sha256": patch["sha256"]})
    return applied


def fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare(config, row, archive, destination, manifest, repository):
    entry = row_for(config, row)
    source = entry["source"]
    verify_archive(archive, source)
    if destination.exists() or destination.is_symlink():
        raise PreparationError("CPython destination already exists: %s" % destination)
    if manifest.exists() or manifest.is_symlink():
        raise PreparationError("CPython manifest already exists: %s" % manifest)
    if manifest == destination or destination in manifest.parents:
        raise PreparationError("CPython manifest must be outside the source tree")
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".%s." % destination.name, dir=str(destination.parent))
    )
    manifest_temporary = None
    published_source = False
    published_manifest = False
    try:
        source_root = extract_archive(archive, temporary, entry["version"])
        applied = apply_patches(source_root, entry.get("patches", []), repository)
        minor = entry["version"].rsplit(".", 1)[0]
        identity = {
            "schema_version": 1,
            "kind": "crossforge-cpython-source-row",
            "row": row,
            "version": entry["version"],
            "minor": minor,
            "compact": minor.replace(".", ""),
            "adapter": entry["adapter"],
            "support": entry["support"],
            "release_sha256": canonical_sha256(config),
            "source": {
                "url": source["url"],
                "size": source["size"],
                "sha256": source["sha256"],
            },
            "patches": applied,
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix=".%s." % manifest.name,
            suffix=".tmp",
            dir=str(manifest.parent),
            delete=False,
            encoding="utf-8",
        ) as stream:
            manifest_temporary = Path(stream.name)
            stream.write(
                json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())

        source_root.rename(destination)
        published_source = True
        os.replace(str(manifest_temporary), str(manifest))
        manifest_temporary = None
        published_manifest = True
        fsync_directory(destination.parent)
        if manifest.parent != destination.parent:
            fsync_directory(manifest.parent)
    except BaseException:
        if published_manifest:
            try:
                manifest.unlink()
            except OSError:
                pass
        if published_source:
            shutil.rmtree(str(destination), ignore_errors=True)
        if manifest_temporary is not None:
            try:
                manifest_temporary.unlink()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(str(temporary), ignore_errors=True)
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--row", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=REPOSITORY / "config/release.json")
    parser.add_argument(
        "--schema", type=Path, default=REPOSITORY / "config/schemas/release.schema.json"
    )
    arguments = parser.parse_args()
    try:
        config = load_json(arguments.config)
        schema = load_json(arguments.schema)
        validate_schema_subset(schema)
        validate(config, schema, schema, "$")
        identity = prepare(
            config,
            arguments.row,
            arguments.archive,
            arguments.destination,
            arguments.manifest,
            REPOSITORY,
        )
    except (OSError, PreparationError, ValidationError, tarfile.TarError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "prepared: CPython %s (%s)" % (identity["version"], identity["row"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
