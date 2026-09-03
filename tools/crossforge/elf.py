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
            require(
                entry == "$ORIGIN"
                or (
                    entry.startswith("$ORIGIN/")
                    and posixpath.normpath(entry) == entry
                    and ".." not in entry.split("/")
                ),
                "unsafe DT_RUNPATH entry: %s" % entry,
            )
            search.append(entry)
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


def audit_packages(packages, staging_root, target, readelf, sysroot):
    readelf = executable(readelf, "target readelf")
    version_output_sha256 = validate_readelf(readelf)
    providers = provider_inventory(sysroot)
    available = set(providers)
    for contents in packages.values():
        for content in contents:
            if content["type"] == "file" and "elf" in content:
                available.add(posixpath.basename(content["destination"]))
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
            missing = sorted(set(dynamic["needed"]) - available)
            require(
                not missing,
                "ELF has unresolved DT_NEEDED providers: %s"
                % ", ".join(missing),
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
    return {
        "readelf_sha256": sha256_file(readelf.resolve(strict=True)),
        "readelf_version_output_sha256": version_output_sha256,
        "providers_count": len(providers),
        "providers_sha256": canonical_sha256(providers),
        "elf_count": elf_count,
    }
