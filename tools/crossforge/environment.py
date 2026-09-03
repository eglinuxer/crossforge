"""Resolve explicit Crossforge build environments without project guessing."""

import os
from pathlib import Path


TARGETS = {
    "x86_64": {
        "triple": "x86_64-unknown-linux-gnu",
        "cmake": "x86_64-unknown-linux-gnu.cmake",
        "vcpkg": {"static": "crossforge-x64-el8", "dynamic": "crossforge-x64-el8-dynamic"},
    },
    "aarch64": {
        "triple": "aarch64-unknown-linux-gnu",
        "cmake": "aarch64-unknown-linux-gnu.cmake",
        "vcpkg": {"static": "crossforge-arm64-el8", "dynamic": "crossforge-arm64-el8-dynamic"},
    },
}


class EnvironmentError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise EnvironmentError(message)


def rooted(root, absolute):
    require(str(absolute).startswith("/"), "layout path must be absolute")
    return Path(root) / str(absolute).lstrip("/")


def executable(path, label):
    require(
        path.is_file() and not path.is_symlink() and os.access(str(path), os.X_OK),
        "%s is unavailable: %s" % (label, path),
    )
    return str(path)


def directory(path, label):
    require(path.is_dir() and not path.is_symlink(), "%s is unavailable: %s" % (label, path))
    return str(path)


def regular_file(path, label):
    require(path.is_file() and not path.is_symlink(), "%s is unavailable: %s" % (label, path))
    return str(path)


def python_row(release, minor):
    matches = [
        entry
        for entry in release["python"]["versions"]
        if entry["version"].rsplit(".", 1)[0] == minor
    ]
    require(len(matches) == 1, "unsupported Python minor: %s" % minor)
    return "cp" + minor.replace(".", ""), matches[0]


def python_environment(release, root, minor, triple):
    row, _entry = python_row(release, minor)
    build_prefix = rooted(root, "/opt/crossforge/python/%s/build" % row)
    target_prefix = rooted(
        root, "/opt/crossforge/python/%s/targets/%s" % (row, triple)
    )
    build_python = build_prefix / "bin" / ("python" + minor)
    directory(target_prefix, "target Python %s" % minor)
    executable(build_python, "build Python %s" % minor)
    candidates = sorted(target_prefix.glob("lib/python%s/**/_sysconfigdata_*.py" % minor))
    require(
        len(candidates) == 1
        and candidates[0].is_file()
        and not candidates[0].is_symlink(),
        "target Python sysconfig data is not unique",
    )
    sysconfig = candidates[0]
    return {
        "CROSSFORGE_PYTHON": minor,
        "CROSSFORGE_PYTHON_BUILD": str(build_python),
        "CROSSFORGE_PYTHON_PREFIX": str(target_prefix),
        "PYTHON_FOR_BUILD": str(build_python),
        "_PYTHON_SYSCONFIGDATA_NAME": sysconfig.stem,
        "_PYTHON_SYSCONFIGDATA_PATH": str(sysconfig.parent),
    }


def target_environment(root, arch):
    record = TARGETS[arch]
    triple = record["triple"]
    compiler_root = rooted(root, "/opt/crossforge/targets/%s/bin" % triple)
    sysroot = rooted(root, "/opt/crossforge/sysroots/el8/%s" % arch)
    directory(compiler_root, "%s compiler root" % arch)
    directory(sysroot, "%s sysroot" % arch)
    tools = {}
    for variable, suffix in (
        ("CC", "gcc"),
        ("CXX", "g++"),
        ("AR", "ar"),
        ("AS", "as"),
        ("LD", "ld"),
        ("NM", "nm"),
        ("OBJCOPY", "objcopy"),
        ("OBJDUMP", "objdump"),
        ("RANLIB", "ranlib"),
        ("READELF", "readelf"),
        ("STRIP", "strip"),
    ):
        tools[variable] = executable(
            compiler_root / (triple + "-" + suffix),
            "%s %s" % (arch, suffix),
        )
    tools["CC"] += " --sysroot=" + str(sysroot)
    tools["CXX"] += " --sysroot=" + str(sysroot)
    cmake_file = rooted(root, "/opt/crossforge/cmake/" + record["cmake"])
    meson_file = rooted(root, "/opt/crossforge/meson/%s.ini" % triple)
    regular_file(cmake_file, "%s CMake toolchain" % arch)
    regular_file(meson_file, "%s Meson cross file" % arch)
    tools.update(
        {
            "CROSSFORGE_TARGET": arch,
            "CROSSFORGE_TARGET_TRIPLE": triple,
            "CROSSFORGE_SYSROOT": str(sysroot),
            "CMAKE_TOOLCHAIN_FILE": str(cmake_file),
            "MESON_CROSS_FILE": str(meson_file),
            "PKG_CONFIG_SYSROOT_DIR": str(sysroot),
            "PKG_CONFIG_LIBDIR": ":".join(
                str(sysroot / relative)
                for relative in (
                    "usr/lib64/pkgconfig",
                    "usr/lib/pkgconfig",
                    "usr/share/pkgconfig",
                )
            ),
        }
    )
    return tools


def host_environment(root):
    compiler_root = rooted(root, "/opt/rh/gcc-toolset-15/root/usr/bin")
    directory(compiler_root, "native GTS compiler root")
    tools = {}
    for variable, name in (
        ("CC", "gcc"),
        ("CXX", "g++"),
        ("AR", "ar"),
        ("AS", "as"),
        ("LD", "ld"),
        ("NM", "nm"),
        ("OBJCOPY", "objcopy"),
        ("OBJDUMP", "objdump"),
        ("RANLIB", "ranlib"),
        ("READELF", "readelf"),
        ("STRIP", "strip"),
    ):
        tools[variable] = executable(
            compiler_root / name, "native GTS %s" % name
        )
    tools["CROSSFORGE_TARGET"] = "host"
    return tools


def vcpkg_environment(root, arch, linkage):
    vcpkg_root = rooted(root, "/opt/crossforge/vcpkg/root")
    triplet_root = rooted(root, "/opt/crossforge/vcpkg/triplets")
    executable(vcpkg_root / "vcpkg", "vcpkg")
    directory(triplet_root, "vcpkg triplet root")
    toolchain = vcpkg_root / "scripts/buildsystems/vcpkg.cmake"
    regular_file(toolchain, "vcpkg CMake toolchain")
    if arch is None:
        triplet = "crossforge-host-x64-el8"
    else:
        triplet = TARGETS[arch]["vcpkg"][linkage]
    return {
        "VCPKG_ROOT": str(vcpkg_root),
        "VCPKG_OVERLAY_TRIPLETS": str(triplet_root),
        "VCPKG_DEFAULT_HOST_TRIPLET": "crossforge-host-x64-el8",
        "VCPKG_DEFAULT_TRIPLET": triplet,
        "VCPKG_DISABLE_METRICS": "1",
        "VCPKG_FORCE_SYSTEM_BINARIES": "1",
        "CMAKE_TOOLCHAIN_FILE": str(toolchain),
    }


def build_environment(
    release,
    root=Path("/"),
    target=None,
    python=None,
    vcpkg=False,
    linkage="static",
    base=None,
):
    require(target is None or target in TARGETS, "unsupported target")
    require(linkage in ("static", "dynamic"), "unsupported linkage")
    require(vcpkg or linkage == "static", "--linkage requires --vcpkg")
    require(python is None or target is not None, "--python requires --target")
    environment = dict(os.environ if base is None else base)
    overlay = (
        host_environment(root)
        if target is None
        else target_environment(root, target)
    )
    triple = None if target is None else TARGETS[target]["triple"]
    if python is not None:
        overlay.update(python_environment(release, root, python, triple))
    if vcpkg:
        overlay.update(vcpkg_environment(root, target, linkage))
    path_entries = [
        str(rooted(root, "/opt/crossforge/host-tools/cmake/4.4.0/bin")),
        str(rooted(root, "/opt/crossforge/host-tools/ninja/1.13.2/bin")),
    ]
    if vcpkg:
        path_entries.append(str(rooted(root, "/opt/crossforge/vcpkg/root")))
    existing_path = environment.get("PATH", "")
    environment["PATH"] = ":".join(path_entries + ([existing_path] if existing_path else []))
    environment.update(overlay)
    return environment
