#!/usr/bin/env python3
"""Plan strict, build-system-independent split DEB/RPM packages."""

import argparse
import copy
import datetime
import hashlib
import json
import os
import posixpath
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


CONFIG_SCHEMA = "https://crossforge.dev/schemas/crosspack.schema.json"
PLAN_SCHEMA = "https://crossforge.dev/schemas/crosspack-plan.schema.json"
RESULT_SCHEMA = "https://crossforge.dev/schemas/crosspack-result.schema.json"
STAGING_SCHEMA = "https://crossforge.dev/schemas/crosspack-staging.schema.json"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")
DEB_PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
RPM_PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_~:-]*$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9+._@~-]+(?:/[A-Za-z0-9+._@~-]+)*$")
DESTINATION_RE = re.compile(
    r"^/(?:[A-Za-z0-9+._@~-]+(?:/[A-Za-z0-9+._@~-]+)*)?$"
)
OWNER_RE = re.compile(r"^(?:[a-z_][a-z0-9_-]{0,31}|[0-9]{1,10})$")
PRIORITIES = {"required", "important", "standard", "optional", "extra"}
CONFIG_TYPES = {"none", "config", "noreplace"}
PACKAGE_FORMATS = ("deb", "rpm")
DEB_RELATION_FIELDS = (
    "depends",
    "pre_depends",
    "recommends",
    "suggests",
    "conflicts",
    "provides",
    "replaces",
    "breaks",
)
RPM_RELATION_FIELDS = (
    "requires",
    "recommends",
    "suggests",
    "conflicts",
    "provides",
    "obsoletes",
)
SCRIPT_FIELDS = (
    "pre_install",
    "post_install",
    "pre_remove",
    "post_remove",
)
NFPM_SCRIPT_FIELDS = {
    "pre_install": "preinstall",
    "post_install": "postinstall",
    "pre_remove": "preremove",
    "post_remove": "postremove",
}
SCRIPT_INTERPRETER = "/bin/sh"
SCRIPT_MAX_SIZE = 1024 * 1024
INDEPENDENT_FORBIDDEN_SUFFIXES = (
    ".a",
    ".o",
    ".obj",
    ".pyc",
    ".qmlc",
    ".jsc",
    ".debug",
)
ARCHITECTURES = {
    "x86_64": {"deb": "amd64", "rpm": "x86_64", "elf_machine": 62},
    "aarch64": {"deb": "arm64", "rpm": "aarch64", "elf_machine": 183},
}
DEB_MULTIARCH_LIBRARIES = {
    "x86_64": (
        "/lib/x86_64-linux-gnu",
        "/usr/lib/x86_64-linux-gnu",
    ),
    "aarch64": (
        "/lib/aarch64-linux-gnu",
        "/usr/lib/aarch64-linux-gnu",
    ),
}
ELF = runpy.run_path(str(Path(__file__).with_name("elf.py")))
ElfError = ELF["ElfError"]


class CrosspackError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise CrosspackError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def reject_nonfinite(value):
    raise CrosspackError("non-finite JSON number: %s" % value)


def load_json(path):
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(
                stream,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_nonfinite,
            )
    except (OSError, ValueError) as error:
        raise CrosspackError("cannot load %s: %s" % (path, error)) from error
    require(isinstance(value, dict), "%s must contain a JSON object" % path)
    return value


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise CrosspackError("cannot hash %s: %s" % (path, error)) from error
    return digest.hexdigest()


def sha256_digest(value, label, nullable=False):
    if nullable and value is None:
        return None
    require(
        isinstance(value, str)
        and re.match(r"^[0-9a-f]{64}$", value) is not None,
        "%s must be a lowercase SHA256 digest" % label,
    )
    return value


def exact_keys(value, keys, label):
    require(isinstance(value, dict), "%s must be an object" % label)
    require(
        set(value) == set(keys),
        "%s keys differ: expected %s" % (label, ", ".join(sorted(keys))),
    )


def text(value, label):
    require(
        isinstance(value, str)
        and value
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value,
        "%s must be non-empty single-line text" % label,
    )


def package_description(value, label):
    require(
        isinstance(value, str)
        and 0 < len(value) <= 16384
        and "\x00" not in value
        and "\r" not in value
        and "\t" not in value
        and all(character == "\n" or ord(character) >= 32 for character in value),
        "%s must be canonical UTF-8 paragraph text" % label,
    )
    lines = value.split("\n")
    require(
        lines[0]
        and lines[-1]
        and all(line == "" or line == line.strip() for line in lines)
        and all(lines[index] or lines[index - 1] for index in range(1, len(lines))),
        "%s must use single blank lines between paragraphs" % label,
    )


def name(value, label):
    require(
        isinstance(value, str) and NAME_RE.match(value) is not None,
        "%s is not a safe lowercase name" % label,
    )


def version(value, label):
    require(
        isinstance(value, str) and VERSION_RE.match(value) is not None,
        "%s is not a safe version" % label,
    )


def selected_formats(value=None):
    if value in (None, "both"):
        return PACKAGE_FORMATS
    if isinstance(value, str):
        value = (value,)
    require(
        isinstance(value, (list, tuple))
        and value
        and len(value) == len(set(value))
        and set(value) <= set(PACKAGE_FORMATS),
        "package formats must select deb, rpm, or both",
    )
    return tuple(packager for packager in PACKAGE_FORMATS if packager in value)


def source_path(value, label):
    require(isinstance(value, str), "%s must be text" % label)
    if value == ".":
        return value
    require(
        SOURCE_RE.match(value) is not None
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in ("", ".", "..") for part in value.split("/")),
        "%s is not a canonical relative path" % label,
    )
    return value


def destination_path(value, label):
    require(
        isinstance(value, str)
        and DESTINATION_RE.match(value) is not None
        and "\\" not in value
        and posixpath.normpath(value) == value,
        "%s is not a canonical absolute package path" % label,
    )
    return value


def destination_values(value, label):
    if isinstance(value, str):
        destination = destination_path(value, label)
        return {packager: destination for packager in PACKAGE_FORMATS}
    exact_keys(value, PACKAGE_FORMATS, label)
    return {
        packager: destination_path(
            value[packager], "%s.%s" % (label, packager)
        )
        for packager in PACKAGE_FORMATS
    }


def validate_dependency_list(value, label):
    require(isinstance(value, list), "%s must be an array" % label)
    require(len(value) == len(set(value)), "%s contains duplicates" % label)
    for index, item in enumerate(value):
        text(item, "%s[%d]" % (label, index))


def validate_file_attributes(value, label):
    require(isinstance(value, dict) and value, "%s must be a non-empty object" % label)
    allowed = {"mode", "owner", "group", "config"}
    require(set(value) <= allowed, "%s has unknown fields" % label)
    if "mode" in value:
        require(
            isinstance(value["mode"], str)
            and re.match(r"^0[0-7]{3}$", value["mode"]),
            "%s.mode must be a four-digit octal string" % label,
        )
        mode = int(value["mode"], 8)
        require(mode & 0o6002 == 0, "%s.mode is unsafe" % label)
    for field in ("owner", "group"):
        if field in value:
            require(
                isinstance(value[field], str) and OWNER_RE.match(value[field]),
                "%s.%s is not a safe account identifier" % (label, field),
            )
    if "config" in value:
        require(
            value["config"] in CONFIG_TYPES,
            "%s.config is unsupported" % label,
        )


def validate_relations(value, label):
    exact_keys(value, {"components", "deb", "rpm"}, label)
    components = value["components"]
    require(isinstance(components, list), label + ".components must be an array")
    require(
        len(components) == len(set(components)),
        label + ".components contains duplicates",
    )
    for dependency in components:
        name(dependency, label + ".components")
    for packager, fields in (
        ("deb", DEB_RELATION_FIELDS),
        ("rpm", RPM_RELATION_FIELDS),
    ):
        relations = value[packager]
        exact_keys(relations, fields, label + "." + packager)
        for field in fields:
            validate_dependency_list(
                relations[field], "%s.%s.%s" % (label, packager, field)
            )


def validate_scripts(value, label):
    require(
        isinstance(value, dict)
        and value
        and set(value) <= {"deb", "rpm"},
        "%s must be a non-empty DEB/RPM object" % label,
    )
    for packager in sorted(value):
        scripts = value[packager]
        require(
            isinstance(scripts, dict)
            and scripts
            and set(scripts) <= set(SCRIPT_FIELDS),
            "%s.%s must contain known lifecycle hooks" % (label, packager),
        )
        for field, source in scripts.items():
            source_path(source, "%s.%s.%s" % (label, packager, field))


def normalized_scripts(component):
    configured = component.get("scripts", {})
    return {
        packager: {
            field: configured.get(packager, {}).get(field)
            for field in SCRIPT_FIELDS
        }
        for packager in ("deb", "rpm")
    }


def validate_config(config):
    exact_keys(
        config,
        {
            "$schema",
            "schema_version",
            "project",
            "target",
            "debug_symbols",
            "components",
        },
        "crosspack",
    )
    require(config["$schema"] == CONFIG_SCHEMA, "crosspack schema differs")
    require(config["schema_version"] == 1, "unsupported crosspack schema version")
    require(config["target"] in ARCHITECTURES, "unsupported crosspack target")
    debug_symbols = config["debug_symbols"]
    if debug_symbols is not None:
        exact_keys(
            debug_symbols,
            {"component", "destination_prefixes"},
            "debug_symbols",
        )
        name(debug_symbols["component"], "debug_symbols.component")
        prefixes = destination_values(
            debug_symbols["destination_prefixes"],
            "debug_symbols.destination_prefixes",
        )
        require(
            all(prefix != "/" for prefix in prefixes.values()),
            "debug destination prefix cannot be the package root",
        )

    project_keys = {
        "name",
        "version",
        "epoch",
        "release",
        "vendor",
        "homepage",
        "maintainer",
        "license",
        "section",
        "priority",
        "source_date_epoch",
    }
    project = config["project"]
    exact_keys(project, project_keys, "project")
    name(project["name"], "project.name")
    version(project["version"], "project.version")
    exact_keys(project["epoch"], {"deb", "rpm"}, "project.epoch")
    exact_keys(project["release"], {"deb", "rpm"}, "project.release")
    for packager in ("deb", "rpm"):
        epoch = project["epoch"][packager]
        require(
            isinstance(epoch, int)
            and not isinstance(epoch, bool)
            and 0 <= epoch <= 2147483647,
            "project.epoch.%s must be a non-negative integer" % packager,
        )
        version(project["release"][packager], "project.release.%s" % packager)
    for field in ("vendor", "maintainer", "license"):
        text(project[field], "project.%s" % field)
    require(
        isinstance(project["homepage"], str)
        and re.match(r"^https?://\S+$", project["homepage"]) is not None,
        "project.homepage must be an HTTP(S) URL",
    )
    name(project["section"], "project.section")
    require(project["priority"] in PRIORITIES, "project.priority differs")
    require(
        isinstance(project["source_date_epoch"], int)
        and not isinstance(project["source_date_epoch"], bool)
        and 0 <= project["source_date_epoch"] <= 253402300799,
        "project.source_date_epoch must be a non-negative integer",
    )

    components = config["components"]
    require(isinstance(components, list) and components, "components must be non-empty")
    logical_names = set()
    package_names = {"deb": set(), "rpm": set()}
    for component_index, component in enumerate(components):
        label = "components[%d]" % component_index
        component_keys = {
            "name",
            "package_names",
            "summary",
            "description",
            "files",
            "relations",
        }
        require(
            isinstance(component, dict)
            and component_keys <= set(component)
            and set(component)
            <= component_keys | {"architecture", "scripts"},
            "%s keys differ: expected %s with optional architecture/scripts"
            % (label, ", ".join(sorted(component_keys))),
        )
        name(component["name"], label + ".name")
        text(component["summary"], label + ".summary")
        require(
            len(component["summary"]) <= 256,
            label + ".summary is too long",
        )
        require(
            component.get("architecture", "target")
            in ("target", "independent"),
            label + ".architecture differs",
        )
        require(component["name"] not in logical_names, "component name is duplicated")
        logical_names.add(component["name"])
        package_description(component["description"], label + ".description")

        exact_keys(component["package_names"], {"deb", "rpm"}, label + ".package_names")
        for packager in ("deb", "rpm"):
            package_name = component["package_names"][packager]
            expression = (
                DEB_PACKAGE_NAME_RE if packager == "deb" else RPM_PACKAGE_NAME_RE
            )
            require(
                isinstance(package_name, str)
                and expression.match(package_name) is not None,
                "%s package name is invalid" % packager,
            )
            require(
                package_name not in package_names[packager],
                "%s package name is duplicated" % packager,
            )
            package_names[packager].add(package_name)

        mappings = component["files"]
        require(isinstance(mappings, list), label + ".files must be an array")
        observed_mappings = set()
        for mapping_index, mapping in enumerate(mappings):
            mapping_label = "%s.files[%d]" % (label, mapping_index)
            require(
                isinstance(mapping, dict)
                and {"source", "destination"} <= set(mapping)
                and set(mapping) <= {"source", "destination", "attributes"},
                mapping_label + " fields differ",
            )
            source = source_path(mapping["source"], mapping_label + ".source")
            destinations = destination_values(
                mapping["destination"], mapping_label + ".destination"
            )
            require(
                (source, tuple(sorted(destinations.items())))
                not in observed_mappings,
                "file mapping is duplicated",
            )
            observed_mappings.add(
                (source, tuple(sorted(destinations.items())))
            )
            if "attributes" in mapping:
                validate_file_attributes(
                    mapping["attributes"], mapping_label + ".attributes"
                )

        validate_relations(component["relations"], label + ".relations")
        if "scripts" in component:
            validate_scripts(component["scripts"], label + ".scripts")

    component_map = {item["name"]: item for item in components}
    if debug_symbols is None:
        require(
            all(item["files"] for item in components),
            "component files must be non-empty when debug splitting is disabled",
        )
    else:
        debug_component = debug_symbols["component"]
        require(
            debug_component in component_map,
            "debug_symbols references an unknown component",
        )
        require(
            component_map[debug_component]["files"] == [],
            "debug component files must be generated, not declared",
        )
        require(
            component_map[debug_component].get("architecture", "target")
            == "target",
            "debug component must use target architecture",
        )
        require(
            all(
                item["files"]
                for item in components
                if item["name"] != debug_component
            ),
            "non-debug component files must be non-empty",
        )
        require(
            all(
                debug_component
                not in item["relations"]["components"]
                for item in components
                if item["name"] != debug_component
            ),
            "a runtime component cannot depend on generated debug symbols",
        )
    for component in components:
        dependencies = component["relations"]["components"]
        require(component["name"] not in dependencies, "component cannot depend on itself")
        require(
            all(item in component_map for item in dependencies),
            "%s references an unknown component" % component["name"],
        )
    validate_acyclic(component_map)
    return config


def validate_acyclic(components):
    visiting = set()
    visited = set()

    def visit(component):
        if component in visited:
            return
        require(component not in visiting, "component dependency cycle includes %s" % component)
        visiting.add(component)
        for dependency in components[component]["relations"]["components"]:
            visit(dependency)
        visiting.remove(component)
        visited.add(component)

    for component in sorted(components):
        visit(component)


def file_mode(path):
    try:
        return path.lstat().st_mode
    except OSError as error:
        raise CrosspackError("cannot inspect %s: %s" % (path, error)) from error


def elf_identity(path, target):
    try:
        return ELF["header_identity"](path, target)
    except ElfError as error:
        raise CrosspackError(str(error)) from error


def inventory_entry(root, path, target, kind=None):
    relative = path.relative_to(root).as_posix()
    mode_value = file_mode(path)
    kind = kind or (
        "symlink"
        if stat.S_ISLNK(mode_value)
        else "directory"
        if stat.S_ISDIR(mode_value)
        else "file"
        if stat.S_ISREG(mode_value)
        else None
    )
    require(kind is not None, "unsupported staged file type: %s" % relative)
    result = {
        "source": relative,
        "type": kind,
        "mode": stat.S_IMODE(mode_value),
    }
    if kind == "file":
        require(mode_value & 0o6000 == 0, "setuid/setgid staged file is forbidden: %s" % relative)
        require(mode_value & 0o002 == 0, "world-writable staged file is forbidden: %s" % relative)
        result["size"] = path.stat().st_size
        result["sha256"] = sha256_file(path)
        elf = elf_identity(path, target)
        if elf is not None:
            result["elf"] = elf
    elif kind == "directory":
        require(mode_value & 0o002 == 0, "world-writable empty directory is forbidden: %s" % relative)
    else:
        try:
            link_target = os.readlink(str(path))
        except OSError as error:
            raise CrosspackError("cannot read symlink %s: %s" % (relative, error)) from error
        text(link_target, "symlink target for %s" % relative)
        result["link_target"] = link_target
    return result


def inventory_staging(staging_root, target):
    root = Path(staging_root)
    require(root.is_dir() and not root.is_symlink(), "staging root must be a real directory")
    entries = []
    real_directories = []
    try:
        walker = os.walk(str(root), topdown=True, followlinks=False)
        for current_text, directory_names, file_names in walker:
            current = Path(current_text)
            directory_names.sort()
            file_names.sort()
            real_directories.append(current)
            for directory_name in list(directory_names):
                path = current / directory_name
                mode_value = file_mode(path)
                if stat.S_ISLNK(mode_value):
                    directory_names.remove(directory_name)
                    entries.append(inventory_entry(root, path, target, "symlink"))
                else:
                    require(stat.S_ISDIR(mode_value), "staged directory changed type")
            for file_name in file_names:
                entries.append(inventory_entry(root, current / file_name, target))
        for directory in real_directories:
            if directory != root and not any(directory.iterdir()):
                entries.append(inventory_entry(root, directory, target, "directory"))
    except OSError as error:
        raise CrosspackError("cannot inventory staging root: %s" % error) from error
    entries.sort(key=lambda item: item["source"])
    require(entries, "staging root has no packageable entries")
    return entries


def create_staging_manifest(
    config_path,
    staging_root,
    variant_id,
    resolution_sha256=None,
):
    config = load_json(config_path)
    validate_config(config)
    variant_id = sha256_digest(variant_id, "variant_id")
    resolution_sha256 = sha256_digest(
        resolution_sha256, "resolution_sha256", nullable=True
    )
    entries = inventory_staging(staging_root, config["target"])
    return {
        "$schema": STAGING_SCHEMA,
        "schema_version": 1,
        "kind": "crossforge-sealed-staging",
        "state": "sealed",
        "config_sha256": canonical_sha256(config),
        "target": config["target"],
        "variant_id": variant_id,
        "resolution_sha256": resolution_sha256,
        "inventory_sha256": canonical_sha256(entries),
        "entries": entries,
    }


def verify_staging_manifest(config, staging_root, manifest_path):
    require(manifest_path is not None, "a sealed staging manifest is required")
    manifest_path = Path(manifest_path)
    require(
        manifest_path.is_file() and not manifest_path.is_symlink(),
        "staging manifest must be a regular non-symlink file",
    )
    manifest = load_json(manifest_path)
    exact_keys(
        manifest,
        {
            "$schema",
            "schema_version",
            "kind",
            "state",
            "config_sha256",
            "target",
            "variant_id",
            "resolution_sha256",
            "inventory_sha256",
            "entries",
        },
        "staging manifest",
    )
    require(manifest["$schema"] == STAGING_SCHEMA, "staging schema differs")
    require(manifest["schema_version"] == 1, "staging schema version differs")
    require(
        manifest["kind"] == "crossforge-sealed-staging"
        and manifest["state"] == "sealed",
        "staging manifest state differs",
    )
    sha256_digest(manifest["config_sha256"], "staging config_sha256")
    sha256_digest(manifest["variant_id"], "staging variant_id")
    sha256_digest(
        manifest["resolution_sha256"],
        "staging resolution_sha256",
        nullable=True,
    )
    sha256_digest(manifest["inventory_sha256"], "staging inventory_sha256")
    require(
        manifest["config_sha256"] == canonical_sha256(config),
        "staging manifest belongs to a different package config",
    )
    require(
        manifest["target"] == config["target"],
        "staging manifest target differs",
    )
    inventory = inventory_staging(staging_root, config["target"])
    inventory_sha256 = canonical_sha256(inventory)
    require(
        canonical_sha256(manifest["entries"]) == inventory_sha256
        and manifest["inventory_sha256"] == inventory_sha256,
        "staging tree differs from its sealed manifest",
    )
    return {
        "state": "sealed",
        "manifest_sha256": canonical_sha256(manifest),
        "variant_id": manifest["variant_id"],
        "resolution_sha256": manifest["resolution_sha256"],
        "sealed_inventory_sha256": inventory_sha256,
    }


def source_node(staging_root, source):
    root = Path(staging_root)
    if source == ".":
        return root, "directory"
    path = root
    parts = source.split("/")
    for index, part in enumerate(parts):
        path = path / part
        mode_value = file_mode(path)
        if index < len(parts) - 1:
            require(
                stat.S_ISDIR(mode_value) and not stat.S_ISLNK(mode_value),
                "mapping source traverses a symlink: %s" % source,
            )
    mode_value = file_mode(path)
    kind = (
        "symlink"
        if stat.S_ISLNK(mode_value)
        else "directory"
        if stat.S_ISDIR(mode_value)
        else "file"
        if stat.S_ISREG(mode_value)
        else None
    )
    require(kind is not None, "mapping source has unsupported type: %s" % source)
    return path, kind


def mapped_destination(base, source, entry_source):
    if source == entry_source:
        relative = "."
    elif source == ".":
        relative = entry_source
    else:
        relative = entry_source[len(source) + 1 :]
    destination = base if relative == "." else posixpath.join(base, relative)
    return destination_path(posixpath.normpath(destination), "expanded destination")


def apply_mapping_attributes(mapping, item, match_count, source_kind):
    content = dict(item)
    content["owner"] = "root"
    content["group"] = "root"
    content["config"] = "none"
    attributes = mapping.get("attributes")
    if attributes is None:
        return content
    require(
        match_count == 1,
        "file attributes require a mapping that resolves exactly one entry",
    )
    require(
        source_kind != "directory"
        or (
            item["source"] == mapping["source"]
            and item["type"] == "directory"
        ),
        "file attributes cannot be applied recursively to a directory mapping",
    )
    if "mode" in attributes:
        require(
            content["type"] != "symlink",
            "symlink mode cannot be overridden",
        )
        content["mode"] = int(attributes["mode"], 8)
    content["owner"] = attributes.get("owner", "root")
    content["group"] = attributes.get("group", "root")
    content["config"] = attributes.get("config", "none")
    if content["config"] != "none":
        require(
            content["type"] == "file",
            "only a regular file can be marked as configuration",
        )
    return content


def expand_mappings(config, staging_root, inventory):
    by_source = {item["source"]: item for item in inventory}
    assigned_sources = {}
    destination_owners = {packager: {} for packager in PACKAGE_FORMATS}
    packages = {}
    for component in config["components"]:
        component_name = component["name"]
        contents = {packager: [] for packager in PACKAGE_FORMATS}
        for mapping in component["files"]:
            source = mapping["source"]
            destinations = destination_values(
                mapping["destination"], "mapping destination"
            )
            _path, kind = source_node(staging_root, source)
            for packager in PACKAGE_FORMATS:
                require(
                    kind == "directory" or destinations[packager] != "/",
                    "%s file or symlink cannot replace the package root"
                    % packager,
                )
            if kind == "directory":
                prefix = "" if source == "." else source + "/"
                matches = [
                    item
                    for item in inventory
                    if (source != "." and item["source"] == source)
                    or item["source"].startswith(prefix)
                ]
            else:
                matches = [by_source[source]] if source in by_source else []
            require(matches, "mapping source expands to no packageable entries: %s" % source)
            for item in matches:
                item_source = item["source"]
                require(
                    item_source not in assigned_sources,
                    "staged entry is assigned more than once: %s" % item_source,
                )
                assigned_sources[item_source] = component_name
                base_content = apply_mapping_attributes(
                    mapping, item, len(matches), kind
                )
                for packager in PACKAGE_FORMATS:
                    destination = mapped_destination(
                        destinations[packager], source, item_source
                    )
                    require(
                        destination not in destination_owners[packager],
                        "%s package destination overlaps: %s"
                        % (packager, destination),
                    )
                    destination_owners[packager][destination] = component_name
                    content = dict(base_content)
                    content["destination"] = destination
                    contents[packager].append(content)
        require(
            all(contents.values()),
            "component has no expanded contents: %s" % component_name,
        )
        for packager in PACKAGE_FORMATS:
            contents[packager].sort(key=lambda item: item["destination"])
        packages[component_name] = contents
    missing = sorted(set(by_source) - set(assigned_sources))
    require(not missing, "staged entries are unassigned: %s" % ", ".join(missing))
    for packager in PACKAGE_FORMATS:
        ordered_destinations = sorted(destination_owners[packager])
        for index, destination in enumerate(ordered_destinations):
            descendants = [
                item
                for item in ordered_destinations[index + 1 :]
                if item.startswith(destination + "/")
            ]
            require(
                not descendants,
                "%s package destination tree overlaps: %s and %s"
                % (
                    packager,
                    destination,
                    descendants[0] if descendants else "",
                ),
            )
        validate_symlinks(
            config,
            {
                component: contents[packager]
                for component, contents in packages.items()
            },
            destination_owners[packager],
        )
    return packages


def package_architectures(component, target):
    if component.get("architecture", "target") == "independent":
        return {"deb": "all", "rpm": "noarch"}
    return {
        "deb": ARCHITECTURES[target]["deb"],
        "rpm": ARCHITECTURES[target]["rpm"],
    }


def validate_independent_components(config, packages, staging_root):
    for component in config["components"]:
        if component.get("architecture", "target") != "independent":
            continue
        observed_sources = set()
        for packager in PACKAGE_FORMATS:
            for content in packages[component["name"]][packager]:
                destination = content["destination"]
                lower = destination.lower()
                basename = posixpath.basename(lower)
                require(
                    content.get("elf") is None,
                    "%s independent component contains ELF: %s"
                    % (packager, destination),
                )
                require(
                    not lower.startswith("/usr/lib/debug/")
                    and not basename.startswith("_sysconfigdata_")
                    and not lower.endswith(INDEPENDENT_FORBIDDEN_SUFFIXES)
                    and re.search(r"\.so(?:\.[0-9]+)*$", lower) is None,
                    "%s independent component contains target artifact: %s"
                    % (packager, destination),
                )
                if (
                    content["type"] == "file"
                    and content["source"] not in observed_sources
                ):
                    observed_sources.add(content["source"])
                    try:
                        source = Path(staging_root) / content["source"]
                        with source.open("rb") as stream:
                            prefix = stream.read(8)
                    except OSError as error:
                        raise CrosspackError(
                            "cannot inspect independent file %s: %s"
                            % (destination, error)
                        ) from error
                    require(
                        prefix not in (b"!<arch>\n", b"!<thin>\n")
                        and not prefix.startswith(b"BC\xc0\xde")
                        and not prefix.startswith(b"\xde\xc0\x17\x0b"),
                        "independent component contains target binary: %s"
                        % destination,
                    )
                if content["type"] == "symlink":
                    target = content["link_target"].lower()
                    require(
                        not any(
                            marker in target
                            for marker in (
                                "x86_64",
                                "aarch64",
                                "x86_64-unknown-linux-gnu",
                                "aarch64-unknown-linux-gnu",
                            )
                        ),
                        "%s independent component contains target symlink: %s"
                        % (packager, destination),
                    )


def validate_symlinks(config, packages, destination_owners):
    component_map = {item["name"]: item for item in config["components"]}
    destinations = sorted(destination_owners)
    for component_name, contents in packages.items():
        allowed = set(component_map[component_name]["relations"]["components"])
        allowed.add(component_name)
        for content in contents:
            if content["type"] != "symlink":
                continue
            target = content["link_target"]
            if target.startswith("/"):
                resolved = posixpath.normpath(target)
            else:
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(content["destination"]), target)
                )
            require(resolved.startswith("/"), "symlink target escapes package root")
            owners = {
                destination_owners[destination]
                for destination in destinations
                if destination == resolved or destination.startswith(resolved + "/")
            }
            require(owners, "symlink target is not packaged: %s" % content["destination"])
            require(
                owners <= allowed,
                "symlink crosses an undeclared component dependency: %s"
                % content["destination"],
            )


def version_release(project, packager, include_epoch=True):
    value = "%s-%s" % (project["version"], project["release"][packager])
    epoch = project["epoch"][packager]
    return "%d:%s" % (epoch, value) if include_epoch and epoch else value


def package_relations(config, component, packager):
    project = config["project"]
    exact_version = version_release(project, packager)
    components = {item["name"]: item for item in config["components"]}
    internal = []
    for logical_name in sorted(component["relations"]["components"]):
        package_name = components[logical_name]["package_names"][packager]
        if packager == "deb":
            internal.append("%s (= %s)" % (package_name, exact_version))
        else:
            internal.append("%s = %s" % (package_name, exact_version))
    relations = {
        key: sorted(value)
        for key, value in component["relations"][packager].items()
    }
    dependency_field = "depends" if packager == "deb" else "requires"
    relations[dependency_field] = sorted(relations[dependency_field] + internal)
    return relations


def objcopy_identity(objcopy):
    objcopy = Path(objcopy)
    try:
        resolved = objcopy.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CrosspackError("target objcopy is unavailable: %s" % objcopy) from error
    require(
        resolved.is_file() and os.access(str(resolved), os.X_OK),
        "target objcopy is unavailable: %s" % objcopy,
    )
    stdout, stderr = run_command([objcopy, "--version"])
    first = stdout.splitlines()[0] if stdout.splitlines() else ""
    require("GNU objcopy" in first, "target objcopy identity differs")
    return {
        "path": objcopy,
        "sha256": sha256_file(resolved),
        "version_output_sha256": hashlib.sha256(
            (stdout + stderr).encode("utf-8")
        ).hexdigest(),
    }


def prepare_debug_staging(config, staging_root, objcopy, workspace):
    validate_config(config)
    if config["debug_symbols"] is None:
        return config, Path(staging_root), None
    tool = objcopy_identity(objcopy)
    staging_root = Path(staging_root)
    inventory = inventory_staging(staging_root, config["target"])
    reserved = ".crossforge-debug-symbols"
    require(
        all(
            item["source"] != reserved
            and not item["source"].startswith(reserved + "/")
            for item in inventory
        ),
        "staging tree uses the reserved debug-symbol path",
    )
    debug_name = config["debug_symbols"]["component"]
    debug_component = next(
        item for item in config["components"] if item["name"] == debug_name
    )
    require(
        all(
            not values
            for packager in ("deb", "rpm")
            for values in debug_component["relations"][packager].values()
        ),
        "debug component cannot declare external package relations",
    )
    base_config = copy.deepcopy(config)
    base_config["debug_symbols"] = None
    base_config["components"] = [
        item for item in base_config["components"] if item["name"] != debug_name
    ]
    base_inventory = inventory_staging(staging_root, config["target"])
    base_packages = expand_mappings(base_config, staging_root, base_inventory)
    candidates = []
    owners = set()
    for component in sorted(base_packages):
        rpm_by_source = {
            content["source"]: content
            for content in base_packages[component]["rpm"]
        }
        for content in base_packages[component]["deb"]:
            if content.get("elf", {}).get("type") not in (
                "dynamic",
                "executable",
            ):
                continue
            candidates.append(
                (
                    component,
                    content,
                    {
                        "deb": content["destination"],
                        "rpm": rpm_by_source[content["source"]]["destination"],
                    },
                )
            )
            owners.add(component)
    require(candidates, "debug splitting found no loadable target ELF files")
    require(
        set(debug_component["relations"]["components"]) == owners,
        "debug component dependencies must exactly name ELF-owning components",
    )

    prepared = Path(workspace) / "prepared-staging"
    require(not prepared.exists(), "prepared staging path already exists")
    shutil.copytree(str(staging_root), str(prepared), symlinks=True)
    require(
        canonical_sha256(inventory_staging(staging_root, config["target"]))
        == canonical_sha256(inventory),
        "staging tree changed while preparing debug symbols",
    )
    mappings = []
    records = []
    destination_prefixes = destination_values(
        config["debug_symbols"]["destination_prefixes"],
        "debug_symbols.destination_prefixes",
    )
    debug_root = prepared / reserved
    debug_root.mkdir()
    for index, (component, content, runtime_destinations) in enumerate(
        sorted(candidates, key=lambda item: item[1]["source"])
    ):
        runtime = prepared / content["source"]
        debug_destinations = {
            packager: destination_path(
                destination_prefixes[packager].rstrip("/")
                + runtime_destinations[packager]
                + ".debug",
                "%s debug destination" % packager,
            )
            for packager in PACKAGE_FORMATS
        }
        debug_source = "%s/%06d/%s.debug" % (
            reserved,
            index,
            runtime.name,
        )
        debug_file = prepared / debug_source
        debug_file.parent.mkdir(parents=True, exist_ok=True)
        run_command([tool["path"], "--only-keep-debug", runtime, debug_file])
        run_command([tool["path"], "--strip-debug", runtime])
        run_command(
            [tool["path"], "--add-gnu-debuglink=" + str(debug_file), runtime]
        )
        debuglink = Path(workspace) / ("debuglink-%06d" % index)
        debug_info = Path(workspace) / ("debug-info-%06d" % index)
        run_command(
            [
                tool["path"],
                "--dump-section",
                ".gnu_debuglink=" + str(debuglink),
                runtime,
            ]
        )
        run_command(
            [
                tool["path"],
                "--dump-section",
                ".debug_info=" + str(debug_info),
                debug_file,
            ]
        )
        require(
            debug_file.is_file()
            and debug_file.stat().st_size > 0
            and debuglink.is_file()
            and debuglink.stat().st_size > 0
            and debug_info.is_file()
            and debug_info.stat().st_size > 0,
            "target objcopy did not emit debug artifacts",
        )
        os.chmod(str(debug_file), 0o644)
        mappings.append(
            {"source": debug_source, "destination": debug_destinations}
        )
        records.append(
            {
                "component": component,
                "runtime_destinations": runtime_destinations,
                "runtime_sha256": sha256_file(runtime),
                "debug_destinations": debug_destinations,
                "debug_sha256": sha256_file(debug_file),
            }
        )
    effective = copy.deepcopy(config)
    effective["debug_symbols"] = None
    selected = next(
        item for item in effective["components"] if item["name"] == debug_name
    )
    selected["files"] = mappings
    return effective, prepared, {
        "component": debug_name,
        "destination_prefixes": destination_prefixes,
        "objcopy_sha256": tool["sha256"],
        "objcopy_version_output_sha256": tool["version_output_sha256"],
        "generated_count": len(records),
        "files": records,
    }


def script_output_path(root, component, packager, field):
    return Path(root) / component / packager / field


def read_script(source, config_root, label):
    root = Path(config_root)
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise CrosspackError(
            "cannot resolve script root %s: %s" % (root, error)
        ) from error
    require(root.is_dir(), "script root must be a directory")
    path = root / source
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CrosspackError("cannot resolve %s: %s" % (label, error)) from error
    require(resolved == path, "%s must not traverse a symlink" % label)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise CrosspackError("cannot open %s: %s" % (label, error)) from error
    try:
        metadata = os.fstat(descriptor)
        require(stat.S_ISREG(metadata.st_mode), "%s must be a regular file" % label)
        require(
            0 < metadata.st_size <= SCRIPT_MAX_SIZE,
            "%s must be between 1 byte and %d bytes" % (label, SCRIPT_MAX_SIZE),
        )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(SCRIPT_MAX_SIZE + 1)
    finally:
        os.close(descriptor)
    require(
        len(payload) == metadata.st_size and len(payload) <= SCRIPT_MAX_SIZE,
        "%s changed while it was read" % label,
    )
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CrosspackError("%s must be UTF-8: %s" % (label, error)) from error
    require(
        "\x00" not in decoded and "\r" not in decoded,
        "%s contains unsafe bytes" % label,
    )
    require(
        payload.split(b"\n", 1)[0] == b"#!" + SCRIPT_INTERPRETER.encode("ascii"),
        "%s must start with #!%s" % (label, SCRIPT_INTERPRETER),
    )
    return payload


def prepare_scripts(config, config_root=None, output_root=None):
    configured = {
        component["name"]: normalized_scripts(component)
        for component in config["components"]
    }
    active = any(
        source is not None
        for scripts in configured.values()
        for packager in ("deb", "rpm")
        for source in scripts[packager].values()
    )
    if active:
        require(
            config_root is not None,
            "script root is required by configured scripts",
        )
    prepared_root = None
    if output_root is not None:
        prepared_root = Path(output_root)
        require(
            not prepared_root.exists() and not prepared_root.is_symlink(),
            "prepared script root already exists",
        )
        prepared_root.mkdir(parents=True)
    result = {}
    for component in config["components"]:
        component_name = component["name"]
        result[component_name] = {"deb": {}, "rpm": {}}
        for packager in ("deb", "rpm"):
            for field in SCRIPT_FIELDS:
                source = configured[component_name][packager][field]
                if source is None:
                    result[component_name][packager][field] = None
                    continue
                label = "components.%s.scripts.%s.%s" % (
                    component_name,
                    packager,
                    field,
                )
                payload = read_script(source, config_root, label)
                record = {
                    "source": source,
                    "interpreter": SCRIPT_INTERPRETER,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                result[component_name][packager][field] = record
                if prepared_root is not None:
                    destination = script_output_path(
                        prepared_root, component_name, packager, field
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(payload)
                    destination.chmod(0o700)
    return result


def build_plan(
    config,
    staging_root,
    readelf=None,
    sysroot=None,
    config_sha256=None,
    debug_symbols=None,
    config_root=None,
    script_root=None,
    formats=None,
    staging_provenance=None,
):
    validate_config(config)
    require(
        (readelf is None) == (sysroot is None),
        "target readelf and sysroot must be provided together",
    )
    inventory = inventory_staging(staging_root, config["target"])
    prepared_inventory_sha256 = canonical_sha256(inventory)
    if staging_provenance is None:
        staging_provenance = {
            "state": "observed-unsealed",
            "manifest_sha256": None,
            "variant_id": None,
            "resolution_sha256": None,
            "sealed_inventory_sha256": prepared_inventory_sha256,
        }
    staging = dict(staging_provenance)
    staging["prepared_inventory_sha256"] = prepared_inventory_sha256
    expanded = expand_mappings(config, staging_root, inventory)
    validate_independent_components(config, expanded, staging_root)
    scripts = prepare_scripts(config, config_root, script_root)
    elf_audit = {packager: None for packager in PACKAGE_FORMATS}
    if readelf is not None:
        try:
            component_dependencies = {
                component["name"]: component["relations"]["components"]
                for component in config["components"]
            }
            for packager in PACKAGE_FORMATS:
                elf_audit[packager] = ELF["audit_packages"](
                    {
                        component: contents[packager]
                        for component, contents in expanded.items()
                    },
                    staging_root,
                    config["target"],
                    readelf,
                    sysroot,
                    component_dependencies,
                    (
                        DEB_MULTIARCH_LIBRARIES[config["target"]]
                        if packager == "deb"
                        else ()
                    ),
                )
        except ElfError as error:
            raise CrosspackError(str(error)) from error
    packages = []
    for component in sorted(config["components"], key=lambda item: item["name"]):
        packages.append(
            {
                "component": component["name"],
                "package_names": dict(component["package_names"]),
                "architecture": component.get("architecture", "target"),
                "architecture_qualification": (
                    "declared-independent"
                    if component.get("architecture", "target") == "independent"
                    else "target-specific"
                ),
                "architectures": package_architectures(
                    component, config["target"]
                ),
                "summary": component["summary"],
                "description": component["description"],
                "relations": {
                    "components": sorted(component["relations"]["components"]),
                    "deb": package_relations(config, component, "deb"),
                    "rpm": package_relations(config, component, "rpm"),
                },
                "scripts": scripts[component["name"]],
                "contents": expanded[component["name"]],
            }
        )
    return {
        "$schema": PLAN_SCHEMA,
        "schema_version": 1,
        "kind": "crossforge-crosspack-plan",
        "config_sha256": config_sha256 or canonical_sha256(config),
        "staging_sha256": prepared_inventory_sha256,
        "staging": staging,
        "target": config["target"],
        "formats": list(selected_formats(formats)),
        "architectures": {
            "deb": ARCHITECTURES[config["target"]]["deb"],
            "rpm": ARCHITECTURES[config["target"]]["rpm"],
        },
        "elf_audit": elf_audit,
        "debug_symbols": debug_symbols,
        "project": dict(config["project"]),
        "packages": packages,
    }


def source_date_time(project):
    return datetime.datetime.fromtimestamp(
        project["source_date_epoch"], datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def nfpm_content(content, staging_root, timestamp):
    result = {
        "dst": content["destination"],
        "file_info": {
            "mode": content["mode"],
            "mtime": timestamp,
            "owner": content["owner"],
            "group": content["group"],
        },
    }
    if content["type"] == "file":
        result["src"] = str((Path(staging_root) / content["source"]).resolve())
        if content["config"] == "config":
            result["type"] = "config"
        elif content["config"] == "noreplace":
            result["type"] = "config|noreplace"
    elif content["type"] == "symlink":
        result["src"] = content["link_target"]
        result["type"] = "symlink"
    else:
        result["type"] = "dir"
    return result


def prepared_scripts(package, packager, script_root):
    result = {}
    for field in SCRIPT_FIELDS:
        record = package["scripts"][packager][field]
        if record is None:
            continue
        require(script_root is not None, "prepared script root is required")
        path = script_output_path(
            script_root, package["component"], packager, field
        )
        require(
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_size == record["size"]
            and sha256_file(path) == record["sha256"],
            "prepared script differs: %s/%s/%s"
            % (package["component"], packager, field),
        )
        result[NFPM_SCRIPT_FIELDS[field]] = str(path.resolve())
    return result


def nfpm_config(plan_document, package, packager, staging_root, script_root=None):
    require(packager in ("deb", "rpm"), "unsupported package format")
    project = plan_document["project"]
    timestamp = source_date_time(project)
    relations = package["relations"][packager]
    result = {
        "name": package["package_names"][packager],
        "arch": package["architectures"][packager],
        "platform": "linux",
        "version": project["version"],
        "version_schema": "none",
        "epoch": str(project["epoch"][packager]),
        "release": project["release"][packager],
        "section": project["section"],
        "priority": project["priority"],
        "maintainer": project["maintainer"],
        "description": package["summary"] + "\n" + package["description"],
        "vendor": project["vendor"],
        "homepage": project["homepage"],
        "license": project["license"],
        "mtime": timestamp,
        "disable_globbing": True,
        "umask": 0,
        "depends": list(
            relations["depends" if packager == "deb" else "requires"]
        ),
        "recommends": list(relations["recommends"]),
        "suggests": list(relations["suggests"]),
        "conflicts": list(relations["conflicts"]),
        "provides": list(relations["provides"]),
        "replaces": list(
            relations["replaces" if packager == "deb" else "obsoletes"]
        ),
        "contents": [
            nfpm_content(content, staging_root, timestamp)
            for content in package["contents"][packager]
        ],
    }
    scripts = prepared_scripts(package, packager, script_root)
    if scripts:
        result["scripts"] = scripts
    if packager == "deb":
        result["deb"] = {
            "arch": package["architectures"]["deb"],
            "compression": "gzip",
            "predepends": list(relations["pre_depends"]),
            "breaks": list(relations["breaks"]),
        }
    else:
        result["rpm"] = {
            "arch": package["architectures"]["rpm"],
            "buildhost": "crossforge.invalid",
            "compression": "gzip",
            "packager": project["maintainer"],
            "summary": package["summary"],
        }
    return result


def render_nfpm_configs(plan_document, staging_root, script_root=None):
    result = {packager: {} for packager in plan_document["formats"]}
    for package in plan_document["packages"]:
        component = package["component"]
        for packager in plan_document["formats"]:
            result[packager][component] = nfpm_config(
                plan_document, package, packager, staging_root, script_root
            )
    return result


def package_filename(plan_document, package, packager):
    project = plan_document["project"]
    package_name = package["package_names"][packager]
    package_version = version_release(project, packager, include_epoch=False)
    architecture = package["architectures"][packager]
    if packager == "deb":
        return "%s_%s_%s.deb" % (package_name, package_version, architecture)
    return "%s-%s.%s.rpm" % (package_name, package_version, architecture)


def run_command(arguments, environment=None):
    process = subprocess.run(
        [str(item) for item in arguments],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "command failed (%s):\n%s%s"
        % (" ".join(str(item) for item in arguments), process.stdout, process.stderr),
    )
    return process.stdout, process.stderr


def nfpm_identity(nfpm_path, expected_version, expected_sha256):
    nfpm_path = Path(nfpm_path)
    require(
        nfpm_path.is_file()
        and not nfpm_path.is_symlink()
        and os.access(str(nfpm_path), os.X_OK),
        "nFPM must be an executable regular file",
    )
    require(
        re.match(r"^[0-9a-f]{64}$", expected_sha256 or "") is not None,
        "expected nFPM SHA256 is invalid",
    )
    observed_sha256 = sha256_file(nfpm_path)
    require(observed_sha256 == expected_sha256, "nFPM SHA256 differs")
    stdout, stderr = run_command([nfpm_path, "--version"])
    match = re.search(r"^GitVersion:\s+(\S+)\s*$", stdout, re.MULTILINE)
    require(
        match is not None and match.group(1) == expected_version,
        "nFPM version differs",
    )
    return {
        "version": expected_version,
        "sha256": observed_sha256,
        "version_output_sha256": hashlib.sha256(
            (stdout + stderr).encode("utf-8")
        ).hexdigest(),
    }


def package(
    config_path,
    staging_root,
    output_directory,
    nfpm_path,
    nfpm_version,
    nfpm_sha256,
    readelf,
    sysroot,
    objcopy,
    formats=None,
    staging_manifest_path=None,
):
    config = load_json(config_path)
    validate_config(config)
    staging_provenance = verify_staging_manifest(
        config, staging_root, staging_manifest_path
    )
    output_directory = Path(output_directory)
    require(not output_directory.exists(), "output directory already exists")
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    require(
        output_directory.parent.is_dir()
        and not output_directory.parent.is_symlink(),
        "output parent must be a real directory",
    )
    identity = nfpm_identity(nfpm_path, nfpm_version, nfpm_sha256)
    with tempfile.TemporaryDirectory(
        prefix=".crosspack-", dir=str(output_directory.parent)
    ) as temporary:
        workspace = Path(temporary)
        effective_config, package_staging, debug_symbols = prepare_debug_staging(
            config, staging_root, objcopy, workspace
        )
        plan_document = build_plan(
            effective_config,
            package_staging,
            readelf,
            sysroot,
            canonical_sha256(config),
            debug_symbols,
            Path(config_path).resolve().parent,
            workspace / "scripts",
            selected_formats(formats),
            staging_provenance,
        )
        rendered = render_nfpm_configs(
            plan_document, package_staging, workspace / "scripts"
        )
        artifacts = []
        result_root = workspace / "result"
        config_root = workspace / "configs"
        package_root = result_root / "packages"
        result_root.mkdir()
        config_root.mkdir()
        package_root.mkdir(parents=True)
        write_json(plan_document, str(result_root / "crosspack-plan.json"))
        environment = dict(os.environ)
        environment.update(
            {
                "LC_ALL": "C",
                "SOURCE_DATE_EPOCH": str(config["project"]["source_date_epoch"]),
                "TZ": "UTC",
            }
        )
        packages = {
            item["component"]: item for item in plan_document["packages"]
        }
        for packager in plan_document["formats"]:
            for component in sorted(packages):
                current_inventory = inventory_staging(
                    package_staging, plan_document["target"]
                )
                require(
                    canonical_sha256(current_inventory)
                    == plan_document["staging_sha256"],
                    "staging tree changed while packaging",
                )
                nfpm_document = rendered[packager][component]
                config_file = config_root / (component + "." + packager + ".json")
                write_json(nfpm_document, str(config_file))
                filename = package_filename(
                    plan_document, packages[component], packager
                )
                artifact = package_root / filename
                run_command(
                    [
                        nfpm_path,
                        "package",
                        "--config",
                        config_file,
                        "--packager",
                        packager,
                        "--target",
                        artifact,
                    ],
                    environment,
                )
                require(
                    artifact.is_file() and not artifact.is_symlink(),
                    "nFPM did not emit the expected package",
                )
                artifacts.append(
                    {
                        "component": component,
                        "format": packager,
                        "path": "packages/" + filename,
                        "size": artifact.stat().st_size,
                        "sha256": sha256_file(artifact),
                    }
                )
        result = {
            "$schema": RESULT_SCHEMA,
            "schema_version": 1,
            "kind": "crossforge-crosspack-result",
            "plan_sha256": canonical_sha256(plan_document),
            "nfpm": identity,
            "artifacts": artifacts,
        }
        write_json(result, str(result_root / "crosspack-result.json"))
        shutil.rmtree(str(config_root))
        result_root.replace(output_directory)
    return result


def plan(
    config_path,
    staging_root,
    readelf=None,
    sysroot=None,
    objcopy=None,
    formats=None,
    staging_manifest_path=None,
):
    config = load_json(config_path)
    validate_config(config)
    staging_provenance = verify_staging_manifest(
        config, staging_root, staging_manifest_path
    )
    with tempfile.TemporaryDirectory(prefix="crosspack-plan-") as temporary:
        effective, prepared, debug_symbols = prepare_debug_staging(
            config, staging_root, objcopy, Path(temporary)
        )
        return build_plan(
            effective,
            prepared,
            readelf,
            sysroot,
            canonical_sha256(config),
            debug_symbols,
            Path(config_path).resolve().parent,
            Path(temporary) / "scripts",
            selected_formats(formats),
            staging_provenance,
        )


def write_json(document, output):
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if output == "-":
        sys.stdout.write(payload)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_text = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_text)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
        temporary.replace(path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def write_new_json(document, output):
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(
        path.parent.is_dir() and not path.parent.is_symlink(),
        "output parent must be a real directory",
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags, 0o644)
    except OSError as error:
        raise CrosspackError("cannot create %s: %s" % (path, error)) from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(payload)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            path.unlink()
        except OSError:
            pass
        raise


def write_staging_manifest(document, staging_root, output):
    root = Path(staging_root).resolve(strict=True)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    require(
        parent != root and root not in parent.parents,
        "staging manifest must be written outside the staged tree",
    )
    write_new_json(document, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command")
    seal_parser = subparsers.add_parser("seal", allow_abbrev=False)
    seal_parser.add_argument("--config", type=Path, required=True)
    seal_parser.add_argument("--staging-root", type=Path, required=True)
    seal_parser.add_argument("--variant-id", required=True)
    seal_parser.add_argument("--resolution-sha256")
    seal_parser.add_argument("--output", type=Path, required=True)
    plan_parser = subparsers.add_parser("plan", allow_abbrev=False)
    plan_parser.add_argument("--config", type=Path, required=True)
    plan_parser.add_argument("--staging-root", type=Path, required=True)
    plan_parser.add_argument("--readelf", type=Path, required=True)
    plan_parser.add_argument("--sysroot", type=Path, required=True)
    plan_parser.add_argument("--objcopy", type=Path, required=True)
    plan_parser.add_argument(
        "--format", choices=("both", "deb", "rpm"), default="both"
    )
    plan_parser.add_argument("--staging-manifest", type=Path, required=True)
    plan_parser.add_argument("--output", default="-")
    package_parser = subparsers.add_parser("package", allow_abbrev=False)
    package_parser.add_argument("--config", type=Path, required=True)
    package_parser.add_argument("--staging-root", type=Path, required=True)
    package_parser.add_argument("--output-directory", type=Path, required=True)
    package_parser.add_argument("--nfpm", type=Path, required=True)
    package_parser.add_argument("--nfpm-version", required=True)
    package_parser.add_argument("--nfpm-sha256", required=True)
    package_parser.add_argument("--readelf", type=Path, required=True)
    package_parser.add_argument("--sysroot", type=Path, required=True)
    package_parser.add_argument("--objcopy", type=Path, required=True)
    package_parser.add_argument(
        "--format", choices=("both", "deb", "rpm"), default="both"
    )
    package_parser.add_argument("--staging-manifest", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        require(
            arguments.command in ("seal", "plan", "package"),
            "a crosspack command is required",
        )
        if arguments.command == "seal":
            manifest = create_staging_manifest(
                arguments.config,
                arguments.staging_root,
                arguments.variant_id,
                arguments.resolution_sha256,
            )
            write_staging_manifest(
                manifest, arguments.staging_root, arguments.output
            )
        elif arguments.command == "plan":
            report = plan(
                arguments.config,
                arguments.staging_root,
                arguments.readelf,
                arguments.sysroot,
                arguments.objcopy,
                arguments.format,
                arguments.staging_manifest,
            )
            write_json(report, arguments.output)
        else:
            package(
                arguments.config,
                arguments.staging_root,
                arguments.output_directory,
                arguments.nfpm,
                arguments.nfpm_version,
                arguments.nfpm_sha256,
                arguments.readelf,
                arguments.sysroot,
                arguments.objcopy,
                arguments.format,
                arguments.staging_manifest,
            )
    except CrosspackError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
