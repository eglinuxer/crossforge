"""Target ELF inspection used by crosspack's fail-closed package boundary."""

import hashlib
import json
import os
import posixpath
import re
import subprocess
from pathlib import Path


MACHINES = {"x86_64": 62, "aarch64": 183}
ELF_TYPES = {1: "relocatable", 2: "executable", 3: "dynamic", 4: "core"}
SONAME_RE = re.compile(r"^[A-Za-z0-9+._-]+\.so(?:\.[A-Za-z0-9+._-]+)*\Z")
RUNPATH_PART_RE = re.compile(r"^[A-Za-z0-9+._@~-]+\Z")
SYSTEM_LIBRARY_DIRECTORIES = ("/lib", "/lib64", "/usr/lib", "/usr/lib64")


class ElfError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ElfError(message)


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
        raise ElfError("cannot hash %s: %s" % (path, error)) from error
    return digest.hexdigest()


def header_identity(path, target):
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(20)
    except OSError as error:
        raise ElfError("cannot inspect ELF %s: %s" % (path, error)) from error
    if not header.startswith(b"\x7fELF"):
        return None
    require(len(header) >= 20, "truncated ELF file: %s" % path)
    require(header[4] == 2, "ELF is not 64-bit: %s" % path)
    require(header[5] == 1, "ELF is not little-endian: %s" % path)
    machine = int.from_bytes(header[18:20], byteorder="little")
    require(machine == MACHINES[target], "ELF target differs for %s" % path)
    elf_type = int.from_bytes(header[16:18], byteorder="little")
    require(elf_type in ELF_TYPES, "ELF type is unsupported for %s" % path)
    return {
        "class": 64,
        "endianness": "little",
        "machine": target,
        "type": ELF_TYPES[elf_type],
    }


def executable(path, label):
    path = Path(path)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ElfError("%s is unavailable: %s" % (label, path)) from error
    require(
        resolved.is_file() and os.access(str(resolved), os.X_OK),
        "%s is unavailable: %s" % (label, path),
    )
    return path


def run_readelf(readelf, arguments, path):
    process = subprocess.run(
        [str(readelf)] + list(arguments) + [str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0,
        "readelf failed for %s:\n%s%s" % (path, process.stdout, process.stderr),
    )
    return process.stdout


def validate_readelf(readelf):
    process = subprocess.run(
        [str(readelf), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    require(
        process.returncode == 0
        and process.stdout.splitlines()
        and "GNU readelf" in process.stdout.splitlines()[0],
        "target readelf identity differs",
    )
    return hashlib.sha256(
        (process.stdout + process.stderr).encode("utf-8")
    ).hexdigest()


def dynamic_identity(output):
    needed = re.findall(
        r"\(NEEDED\).*Shared library: \[([^]]+)\]", output
    )
    sonames = re.findall(r"\(SONAME\).*Library soname: \[([^]]+)\]", output)
    rpaths = re.findall(r"\(RPATH\).*Library rpath: \[([^]]*)\]", output)
    runpaths = re.findall(
        r"\(RUNPATH\).*Library runpath: \[([^]]*)\]", output
    )
    require(len(needed) == len(set(needed)), "ELF repeats DT_NEEDED")
    require(len(sonames) <= 1, "ELF repeats DT_SONAME")
    require(not rpaths, "DT_RPATH is forbidden")
    require("(TEXTREL)" not in output, "ELF text relocations are forbidden")
    for soname in needed + sonames:
        require(
            SONAME_RE.match(soname) is not None and "/" not in soname,
            "ELF has an unsafe shared-library name: %s" % soname,
        )
    search = []
    for value in runpaths:
        for entry in value.split(":"):
            require(entry, "empty DT_RUNPATH entry is forbidden")
            parts = entry.split("/")
            require(
                entry == "$ORIGIN"
                or (
                    entry.startswith("$ORIGIN/")
                    and all(
                        part == ".." or RUNPATH_PART_RE.match(part)
                        for part in parts[1:]
                    )
                ),
                "unsafe DT_RUNPATH entry: %s" % entry,
            )
            seen_named_part = False
            for part in parts[1:]:
                if part == "..":
                    require(
                        not seen_named_part,
                        "DT_RUNPATH parent traversal is not canonical: %s" % entry,
                    )
                else:
                    seen_named_part = True
            search.append(entry)
    require(len(search) == len(set(search)), "ELF repeats a DT_RUNPATH entry")
    return {
        "needed": needed,
        "soname": sonames[0] if sonames else None,
        "runpath": search,
    }


def dynamic_symbols(output):
    exports = []
    for line in output.splitlines():
        fields = line.split(None, 7)
        if len(fields) != 8 or not fields[0].endswith(":"):
            continue
        _number, _value, _size, symbol_type, binding, visibility, index, name = fields
        if index == "UND" or binding not in ("GLOBAL", "WEAK"):
            continue
        if visibility not in ("DEFAULT", "PROTECTED"):
            continue
        name = re.sub(r" \([0-9]+\)\Z", "", name)
        if not name:
            continue
        exports.append(
            {
                "name": name,
                "binding": binding.lower(),
                "type": symbol_type.lower(),
                "visibility": visibility.lower(),
            }
        )
    exports.sort(
        key=lambda item: (
            item["name"],
            item["binding"],
            item["type"],
            item["visibility"],
        )
    )
    return exports


def provider_inventory(sysroot):
    sysroot = Path(sysroot)
    require(
        sysroot.is_dir() and not sysroot.is_symlink(),
        "target sysroot must be a real directory",
    )
    names = set()
    for relative in ("lib", "lib64", "usr/lib", "usr/lib64"):
        directory = sysroot / relative
        if not directory.is_dir():
            continue
        try:
            candidates = directory.rglob("*.so*")
            for path in candidates:
                if path.is_file() or path.is_symlink():
                    name = path.name
                    if SONAME_RE.match(name):
                        names.add(name)
        except OSError as error:
            raise ElfError("cannot inventory target sysroot: %s" % error) from error
    ordered = sorted(names)
    require(ordered, "target sysroot has no shared-library providers")
    return ordered


def is_within(path, prefix):
    return path == prefix or path.startswith(prefix + "/")


def private_install_prefix(destination):
    parts = destination.split("/")
    if len(parts) >= 5 and parts[1] == "opt":
        return "/".join(parts[:4])
    if len(parts) >= 5 and parts[1:3] == ["usr", "libexec"]:
        return "/".join(parts[:4])
    return None


def resolve_runpath(destination, entries):
    origin = posixpath.dirname(destination)
    resolved = []
    for entry in entries:
        relative = "" if entry == "$ORIGIN" else entry[len("$ORIGIN/") :]
        directory = posixpath.normpath(posixpath.join(origin, relative))
        require(directory.startswith("/"), "DT_RUNPATH escapes the package root")
        if ".." in entry.split("/"):
            prefix = private_install_prefix(destination)
            require(
                prefix is not None and is_within(directory, prefix),
                "DT_RUNPATH parent traversal escapes the private install prefix: %s"
                % entry,
            )
        resolved.append({"entry": entry, "directory": directory})
    directories = [item["directory"] for item in resolved]
    require(
        len(directories) == len(set(directories)),
        "DT_RUNPATH resolves the same directory more than once",
    )
    return resolved


def destination_index(packages):
    result = {}
    for component, contents in packages.items():
        for content in contents:
            destination = content["destination"]
            require(destination not in result, "package destination is duplicated")
            result[destination] = {"component": component, "content": content}
    return result


def follow_package_symlink(index, destination):
    seen = set()
    current = destination
    while True:
        require(current not in seen, "package provider symlink cycle: %s" % destination)
        seen.add(current)
        record = index.get(current)
        if record is None:
            return None
        content = record["content"]
        if content["type"] != "symlink":
            return record
        target = content["link_target"]
        current = (
            posixpath.normpath(target)
            if target.startswith("/")
            else posixpath.normpath(posixpath.join(posixpath.dirname(current), target))
        )
        require(current.startswith("/"), "package provider symlink escapes package root")


def package_provider(index, directory, soname):
    destination = posixpath.join(directory, soname)
    record = follow_package_symlink(index, destination)
    if record is None:
        return None
    content = record["content"]
    elf = content.get("elf")
    require(
        content["type"] == "file"
        and isinstance(elf, dict)
        and elf.get("type") == "dynamic"
        and elf.get("soname") == soname,
        "packaged DT_NEEDED provider is not a matching shared library: %s"
        % destination,
    )
    return {
        "soname": soname,
        "kind": "package",
        "destination": record["content"]["destination"],
        "component": record["component"],
        "sha256": content["sha256"],
    }


def sysroot_provider(sysroot, directory, soname):
    if directory not in SYSTEM_LIBRARY_DIRECTORIES:
        return None
    root = Path(sysroot).resolve()
    directory_path = Path(sysroot) / directory.lstrip("/")
    paths = [directory_path / soname]
    if directory_path.is_dir():
        paths.extend(sorted(directory_path.glob("*/" + soname)))
    paths = [path for path in paths if path.exists() or path.is_symlink()]
    if not paths:
        return None
    resolved_paths = {}
    for candidate in paths:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ElfError(
                "cannot resolve sysroot provider %s: %s" % (candidate, error)
            ) from error
        require(root in resolved.parents, "sysroot provider escapes the target root")
        require(resolved.is_file(), "sysroot provider is not a regular file")
        resolved_paths[str(resolved)] = resolved
    require(
        len(resolved_paths) == 1,
        "sysroot has ambiguous provider %s below %s" % (soname, directory),
    )
    resolved = next(iter(resolved_paths.values()))
    canonical_destination = "/" + str(resolved.relative_to(root))
    return {
        "soname": soname,
        "kind": "sysroot",
        "destination": canonical_destination,
        "component": None,
        "sha256": sha256_file(resolved),
    }


def resolve_needed_provider(
    consumer_component,
    consumer_destination,
    soname,
    runpath,
    index,
    component_dependencies,
    sysroot,
):
    search = [item["directory"] for item in resolve_runpath(consumer_destination, runpath)]
    search.extend(SYSTEM_LIBRARY_DIRECTORIES)
    candidates = []
    for directory in search:
        packaged = package_provider(index, directory, soname)
        system = sysroot_provider(sysroot, directory, soname)
        if packaged is not None:
            candidates.append(packaged)
        if system is not None:
            candidates.append(system)
    unique = {}
    for candidate in candidates:
        key = (
            candidate["kind"],
            candidate["component"],
            candidate["destination"],
            candidate["sha256"],
        )
        unique[key] = candidate
    candidates = sorted(
        unique.values(),
        key=lambda item: (
            item["kind"],
            item["component"] or "",
            item["destination"],
        ),
    )
    require(
        candidates,
        "ELF has unresolved DT_NEEDED provider %s from %s"
        % (soname, consumer_destination),
    )
    require(
        len(candidates) == 1,
        "ELF has ambiguous DT_NEEDED provider %s from %s: %s"
        % (
            soname,
            consumer_destination,
            ", ".join(item["destination"] for item in candidates),
        ),
    )
    provider = candidates[0]
    if provider["kind"] == "package" and provider["component"] != consumer_component:
        allowed = set(component_dependencies.get(consumer_component, ()))
        require(
            provider["component"] in allowed,
            "ELF provider crosses an undeclared component dependency: %s -> %s"
            % (consumer_component, provider["component"]),
        )
    return provider


def audit_packages(
    packages,
    staging_root,
    target,
    readelf,
    sysroot,
    component_dependencies=None,
):
    readelf = executable(readelf, "target readelf")
    version_output_sha256 = validate_readelf(readelf)
    providers = provider_inventory(sysroot)
    component_dependencies = component_dependencies or {}
    elf_count = 0
    for component in sorted(packages):
        for content in packages[component]:
            if "elf" not in content:
                continue
            path = Path(staging_root) / content["source"]
            header = header_identity(path, target)
            dynamic = dynamic_identity(
                run_readelf(readelf, ("--wide", "--dynamic"), path)
            )
            exports = dynamic_symbols(
                run_readelf(readelf, ("--wide", "--dyn-syms"), path)
            )
            content["elf"] = dict(
                header,
                soname=dynamic["soname"],
                needed=dynamic["needed"],
                runpath=dynamic["runpath"],
                exports_count=len(exports),
                exports_sha256=canonical_sha256(exports),
            )
            elf_count += 1
    index = destination_index(packages)
    for component in sorted(packages):
        for content in packages[component]:
            if "elf" not in content:
                continue
            content["elf"]["runpath_resolution"] = resolve_runpath(
                content["destination"], content["elf"]["runpath"]
            )
            content["elf"]["needed_providers"] = [
                resolve_needed_provider(
                    component,
                    content["destination"],
                    soname,
                    content["elf"]["runpath"],
                    index,
                    component_dependencies,
                    sysroot,
                )
                for soname in content["elf"]["needed"]
            ]
    return {
        "readelf_sha256": sha256_file(readelf.resolve(strict=True)),
        "readelf_version_output_sha256": version_output_sha256,
        "providers_count": len(providers),
        "providers_sha256": canonical_sha256(providers),
        "elf_count": elf_count,
    }
