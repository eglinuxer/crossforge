#!/usr/bin/env python3
"""Shared isolated-install and ELF helpers for vcpkg qualification gates."""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path


HOST_TRIPLET = "crossforge-host-x64-el8"
TRIPLETS = {
    HOST_TRIPLET: {
        "arch": "x86_64",
        "cross": False,
        "linkage": "static",
        "triple": None,
        "machine": "Advanced Micro Devices X86-64",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
    },
    "crossforge-x64-el8": {
        "arch": "x86_64",
        "cross": True,
        "linkage": "static",
        "triple": "x86_64-unknown-linux-gnu",
        "machine": "Advanced Micro Devices X86-64",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
    },
    "crossforge-x64-el8-dynamic": {
        "arch": "x86_64",
        "cross": True,
        "linkage": "dynamic",
        "triple": "x86_64-unknown-linux-gnu",
        "machine": "Advanced Micro Devices X86-64",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
    },
    "crossforge-arm64-el8": {
        "arch": "aarch64",
        "cross": True,
        "linkage": "static",
        "triple": "aarch64-unknown-linux-gnu",
        "machine": "AArch64",
        "interpreter": "/lib/ld-linux-aarch64.so.1",
    },
    "crossforge-arm64-el8-dynamic": {
        "arch": "aarch64",
        "cross": True,
        "linkage": "dynamic",
        "triple": "aarch64-unknown-linux-gnu",
        "machine": "AArch64",
        "interpreter": "/lib/ld-linux-aarch64.so.1",
    },
}


class QualificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise QualificationError(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(arguments, cwd=None, env=None):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if process.returncode != 0:
        details = process.stdout + process.stderr
        buildtrees = next(
            (
                Path(str(argument).split("=", 1)[1])
                for argument in arguments
                if str(argument).startswith("--x-buildtrees-root=")
            ),
            None,
        )
        if buildtrees is not None and buildtrees.is_dir():
            referenced = []
            for line in details.splitlines():
                candidate = Path(line.strip())
                try:
                    candidate.relative_to(buildtrees)
                except ValueError:
                    continue
                if candidate.suffix == ".log" and candidate.is_file():
                    referenced.append(candidate)
            discovered = sorted(
                buildtrees.rglob("*.log"),
                key=lambda path: (
                    "/crossforge-host-probe/" not in path.as_posix()
                    and "/crossforge-target-probe/" not in path.as_posix(),
                    "err.log" not in path.name,
                    path.as_posix(),
                ),
            )
            logs = []
            for path in referenced + discovered:
                if path not in logs:
                    logs.append(path)
            for path in logs[:20]:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                details += "\n--- %s ---\n%s" % (path, content[-12000:])
        raise QualificationError(
            "command failed (%s):\n%s"
            % (" ".join(str(argument) for argument in arguments), details)
        )
    return process.stdout, process.stderr


def profile_tools(profile):
    triple = profile["triple"]
    if triple is None:
        root = Path("/opt/rh/gcc-toolset-15/root/usr/bin")
        return root / "gcc", root / "g++", root / "readelf", None
    root = Path("/opt/crossforge/targets") / triple / "bin"
    sysroot = Path("/opt/crossforge/sysroots/el8") / profile["arch"]
    return (
        root / (triple + "-gcc"),
        root / (triple + "-g++"),
        root / (triple + "-readelf"),
        sysroot,
    )


def verify_machine(readelf, path, expected):
    header, _stderr = run([readelf, "-h", path])
    machines = set(
        re.findall(r"^\s*Machine:\s+(.+?)\s*$", header, re.MULTILINE)
    )
    require(machines == {expected}, "ELF machine differs for %s" % path)


def validate_shared_library_dynamic(dynamic, label):
    require("TEXTREL" not in dynamic, "%s contains TEXTREL" % label)
    tags = re.findall(
        r"\((RPATH|RUNPATH)\).*Library (?:rpath|runpath): \[([^]]*)\]",
        dynamic,
    )
    require(
        tags == [("RUNPATH", "$ORIGIN")],
        "%s runpath differs: %r" % (label, tags),
    )
    return tags[0][1]


def isolated_install(
    vcpkg_root,
    manifest_root,
    triplet,
    asset_paths,
    work,
    overlay_ports=None,
    seed_installed=None,
):
    triplet_work = Path(work) / triplet
    roots = {
        name: triplet_work / name
        for name in (
            "downloads",
            "buildtrees",
            "packages",
            "installed",
            "cache",
        )
    }
    for name, path in roots.items():
        if name != "installed" or seed_installed is None:
            path.mkdir(parents=True, exist_ok=True)
    if seed_installed is not None:
        seed_installed = Path(seed_installed)
        require(
            seed_installed.is_dir() and not seed_installed.is_symlink(),
            "vcpkg installed seed is unsafe",
        )
        try:
            seed_installed.resolve().relative_to(Path(work).resolve())
        except ValueError:
            raise QualificationError("vcpkg installed seed escapes qualification work")
        shutil.copytree(
            str(seed_installed), str(roots["installed"]), symlinks=True
        )
    (roots["cache"] / "home").mkdir()
    (roots["cache"] / "xdg").mkdir()
    names = []
    for source in asset_paths:
        source = Path(source)
        require(
            source.is_file() and not source.is_symlink(),
            "vcpkg input asset is unsafe: %s" % source,
        )
        names.append(source.name)
        shutil.copy2(str(source), str(roots["downloads"] / source.name))
    require(len(names) == len(set(names)), "vcpkg input assets repeat a filename")

    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(roots["cache"] / "home"),
            "XDG_CACHE_HOME": str(roots["cache"] / "xdg"),
            "VCPKG_BINARY_SOURCES": "clear",
            "X_VCPKG_ASSET_SOURCES": "clear;x-block-origin",
        }
    )
    command = [
        Path(vcpkg_root) / "vcpkg",
        "install",
        "--x-manifest-root=" + str(manifest_root),
        "--x-install-root=" + str(roots["installed"]),
        "--triplet=" + triplet,
        "--host-triplet=" + HOST_TRIPLET,
        "--downloads-root=" + str(roots["downloads"]),
        "--x-buildtrees-root=" + str(roots["buildtrees"]),
        "--x-packages-root=" + str(roots["packages"]),
        "--binarysource=clear",
        "--no-downloads",
        "--disable-metrics",
    ]
    if overlay_ports is not None:
        command.insert(4, "--overlay-ports=" + str(overlay_ports))
    stdout, stderr = run(command, cwd=manifest_root, env=environment)
    roots["stdout"] = stdout
    roots["stderr"] = stderr
    return roots
