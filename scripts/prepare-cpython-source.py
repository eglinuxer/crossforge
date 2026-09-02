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
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
RELEASE_VALIDATOR = None
ROW_CONTRACT = None
COMPONENT_READER = None
EXACT_VERSION = re.compile(r"^3\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
ROW_NAME = re.compile(r"^cp[0-9]+\Z")
SHA256 = re.compile(r"^[0-9a-f]{64}\Z")


class PreparationError(Exception):
    pass


def release_validator():
    global RELEASE_VALIDATOR
    if RELEASE_VALIDATOR is None:
        RELEASE_VALIDATOR = runpy.run_path(
            str(SCRIPT_DIRECTORY / "validate-release.py")
        )
    return RELEASE_VALIDATOR


def row_contract_tools():
    global ROW_CONTRACT
    if ROW_CONTRACT is None:
        ROW_CONTRACT = runpy.run_path(
            str(SCRIPT_DIRECTORY / "python_row_contract.py")
        )
    return ROW_CONTRACT


def component_reader():
    global COMPONENT_READER
    if COMPONENT_READER is None:
        COMPONENT_READER = runpy.run_path(
            str(SCRIPT_DIRECTORY / "release_component.py")
        )
    return COMPONENT_READER


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
    tools = row_contract_tools()
    try:
        binding = tools["bind_release"](config, row=row)
    except tools["ContractError"] as error:
        raise PreparationError(str(error)) from error
    entry = binding["entry"]
    if entry["source"]["status"] != "locked":
        raise PreparationError("CPython source is not locked: %s" % row)
    return entry


def _component_materials(reader, document):
    materials = {}
    for record in document["materials"]:
        path = reader["decode_json_pointer"](record["path"])
        if path in materials:
            raise PreparationError("component repeats a material path")
        materials[path] = record["value"]
    return materials


def _require_material(materials, path, expected_type, label):
    if path not in materials:
        raise PreparationError("component is missing %s" % label)
    value = materials[path]
    if type(value) is not expected_type:
        raise PreparationError("component %s has the wrong JSON type" % label)
    return value


def _load_component(path, name, digest):
    reader = component_reader()
    try:
        return reader["load_component"](path, name, "build", digest)
    except reader["ComponentError"] as error:
        raise PreparationError("invalid %s component: %s" % (name, error)) from error


def _policy_from_component(row, path, digest):
    if ROW_NAME.match(row) is None:
        raise PreparationError("invalid CPython row: %s" % row)
    name = "implementation/python-%s-build-policy" % row
    document = _load_component(path, name, digest)
    if document["dependencies"] != []:
        raise PreparationError("Python build policy must not have dependencies")
    reader = component_reader()
    materials = _component_materials(reader, document)
    prefix = ("@implementation", "python_rows", row)
    expected_paths = {
        prefix + ("minor",),
        prefix + ("row",),
        prefix + ("adapter",),
        prefix + ("sysconfig_isolation",),
    }
    if set(materials) != expected_paths:
        raise PreparationError("Python build policy material set differs")
    policy = {
        "component": name,
        "canonical_sha256": digest,
        "minor": _require_material(materials, prefix + ("minor",), str, "minor"),
        "row": _require_material(materials, prefix + ("row",), str, "row"),
        "adapter": _require_material(
            materials, prefix + ("adapter",), str, "adapter"
        ),
        "sysconfig_isolation": _require_material(
            materials,
            prefix + ("sysconfig_isolation",),
            bool,
            "sysconfig isolation",
        ),
    }
    if (
        policy["row"] != row
        or policy["minor"] != row[2:3] + "." + row[3:]
        or policy["adapter"] not in ("legacy", "transition", "modern")
        or policy["sysconfig_isolation"] is not True
    ):
        raise PreparationError("Python build policy differs from row contract")
    return policy, document


def _patches_from_materials(materials, prefix, minor):
    empty_path = prefix + ("patches",)
    patch_paths = [path for path in materials if path[:1] == ("patches",)]
    if empty_path in materials:
        if materials[empty_path] != [] or patch_paths != [empty_path]:
            raise PreparationError("component empty patch material is invalid")
        return []
    records = {}
    for relative in patch_paths:
        if (
            len(relative) != 3
            or not relative[1].isdigit()
            or str(int(relative[1])) != relative[1]
            or relative[2] not in ("file", "sha256")
        ):
            raise PreparationError("component patch material path is invalid")
        records.setdefault(int(relative[1]), {})[relative[2]] = materials[relative]
    if sorted(records) != list(range(len(records))):
        raise PreparationError("component patch indexes are not contiguous")
    patches = []
    for index in range(len(records)):
        record = records[index]
        if set(record) != {"file", "sha256"}:
            raise PreparationError("component patch fields differ")
        if type(record["file"]) is not str or type(record["sha256"]) is not str:
            raise PreparationError("component patch fields have wrong JSON types")
        relative = PurePosixPath(record["file"])
        if (
            relative.is_absolute()
            or relative.parts[:3] != ("patches", "cpython", minor)
            or len(relative.parts) != 4
            or any(part in ("", ".", "..") for part in relative.parts)
            or str(relative) != record["file"]
            or not relative.name.endswith(".patch")
            or SHA256.match(record["sha256"]) is None
        ):
            raise PreparationError("component patch path/hash is invalid")
        patches.append({"file": record["file"], "sha256": record["sha256"]})
    return patches


def row_from_components(
    row,
    expected_version,
    expected_adapter,
    source_component,
    source_component_sha256,
    policy_component,
    policy_component_sha256,
):
    if (
        type(expected_version) is not str
        or EXACT_VERSION.match(expected_version) is None
        or type(expected_adapter) is not str
        or expected_adapter not in ("legacy", "transition", "modern")
    ):
        raise PreparationError("invalid expected CPython version/adapter")
    expected_minor = expected_version.rsplit(".", 1)[0]
    if row != "cp" + expected_minor.replace(".", ""):
        raise PreparationError("expected CPython row/version mismatch")
    policy, _policy_document = _policy_from_component(
        row, policy_component, policy_component_sha256
    )
    source_name = "python/%s-source" % row
    document = _load_component(
        source_component, source_name, source_component_sha256
    )
    expected_dependency = [
        {
            "component": policy["component"],
            "canonical_sha256": policy_component_sha256,
        }
    ]
    if document["dependencies"] != expected_dependency:
        raise PreparationError("Python source component policy dependency differs")
    reader = component_reader()
    materials = _component_materials(reader, document)
    indexes = {
        path[2]
        for path in materials
        if len(path) >= 4 and path[:2] == ("python", "versions")
    }
    if len(indexes) != 1 or not next(iter(indexes)).isdigit():
        raise PreparationError("Python source component must select one version prefix")
    prefix = ("python", "versions", next(iter(indexes)))
    relative_materials = {
        path[len(prefix):]: value
        for path, value in materials.items()
        if path[: len(prefix)] == prefix
    }
    if len(relative_materials) != len(materials):
        raise PreparationError("Python source component has unrelated materials")
    version = _require_material(
        relative_materials, ("version",), str, "version"
    )
    adapter = _require_material(
        relative_materials, ("adapter",), str, "adapter"
    )
    source = {
        field: _require_material(
            relative_materials,
            ("source", field),
            int if field == "size" else str,
            "source %s" % field,
        )
        for field in ("status", "url", "sha256", "size")
    }
    patches = _patches_from_materials(relative_materials, (), policy["minor"])
    allowed = {
        ("version",),
        ("adapter",),
        ("source", "status"),
        ("source", "url"),
        ("source", "sha256"),
        ("source", "size"),
    }
    if patches:
        for index in range(len(patches)):
            allowed.add(("patches", str(index), "file"))
            allowed.add(("patches", str(index), "sha256"))
    else:
        allowed.add(("patches",))
    if set(relative_materials) != allowed:
        raise PreparationError("Python source component material set differs")
    minor = version.rsplit(".", 1)[0] if EXACT_VERSION.match(version) else ""
    if (
        source["status"] != "locked"
        or not source["url"].startswith("https://")
        or SHA256.match(source["sha256"]) is None
        or type(source["size"]) is not int
        or source["size"] <= 0
        or adapter != policy["adapter"]
        or version != expected_version
        or adapter != expected_adapter
        or minor != policy["minor"]
        or row != "cp" + minor.replace(".", "")
    ):
        raise PreparationError("Python source component differs from build policy")
    entry = {
        "version": version,
        "adapter": adapter,
        "source": source,
        "patches": patches,
    }
    identities = {
        "source_component": {
            "component": source_name,
            "canonical_sha256": source_component_sha256,
        },
        "build_policy": policy,
    }
    return entry, identities


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


def _function_block(source, name, parameters, version):
    definition = re.compile(
        r"^def[ \t]+%s[ \t]*\([ \t]*%s[ \t]*\)[ \t]*:[ \t]*(?:#.*)?$"
        % (re.escape(name), parameters),
        re.MULTILINE,
    )
    matches = list(definition.finditer(source))
    if len(matches) != 1:
        raise PreparationError(
            "CPython %s sysconfig must define %s exactly once" % (version, name)
        )
    start = matches[0].start()
    following = re.search(r"^(?:def|class)[ \t]+", source[matches[0].end():], re.MULTILINE)
    end = len(source) if following is None else matches[0].end() + following.start()
    return source[start:end]


def _semantic_source(source):
    """Remove comment-only and triple-quoted text without parsing target syntax."""
    result = []
    quote = None
    triple = re.compile(r"(?:[rRuUbBfF]{0,2})?(\"\"\"|''')")
    for line in source.splitlines():
        if quote is not None:
            if quote in line:
                quote = None
            result.append("")
            continue
        if line.lstrip().startswith("#"):
            result.append("")
            continue
        match = triple.search(line)
        if match is not None:
            delimiter = match.group(1)
            remainder = line[match.end():]
            if delimiter not in remainder:
                quote = delimiter
            result.append("")
            continue
        result.append(line)
    if quote is not None:
        raise PreparationError("CPython sysconfig has an unterminated triple-quoted string")
    return "\n".join(result)


def _validate_filefinder_sysconfig(sysconfig, version):
    initializer = _function_block(sysconfig, "_init_posix", r"vars", version)
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


def _validate_pathfinder_sysconfig(sysconfig, version):
    semantic = _semantic_source(sysconfig)
    importer = _function_block(
        semantic, "_import_from_directory", r"path[ \t]*,[ \t]*name", version
    )
    getter = _function_block(semantic, "_get_sysconfigdata", r"", version)
    initializer = _function_block(semantic, "_init_posix", r"vars", version)

    environment_read = (
        r"os\.environ\.get\([ \t]*(['\"])"
        r"_PYTHON_SYSCONFIGDATA_PATH\1[ \t]*\)"
    )
    if len(re.findall(environment_read, semantic)) != 1 or re.search(
        r"^[ \t]+path[ \t]*=[ \t]*%s[ \t]*$" % environment_read,
        getter,
        re.MULTILINE,
    ) is None:
        raise PreparationError(
            "CPython %s sysconfig must read the target-data path exactly once"
            % version
        )

    importer_patterns = (
        r"^[ \t]+spec[ \t]*=[ \t]*importlib\.machinery\.PathFinder\.find_spec"
        r"\([ \t]*name[ \t]*,[ \t]*\[[ \t]*path[ \t]*\][ \t]*\)[ \t]*$",
        r"^[ \t]+module[ \t]*=[ \t]*importlib\.util\.module_from_spec"
        r"\([ \t]*spec[ \t]*\)[ \t]*$",
        r"^[ \t]+spec\.loader\.exec_module\([ \t]*module[ \t]*\)[ \t]*$",
        r"^[ \t]+sys\.modules\[[ \t]*name[ \t]*\][ \t]*=[ \t]*module[ \t]*$",
        r"^[ \t]+return[ \t]+sys\.modules\[[ \t]*name[ \t]*\][ \t]*$",
    )
    if any(
        re.search(pattern, importer, re.MULTILINE) is None
        for pattern in importer_patterns
    ) or any(
        len(re.findall(pattern, importer)) != 1
        for pattern in (
            r"importlib\.machinery\.PathFinder\.find_spec\s*\(",
            r"importlib\.util\.module_from_spec\s*\(",
            r"spec\.loader\.exec_module\s*\(",
        )
    ) or re.search(r"(?:sys\.path|import_module\s*\()", importer):
        raise PreparationError(
            "CPython %s sysconfig lacks explicit PathFinder target-data loading"
            % version
        )

    branch = (
        r"^[ \t]+module[ \t]*=[ \t]*_import_from_directory"
        r"\([ \t]*path[ \t]*,[ \t]*name[ \t]*\)[ \t]+if[ \t]+path[ \t]+else[ \t]+"
        r"importlib\.import_module\([ \t]*name[ \t]*\)[ \t]*$"
    )
    if (
        re.search(branch, getter, re.MULTILINE) is None
        or len(re.findall(r"_import_from_directory\s*\(", getter)) != 1
        or len(re.findall(r"importlib\.import_module\s*\(", getter)) != 1
        or re.search(
            r"^[ \t]+return[ \t]+module\.build_time_vars[ \t]*$",
            getter,
            re.MULTILINE,
        )
        is None
    ):
        raise PreparationError(
            "CPython %s sysconfig target-data path/fallback branch differs" % version
        )

    if (
        re.search(
            r"^[ \t]+vars\.update\([ \t]*_get_sysconfigdata\(\)"
            r"(?:[ \t]*\|[ \t]*vars)?[ \t]*\)[ \t]*$",
            initializer,
            re.MULTILINE,
        )
        is None
        or len(re.findall(r"_get_sysconfigdata\s*\(\s*\)", initializer)) != 1
    ):
        raise PreparationError(
            "CPython %s sysconfig POSIX initializer ignores isolated target data"
            % version
        )


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
    # Python 3.6 parser.  The 3.11-3.13 profile performs the gh-115382 loading
    # directly in _init_posix.  CPython 3.14 moved it behind _get_sysconfigdata
    # and uses PathFinder with an explicit one-element search path.
    sysconfig = contents["sysconfig"]
    if version.startswith("3.14."):
        _validate_pathfinder_sysconfig(sysconfig, version)
    else:
        _validate_filefinder_sysconfig(sysconfig, version)


def fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _source_manifest(entry, row, applied, context):
    source = entry["source"]
    minor = entry["version"].rsplit(".", 1)[0]
    common = {
        "kind": "crossforge-cpython-source-row",
        "row": row,
        "version": entry["version"],
        "minor": minor,
        "compact": minor.replace(".", ""),
        "adapter": entry["adapter"],
        "source": {
            "url": source["url"],
            "size": source["size"],
            "sha256": source["sha256"],
        },
        "patches": applied,
    }
    if context["mode"] == "release":
        common.update(
            {
                "schema_version": 1,
                "support": entry["support"],
                "release_sha256": canonical_sha256(context["release"]),
            }
        )
    elif context["mode"] == "component":
        common.update(
            {
                "schema_version": 2,
                "source_component": context["source_component"],
                "build_policy": context["build_policy"],
            }
        )
    else:
        raise PreparationError("unsupported CPython source manifest mode")
    return common


def _prepare_entry(entry, row, archive, destination, manifest, repository, context):
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
        identity = _source_manifest(entry, row, applied, context)
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


def prepare(config, row, archive, destination, manifest, repository):
    entry = row_for(config, row)
    return _prepare_entry(
        entry,
        row,
        archive,
        destination,
        manifest,
        repository,
        {"mode": "release", "release": config},
    )


def prepare_component(
    row,
    version,
    adapter,
    archive,
    destination,
    manifest,
    repository,
    source_component,
    source_component_sha256,
    policy_component,
    policy_component_sha256,
):
    entry, identities = row_from_components(
        row,
        version,
        adapter,
        source_component,
        source_component_sha256,
        policy_component,
        policy_component_sha256,
    )
    context = {"mode": "component"}
    context.update(identities)
    return _prepare_entry(
        entry, row, archive, destination, manifest, repository, context
    )


def load_release_configuration(config_path, schema_path):
    tools = release_validator()
    try:
        config = tools["load_json"](config_path)
        schema = tools["load_json"](schema_path)
        tools["validate_schema_subset"](schema)
        tools["validate"](config, schema, schema, "$")
    except tools["ValidationError"] as error:
        raise PreparationError(str(error)) from error
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--row", required=True)
    parser.add_argument("--version")
    parser.add_argument("--adapter")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--source-component", type=Path)
    parser.add_argument("--source-component-sha256")
    parser.add_argument("--policy-component", type=Path)
    parser.add_argument("--policy-component-sha256")
    arguments = parser.parse_args()
    try:
        component_values = (
            arguments.source_component,
            arguments.source_component_sha256,
            arguments.policy_component,
            arguments.policy_component_sha256,
        )
        component_mode = any(value is not None for value in component_values)
        if component_mode and not all(value is not None for value in component_values):
            raise PreparationError(
                "source and policy component files/digests must be provided together"
            )
        if component_mode and (
            arguments.config is not None or arguments.schema is not None
        ):
            raise PreparationError(
                "component inputs and full release inputs are mutually exclusive"
            )
        if component_mode and (
            arguments.version is None or arguments.adapter is None
        ):
            raise PreparationError(
                "component mode requires expected version and adapter"
            )
        if not component_mode and (
            arguments.version is not None or arguments.adapter is not None
        ):
            raise PreparationError(
                "expected version/adapter are component-mode inputs"
            )
        if component_mode:
            identity = prepare_component(
                arguments.row,
                arguments.version,
                arguments.adapter,
                arguments.archive,
                arguments.destination,
                arguments.manifest,
                REPOSITORY,
                arguments.source_component,
                arguments.source_component_sha256,
                arguments.policy_component,
                arguments.policy_component_sha256,
            )
        else:
            config_path = arguments.config or REPOSITORY / "config/release.json"
            schema_path = (
                arguments.schema
                or REPOSITORY / "config/schemas/release.schema.json"
            )
            config = load_release_configuration(config_path, schema_path)
            identity = prepare(
                config,
                arguments.row,
                arguments.archive,
                arguments.destination,
                arguments.manifest,
                REPOSITORY,
            )
    except (OSError, PreparationError, tarfile.TarError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "prepared: CPython %s (%s)" % (identity["version"], identity["row"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
