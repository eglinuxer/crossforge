#!/usr/bin/env python3
"""Validate canonical RPM plans, DNF transactions, and content locks."""

import argparse
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


REPOSITORY = Path(__file__).resolve().parents[1]
STRICT = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
ValidationError = STRICT["ValidationError"]
load_json = STRICT["load_json"]
COMPONENT_READER = None
SCHEMAS = {
    "rpm-plan": REPOSITORY / "config/schemas/rpm-plan.schema.json",
    "rpm-transaction": REPOSITORY / "config/schemas/rpm-transaction.schema.json",
    "rpm-lock": REPOSITORY / "config/schemas/rpm-lock.schema.json",
}
TARGET_TRIPLES = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}


def component_reader():
    global COMPONENT_READER
    if COMPONENT_READER is None:
        COMPONENT_READER = runpy.run_path(
            str(REPOSITORY / "scripts/release_component.py")
        )
    return COMPONENT_READER


SYSROOT_ROOTS = {
    "glibc",
    "glibc-devel",
    "glibc-headers",
    "glibc-minimal-langpack",
    "kernel-headers",
    "libgcc",
    "libstdc++",
    "bzip2-devel",
    "libffi-devel",
    "libuuid-devel",
    "openssl-devel",
    "sqlite-devel",
    "xz-devel",
    "zlib-devel",
}
SYSROOT_FORBIDDEN = {
    "binutils",
    "gcc",
    "glibc-static",
    "libstdc++-devel",
    "libstdc++-static",
}
HOST_COMMON_ROOTS = {
    "bzip2",
    "diffutils",
    "file",
    "findutils",
    "gcc-toolset-15-binutils",
    "gcc-toolset-15-gcc",
    "gcc-toolset-15-gcc-c++",
    "glibc-devel",
    "gmp-devel",
    "gzip",
    "libmpc-devel",
    "make",
    "mpfr-devel",
    "patch",
    "perl-interpreter",
    "redhat-rpm-config",
    "rpm-build",
    "scl-utils-build",
    "sed",
    "tar",
    "which",
    "xz",
    "zlib-devel",
}
HOST_GCC_ROOTS = {"bison", "flex", "libzstd-devel"}
HOST_GCC_TEST_ROOTS = {"dejagnu", "expect", "gcc"}
HOST_GCC_TEST_FORWARD = {
    "annobin",
    "cpp",
    "dejagnu",
    "expect",
    "gcc",
    "gcc-plugin-annobin",
    "isl",
    "libgcc",
}
HOST_GCC_TEST_REPOSITORIES = {
    "baseos": (
        "https://download.rockylinux.org/pub/rocky/8.10/BaseOS/x86_64/os/"
    ),
    "appstream": (
        "https://download.rockylinux.org/pub/rocky/8.10/AppStream/x86_64/os/"
    ),
    "powertools": (
        "https://download.rockylinux.org/pub/rocky/8.10/PowerTools/x86_64/os/"
    ),
}
HOST_PYTHON_ROOTS = {
    "bzip2-devel",
    "libffi-devel",
    "libuuid-devel",
    "openssl-devel",
    "sqlite-devel",
    "xz-devel",
}
HOST_RUNTIME_ROOTS = {
    "autoconf",
    "automake",
    "bash",
    "bison",
    "bzip2",
    "bzip2-libs",
    "ca-certificates",
    "cmake",
    "coreutils-single",
    "curl",
    "diffutils",
    "file",
    "findutils",
    "flex",
    "gawk",
    "gcc-toolset-15-binutils",
    "gcc-toolset-15-gcc",
    "gcc-toolset-15-gcc-c++",
    "git-core",
    "glibc-devel",
    "grep",
    "gzip",
    "libffi",
    "libtool",
    "libuuid",
    "make",
    "meson",
    "ninja-build",
    "openssl-libs",
    "patch",
    "perl-IPC-Cmd",
    "perl-Time-Piece",
    "pkgconf-pkg-config",
    "platform-python",
    "sed",
    "sqlite-libs",
    "tar",
    "unzip",
    "which",
    "xz",
    "xz-libs",
    "zip",
    "zlib",
}
HOST_RUNTIME_POWERTOOLS_FORWARD = {"meson", "ninja-build"}
HOST_RUNTIME_REPOSITORIES = {
    "baseos": (
        "https://download.rockylinux.org/pub/rocky/8.10/BaseOS/x86_64/os/"
    ),
    "appstream": (
        "https://download.rockylinux.org/pub/rocky/8.10/AppStream/x86_64/os/"
    ),
    "powertools": (
        "https://download.rockylinux.org/pub/rocky/8.10/PowerTools/x86_64/os/"
    ),
}
HOST_RUNTIME_ALLOWED_DEVEL = {
    "gcc-toolset-15-libstdc++-devel",
    "glibc-devel",
    "libxcrypt-devel",
    "platform-python-devel",
    "python36-devel",
}
HOST_RUNTIME_FORBIDDEN = {
    "bzip2-devel",
    "dejagnu",
    "expect",
    "gmp-devel",
    "gperf",
    "libffi-devel",
    "libmpc-devel",
    "libuuid-devel",
    "libzstd-devel",
    "mpfr-devel",
    "openssl-devel",
    "redhat-rpm-config",
    "rpm-build",
    "scl-utils-build",
    "sqlite-devel",
    "xz-devel",
    "zlib-devel",
}
HOST_MODULES = [
    "perl:5.26",
    "perl-IO-Socket-SSL:2.066",
    "perl-libwww-perl:6.34",
]
QT_HOST_MODULES = ["nodejs:20"] + HOST_MODULES + ["python38:3.8"]
QT_TARGET_ROOTS = {
    "alsa-lib-devel",
    "at-spi2-core-devel",
    "cups-devel",
    "dbus-devel",
    "expat-devel",
    "fontconfig-devel",
    "freetype-devel",
    "glib2-devel",
    "libICE-devel",
    "libSM-devel",
    "libX11-devel",
    "libX11-xcb",
    "libXcomposite-devel",
    "libXcursor-devel",
    "libXdamage-devel",
    "libXext-devel",
    "libXfixes-devel",
    "libXi-devel",
    "libXrandr-devel",
    "libXrender-devel",
    "libXtst-devel",
    "libatomic",
    "libdrm-devel",
    "libinput-devel",
    "libjpeg-turbo-devel",
    "libpng-devel",
    "libxcb-devel",
    "libxkbcommon-devel",
    "libxkbcommon-x11-devel",
    "libxkbfile-devel",
    "libxshmfence-devel",
    "mesa-libEGL-devel",
    "mesa-libGL-devel",
    "mesa-libgbm-devel",
    "nss-devel",
    "openssl-devel",
    "pciutils-devel",
    "pulseaudio-libs-devel",
    "sqlite-devel",
    "systemd-devel",
    "wayland-devel",
    "wayland-protocols-devel",
    "xcb-util-devel",
    "xcb-util-image-devel",
    "xcb-util-keysyms-devel",
    "xcb-util-renderutil-devel",
    "xcb-util-wm-devel",
    "xorg-x11-proto-devel",
    "zlib-devel",
}
QT_TARGET_NOARCH_ROOTS = {
    "wayland-protocols-devel",
    "xorg-x11-proto-devel",
}
QT_RUNTIME_ROOTS = {
    "alsa-lib",
    "at-spi2-core",
    "ca-certificates",
    "cups-libs",
    "dbus-libs",
    "dejavu-sans-fonts",
    "expat",
    "fontconfig",
    "freetype",
    "glib2",
    "glibc",
    "libICE",
    "libSM",
    "libX11",
    "libX11-xcb",
    "libXcomposite",
    "libXcursor",
    "libXdamage",
    "libXext",
    "libXfixes",
    "libXi",
    "libXrandr",
    "libXrender",
    "libXtst",
    "libatomic",
    "libdrm",
    "libgcc",
    "libglvnd-egl",
    "libglvnd-gles",
    "libglvnd-glx",
    "libglvnd-opengl",
    "libgudev",
    "libinput",
    "libjpeg-turbo",
    "libpng",
    "libstdc++",
    "libwayland-client",
    "libwayland-cursor",
    "libwayland-egl",
    "libwayland-server",
    "libxcb",
    "libxkbcommon",
    "libxkbcommon-x11",
    "libxkbfile",
    "mesa-libgbm",
    "nspr",
    "nss",
    "openssl-libs",
    "pciutils-libs",
    "pulseaudio-libs",
    "systemd-libs",
    "xcb-util",
    "xcb-util-image",
    "xcb-util-keysyms",
    "xcb-util-renderutil",
    "xcb-util-wm",
    "xkeyboard-config",
    "zlib",
}
QT_RUNTIME_NOARCH_ROOTS = {
    "ca-certificates",
    "dejavu-sans-fonts",
    "xkeyboard-config",
}
HOST_QT_ROOTS = (QT_TARGET_ROOTS - {"libatomic"}) | {
    "bison",
    "flex",
    "gcc-toolset-15-libatomic-devel",
    "gperf",
    "nodejs",
    "pkgconf-pkg-config",
    "python3-html5lib",
    "python38",
    "yasm",
}
QT_TARGET_PARENT_LOCKS = {
    "x86_64": (
        "locks/sysroot-el8-x86_64.json",
        "ffe67b4d7448b02be7db774c8d85fdc8b13da695c0184de444949c641876ef57",
    ),
    "aarch64": (
        "locks/sysroot-el8-aarch64.json",
        "70436d94443b019fbe61237bf93b8bb457927f3de09429e6ef7f37598b482b67",
    ),
}
HOST_QT_PARENT_LOCK = (
    "locks/host-build-common-el8-x86_64.json",
    "d6d1b33772faa48658a30b01f5cc6a254c0b6c7640d52296b2e0f4efe7945c01",
)
HOST_QT_POWERTOOLS_FORWARD = {
    "gperf",
    "libinput-devel",
    "libxkbcommon-x11-devel",
    "libxkbfile-devel",
    "mesa-libgbm-devel",
    "xcb-util-devel",
    "xcb-util-image-devel",
    "xcb-util-keysyms-devel",
    "xcb-util-renderutil-devel",
    "xcb-util-wm-devel",
    "yasm",
}
HOST_QT_REPLACEMENTS = {
    "dbus",
    "dbus-common",
    "dbus-daemon",
    "dbus-libs",
    "dbus-tools",
    "expat",
    "glib2",
    "openssl",
    "openssl-libs",
    "sqlite-libs",
    "systemd",
    "systemd-libs",
    "systemd-pam",
}
QT_TARGET_POWERTOOLS_FORWARD = HOST_QT_POWERTOOLS_FORWARD - {"gperf", "yasm"}
QT_FORBIDDEN_HOST_TOOLS = {
    "bison",
    "flex",
    "gperf",
    "nodejs",
    "python3-html5lib",
    "python38",
}


def canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def safe_posix_location(value, label):
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "?" in value
        or "#" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError("unsafe %s: %s" % (label, value))
    relative = PurePosixPath(value)
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or ":" in relative.parts[0]
        or str(relative) != value
    ):
        raise ValidationError("unsafe %s: %s" % (label, value))
    return relative


def checked_metadata_path(metadata_root, location, trusted_anchor):
    relative = safe_posix_location(location, "repository metadata location")
    if trusted_anchor.is_symlink():
        raise ValidationError("trusted metadata anchor is a symlink")
    try:
        root_relative = metadata_root.relative_to(trusted_anchor)
    except ValueError:
        raise ValidationError("repository metadata root escapes its trusted anchor")
    current = trusted_anchor
    for part in root_relative.parts + relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValidationError("repository metadata path contains a symlink")
    anchor = trusted_anchor.resolve()
    root = metadata_root.resolve()
    path = current.resolve()
    if anchor != root and anchor not in root.parents:
        raise ValidationError("repository metadata root escaped its trusted anchor")
    if root != path and root not in path.parents:
        raise ValidationError("repository metadata escaped its directory")
    return current


def parse_repomd(path):
    namespace = {"repo": "http://linux.duke.edu/metadata/repo"}
    try:
        root = ElementTree.parse(str(path)).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ValidationError("cannot parse checked repomd.xml: %s" % error)
    revision = root.find("repo:revision", namespace)
    records = []
    seen_types = set()
    seen_locations = set()
    for element in root.findall("repo:data", namespace):
        metadata_type = element.get("type")
        checksum = element.find("repo:checksum", namespace)
        open_checksum = element.find("repo:open-checksum", namespace)
        location = element.find("repo:location", namespace)
        size = element.find("repo:size", namespace)
        open_size = element.find("repo:open-size", namespace)
        if (
            not metadata_type
            or metadata_type in seen_types
            or checksum is None
            or checksum.get("type") != "sha256"
            or not is_sha256(checksum.text)
            or location is None
            or not location.get("href")
            or size is None
            or not size.text
        ):
            raise ValidationError("invalid or duplicate checked repomd record")
        relative = str(
            safe_posix_location(location.get("href"), "repomd metadata location")
        )
        if relative in seen_locations:
            raise ValidationError("duplicate checked repomd metadata location")
        try:
            compressed_size = int(size.text)
            expanded_size = int(open_size.text) if open_size is not None else None
        except (TypeError, ValueError):
            raise ValidationError("invalid checked repomd metadata size")
        if compressed_size <= 0 or (expanded_size is not None and expanded_size < 0):
            raise ValidationError("invalid checked repomd metadata size")
        expanded_checksum = None
        if open_checksum is not None:
            if open_checksum.get("type") != "sha256" or not is_sha256(open_checksum.text):
                raise ValidationError("invalid checked repomd open checksum")
            expanded_checksum = {
                "algorithm": "sha256",
                "value": open_checksum.text,
            }
        if (expanded_checksum is None) != (expanded_size is None):
            raise ValidationError("checked repomd open checksum/size must be paired")
        records.append(
            {
                "type": metadata_type,
                "location": relative,
                "checksum": {"algorithm": "sha256", "value": checksum.text},
                "size": compressed_size,
                "open_checksum": expanded_checksum,
                "open_size": expanded_size,
            }
        )
        seen_types.add(metadata_type)
        seen_locations.add(relative)
    missing = sorted({"primary", "filelists", "primary_db"} - seen_types)
    if missing:
        raise ValidationError("checked repomd is missing metadata: %s" % ", ".join(missing))
    return (
        revision.text if revision is not None and revision.text else None,
        sorted(records, key=lambda item: (item["type"], item["location"])),
    )


def validate_repomd_claim(repository, repomd_path):
    revision, records = parse_repomd(repomd_path)
    if revision != repository["repomd"]["revision"]:
        raise ValidationError("transaction revision differs from signed repomd")
    if records != repository["metadata"]:
        raise ValidationError("transaction metadata differs from signed repomd")


def validate_repository_trust(repository, trust):
    if repository["gpg_key"] != {
        "sha256": trust["sha256"],
        "fingerprint": trust["fingerprint"],
    }:
        raise ValidationError("repository key differs from release binding")
    if repository["repomd"]["signature"]["fingerprint"] != trust["fingerprint"]:
        raise ValidationError("repomd signature claim differs from release binding")


def verify_detached_signature(key, fingerprint, signature, content):
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    with tempfile.TemporaryDirectory(prefix="crossforge-repomd-gpg-") as temporary:
        os.chmod(temporary, 0o700)
        imported = subprocess.run(
            [
                "gpg",
                "--batch",
                "--no-autostart",
                "--homedir",
                temporary,
                "--import",
                str(key),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if imported.returncode != 0:
            raise ValidationError("cannot import locked Rocky key for repomd verification")
        verified = subprocess.run(
            [
                "gpg",
                "--batch",
                "--no-autostart",
                "--homedir",
                temporary,
                "--status-fd",
                "1",
                "--verify",
                str(signature),
                str(content),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if verified.returncode != 0:
            raise ValidationError("repomd detached signature verification failed")
        valid = [
            line.split()[2].lower()
            for line in verified.stdout.splitlines()
            if line.startswith("[GNUPG:] VALIDSIG ") and len(line.split()) >= 3
        ]
        if valid != [fingerprint]:
            raise ValidationError("repomd signature uses an unexpected key")


def schema_for(document):
    if not isinstance(document, dict) or document.get("kind") not in SCHEMAS:
        raise ValidationError("unsupported RPM document kind")
    return load_json(SCHEMAS[document["kind"]])


def validate_schema(document):
    schema = schema_for(document)
    STRICT["validate_schema_subset"](schema)
    STRICT["validate"](document, schema, schema, "$")


def reject_duplicates(values, label):
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValidationError("duplicate %s: %s" % (label, ", ".join(duplicates)))


def sorted_unique(values, label):
    if values != sorted(values) or len(values) != len(set(values)):
        raise ValidationError("%s must be sorted and unique" % label)


def nevra_name_arch(nevra):
    try:
        name_epoch, _version_release = nevra.split(":", 1)
        name, epoch = name_epoch.rsplit("-", 1)
        arch = nevra.rsplit(".", 1)[1]
    except (IndexError, ValueError):
        raise ValidationError("invalid canonical NEVRA: %s" % nevra)
    if not name or not epoch.isdigit() or not arch:
        raise ValidationError("invalid canonical NEVRA: %s" % nevra)
    return name, arch


def repository_file(reference, label):
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("unsafe %s: %s" % (label, reference))
    path = (REPOSITORY / relative).resolve()
    if REPOSITORY not in path.parents:
        raise ValidationError("%s escapes the repository" % label)
    return path


def expected_role_roots(role):
    return {
        "target-sysroot": SYSROOT_ROOTS,
        "host-build-common": HOST_COMMON_ROOTS,
        "host-gcc-build": HOST_GCC_ROOTS,
        "host-gcc-test": HOST_GCC_TEST_ROOTS,
        "host-python-build": HOST_PYTHON_ROOTS,
        "host-runtime": HOST_RUNTIME_ROOTS,
        "host-qt-build": HOST_QT_ROOTS,
        "qt-target": QT_TARGET_ROOTS,
        "qt-runtime": QT_RUNTIME_ROOTS,
    }[role]


def validate_locked_host_runtime_contract(transaction):
    """Enforce the product runtime policy without reopening its plan."""
    identity = transaction["identity"]
    if identity != {
        "name": "host-runtime-el8-x86_64",
        "role": "host-runtime",
        "distribution": "rocky",
        "release": "8.10",
        "baseline": "el8",
        "arch": "x86_64",
        "target_triple": None,
    }:
        raise ValidationError("locked host runtime identity differs")
    if transaction["base"] != {
        "mode": "image",
        "parent_lock": None,
        "parent_sha256": None,
    }:
        raise ValidationError("locked host runtime base differs")
    if transaction["solver_policy"] != {
        "allowed_arches": ["x86_64", "noarch"],
        "install_weak_deps": False,
        "best": True,
        "strict": True,
        "allow_erasing": False,
        "module_platform_id": "platform:el8",
        "enabled_modules": sorted(HOST_MODULES),
    }:
        raise ValidationError("locked host runtime solver policy differs")
    if transaction["resolver"]["load_system_repo"] is not True:
        raise ValidationError("host runtime must resolve from the image RPMDB")
    repositories = {
        item["id"]: item["baseurl"] for item in transaction["repositories"]
    }
    if repositories != HOST_RUNTIME_REPOSITORIES:
        raise ValidationError("locked host runtime repositories differ")
    requests = transaction["requests"]
    request_names = [item["name"] for item in requests]
    if request_names != sorted(HOST_RUNTIME_ROOTS):
        raise ValidationError("locked host runtime roots differ")
    for request in requests:
        resolved_name, resolved_arch = nevra_name_arch(
            request["resolved_nevra"]
        )
        if (
            request["arch"] != "any"
            or request["purpose"] != "host-runtime"
            or resolved_name != request["name"]
            or resolved_arch not in ("x86_64", "noarch")
        ):
            raise ValidationError("locked host runtime request differs")
    forward = [
        item
        for item in transaction["items"]
        if item["action"] in ("install", "upgrade")
    ]
    removed = [
        item for item in transaction["items"] if item["action"] == "remove"
    ]
    powertools_names = {
        item["name"]
        for item in forward
        if item["repo_id"] == "powertools"
    }
    if powertools_names != HOST_RUNTIME_POWERTOOLS_FORWARD:
        raise ValidationError("host runtime PowerTools package set differs")
    base = validate_manifest(
        transaction["manifests"]["base"], "host runtime base manifest"
    )
    remove_manifest = validate_manifest(
        transaction["manifests"]["remove"],
        "host runtime remove manifest",
    )
    result = validate_manifest(
        transaction["manifests"]["result"], "host runtime result manifest"
    )
    forward_nevras = {item["nevra"] for item in forward}
    removed_nevras = {item["nevra"] for item in removed}
    if (
        removed_nevras != set(remove_manifest)
        or (set(base) - removed_nevras) | forward_nevras != set(result)
    ):
        raise ValidationError("locked host runtime transaction algebra differs")
    for request in requests:
        expected_disposition = (
            "transaction"
            if request["resolved_nevra"] in forward_nevras
            else "base"
        )
        if (
            request["resolved_nevra"] not in result
            or request["disposition"] != expected_disposition
        ):
            raise ValidationError("locked host runtime root disposition differs")
    result_names = {nevra_name_arch(nevra)[0] for nevra in result}
    forbidden = sorted(result_names.intersection(HOST_RUNTIME_FORBIDDEN))
    if forbidden:
        raise ValidationError(
            "build-only packages entered host runtime: %s"
            % ", ".join(forbidden)
        )
    devel = {name for name in result_names if name.endswith("-devel")}
    if devel != HOST_RUNTIME_ALLOWED_DEVEL:
        raise ValidationError("host runtime development package set differs")


def validate_locked_host_gcc_test_contract(transaction):
    """Keep the DejaGNU closure test-only and exactly origin-scoped."""
    if transaction["identity"] != {
        "name": "host-gcc-test-el8-x86_64",
        "role": "host-gcc-test",
        "distribution": "rocky",
        "release": "8.10",
        "baseline": "el8",
        "arch": "x86_64",
        "target_triple": None,
    }:
        raise ValidationError("locked GCC test host identity differs")
    if transaction["base"] != {
        "mode": "lock",
        "parent_lock": "locks/host-gcc-build-el8-x86_64.json",
        "parent_sha256": (
            "de271a4ba8a9e4bc31c8fc8a92c05fde"
            "4736afbc64b1251e06751a0cb96c2be0"
        ),
    }:
        raise ValidationError("locked GCC test host base differs")
    if transaction["solver_policy"] != {
        "allowed_arches": ["x86_64", "noarch"],
        "install_weak_deps": False,
        "best": True,
        "strict": True,
        "allow_erasing": False,
        "module_platform_id": "platform:el8",
        "enabled_modules": sorted(HOST_MODULES),
    }:
        raise ValidationError("locked GCC test host solver policy differs")
    if transaction["resolver"]["load_system_repo"] is not True:
        raise ValidationError("GCC test host must resolve from its parent RPMDB")
    repositories = {
        item["id"]: item["baseurl"] for item in transaction["repositories"]
    }
    if repositories != HOST_GCC_TEST_REPOSITORIES:
        raise ValidationError("locked GCC test host repositories differ")
    requests = transaction["requests"]
    if [request["name"] for request in requests] != sorted(HOST_GCC_TEST_ROOTS):
        raise ValidationError("locked GCC test host roots differ")
    expected_origins = {
        "annobin": "appstream",
        "cpp": "appstream",
        "dejagnu": "powertools",
        "expect": "baseos",
        "gcc": "appstream",
        "gcc-plugin-annobin": "appstream",
        "isl": "appstream",
        "libgcc": "baseos",
    }
    expected_reasons = {
        "annobin": "dependency",
        "cpp": "dependency",
        "dejagnu": "user",
        "expect": "user",
        "gcc": "user",
        "gcc-plugin-annobin": "dependency",
        "isl": "dependency",
        "libgcc": "unknown",
    }
    expected_actions = {
        name: ("upgrade" if name == "libgcc" else "install")
        for name in HOST_GCC_TEST_FORWARD
    }
    forward = [
        item
        for item in transaction["items"]
        if item["action"] in ("install", "upgrade")
    ]
    if {item["name"] for item in forward} != HOST_GCC_TEST_FORWARD:
        raise ValidationError("GCC test host delta differs")
    for item in forward:
        if (
            item["action"] != expected_actions[item["name"]]
            or item["reason"] != expected_reasons[item["name"]]
            or item["repo_id"] != expected_origins[item["name"]]
        ):
            raise ValidationError("GCC test host package origin differs")
    removed = [item for item in transaction["items"] if item["action"] == "remove"]
    if len(removed) != 1 or (
        removed[0]["name"], removed[0]["reason"], removed[0]["repo_id"]
    ) != ("libgcc", "unknown", "@System"):
        raise ValidationError("GCC test host replacement set differs")
    forward_nevras = {item["nevra"] for item in forward}
    for request in requests:
        resolved_name, resolved_arch = nevra_name_arch(request["resolved_nevra"])
        if (
            request["arch"] != "any"
            or request["purpose"] != "gcc-testsuite"
            or request["disposition"] != "transaction"
            or request["resolved_nevra"] not in forward_nevras
            or resolved_name != request["name"]
            or resolved_arch not in ("x86_64", "noarch")
        ):
            raise ValidationError("locked GCC test host request differs")
    base = set(validate_manifest(
        transaction["manifests"]["base"], "GCC test host base manifest"
    ))
    result = set(validate_manifest(
        transaction["manifests"]["result"], "GCC test host result manifest"
    ))
    remove = set(validate_manifest(
        transaction["manifests"]["remove"], "GCC test host remove manifest"
    ))
    if remove != {removed[0]["nevra"]} or result != (base - remove) | forward_nevras:
        raise ValidationError("GCC test host transaction algebra differs")


def validate_locked_host_qt_contract(transaction):
    """Keep the large Qt build closure exact, host-only, and Rocky-only."""
    if transaction["identity"] != {
        "name": "host-qt-build-el8-x86_64",
        "role": "host-qt-build",
        "distribution": "rocky",
        "release": "8.10",
        "baseline": "el8",
        "arch": "x86_64",
        "target_triple": None,
    }:
        raise ValidationError("locked Qt host identity differs")
    if transaction["base"] != {
        "mode": "lock",
        "parent_lock": HOST_QT_PARENT_LOCK[0],
        "parent_sha256": HOST_QT_PARENT_LOCK[1],
    }:
        raise ValidationError("locked Qt host parent differs")
    if transaction["solver_policy"] != {
        "allowed_arches": ["x86_64", "noarch"],
        "install_weak_deps": False,
        "best": True,
        "strict": True,
        "allow_erasing": False,
        "module_platform_id": "platform:el8",
        "enabled_modules": sorted(QT_HOST_MODULES),
    }:
        raise ValidationError("locked Qt host solver policy differs")
    if transaction["resolver"]["load_system_repo"] is not True:
        raise ValidationError("Qt host must resolve from its parent RPMDB")
    repositories = {
        item["id"]: item["baseurl"] for item in transaction["repositories"]
    }
    if repositories != {
        "baseos": (
            "https://download.rockylinux.org/pub/rocky/8.10/BaseOS/x86_64/os/"
        ),
        "appstream": (
            "https://download.rockylinux.org/pub/rocky/8.10/AppStream/x86_64/os/"
        ),
        "powertools": (
            "https://download.rockylinux.org/pub/rocky/8.10/PowerTools/x86_64/os/"
        ),
    }:
        raise ValidationError("locked Qt host repositories differ")
    requests = transaction["requests"]
    if {request["name"] for request in requests} != HOST_QT_ROOTS:
        raise ValidationError("locked Qt host roots differ")
    for request in requests:
        _name, resolved_arch = nevra_name_arch(request["resolved_nevra"])
        if (
            request["arch"] != "any"
            or request["purpose"] != "qt-host-build"
            or resolved_arch not in ("x86_64", "noarch")
        ):
            raise ValidationError("locked Qt host request differs")
    forward = [
        item
        for item in transaction["items"]
        if item["action"] in ("install", "upgrade")
    ]
    powertools = {
        item["name"] for item in forward if item["repo_id"] == "powertools"
    }
    if powertools != HOST_QT_POWERTOOLS_FORWARD:
        raise ValidationError("locked Qt host PowerTools package set differs")
    removed = {
        item["name"]
        for item in transaction["items"]
        if item["action"] == "remove"
    }
    upgraded = {
        item["name"] for item in forward if item["action"] == "upgrade"
    }
    if removed != HOST_QT_REPLACEMENTS or upgraded != HOST_QT_REPLACEMENTS:
        raise ValidationError("locked Qt host replacement set differs")


def validate_locked_qt_target_contract(transaction):
    """Keep each Qt target overlay architecture-pure and parent-bound."""
    identity = transaction["identity"]
    arch = identity["arch"]
    if identity != {
        "name": "qt-target-el8-%s" % arch,
        "role": "qt-target",
        "distribution": "rocky",
        "release": "8.10",
        "baseline": "el8",
        "arch": arch,
        "target_triple": TARGET_TRIPLES.get(arch),
    }:
        raise ValidationError("locked Qt target identity differs")
    parent = QT_TARGET_PARENT_LOCKS.get(arch)
    if parent is None or transaction["base"] != {
        "mode": "lock",
        "parent_lock": parent[0],
        "parent_sha256": parent[1],
    }:
        raise ValidationError("locked Qt target parent differs")
    if transaction["solver_policy"] != {
        "allowed_arches": [arch, "noarch"],
        "install_weak_deps": False,
        "best": True,
        "strict": True,
        "allow_erasing": False,
        "module_platform_id": "platform:el8",
        "enabled_modules": [],
    }:
        raise ValidationError("locked Qt target solver policy differs")
    repositories = {
        item["id"]: item["baseurl"] for item in transaction["repositories"]
    }
    if repositories != {
        "baseos": (
            "https://download.rockylinux.org/pub/rocky/8.10/BaseOS/%s/os/" % arch
        ),
        "appstream": (
            "https://download.rockylinux.org/pub/rocky/8.10/AppStream/%s/os/" % arch
        ),
        "powertools": (
            "https://download.rockylinux.org/pub/rocky/8.10/PowerTools/%s/os/" % arch
        ),
    }:
        raise ValidationError("locked Qt target repositories differ")
    if transaction["resolver"]["load_system_repo"] is not True:
        raise ValidationError("Qt target must resolve from its parent RPMDB")
    requests = transaction["requests"]
    if {request["name"] for request in requests} != QT_TARGET_ROOTS:
        raise ValidationError("locked Qt target roots differ")
    for request in requests:
        expected_selector = (
            "noarch"
            if request["name"] in QT_TARGET_NOARCH_ROOTS
            else "target"
        )
        _name, resolved_arch = nevra_name_arch(request["resolved_nevra"])
        expected_arch = "noarch" if expected_selector == "noarch" else arch
        if (
            request["arch"] != expected_selector
            or request["purpose"] != "qt-target"
            or resolved_arch != expected_arch
        ):
            raise ValidationError("locked Qt target request differs")
    if any(item["action"] != "install" for item in transaction["items"]):
        raise ValidationError("Qt target overlay may only install RPMs")
    forward_names = {item["name"] for item in transaction["items"]}
    if forward_names.intersection(QT_FORBIDDEN_HOST_TOOLS):
        raise ValidationError("host tool entered the Qt target overlay")
    powertools = {
        item["name"]
        for item in transaction["items"]
        if item["repo_id"] == "powertools"
    }
    if powertools != QT_TARGET_POWERTOOLS_FORWARD:
        raise ValidationError("locked Qt target PowerTools package set differs")


def validate_locked_qt_runtime_contract(transaction):
    """Keep the clean-Rocky Qt runtime closure target-only and development-free."""
    identity = transaction["identity"]
    arch = identity["arch"]
    if identity != {
        "name": "qt-runtime-el8-%s" % arch,
        "role": "qt-runtime",
        "distribution": "rocky",
        "release": "8.10",
        "baseline": "el8",
        "arch": arch,
        "target_triple": TARGET_TRIPLES.get(arch),
    }:
        raise ValidationError("locked Qt runtime identity differs")
    if transaction["base"] != {
        "mode": "empty",
        "parent_lock": None,
        "parent_sha256": None,
    }:
        raise ValidationError("locked Qt runtime must resolve from an empty root")
    if transaction["solver_policy"] != {
        "allowed_arches": [arch, "noarch"],
        "install_weak_deps": False,
        "best": True,
        "strict": True,
        "allow_erasing": False,
        "module_platform_id": "platform:el8",
        "enabled_modules": [],
    }:
        raise ValidationError("locked Qt runtime solver policy differs")
    repositories = {
        item["id"]: item["baseurl"] for item in transaction["repositories"]
    }
    if repositories != {
        "baseos": (
            "https://download.rockylinux.org/pub/rocky/8.10/BaseOS/%s/os/" % arch
        ),
        "appstream": (
            "https://download.rockylinux.org/pub/rocky/8.10/AppStream/%s/os/" % arch
        ),
        "powertools": (
            "https://download.rockylinux.org/pub/rocky/8.10/PowerTools/%s/os/" % arch
        ),
    }:
        raise ValidationError("locked Qt runtime repositories differ")
    if transaction["resolver"]["load_system_repo"] is not False:
        raise ValidationError("Qt runtime must resolve independently of the host RPMDB")
    requests = transaction["requests"]
    if {request["name"] for request in requests} != QT_RUNTIME_ROOTS:
        raise ValidationError("locked Qt runtime roots differ")
    for request in requests:
        expected_selector = (
            "noarch"
            if request["name"] in QT_RUNTIME_NOARCH_ROOTS
            else "target"
        )
        _name, resolved_arch = nevra_name_arch(request["resolved_nevra"])
        expected_arch = "noarch" if expected_selector == "noarch" else arch
        if (
            request["arch"] != expected_selector
            or request["purpose"] != "qt-runtime"
            or resolved_arch != expected_arch
            or request["disposition"] != "transaction"
        ):
            raise ValidationError("locked Qt runtime request differs")
    if transaction["manifests"]["base"]["packages"]:
        raise ValidationError("locked Qt runtime base manifest is not empty")
    if transaction["manifests"]["remove"]["packages"]:
        raise ValidationError("locked Qt runtime remove manifest is not empty")
    if any(item["action"] != "install" for item in transaction["items"]):
        raise ValidationError("Qt runtime closure may only install RPMs")
    forbidden = sorted(
        item["name"]
        for item in transaction["items"]
        if item["name"].endswith(("-devel", "-headers", "-static"))
        or item["name"] in QT_FORBIDDEN_HOST_TOOLS
    )
    if forbidden:
        raise ValidationError(
            "development or host packages entered the Qt runtime: %s"
            % ", ".join(forbidden)
        )


def validate_plan_semantics(plan):
    identity = plan["identity"]
    role = identity["role"]
    arch = identity["arch"]
    if role in ("target-sysroot", "qt-target", "qt-runtime"):
        expected_name = {
            "target-sysroot": "sysroot-el8-%s" % arch,
            "qt-target": "qt-target-el8-%s" % arch,
            "qt-runtime": "qt-runtime-el8-%s" % arch,
        }[role]
        if identity["target_triple"] != TARGET_TRIPLES[arch]:
            raise ValidationError("target RPM triple and arch disagree")
        expected_mode = "lock" if role == "qt-target" else "empty"
        if plan["base"]["mode"] != expected_mode:
            raise ValidationError(
                "%s must use a %s base" % (role, expected_mode)
            )
        expected_repositories = ["baseos"]
        expected_repository_urls = [
            "https://download.rockylinux.org/pub/rocky/8.10/BaseOS/%s/os/" % arch
        ]
        if role in ("qt-target", "qt-runtime"):
            expected_repositories.extend(["appstream", "powertools"])
            expected_repository_urls.extend(
                [
                    "https://download.rockylinux.org/pub/rocky/8.10/AppStream/%s/os/"
                    % arch,
                    "https://download.rockylinux.org/pub/rocky/8.10/PowerTools/%s/os/"
                    % arch,
                ]
            )
            if role == "qt-target":
                parent_lock, parent_sha256 = QT_TARGET_PARENT_LOCKS[arch]
                if plan["base"] != {
                    "mode": "lock",
                    "parent_lock": parent_lock,
                    "parent_sha256": parent_sha256,
                }:
                    raise ValidationError("Qt target parent sysroot lock differs")
        expected_modules = []
    else:
        if arch != "x86_64":
            raise ValidationError("host RPM plans must use x86_64")
        expected_name = "%s-el8-x86_64" % role
        if identity["target_triple"] is not None:
            raise ValidationError("host plan target_triple must be null")
        expected_repositories = ["baseos", "appstream"]
        expected_repository_urls = [
            "https://download.rockylinux.org/pub/rocky/8.10/BaseOS/x86_64/os/",
            "https://download.rockylinux.org/pub/rocky/8.10/AppStream/x86_64/os/",
        ]
        if role in ("host-gcc-test", "host-runtime", "host-qt-build"):
            expected_repositories.append("powertools")
            expected_repository_urls.append(
                "https://download.rockylinux.org/pub/rocky/8.10/PowerTools/x86_64/os/"
            )
        expected_modules = (
            QT_HOST_MODULES if role == "host-qt-build" else HOST_MODULES
        )
        expected_mode = (
            "image"
            if role in ("host-build-common", "host-runtime")
            else "lock"
        )
        if plan["base"]["mode"] != expected_mode:
            raise ValidationError("%s must use a %s base" % (role, expected_mode))
        if role == "host-qt-build" and plan["base"] != {
            "mode": "lock",
            "parent_lock": HOST_QT_PARENT_LOCK[0],
            "parent_sha256": HOST_QT_PARENT_LOCK[1],
        }:
            raise ValidationError("Qt host parent build lock differs")
    if identity["name"] != expected_name:
        raise ValidationError("RPM plan identity name differs from its role/architecture")
    if [repo["id"] for repo in plan["repositories"]] != expected_repositories:
        raise ValidationError("repository order/set differs from the role contract")
    if [repo["baseurl"] for repo in plan["repositories"]] != expected_repository_urls:
        raise ValidationError("repository URLs differ from the Rocky role contract")
    if plan["solver_policy"]["allowed_arches"] != [arch, "noarch"]:
        raise ValidationError("allowed_arches must be target arch followed by noarch")
    if plan["solver_policy"]["module_platform_id"] != "platform:el8":
        raise ValidationError("module platform must be platform:el8")
    if plan["solver_policy"]["enabled_modules"] != expected_modules:
        raise ValidationError("enabled module streams differ from the role contract")
    root_names = [root["name"] for root in plan["roots"]]
    reject_duplicates(root_names, "root package")
    if set(root_names) != expected_role_roots(role):
        raise ValidationError("root set differs from the %s contract" % role)
    if role == "target-sysroot":
        if any(root["arch"] != "target" for root in plan["roots"]):
            raise ValidationError("sysroot roots must select the target arch")
    elif role == "qt-target":
        for root in plan["roots"]:
            expected_arch = (
                "noarch"
                if root["name"] in QT_TARGET_NOARCH_ROOTS
                else "target"
            )
            if root["arch"] != expected_arch:
                raise ValidationError("Qt target root architecture differs")
    elif role == "qt-runtime":
        for root in plan["roots"]:
            expected_arch = (
                "noarch"
                if root["name"] in QT_RUNTIME_NOARCH_ROOTS
                else "target"
            )
            if root["arch"] != expected_arch:
                raise ValidationError("Qt runtime root architecture differs")
    elif any(root["arch"] != "any" for root in plan["roots"]):
        raise ValidationError("host roots must use DNF's best target/noarch choice")
    expected_purpose = {
        "host-qt-build": "qt-host-build",
        "qt-target": "qt-target",
        "qt-runtime": "qt-runtime",
    }.get(role)
    if expected_purpose is not None and any(
        root["purpose"] != expected_purpose for root in plan["roots"]
    ):
        raise ValidationError("%s root purpose differs" % role)


def load_referenced_plan(transaction):
    reference = transaction["plan"]
    path = repository_file(reference["file"], "plan reference")
    plan = load_json(path)
    validate_schema(plan)
    validate_plan_semantics(plan)
    if canonical_sha256(plan) != reference["canonical_sha256"]:
        raise ValidationError("transaction plan digest mismatch")
    if transaction["identity"] != plan["identity"]:
        raise ValidationError("transaction identity differs from plan")
    if transaction["base"] != plan["base"]:
        raise ValidationError("transaction base differs from plan")
    transaction_policy = dict(transaction["solver_policy"])
    plan_policy = dict(plan["solver_policy"])
    transaction_policy["enabled_modules"] = sorted(
        transaction_policy["enabled_modules"]
    )
    plan_policy["enabled_modules"] = sorted(plan_policy["enabled_modules"])
    if transaction_policy != plan_policy:
        raise ValidationError("transaction policy differs from plan")
    transaction_repositories = transaction["repositories"]
    plan_repositories = {item["id"]: item for item in plan["repositories"]}
    if [item["id"] for item in transaction_repositories] != sorted(plan_repositories):
        raise ValidationError("transaction repositories differ from plan")
    for actual in transaction_repositories:
        if actual["baseurl"] != plan_repositories[actual["id"]]["baseurl"]:
            raise ValidationError("transaction repository URL differs from plan")
    return plan


def validate_manifest(manifest, label):
    packages = manifest["packages"]
    sorted_unique(packages, label)
    if canonical_sha256(packages) != manifest["canonical_sha256"]:
        raise ValidationError("%s canonical digest is invalid" % label)
    return packages


def validate_nevra_fields(item):
    expected = "%s-%d:%s-%s.%s" % (
        item["name"],
        item["epoch"],
        item["version"],
        item["release"],
        item["arch"],
    )
    if item["nevra"] != expected:
        raise ValidationError("transaction NEVRA fields encode %s" % expected)


def load_parent_transaction(plan):
    if plan["base"]["mode"] != "lock":
        return None
    lock_path = repository_file(plan["base"]["parent_lock"], "parent lock")
    parent_lock = load_json(lock_path)
    validate_schema(parent_lock)
    if canonical_sha256(parent_lock) != plan["base"]["parent_sha256"]:
        raise ValidationError("parent lock digest differs from plan")
    return load_referenced_transaction(parent_lock)


def validate_transaction_semantics(transaction):
    plan = load_referenced_plan(transaction)
    role = transaction["identity"]["role"]
    resolver = transaction["resolver"]
    expected_system_repo = plan["base"]["mode"] != "empty"
    if resolver["load_system_repo"] is not expected_system_repo:
        raise ValidationError("resolver system-repo mode differs from plan base")
    component_names = [item["name"] for item in resolver["components"]]
    sorted_unique(component_names, "resolver components")
    repositories = transaction["repositories"]
    for repository in repositories:
        metadata_types = [item["type"] for item in repository["metadata"]]
        reject_duplicates(metadata_types, "repository metadata type")
        if "primary" not in metadata_types:
            raise ValidationError("repository does not bind primary metadata")
        if not repository["gpgcheck"] or not repository["repo_gpgcheck"]:
            raise ValidationError("repository and payload GPG checks are mandatory")
    requests = transaction["requests"]
    if [
        (item["name"], item["arch"], item["purpose"])
        for item in requests
    ] != [
        (item["name"], item["arch"], item["purpose"])
        for item in sorted(
            plan["roots"], key=lambda item: (item["name"], item["arch"], item["purpose"])
        )
    ]:
        raise ValidationError("transaction requests differ from sorted plan roots")
    items = transaction["items"]
    item_keys = [(item["nevra"], item["action"]) for item in items]
    if item_keys != sorted(item_keys) or len(item_keys) != len(set(item_keys)):
        raise ValidationError("transaction items must be sorted and unique")
    allowed_arches = set(plan["solver_policy"]["allowed_arches"])
    repositories_by_id = {item["id"]: item for item in repositories}
    repo_ids = set(repositories_by_id)
    forward = []
    removed = []
    for item in items:
        validate_nevra_fields(item)
        if item["arch"] not in allowed_arches:
            raise ValidationError("transaction selected a forbidden architecture")
        if item["action"] in ("install", "upgrade"):
            forward.append(item)
            allowed_reasons = {"user", "dependency"}
            if role != "target-sysroot" and item["action"] == "upgrade":
                # DNF 4.7 reports some explicitly requested upgrades as
                # unknown; preserve that fact instead of inventing user.
                allowed_reasons.add("unknown")
            if item["reason"] not in allowed_reasons:
                raise ValidationError("forward item has non-canonical DNF reason")
            if item["repo_id"] not in repo_ids:
                raise ValidationError("forward item uses an undeclared repository")
            if any(
                item[field] is None
                for field in (
                    "location", "url", "repository_checksum", "size", "install_size", "source_rpm"
                )
            ):
                raise ValidationError("forward item lacks repository identity")
            if item["repository_checksum"]["algorithm"] != "sha256":
                raise ValidationError("RPM repository checksum must be SHA256")
            location = str(safe_posix_location(item["location"], "RPM location"))
            expected_url = repositories_by_id[item["repo_id"]]["baseurl"] + location
            if item["url"] != expected_url:
                raise ValidationError("RPM URL differs from repository baseurl/location")
            if role in ("target-sysroot", "qt-target", "qt-runtime") and item["name"] in SYSROOT_FORBIDDEN:
                raise ValidationError("forbidden package entered target sysroot")
        elif item["action"] == "remove":
            removed.append(item)
            if any(
                item[field] is not None
                for field in (
                    "location", "url", "repository_checksum", "size", "install_size", "source_rpm"
                )
            ):
                raise ValidationError("remove item carries repository payload identity")
        else:
            raise ValidationError("unsupported transaction action: %s" % item["action"])
    base_manifest = validate_manifest(transaction["manifests"]["base"], "base manifest")
    remove_manifest = validate_manifest(transaction["manifests"]["remove"], "remove manifest")
    result_manifest = validate_manifest(transaction["manifests"]["result"], "result manifest")
    forward_nevras = {item["nevra"] for item in forward}
    removed_nevras = {item["nevra"] for item in removed}
    if removed_nevras != set(remove_manifest):
        raise ValidationError("remove items differ from remove manifest")
    expected_result = (set(base_manifest) - removed_nevras) | forward_nevras
    if expected_result != set(result_manifest):
        raise ValidationError("transaction items do not encode the result manifest")
    request_nevras = {item["resolved_nevra"] for item in requests}
    if len(request_nevras) != len(requests):
        raise ValidationError("multiple roots resolve to the same RPM")
    if not request_nevras.issubset(set(result_manifest)):
        raise ValidationError("a root request is absent from result manifest")
    for request in requests:
        resolved_name, resolved_arch = nevra_name_arch(request["resolved_nevra"])
        if resolved_name != request["name"]:
            raise ValidationError("root request resolves to a different package name")
        selector = request["arch"]
        target_arch = transaction["identity"]["arch"]
        if selector == "target" and resolved_arch != target_arch:
            raise ValidationError("target root resolves to the wrong architecture")
        if selector == "noarch" and resolved_arch != "noarch":
            raise ValidationError("noarch root resolves to the wrong architecture")
        if selector == "any" and resolved_arch not in (target_arch, "noarch"):
            raise ValidationError("host root resolves outside target/noarch")
        expected = "transaction" if request["resolved_nevra"] in forward_nevras else "base"
        if request["disposition"] != expected:
            raise ValidationError("root request disposition is incorrect")
        if expected == "transaction":
            matches = [item for item in forward if item["nevra"] == request["resolved_nevra"]]
            if len(matches) != 1:
                raise ValidationError("transaction root is not a unique forward item")
            accepted = {"user"} if role == "target-sysroot" else {"user", "unknown"}
            if matches[0]["reason"] not in accepted:
                raise ValidationError("transaction root has an invalid DNF reason")
    if role in ("target-sysroot", "qt-runtime"):
        if base_manifest or removed_nevras:
            raise ValidationError("empty-root target transaction must start empty")
        if any(item["action"] != "install" for item in forward):
            raise ValidationError("empty-root target transaction may only install")
        user_nevras = {item["nevra"] for item in forward if item["reason"] == "user"}
        if user_nevras != request_nevras:
            raise ValidationError("empty-root DNF user set differs from roots")
    parent = load_parent_transaction(plan)
    if parent is not None:
        if parent["manifests"]["result"] != transaction["manifests"]["base"]:
            raise ValidationError("delta base differs from parent result manifest")
    if role == "host-gcc-test":
        validate_locked_host_gcc_test_contract(transaction)
    if role == "host-runtime":
        validate_locked_host_runtime_contract(transaction)
    if role == "host-qt-build":
        validate_locked_host_qt_contract(transaction)
    if role == "qt-target":
        validate_locked_qt_target_contract(transaction)
    if role == "qt-runtime":
        validate_locked_qt_runtime_contract(transaction)
    return plan


def validate_locked_transaction_semantics(transaction):
    """Validate the immutable transaction without reopening its maintenance plan."""
    identity = transaction["identity"]
    role = identity["role"]
    arch = identity["arch"]
    if role in ("target-sysroot", "qt-target", "qt-runtime"):
        if arch not in TARGET_TRIPLES or identity["target_triple"] != TARGET_TRIPLES[arch]:
            raise ValidationError("locked target transaction arch/triple mismatch")
    elif role not in (
        "host-build-common",
        "host-gcc-build",
        "host-gcc-test",
        "host-python-build",
        "host-runtime",
        "host-qt-build",
    ):
        raise ValidationError("unsupported locked RPM transaction role")
    elif arch != "x86_64" or identity["target_triple"] is not None:
        raise ValidationError("locked host transaction identity is invalid")
    repository_ids = [repository["id"] for repository in transaction["repositories"]]
    reject_duplicates(repository_ids, "locked repository id")
    item_keys = []
    for item in transaction["items"]:
        validate_nevra_fields(item)
        item_keys.append((item["nevra"], item["action"]))
    if item_keys != sorted(item_keys) or len(item_keys) != len(set(item_keys)):
        raise ValidationError("locked transaction items must be sorted and unique")
    for name in ("base", "remove", "result"):
        validate_manifest(
            transaction["manifests"][name],
            "locked transaction %s manifest" % name,
        )
    if role == "host-gcc-test":
        validate_locked_host_gcc_test_contract(transaction)
    if role == "host-runtime":
        validate_locked_host_runtime_contract(transaction)
    if role == "host-qt-build":
        validate_locked_host_qt_contract(transaction)
    if role == "qt-target":
        validate_locked_qt_target_contract(transaction)
    if role == "qt-runtime":
        validate_locked_qt_runtime_contract(transaction)


def load_referenced_transaction(lock, validate_plan=True):
    reference = lock["transaction"]
    path = repository_file(reference["file"], "transaction reference")
    transaction = load_json(path)
    validate_schema(transaction)
    if canonical_sha256(transaction) != reference["canonical_sha256"]:
        raise ValidationError("transaction canonical SHA256 differs from lock")
    if validate_plan:
        validate_transaction_semantics(transaction)
    else:
        validate_locked_transaction_semantics(transaction)
    return transaction


def validate_lock_semantics(lock, validate_plan=True):
    transaction = load_referenced_transaction(lock, validate_plan=validate_plan)
    forward = sorted(
        [item for item in transaction["items"] if item["action"] in ("install", "upgrade")],
        key=lambda item: item["nevra"],
    )
    packages = lock["packages"]
    if [item["nevra"] for item in packages] != [item["nevra"] for item in forward]:
        raise ValidationError("content lock package set differs from DNF transaction")
    fingerprints = {repo["gpg_key"]["fingerprint"] for repo in transaction["repositories"]}
    if len(fingerprints) != 1:
        raise ValidationError("RPM transaction uses multiple signing trust roots")
    fingerprint = next(iter(fingerprints))
    for package, item in zip(packages, forward):
        header = package["header"]
        if header != {
            "name": item["name"],
            "epoch": item["epoch"],
            "version": item["version"],
            "release": item["release"],
            "arch": item["arch"],
            "nevra": item["nevra"],
            "source_rpm": item["source_rpm"],
        }:
            raise ValidationError("verified RPM header differs from transaction")
        if package["received_sha256"] != item["repository_checksum"]["value"]:
            raise ValidationError("received RPM differs from repository checksum")
        if package["signature"]["fingerprint"] != fingerprint:
            raise ValidationError("RPM signature differs from repository trust root")
    return transaction


def component_material_map(document):
    reader = component_reader()
    materials = {}
    for record in document["materials"]:
        path = reader["decode_json_pointer"](record["path"])
        if path in materials:
            raise ValidationError("release component repeats a material path")
        materials[path] = record["value"]
    return materials


def require_material(materials, path, expected_type, label):
    if path not in materials:
        raise ValidationError("release component is missing %s" % label)
    value = materials[path]
    if type(value) is not expected_type:
        raise ValidationError("release component %s has the wrong JSON type" % label)
    return value


def safe_binding_path(value, label):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValidationError("release component %s is not a safe path" % label)
    relative = PurePosixPath(value)
    if (
        not relative.parts
        or relative.is_absolute()
        or value in (".", "..")
        or ".." in relative.parts
        or ":" in relative.parts[0]
        or str(relative) != value
    ):
        raise ValidationError("release component %s is not a safe path" % label)
    return value


def component_binding(document, transaction, component_name, component_sha256):
    materials = component_material_map(document)
    role = transaction["identity"]["role"]
    arch = transaction["identity"]["arch"]
    expected_component = (
        "rpm/sysroot-%s" % arch
        if role == "target-sysroot"
        else "rpm/%s" % role
    )
    if component_name != expected_component:
        raise ValidationError(
            "release component %s does not bind RPM role/arch %s/%s"
            % (component_name, role, arch)
        )

    base = {
        "repository": require_material(
            materials, ("base_image", "repository"), str, "base repository"
        ),
        "tag": require_material(
            materials, ("base_image", "tag"), str, "base tag"
        ),
        "digest": require_material(
            materials, ("base_image", "digest"), str, "base digest"
        ),
    }
    trust = {
        "file": safe_binding_path(
            require_material(
                materials,
                ("trust", "rocky_rpm_key", "file"),
                str,
                "Rocky key file",
            ),
            "Rocky key file",
        ),
        "sha256": require_material(
            materials,
            ("trust", "rocky_rpm_key", "sha256"),
            str,
            "Rocky key SHA256",
        ),
        "fingerprint": require_material(
            materials,
            ("trust", "rocky_rpm_key", "fingerprint"),
            str,
            "Rocky key fingerprint",
        ),
    }
    if not is_sha256(trust["sha256"]):
        raise ValidationError("release component Rocky key SHA256 is invalid")

    if role == "target-sysroot":
        target_indexes = {
            path[1]
            for path in materials
            if len(path) >= 3 and path[0] == "targets"
        }
        if (
            len(target_indexes) != 1
            or not next(iter(target_indexes)).isdigit()
        ):
            raise ValidationError(
                "release component must contain one target material prefix"
            )
        index = next(iter(target_indexes))
        prefix = ("targets", index)
        component_arch = require_material(
            materials, prefix + ("arch",), str, "target architecture"
        )
        triple = require_material(
            materials, prefix + ("triple",), str, "target triple"
        )
        if component_arch != arch or TARGET_TRIPLES.get(arch) != triple:
            raise ValidationError(
                "release component target architecture/triple differs from lock"
            )
        pin_prefix = prefix + ("sysroot",)
    else:
        host_roles = {
            path[1]
            for path in materials
            if len(path) >= 3 and path[0] == "host_locks"
        }
        if host_roles != {role}:
            raise ValidationError(
                "release component host lock role differs from transaction"
            )
        pin_prefix = ("host_locks", role)

    pin = {
        "status": require_material(
            materials, pin_prefix + ("status",), str, "lock status"
        ),
        "lock_file": safe_binding_path(
            require_material(
                materials, pin_prefix + ("lock_file",), str, "lock path"
            ),
            "lock path",
        ),
        "canonical_sha256": require_material(
            materials,
            pin_prefix + ("canonical_sha256",),
            str,
            "lock canonical SHA256",
        ),
    }
    if not is_sha256(pin["canonical_sha256"]):
        raise ValidationError("release component lock SHA256 is invalid")
    return {
        "base": base,
        "trust": trust,
        "pin": pin,
        "identity": {
            "kind": "release-component",
            "component": component_name,
            "scope": "build",
            "canonical_sha256": component_sha256,
        },
    }


def validate_bound_transaction(lock, lock_path, transaction, binding):
    resolver = transaction["resolver"]
    base = binding["base"]
    if resolver["image"] != "%s:%s" % (base["repository"], base["tag"]):
        raise ValidationError("resolver image differs from release binding")
    if resolver["image_digest"] != base["digest"]:
        raise ValidationError("resolver digest differs from release binding")
    trust = binding["trust"]
    for repository in transaction["repositories"]:
        validate_repository_trust(repository, trust)
        metadata_root = REPOSITORY / "locks/metadata" / transaction["identity"]["name"] / repository["id"]
        for record in (repository["repomd"], repository["repomd"]["signature"]):
            path = checked_metadata_path(metadata_root, record["location"], REPOSITORY)
            if not path.is_file():
                raise ValidationError("checked repository identity file is missing: %s" % path)
            if path.stat().st_size != record["size"] or file_sha256(path) != record["sha256"]:
                raise ValidationError("checked repository identity file differs: %s" % path)
    key_path = REPOSITORY / trust["file"]
    if file_sha256(key_path) != trust["sha256"]:
        raise ValidationError("Rocky key file differs from release binding")
    for repository in transaction["repositories"]:
        metadata_root = (
            REPOSITORY
            / "locks/metadata"
            / transaction["identity"]["name"]
            / repository["id"]
        )
        verify_detached_signature(
            key_path,
            trust["fingerprint"],
            checked_metadata_path(
                metadata_root,
                repository["repomd"]["signature"]["location"],
                REPOSITORY,
            ),
            checked_metadata_path(
                metadata_root, repository["repomd"]["location"], REPOSITORY
            ),
        )
        validate_repomd_claim(
            repository,
            checked_metadata_path(
                metadata_root, repository["repomd"]["location"], REPOSITORY
            ),
        )
    pin = binding["pin"]
    try:
        relative = lock_path.resolve().relative_to(REPOSITORY).as_posix()
    except ValueError:
        raise ValidationError("lock path is outside the repository")
    if pin["status"] != "locked" or pin["lock_file"] != relative:
        raise ValidationError("release lock path binding differs")
    if pin["canonical_sha256"] != canonical_sha256(lock):
        raise ValidationError("release lock digest binding differs")
    return transaction


def full_release_binding(release, transaction):
    role = transaction["identity"]["role"]
    if role == "target-sysroot":
        matches = [
            target
            for target in release["targets"]
            if target["arch"] == transaction["identity"]["arch"]
        ]
        if len(matches) != 1:
            raise ValidationError("release target is not unique")
        pin = matches[0]["sysroot"]
    elif role in ("host-qt-build", "qt-target", "qt-runtime"):
        runtime = role == "qt-runtime"
        contract = release["qt"][
            "runtime_qualification" if runtime else "qualification"
        ]
        plan_path = repository_file(contract["plan"]["file"], "Qt plan")
        plan = load_json(plan_path)
        schema = load_json(
            REPOSITORY
            / "config/schemas"
            / (
                "qt-runtime-qualification-plan.schema.json"
                if runtime
                else "qt-qualification-plan.schema.json"
            )
        )
        STRICT["validate_schema_subset"](schema)
        STRICT["validate"](plan, schema, schema, "$")
        if (
            canonical_sha256(plan) != contract["plan"]["canonical_sha256"]
            or plan["status"] != contract["status"]
        ):
            raise ValidationError("release Qt plan binding differs")
        identifier = (
            "host-qt-build"
            if role == "host-qt-build"
            else "%s-%s"
            % (
                "qt-target" if role == "qt-target" else "qt-runtime",
                transaction["identity"]["arch"],
            )
        )
        matches = [item for item in plan["locks"] if item["id"] == identifier]
        if len(matches) != 1:
            raise ValidationError("release Qt lock pin is not unique")
        pin = matches[0]
    else:
        pin = release["host_locks"][role]
    return {
        "base": release["base_image"],
        "trust": release["trust"]["rocky_rpm_key"],
        "pin": pin,
        "identity": {
            "kind": "release-config",
            "canonical_sha256": canonical_sha256(release),
        },
    }


def validate_lock_binding(
    lock,
    lock_path,
    release_path=None,
    release_component=None,
    release_component_name=None,
    release_component_sha256=None,
):
    component_values = (
        release_component,
        release_component_name,
        release_component_sha256,
    )
    component_mode = any(value is not None for value in component_values)
    if component_mode and not all(value is not None for value in component_values):
        raise ValidationError(
            "release component path, name and SHA256 must be provided together"
        )
    if component_mode and release_path is not None:
        raise ValidationError(
            "--release-config and --release-component are mutually exclusive"
        )

    document = None
    if component_mode:
        reader = component_reader()
        try:
            document = reader["load_component"](
                release_component,
                release_component_name,
                "build",
                release_component_sha256,
            )
        except reader["ComponentError"] as error:
            raise ValidationError("invalid release component: %s" % error) from error

    transaction = validate_lock_semantics(lock, validate_plan=not component_mode)
    if component_mode:
        binding = component_binding(
            document,
            transaction,
            release_component_name,
            release_component_sha256,
        )
    else:
        if release_path is None:
            release_path = REPOSITORY / "config/release.json"
        release = load_json(release_path)
        release_schema = load_json(REPOSITORY / "config/schemas/release.schema.json")
        STRICT["validate_schema_subset"](release_schema)
        STRICT["validate"](release, release_schema, release_schema, "$")
        binding = full_release_binding(release, transaction)
    validate_bound_transaction(lock, lock_path, transaction, binding)
    return transaction, binding["identity"]


def validate_release_binding(lock, lock_path, release_path):
    transaction, _identity = validate_lock_binding(
        lock, lock_path, release_path=release_path
    )
    return transaction


def add_release_binding_arguments(parser, suppress_defaults=False):
    optional = {
        "default": argparse.SUPPRESS
    } if suppress_defaults else {}
    parser.add_argument("--release-config", type=Path, **optional)
    parser.add_argument("--release-component", type=Path, **optional)
    parser.add_argument("--release-component-name", **optional)
    parser.add_argument("--release-component-sha256", **optional)


def release_binding_arguments(arguments):
    values = (
        arguments.release_component,
        arguments.release_component_name,
        arguments.release_component_sha256,
    )
    if any(value is not None for value in values) and not all(
        value is not None for value in values
    ):
        raise ValidationError(
            "release component path, name and SHA256 must be provided together"
        )
    if all(value is not None for value in values) and arguments.release_config is not None:
        raise ValidationError(
            "--release-config and --release-component are mutually exclusive"
        )
    return {
        "release_path": arguments.release_config,
        "release_component": arguments.release_component,
        "release_component_name": arguments.release_component_name,
        "release_component_sha256": arguments.release_component_sha256,
    }


def validate_document(document):
    validate_schema(document)
    if document["kind"] == "rpm-plan":
        validate_plan_semantics(document)
    elif document["kind"] == "rpm-transaction":
        validate_transaction_semantics(document)
    else:
        validate_lock_semantics(document)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("document", type=Path)
    parser.add_argument("--require-lock", action="store_true")
    add_release_binding_arguments(parser)
    arguments = parser.parse_args()
    try:
        document = load_json(arguments.document)
        binding_options = release_binding_arguments(arguments)
        component_mode = binding_options["release_component"] is not None
        if document.get("kind") == "rpm-lock" and component_mode:
            validate_schema(document)
        else:
            validate_document(document)
        if arguments.require_lock and document["kind"] != "rpm-lock":
            raise ValidationError("document is not a verified RPM content lock")
        if document["kind"] == "rpm-lock":
            validate_lock_binding(
                document,
                arguments.document,
                **binding_options
            )
        elif any(
            value is not None
            for value in (
                arguments.release_config,
                arguments.release_component,
                arguments.release_component_name,
                arguments.release_component_sha256,
            )
        ):
            raise ValidationError(
                "release binding options are valid only for RPM locks"
            )
    except (OSError, ValidationError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "valid %s: %s (canonical sha256:%s)"
        % (document["kind"], arguments.document, canonical_sha256(document))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
