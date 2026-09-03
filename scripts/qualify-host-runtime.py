#!/usr/bin/env python3
"""Qualify the independent user-facing host runtime without network access."""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class QualificationError(RuntimeError):
    pass


EXPECTED_TOOLS = {
    "ar": "/opt/rh/gcc-toolset-15/root/usr/bin/ar",
    "as": "/opt/rh/gcc-toolset-15/root/usr/bin/as",
    "autoconf": "/usr/bin/autoconf",
    "automake": "/usr/bin/automake",
    "autoreconf": "/usr/bin/autoreconf",
    "bison": "/usr/bin/bison",
    "cmake": "/usr/bin/cmake",
    "flex": "/usr/bin/flex",
    "gcc": "/opt/rh/gcc-toolset-15/root/usr/bin/gcc",
    "g++": "/opt/rh/gcc-toolset-15/root/usr/bin/g++",
    "git": "/usr/bin/git",
    "ld": "/opt/rh/gcc-toolset-15/root/usr/bin/ld",
    "libtool": "/usr/bin/libtool",
    "libtoolize": "/usr/bin/libtoolize",
    "make": "/usr/bin/make",
    "meson": "/usr/bin/meson",
    "ninja": "/usr/bin/ninja",
    "pkg-config": "/usr/bin/pkg-config",
}
VERSION_ARGUMENTS = {
    "ar": ("--version",),
    "as": ("--version",),
    "autoconf": ("--version",),
    "automake": ("--version",),
    "autoreconf": ("--version",),
    "bison": ("--version",),
    "cmake": ("--version",),
    "flex": ("--version",),
    "gcc": ("--version",),
    "g++": ("--version",),
    "git": ("--version",),
    "ld": ("--version",),
    "libtool": ("--version",),
    "libtoolize": ("--version",),
    "make": ("--version",),
    "meson": ("--version",),
    "ninja": ("--version",),
    "pkg-config": ("--version",),
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")


def fail(message):
    raise QualificationError(message)


def require(condition, message):
    if not condition:
        fail(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %r" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError) as error:
        raise QualificationError("cannot load %s: %s" % (path, error)) from error
    require(isinstance(value, dict), "%s must contain an object" % path)
    return value


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
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
    require(
        process.returncode == 0,
        "command failed (%s):\n%s"
        % (" ".join(str(item) for item in arguments), process.stdout + process.stderr),
    )
    return process.stdout, process.stderr


def rpm_inventory():
    stdout, _stderr = run(
        [
            "rpm",
            "-qa",
            "--qf",
            "%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\n",
        ]
    )
    return sorted(line for line in stdout.splitlines() if line)


def tool_evidence():
    result = {}
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    for name in sorted(EXPECTED_TOOLS):
        expected = EXPECTED_TOOLS[name]
        observed = shutil.which(name)
        require(observed == expected, "%s resolved outside its qualified path" % name)
        stdout, stderr = run(
            [expected] + list(VERSION_ARGUMENTS[name]), env=environment
        )
        lines = (stdout + stderr).splitlines()
        require(lines and lines[0].strip(), "%s emitted no version" % name)
        owner, _stderr = run(
            [
                "rpm",
                "-qf",
                "--qf",
                "%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}",
                expected,
            ],
            env=environment,
        )
        result[name] = {
            "path": expected,
            "owner_nevra": owner,
            "sha256": sha256_file(expected),
            "version": lines[0].strip(),
        }
    for compiler in ("gcc", "g++"):
        version, _stderr = run([EXPECTED_TOOLS[compiler], "-dumpfullversion"])
        require(version.strip() == "15.2.1", "%s is not GTS15" % compiler)
    return result


def write(path, value):
    path.write_text(value, encoding="utf-8")


def smoke_builds(work):
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    environment["CC"] = EXPECTED_TOOLS["gcc"]
    environment["CXX"] = EXPECTED_TOOLS["g++"]
    source = work / "main.c"
    write(
        source,
        "#include <stdio.h>\nint main(void){puts(\"crossforge-host-c\");return 0;}\n",
    )
    c_binary = work / "native-c"
    run(["gcc", "-O2", source, "-o", c_binary], env=environment)
    c_stdout, _stderr = run([c_binary], env=environment)
    require(c_stdout == "crossforge-host-c\n", "native C smoke differs")

    cxx_source = work / "main.cpp"
    write(
        cxx_source,
        "#include <iostream>\n"
        "static_assert(__GNUC__ == 15, \"GTS15 required\");\n"
        "int main(){std::cout<<\"crossforge-host-cxx\\n\";}\n",
    )
    cxx_binary = work / "native-cxx"
    run(["g++", "-O2", cxx_source, "-o", cxx_binary], env=environment)
    cxx_stdout, _stderr = run([cxx_binary], env=environment)
    require(cxx_stdout == "crossforge-host-cxx\n", "native C++ smoke differs")

    cmake_source = work / "cmake-source"
    cmake_source.mkdir()
    shutil.copy2(str(source), str(cmake_source / "main.c"))
    write(
        cmake_source / "CMakeLists.txt",
        "cmake_minimum_required(VERSION 3.16)\n"
        "project(crossforge_host C)\n"
        "add_executable(crossforge-cmake main.c)\n",
    )
    cmake_build = work / "cmake-build"
    run(
        ["cmake", "-S", cmake_source, "-B", cmake_build, "-G", "Ninja"],
        env=environment,
    )
    run(["cmake", "--build", cmake_build], env=environment)
    cache = (cmake_build / "CMakeCache.txt").read_text(encoding="utf-8")
    require(
        "CMAKE_GENERATOR:INTERNAL=Ninja" in cache
        and "CMAKE_C_COMPILER:FILEPATH=" + EXPECTED_TOOLS["gcc"] in cache,
        "CMake did not use the qualified compiler/generator",
    )
    cmake_stdout, _stderr = run(
        [cmake_build / "crossforge-cmake"], env=environment
    )
    require(cmake_stdout == c_stdout, "CMake/Ninja smoke differs")

    meson_source = work / "meson-source"
    meson_source.mkdir()
    shutil.copy2(str(source), str(meson_source / "main.c"))
    write(
        meson_source / "meson.build",
        "project('crossforge-host', 'c')\n"
        "executable('crossforge-meson', 'main.c')\n",
    )
    meson_build = work / "meson-build"
    run(["meson", "setup", meson_build, meson_source], env=environment)
    run(["meson", "compile", "-C", meson_build], env=environment)
    meson_stdout, _stderr = run(
        [meson_build / "crossforge-meson"], env=environment
    )
    require(meson_stdout == c_stdout, "Meson/Ninja smoke differs")

    autotools = work / "autotools"
    autotools.mkdir()
    (autotools / "m4").mkdir()
    shutil.copy2(str(source), str(autotools / "main.c"))
    write(
        autotools / "configure.ac",
        "AC_INIT([crossforge-host],[1.0])\n"
        "AM_INIT_AUTOMAKE([foreign])\n"
        "AC_CONFIG_MACRO_DIR([m4])\n"
        "AC_PROG_CC\n"
        "LT_INIT\n"
        "AC_CONFIG_FILES([Makefile])\n"
        "AC_OUTPUT\n",
    )
    write(
        autotools / "Makefile.am",
        "ACLOCAL_AMFLAGS = -I m4\n"
        "bin_PROGRAMS = crossforge-autotools\n"
        "crossforge_autotools_SOURCES = main.c\n",
    )
    run(["autoreconf", "-fi"], cwd=autotools, env=environment)
    run([autotools / "configure"], cwd=autotools, env=environment)
    run(["make", "-j2"], cwd=autotools, env=environment)
    autotools_stdout, _stderr = run(
        [autotools / "crossforge-autotools"], env=environment
    )
    require(autotools_stdout == c_stdout, "Autotools smoke differs")
    perl_ipc_stdout, _stderr = run(
        [
            "/usr/bin/perl",
            "-MIPC::Cmd",
            "-MTime::Piece",
            "-e",
            'print "$IPC::Cmd::VERSION\\n"',
        ],
        env=environment,
    )
    require(
        perl_ipc_stdout == "1.02\n",
        "Perl IPC::Cmd host-tool dependency differs",
    )

    return {
        "autotools": hashlib.sha256(autotools_stdout.encode("utf-8")).hexdigest(),
        "cmake_ninja": hashlib.sha256(cmake_stdout.encode("utf-8")).hexdigest(),
        "meson_ninja": hashlib.sha256(meson_stdout.encode("utf-8")).hexdigest(),
        "perl_ipc_cmd": hashlib.sha256(
            perl_ipc_stdout.encode("utf-8")
        ).hexdigest(),
        "native_c": hashlib.sha256(c_stdout.encode("utf-8")).hexdigest(),
        "native_cxx": hashlib.sha256(cxx_stdout.encode("utf-8")).hexdigest(),
    }


def qualify(
    lock_path,
    transaction_path,
    marker_path,
    component_path,
    component_sha256,
):
    lock = load_json(lock_path)
    transaction = load_json(transaction_path)
    marker = load_json(marker_path)
    component = load_json(component_path)
    require(
        SHA256_RE.match(component_sha256) is not None
        and canonical_sha256(component) == component_sha256
        and component.get("kind") == "crossforge-release-component"
        and component.get("component") == "rpm/host-runtime"
        and component.get("scope") == "build",
        "host runtime release component differs",
    )
    expected_binding = {
        "kind": "release-component",
        "component": "rpm/host-runtime",
        "scope": "build",
        "canonical_sha256": component_sha256,
    }
    result = transaction["manifests"]["result"]
    inventory = rpm_inventory()
    require(inventory == result["packages"], "host runtime RPMDB differs from lock")
    require(
        canonical_sha256(inventory) == result["canonical_sha256"],
        "host runtime RPMDB digest differs",
    )
    require(
        set(marker)
        == {
            "schema_version",
            "kind",
            "role",
            "lock_sha256",
            "transaction_sha256",
            "result_sha256",
            "result_item_count",
            "release_binding",
        }
        and marker["schema_version"] == 2
        and marker["kind"] == "host-rpm-install-marker"
        and marker["role"] == "host-runtime"
        and marker["lock_sha256"] == canonical_sha256(lock)
        and marker["transaction_sha256"] == canonical_sha256(transaction)
        and marker["result_sha256"] == result["canonical_sha256"]
        and marker["result_item_count"] == len(inventory)
        and marker["release_binding"] == expected_binding,
        "host runtime install marker differs",
    )
    require(
        marker_path.is_file() and not marker_path.is_symlink(),
        "host runtime marker is missing or unsafe",
    )
    marker_files = sorted(path.name for path in marker_path.parent.iterdir())
    require(
        marker_files == ["host-runtime.json"],
        "build-only host lock markers leaked into runtime",
    )
    path_prefix = os.environ.get("PATH", "").split(":", 1)[0]
    require(platform.machine() == "x86_64", "host runtime architecture differs")
    require(
        path_prefix == "/opt/rh/gcc-toolset-15/root/usr/bin",
        "host runtime PATH does not prefer GTS15",
    )
    with tempfile.TemporaryDirectory(prefix="crossforge-host-runtime-") as temporary:
        smoke = smoke_builds(Path(temporary))
    return {
        "schema_version": 1,
        "kind": "crossforge-host-runtime-qualification",
        "status": "passed",
        "rpm": {
            "lock_sha256": canonical_sha256(lock),
            "marker_sha256": sha256_file(marker_path),
            "release_binding": expected_binding,
            "result_item_count": len(inventory),
            "result_sha256": result["canonical_sha256"],
            "transaction_sha256": canonical_sha256(transaction),
        },
        "environment": {
            "host_arch": platform.machine(),
            "path_prefix": path_prefix,
        },
        "tools": tool_evidence(),
        "smoke": smoke,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--transaction", type=Path, required=True)
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--release-component", type=Path, required=True)
    parser.add_argument("--release-component-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = qualify(
        arguments.lock,
        arguments.transaction,
        arguments.marker,
        arguments.release_component,
        arguments.release_component_sha256,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(arguments.output)
    print("qualified independent host runtime: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, QualificationError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
