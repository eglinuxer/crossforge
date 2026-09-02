#!/usr/bin/env python3
"""Prove that a legacy build Python sees only target sysconfig data."""

import argparse
import os
import shlex
import sys
from pathlib import Path


FIELDS = (
    "AR",
    "CC",
    "CONFIG_ARGS",
    "EXT_SUFFIX",
    "LDSHARED",
    "MULTIARCH",
    "SOABI",
)


class VerificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def normalized_paths(paths):
    return {
        os.path.realpath(path if path else os.getcwd()) for path in paths
    }


def validate(
    canonical,
    legacy,
    sys_path,
    target_sysconfig_directory,
    source_lib,
    expected,
):
    require(set(canonical) == set(FIELDS), "canonical sysconfig fields differ")
    require(set(legacy) == set(FIELDS), "distutils sysconfig fields differ")
    require(canonical == legacy, "stdlib and distutils sysconfig disagree")
    for name, value in canonical.items():
        require(isinstance(value, str) and value, "%s is not target text" % name)
    for name in (
        "CC",
        "AR",
        "LDSHARED",
        "MULTIARCH",
        "SOABI",
        "EXT_SUFFIX",
    ):
        require(
            canonical[name] == expected[name],
            "target sysconfig %s mismatch: %r != %r"
            % (name, canonical[name], expected[name]),
        )
    try:
        config_arguments = shlex.split(canonical["CONFIG_ARGS"])
    except ValueError as error:
        raise VerificationError("target CONFIG_ARGS cannot be parsed") from error
    require(
        config_arguments.count("--host=" + expected["target"]) == 1,
        "target CONFIG_ARGS host mismatch",
    )
    resolved = normalized_paths(sys_path)
    target_directory = os.path.realpath(str(target_sysconfig_directory))
    source_directory = os.path.realpath(str(source_lib))
    require(
        target_directory not in resolved,
        "target extension directory leaked into build Python sys.path",
    )
    require(
        source_directory in resolved,
        "prepared source Lib is absent from build Python sys.path",
    )
    return canonical


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-python", type=Path, required=True)
    parser.add_argument("--target-sysconfig-directory", type=Path, required=True)
    parser.add_argument("--source-lib", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--cc", required=True)
    parser.add_argument("--ar", required=True)
    parser.add_argument("--ldshared", required=True)
    parser.add_argument("--multiarch", required=True)
    parser.add_argument("--soabi", required=True)
    parser.add_argument("--ext-suffix", required=True)
    arguments = parser.parse_args()

    require(
        Path(sys.executable).resolve() == arguments.build_python.resolve(),
        "unexpected build Python executable",
    )
    require(
        os.environ.get("_PYTHON_SYSCONFIGDATA_PATH")
        == str(arguments.target_sysconfig_directory),
        "target sysconfig directory environment mismatch",
    )
    require(
        os.environ.get("PYTHONPATH") == str(arguments.source_lib),
        "build Python PYTHONPATH differs from prepared source Lib",
    )

    import sysconfig
    from distutils import sysconfig as distutils_sysconfig

    canonical = {name: sysconfig.get_config_var(name) for name in FIELDS}
    legacy = {
        name: distutils_sysconfig.get_config_var(name) for name in FIELDS
    }
    validate(
        canonical,
        legacy,
        sys.path,
        arguments.target_sysconfig_directory,
        arguments.source_lib,
        {
            "target": arguments.target,
            "CC": arguments.cc,
            "AR": arguments.ar,
            "LDSHARED": arguments.ldshared,
            "MULTIARCH": arguments.multiarch,
            "SOABI": arguments.soabi,
            "EXT_SUFFIX": arguments.ext_suffix,
        },
    )
    print("verified legacy build-Python target sysconfig: %s" % arguments.target)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, VerificationError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
