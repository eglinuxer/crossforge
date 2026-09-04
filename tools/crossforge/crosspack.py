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
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")
DEB_PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
RPM_PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_~:-]*$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9+._@~-]+(?:/[A-Za-z0-9+._@~-]+)*$")
DESTINATION_RE = re.compile(
    r"^/(?:[A-Za-z0-9+._@~-]+(?:/[A-Za-z0-9+._@~-]+)*)?$"
)
PRIORITIES = {"required", "important", "standard", "optional", "extra"}
ARCHITECTURES = {
    "x86_64": {"deb": "amd64", "rpm": "x86_64", "elf_machine": 62},
    "aarch64": {"deb": "arm64", "rpm": "aarch64", "elf_machine": 183},
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


def load_json(path):
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=reject_duplicate_keys)
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


def validate_dependency_list(value, label):
    require(isinstance(value, list), "%s must be an array" % label)
    require(len(value) == len(set(value)), "%s contains duplicates" % label)
    for index, item in enumerate(value):
        text(item, "%s[%d]" % (label, index))


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
        exact_keys(debug_symbols, {"component"}, "debug_symbols")
        name(debug_symbols["component"], "debug_symbols.component")

    project_keys = {
        "name",
        "version",
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
    version(project["release"], "project.release")
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
        exact_keys(
            component,
            {"name", "package_names", "description", "files", "dependencies"},
            label,
        )
        name(component["name"], label + ".name")
        require(component["name"] not in logical_names, "component name is duplicated")
        logical_names.add(component["name"])
        text(component["description"], label + ".description")

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
            exact_keys(mapping, {"source", "destination"}, mapping_label)
            source = source_path(mapping["source"], mapping_label + ".source")
            destination = destination_path(
                mapping["destination"], mapping_label + ".destination"
            )
            require(
                (source, destination) not in observed_mappings,
                "file mapping is duplicated",
            )
            observed_mappings.add((source, destination))

        dependencies = component["dependencies"]
        exact_keys(dependencies, {"components", "deb", "rpm"}, label + ".dependencies")
        require(
            isinstance(dependencies["components"], list),
            label + ".dependencies.components must be an array",
        )
        require(
            len(dependencies["components"])
            == len(set(dependencies["components"])),
            label + ".dependencies.components contains duplicates",
        )
        for dependency in dependencies["components"]:
            name(dependency, label + ".dependencies.components")
        validate_dependency_list(dependencies["deb"], label + ".dependencies.deb")
        validate_dependency_list(dependencies["rpm"], label + ".dependencies.rpm")

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
                not in item["dependencies"]["components"]
                for item in components
                if item["name"] != debug_component
            ),
            "a runtime component cannot depend on generated debug symbols",
        )
    for component in components:
        dependencies = component["dependencies"]["components"]
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
        for dependency in components[component]["dependencies"]["components"]:
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


def expand_mappings(config, staging_root, inventory):
    by_source = {item["source"]: item for item in inventory}
    assigned_sources = {}
    destination_owners = {}
    packages = {}
    for component in config["components"]:
        component_name = component["name"]
        contents = []
        for mapping in component["files"]:
            source = mapping["source"]
            _path, kind = source_node(staging_root, source)
            require(
                kind == "directory" or mapping["destination"] != "/",
                "a file or symlink cannot replace the package root",
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
                destination = mapped_destination(
                    mapping["destination"], source, item_source
                )
                require(
                    destination not in destination_owners,
                    "package destination overlaps: %s" % destination,
                )
                assigned_sources[item_source] = component_name
                destination_owners[destination] = component_name
                content = dict(item)
                content["destination"] = destination
                contents.append(content)
        require(contents, "component has no expanded contents: %s" % component_name)
        contents.sort(key=lambda item: item["destination"])
        packages[component_name] = contents
    missing = sorted(set(by_source) - set(assigned_sources))
    require(not missing, "staged entries are unassigned: %s" % ", ".join(missing))
    ordered_destinations = sorted(destination_owners)
    for index, destination in enumerate(ordered_destinations):
        descendants = [
            item
            for item in ordered_destinations[index + 1 :]
            if item.startswith(destination + "/")
        ]
        require(
            not descendants,
            "package destination tree overlaps: %s and %s"
            % (destination, descendants[0] if descendants else ""),
        )
    validate_symlinks(config, packages, destination_owners)
    return packages


def validate_symlinks(config, packages, destination_owners):
    component_map = {item["name"]: item for item in config["components"]}
    destinations = sorted(destination_owners)
    for component_name, contents in packages.items():
        allowed = set(component_map[component_name]["dependencies"]["components"])
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


def package_dependencies(config, component, packager):
    project = config["project"]
    version_release = "%s-%s" % (project["version"], project["release"])
    components = {item["name"]: item for item in config["components"]}
    internal = []
    for logical_name in sorted(component["dependencies"]["components"]):
        package_name = components[logical_name]["package_names"][packager]
        if packager == "deb":
            internal.append("%s (= %s)" % (package_name, version_release))
        else:
            internal.append("%s = %s" % (package_name, version_release))
    return sorted(component["dependencies"][packager]) + internal


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
        debug_component["dependencies"]["deb"] == []
        and debug_component["dependencies"]["rpm"] == [],
        "debug component cannot declare external dependencies",
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
        for content in base_packages[component]:
            if content.get("elf", {}).get("type") not in (
                "dynamic",
                "executable",
            ):
                continue
            candidates.append((component, content))
            owners.add(component)
    require(candidates, "debug splitting found no loadable target ELF files")
    require(
        set(debug_component["dependencies"]["components"]) == owners,
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
    debug_root = prepared / reserved
    debug_root.mkdir()
    for index, (component, content) in enumerate(
        sorted(candidates, key=lambda item: item[1]["destination"])
    ):
        runtime = prepared / content["source"]
        debug_destination = "/usr/lib/debug%s.debug" % content["destination"]
        debug_source = reserved + debug_destination
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
            {"source": debug_source, "destination": debug_destination}
        )
        records.append(
            {
                "component": component,
                "runtime_destination": content["destination"],
                "runtime_sha256": sha256_file(runtime),
                "debug_destination": debug_destination,
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
        "objcopy_sha256": tool["sha256"],
        "objcopy_version_output_sha256": tool["version_output_sha256"],
        "generated_count": len(records),
        "files": records,
    }


def build_plan(
    config,
    staging_root,
    readelf=None,
    sysroot=None,
    config_sha256=None,
    debug_symbols=None,
):
    validate_config(config)
    require(
        (readelf is None) == (sysroot is None),
        "target readelf and sysroot must be provided together",
    )
    inventory = inventory_staging(staging_root, config["target"])
    expanded = expand_mappings(config, staging_root, inventory)
    elf_audit = None
    if readelf is not None:
        try:
            component_dependencies = {
                component["name"]: component["dependencies"]["components"]
                for component in config["components"]
            }
            elf_audit = ELF["audit_packages"](
                expanded,
                staging_root,
                config["target"],
                readelf,
                sysroot,
                component_dependencies,
            )
        except ElfError as error:
            raise CrosspackError(str(error)) from error
    packages = []
    for component in sorted(config["components"], key=lambda item: item["name"]):
        packages.append(
            {
                "component": component["name"],
                "package_names": dict(component["package_names"]),
                "description": component["description"],
                "dependencies": {
                    "components": sorted(component["dependencies"]["components"]),
                    "deb": package_dependencies(config, component, "deb"),
                    "rpm": package_dependencies(config, component, "rpm"),
                },
                "contents": expanded[component["name"]],
            }
        )
    return {
        "$schema": PLAN_SCHEMA,
        "schema_version": 1,
        "kind": "crossforge-crosspack-plan",
        "config_sha256": config_sha256 or canonical_sha256(config),
        "staging_sha256": canonical_sha256(inventory),
        "target": config["target"],
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
            "owner": "root",
            "group": "root",
        },
    }
    if content["type"] == "file":
        result["src"] = str((Path(staging_root) / content["source"]).resolve())
    elif content["type"] == "symlink":
        result["src"] = content["link_target"]
        result["type"] = "symlink"
    else:
        result["type"] = "dir"
    return result


def nfpm_config(plan_document, package, packager, staging_root):
    require(packager in ("deb", "rpm"), "unsupported package format")
    project = plan_document["project"]
    timestamp = source_date_time(project)
    common_arch = "amd64" if plan_document["target"] == "x86_64" else "arm64"
    result = {
        "name": package["package_names"][packager],
        "arch": common_arch,
        "platform": "linux",
        "version": project["version"],
        "version_schema": "none",
        "release": project["release"],
        "section": project["section"],
        "priority": project["priority"],
        "maintainer": project["maintainer"],
        "description": package["description"],
        "vendor": project["vendor"],
        "homepage": project["homepage"],
        "license": project["license"],
        "mtime": timestamp,
        "disable_globbing": True,
        "umask": 0,
        "depends": list(package["dependencies"][packager]),
        "contents": [
            nfpm_content(content, staging_root, timestamp)
            for content in package["contents"]
        ],
    }
    if packager == "deb":
        result["deb"] = {
            "arch": plan_document["architectures"]["deb"],
            "compression": "gzip",
        }
    else:
        result["rpm"] = {
            "arch": plan_document["architectures"]["rpm"],
            "buildhost": "crossforge.invalid",
            "compression": "gzip",
            "packager": project["maintainer"],
        }
    return result


def render_nfpm_configs(plan_document, staging_root):
    result = {"deb": {}, "rpm": {}}
    for package in plan_document["packages"]:
        component = package["component"]
        for packager in ("deb", "rpm"):
            result[packager][component] = nfpm_config(
                plan_document, package, packager, staging_root
            )
    return result


def package_filename(plan_document, package, packager):
    project = plan_document["project"]
    package_name = package["package_names"][packager]
    version_release = "%s-%s" % (project["version"], project["release"])
    architecture = plan_document["architectures"][packager]
    if packager == "deb":
        return "%s_%s_%s.deb" % (package_name, version_release, architecture)
    return "%s-%s.%s.rpm" % (package_name, version_release, architecture)


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
):
    config = load_json(config_path)
    validate_config(config)
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
        )
        rendered = render_nfpm_configs(plan_document, package_staging)
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
        for packager in ("deb", "rpm"):
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


def plan(config_path, staging_root, readelf=None, sysroot=None, objcopy=None):
    config = load_json(config_path)
    validate_config(config)
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command")
    plan_parser = subparsers.add_parser("plan", allow_abbrev=False)
    plan_parser.add_argument("--config", type=Path, required=True)
    plan_parser.add_argument("--staging-root", type=Path, required=True)
    plan_parser.add_argument("--readelf", type=Path, required=True)
    plan_parser.add_argument("--sysroot", type=Path, required=True)
    plan_parser.add_argument("--objcopy", type=Path, required=True)
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
    arguments = parser.parse_args(argv)
    try:
        require(arguments.command in ("plan", "package"), "a crosspack command is required")
        if arguments.command == "plan":
            report = plan(
                arguments.config,
                arguments.staging_root,
                arguments.readelf,
                arguments.sysroot,
                arguments.objcopy,
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
            )
    except CrosspackError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
