#!/usr/bin/env python3
"""Verify, extract, and adapt one implemented CPython source row."""

import argparse
import hashlib
import json
import os
import re
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
ROW_CONTRACT = runpy.run_path(str(REPOSITORY / "scripts/python_row_contract.py"))
ContractError = ROW_CONTRACT["ContractError"]


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
    try:
        binding = ROW_CONTRACT["bind_release"](config, row=row)
    except ContractError as error:
        raise PreparationError(str(error)) from error
    entry = binding["entry"]
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
    if (
        relative.is_absolute()
        or relative.parts[:2] != ("patches", "cpython")
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise PreparationError("unsafe CPython patch path: %s" % patch["file"])
    root = repository.resolve()
    candidate = root.joinpath(*relative.parts)
    try:
        path = candidate.resolve()
    except (OSError, RuntimeError) as error:
        raise PreparationError(
            "cannot resolve CPython patch: %s" % patch["file"]
        ) from error
    try:
        path.relative_to(root)
    except ValueError as error:
        raise PreparationError(
            "CPython patch escapes repository: %s" % patch["file"]
        ) from error
    if path != candidate or not path.is_file():
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


def validate_sysconfig_isolation(source_root, version):
    sysconfig_candidates = (
        "Lib/sysconfig.py",
        "Lib/sysconfig/__init__.py",
    )
    sysconfig_paths = [
        relative
        for relative in sysconfig_candidates
        if (source_root / relative).is_file()
    ]
    if len(sysconfig_paths) != 1:
        raise PreparationError(
            "CPython %s has an ambiguous sysconfig source layout" % version
        )
    paths = {
        "configure": source_root / "configure",
        "configure.ac": source_root / "configure.ac",
        "sysconfig": source_root / sysconfig_paths[0],
    }
    try:
        contents = {
            name: path.read_text(encoding="utf-8") for name, path in paths.items()
        }
    except (OSError, UnicodeDecodeError) as error:
        raise PreparationError(
            "CPython %s isolation source is incomplete: %s" % (version, error)
        ) from error

    for name in ("configure", "configure.ac"):
        assignments = [
            line.strip()
            for line in contents[name].splitlines()
            if line.strip().startswith("PYTHON_FOR_BUILD=")
            and "_PYTHON_PROJECT_BASE=" in line
        ]
        require_isolated = len(assignments) == 1 and (
            "PYTHONPATH=$(srcdir)/Lib" in assignments[0]
            and "_PYTHON_SYSCONFIGDATA_PATH=" in assignments[0]
            and "PYTHONPATH=$(shell" not in assignments[0]
            and "`cat pybuilddir.txt`:)$(srcdir)/Lib" not in assignments[0]
        )
        if not require_isolated:
            raise PreparationError(
                "CPython %s %s lacks isolated build-Python sysconfig"
                % (version, name)
            )
    # Do not parse a newer CPython standard library with the Rocky 8 host's
    # Python 3.6 parser.  Limit the check to the exact initializer affected by
    # gh-115382 and recognize its required operations independent of grammar
    # additions in the target Python release.
    sysconfig = contents["sysconfig"]
    start = sysconfig.find("def _init_posix(vars):")
    end = sysconfig.find("def _init_non_posix(vars):", start)
    if start < 0:
        raise PreparationError(
            "CPython %s sysconfig lacks the POSIX initializer" % version
        )
    if end < 0:
        end = len(sysconfig)
    initializer = sysconfig[start:end]
    machinery_import = re.search(
        r"^\s+from importlib\.machinery import ([^\n]+)$",
        initializer,
        re.MULTILINE,
    )
    imported_names = set()
    if machinery_import is not None:
        imported_names = {
            item.strip() for item in machinery_import.group(1).split(",")
        }
    required_patterns = (
        r"os\.environ\.get\((['\"])_PYTHON_SYSCONFIGDATA_PATH\1\)",
        r"FileFinder\s*\(\s*path\s*,\s*\(\s*SourceFileLoader\s*,\s*"
        r"SOURCE_SUFFIXES\s*\)\s*\)\.find_spec\s*\(\s*name\s*\)",
        r"from importlib\.util import module_from_spec",
        r"module_from_spec\s*\(\s*spec\s*\)",
        r"spec\.loader\.exec_module\s*\(\s*_temp\s*\)",
    )
    if (
        not {"FileFinder", "SourceFileLoader", "SOURCE_SUFFIXES"}.issubset(
            imported_names
        )
        or any(re.search(pattern, initializer) is None for pattern in required_patterns)
    ):
        raise PreparationError(
            "CPython %s sysconfig lacks isolated target-data loading" % version
        )


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
        validate_sysconfig_isolation(source_root, entry["version"])
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
