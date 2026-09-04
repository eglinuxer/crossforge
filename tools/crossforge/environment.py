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
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise EnvironmentError("%s is unavailable: %s" % (label, path)) from error
    require(
        resolved.is_file() and os.access(str(resolved), os.X_OK),
        "%s is unavailable: %s" % (label, path),
    )
    return str(path)


def directory(path, label):
    require(path.is_dir() and not path.is_symlink(), "%s is unavailable: %s" % (label, path))
    return str(path)


def regular_file(path, label):
    require(path.is_file() and not path.is_symlink(), "%s is unavailable: %s" % (label, path))
    return str(path)


def environment_path(root, value, label):
    require(
        isinstance(value, str) and value.startswith("/"),
        "%s must be absolute" % label,
    )
    path = Path(value)
    root = Path(root)
    if root == Path("/"):
        return path
    try:
        path.relative_to(root)
    except ValueError:
        return rooted(root, value)
    return path


def usable_directory(path):
    return (
        path.is_dir()
        and not path.is_symlink()
        and os.access(str(path), os.W_OK | os.X_OK)
    )


def ensure_user_directory(path, label):
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise EnvironmentError("%s is not writable: %s" % (label, path)) from error
    require(usable_directory(path), "%s is not writable: %s" % (label, path))
    return path


def runtime_environment(root, environment):
    explicit_home = environment.get("CROSSFORGE_HOME")
    inherited_home = environment.get("HOME")
    if explicit_home:
        home = ensure_user_directory(
            environment_path(root, explicit_home, "CROSSFORGE_HOME"),
            "Crossforge home",
        )
    else:
        home = None
        if inherited_home:
            candidate = environment_path(root, inherited_home, "HOME")
            if usable_directory(candidate):
                home = candidate
        if home is None:
            home = ensure_user_directory(
                rooted(root, "/tmp/crossforge-%d/home" % os.geteuid()),
                "Crossforge fallback home",
            )

    explicit_cache = environment.get("CROSSFORGE_CACHE_ROOT")
    cache = (
        environment_path(root, explicit_cache, "CROSSFORGE_CACHE_ROOT")
        if explicit_cache
        else home / ".cache/crossforge"
    )
    cache = ensure_user_directory(cache, "Crossforge cache")
    xdg_cache = ensure_user_directory(home / ".cache", "XDG cache")
    return {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(xdg_cache),
        "CROSSFORGE_HOME": str(home),
        "CROSSFORGE_CACHE_ROOT": str(cache),
    }


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
    tools = {
        "CC": executable(compiler_root / "gcc", "native GTS gcc"),
        "CXX": executable(compiler_root / "g++", "native GTS g++"),
    }
    system_root = rooted(root, "/usr/bin")
    for variable, name in (
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
            system_root / name, "native host %s" % name
        )
    tools["CROSSFORGE_TARGET"] = "host"
    return tools


def vcpkg_environment(root, arch, linkage, cache_root):
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
    downloads = ensure_user_directory(
        Path(cache_root) / "vcpkg/downloads", "vcpkg downloads cache"
    )
    binary_cache = ensure_user_directory(
        Path(cache_root) / "vcpkg/binary", "vcpkg binary cache"
    )
    return {
        "VCPKG_ROOT": str(vcpkg_root),
        "VCPKG_OVERLAY_TRIPLETS": str(triplet_root),
        "VCPKG_DEFAULT_HOST_TRIPLET": "crossforge-host-x64-el8",
        "VCPKG_DEFAULT_TRIPLET": triplet,
        "VCPKG_DISABLE_METRICS": "1",
        "VCPKG_FORCE_SYSTEM_BINARIES": "1",
        "VCPKG_DOWNLOADS": str(downloads),
        "VCPKG_DEFAULT_BINARY_CACHE": str(binary_cache),
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
    environment.update(runtime_environment(root, environment))
    overlay = (
        host_environment(root)
        if target is None
        else target_environment(root, target)
    )
    triple = None if target is None else TARGETS[target]["triple"]
    if python is not None:
        environment.pop("PYTHONPATH", None)
        overlay.update(python_environment(release, root, python, triple))
    if vcpkg:
        overlay.update(
            vcpkg_environment(
                root, target, linkage, environment["CROSSFORGE_CACHE_ROOT"]
            )
        )
    if target is not None:
        environment.pop("PKG_CONFIG_PATH", None)
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
