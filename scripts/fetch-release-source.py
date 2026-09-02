#!/usr/bin/env python3
"""Fetch a content-locked source from a release or exact component input."""

import argparse
import hashlib
import os
import re
import runpy
import stat
import sys
import tempfile
import urllib.request
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_CONFIG = REPOSITORY / "config/release.json"
DEFAULT_SCHEMA = REPOSITORY / "config/schemas/release.schema.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
SOURCE_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+\Z")
PYTHON_COMPONENT_RE = re.compile(r"^python/(cp[0-9]+)-source\Z")
PYTHON_VERSION_RE = re.compile(
    r"^3\.((?:0|[1-9][0-9]*))\.((?:0|[1-9][0-9]*))\Z"
)
PYTHON_POINTER_RE = re.compile(
    r"^/python/versions/([0-9]+)/(?:version|source/(?:status|url|sha256|size))\Z"
)
_RELEASE_TOOLS = None
_COMPONENT_READER = None


class ValidationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def release_tools():
    """Load full-release validation only in the maintenance input mode."""
    global _RELEASE_TOOLS
    if _RELEASE_TOOLS is None:
        _RELEASE_TOOLS = runpy.run_path(
            str(SCRIPT_DIRECTORY / "validate-release.py")
        )
    return _RELEASE_TOOLS


def component_reader():
    global _COMPONENT_READER
    if _COMPONENT_READER is None:
        _COMPONENT_READER = runpy.run_path(
            str(SCRIPT_DIRECTORY / "release_component.py")
        )
    return _COMPONENT_READER


def source_for(config, component, version=None):
    if component == "python":
        matches = [
            entry
            for entry in config["python"]["versions"]
            if entry["version"] == version
        ]
        if len(matches) != 1:
            raise ValidationError("Python source version is not unique: %s" % version)
        source = matches[0]["source"]
    elif component == "zstd":
        selected = config["python"]["zstd"]
        if version is not None and selected["version"] != version:
            raise ValidationError("zstd source version differs: %s" % version)
        source = selected["source"]
    else:
        if version is not None:
            raise ValidationError("--version is only valid for Python or zstd sources")
        source = config["gts" if component == "gcc" else "binutils"]["source"]
    if source["status"] != "locked":
        raise ValidationError("%s source is not locked" % component)
    if source["size"] <= 0:
        raise ValidationError("%s source size must be positive" % component)
    return source


def _component_material(
    reader,
    document,
    expected_component,
    expected_scope,
    expected_sha256,
    pointer,
    expected_type,
):
    try:
        return reader["material_value"](
            document,
            expected_component,
            expected_scope,
            expected_sha256,
            pointer,
            expected_type,
        )
    except reader["ComponentError"] as error:
        raise ValidationError(str(error)) from error


def _validate_source_values(source, label):
    require(source["status"] == "locked", "%s source is not locked" % label)
    require(
        source["url"].startswith("https://"),
        "%s source URL must use HTTPS" % label,
    )
    require(
        SHA256_RE.match(source["sha256"]),
        "%s source SHA256 is invalid" % label,
    )
    require(source["size"] > 0, "%s source size must be positive" % label)
    return source


def _python_source_base(document):
    indices = set()
    for material in document["materials"]:
        match = PYTHON_POINTER_RE.match(material["path"])
        if match:
            indices.add(match.group(1))
    require(indices, "Python component is missing its source material set")
    require(
        len(indices) == 1,
        "Python component has ambiguous source material sets",
    )
    return "/python/versions/%s" % next(iter(indices))


def source_for_component(
    path,
    expected_component,
    expected_scope,
    expected_sha256,
    source_kind,
    version=None,
):
    """Read one authenticated source projection without loading release.json."""
    require(expected_scope == "build", "source component scope must be build")
    require(
        source_kind in ("gcc", "binutils", "python", "zstd"),
        "unsupported source kind: %r" % source_kind,
    )
    reader = component_reader()
    try:
        document = reader["load_component"](
            path,
            expected_component,
            expected_scope,
            expected_sha256,
        )
    except reader["ComponentError"] as error:
        raise ValidationError(str(error)) from error

    if source_kind == "gcc":
        require(version is None, "--version is only valid for Python or zstd sources")
        require(
            expected_component == "sources/gcc",
            "GCC source requires component sources/gcc",
        )
        base = "/gts/source"
        source_base = base
        component_version = _component_material(
            reader,
            document,
            expected_component,
            expected_scope,
            expected_sha256,
            "/gts/gcc_version",
            "string",
        )
    elif source_kind == "binutils":
        require(version is None, "--version is only valid for Python or zstd sources")
        require(
            expected_component == "sources/binutils",
            "binutils source requires component sources/binutils",
        )
        base = "/binutils/source"
        source_base = base
        component_version = _component_material(
            reader,
            document,
            expected_component,
            expected_scope,
            expected_sha256,
            "/binutils/version",
            "string",
        )
    elif source_kind == "python":
        require(version is not None, "Python source fetch requires --version")
        match = PYTHON_COMPONENT_RE.match(expected_component)
        require(match is not None, "invalid Python source component name")
        require(type(version) is str, "Python source version must be a string")
        version_match = PYTHON_VERSION_RE.match(version)
        require(
            version_match is not None,
            "invalid Python source version: %s" % version,
        )
        expected_row = "cp3%s" % version_match.group(1)
        require(
            match.group(1) == expected_row,
            "Python component row differs from version %s" % version,
        )
        base = _python_source_base(document)
        source_base = base + "/source"
        component_version = _component_material(
            reader,
            document,
            expected_component,
            expected_scope,
            expected_sha256,
            base + "/version",
            "string",
        )
        require(
            component_version == version,
            "Python component version differs: expected %s, found %s"
            % (version, component_version),
        )
    else:
        require(version is not None, "zstd source fetch requires --version")
        require(
            expected_component == "sources/zstd",
            "zstd source requires component sources/zstd",
        )
        require(
            type(version) is str and SOURCE_VERSION_RE.match(version),
            "invalid zstd source version: %s" % version,
        )
        base = "/python/zstd"
        source_base = base + "/source"
        component_version = _component_material(
            reader,
            document,
            expected_component,
            expected_scope,
            expected_sha256,
            base + "/version",
            "string",
        )
        require(
            component_version == version,
            "zstd component version differs: expected %s, found %s"
            % (version, component_version),
        )

    require(
        SOURCE_VERSION_RE.match(component_version),
        "%s component version is invalid" % source_kind,
    )
    source = {}
    for field, expected_type in (
        ("status", "string"),
        ("url", "string"),
        ("sha256", "string"),
        ("size", "integer"),
    ):
        source[field] = _component_material(
            reader,
            document,
            expected_component,
            expected_scope,
            expected_sha256,
            source_base + "/" + field,
            expected_type,
        )
    _validate_source_values(source, source_kind)
    if source_kind == "python":
        expected_url = "https://www.python.org/ftp/python/%s/Python-%s.tar.xz" % (
            version,
            version,
        )
        require(
            source["url"] == expected_url,
            "Python source URL differs from version %s" % version,
        )
    elif source_kind == "zstd":
        expected_url = (
            "https://github.com/facebook/zstd/releases/download/v%s/"
            "zstd-%s.tar.gz" % (version, version)
        )
        require(
            source["url"] == expected_url,
            "zstd source URL differs from version %s" % version,
        )
    return source


def load_maintenance_source(config_path, schema_path, source_kind, version):
    tools = release_tools()
    try:
        config = tools["load_json"](Path(config_path))
        schema = tools["load_json"](Path(schema_path))
        tools["validate_schema_subset"](schema)
        tools["validate"](config, schema, schema, "$")
    except tools["ValidationError"] as error:
        raise ValidationError(str(error)) from error
    if source_kind == "python" and not version:
        raise ValidationError("Python source fetch requires --version")
    return source_for(config, source_kind, version)


def _lstat(path):
    try:
        return os.lstat(str(path))
    except FileNotFoundError:
        return None


def _absolute_path(path):
    return Path(os.path.abspath(str(path)))


def _validate_directory_chain(directory, require_complete):
    """Reject every existing symlink/non-directory from the absolute anchor."""
    directory = _absolute_path(directory)
    parts = directory.parts
    require(parts and directory.is_absolute(), "source directory is not absolute")
    current = Path(parts[0])
    missing = False
    for part in parts[1:]:
        current = current / part
        information = _lstat(current)
        if information is None:
            missing = True
            continue
        require(
            not stat.S_ISLNK(information.st_mode),
            "refusing symlink source directory ancestor: %s" % current,
        )
        require(
            stat.S_ISDIR(information.st_mode),
            "source directory ancestor is not a directory: %s" % current,
        )
    if require_complete:
        require(not missing, "missing source directory: %s" % directory)
        require(
            Path(os.path.realpath(str(directory))) == directory,
            "source directory real path differs: %s" % directory,
        )
    return directory


def _inspect_output(output):
    output = _absolute_path(output)
    _validate_directory_chain(output.parent, require_complete=False)
    information = _lstat(output)
    if information is not None:
        _validate_directory_chain(output.parent, require_complete=True)
        require(
            stat.S_ISREG(information.st_mode),
            "source output is not a regular file: %s" % output,
        )
    return output, information is not None


def verify(path, source):
    path, exists = _inspect_output(path)
    if not exists:
        raise ValidationError("missing source artifact: %s" % path)
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        information = os.fstat(descriptor)
        require(
            stat.S_ISREG(information.st_mode),
            "source artifact is not a regular file: %s" % path,
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if size != source["size"]:
        raise ValidationError("source size mismatch: %s" % path)
    if digest.hexdigest() != source["sha256"]:
        raise ValidationError("source SHA256 mismatch: %s" % path)


def fetch(source, output):
    output, exists = _inspect_output(output)
    if exists:
        verify(output, source)
        print("cached: %s" % output)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    safe_parent = _validate_directory_chain(
        output.parent, require_complete=True
    )
    require(safe_parent == output.parent, "source output directory changed")
    require(_lstat(output) is None, "source output appeared during preparation")
    partial = None
    request = urllib.request.Request(
        source["url"], headers={"User-Agent": "crossforge-source-fetch/1"}
    )
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    length = int(content_length)
                except ValueError:
                    raise ValidationError("invalid HTTP content length")
                if length != source["size"]:
                    raise ValidationError(
                        "HTTP content length differs from locked source identity"
                    )
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=output.name + ".",
                suffix=".part",
                dir=str(output.parent),
                delete=False,
            ) as stream:
                partial = Path(stream.name)
                require(
                    partial.parent == safe_parent,
                    "temporary source is outside the output directory",
                )
                _validate_directory_chain(safe_parent, require_complete=True)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
                    size += len(chunk)
                    if size > source["size"]:
                        raise ValidationError("source download exceeds locked size")
        verify(partial, source)
        _validate_directory_chain(safe_parent, require_complete=True)
        require(_lstat(output) is None, "source output appeared during download")
        require(
            partial.parent == safe_parent,
            "temporary source directory changed",
        )
        os.replace(str(partial), str(output))
        partial = None
        verify(output, source)
    except Exception:
        if partial is not None:
            try:
                _validate_directory_chain(safe_parent, require_complete=True)
                information = _lstat(partial)
                if (
                    partial.parent == safe_parent
                    and information is not None
                    and not stat.S_ISDIR(information.st_mode)
                ):
                    partial.unlink()
            except ValidationError:
                pass
        raise
    print("fetched: %s" % output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_kind", choices=("gcc", "binutils", "python", "zstd")
    )
    parser.add_argument("--version")
    parser.add_argument("--output", type=Path, required=True)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--config", type=Path)
    inputs.add_argument("--component-file", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--expected-component")
    parser.add_argument("--expected-scope")
    parser.add_argument("--expected-sha256")
    arguments = parser.parse_args(argv)
    try:
        if arguments.component_file is not None:
            require(
                arguments.schema is None,
                "--schema is only valid in full-release maintenance mode",
            )
            missing = [
                option
                for option, value in (
                    ("--expected-component", arguments.expected_component),
                    ("--expected-scope", arguments.expected_scope),
                    ("--expected-sha256", arguments.expected_sha256),
                )
                if value is None
            ]
            require(
                not missing,
                "component input requires %s" % ", ".join(missing),
            )
            source = source_for_component(
                arguments.component_file,
                arguments.expected_component,
                arguments.expected_scope,
                arguments.expected_sha256,
                arguments.source_kind,
                arguments.version,
            )
        else:
            unexpected = [
                option
                for option, value in (
                    ("--expected-component", arguments.expected_component),
                    ("--expected-scope", arguments.expected_scope),
                    ("--expected-sha256", arguments.expected_sha256),
                )
                if value is not None
            ]
            require(
                not unexpected,
                "full-release maintenance mode rejects %s"
                % ", ".join(unexpected),
            )
            source = load_maintenance_source(
                arguments.config or DEFAULT_CONFIG,
                arguments.schema or DEFAULT_SCHEMA,
                arguments.source_kind,
                arguments.version,
            )
        fetch(source, arguments.output)
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
