#!/usr/bin/env python3
"""Validate the fixed CPython runtime-provider policy and target locks."""

import argparse
import sys
from pathlib import Path

import python_runtime_providers


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--provider-roots",
        type=Path,
        help=(
            "also verify <path>/{sysroot,clean}/{aarch64,x86_64} DSO "
            "bytes and equality"
        ),
    )
    arguments = parser.parse_args()
    report = python_runtime_providers.validate_repository(
        provider_roots=arguments.provider_roots
    )
    print("python runtime provider policy: %s" % report["policy_sha256"])
    for arch in python_runtime_providers.TARGET_ORDER:
        target = report["targets"][arch]
        print(
            "%s: %d providers, %d RPM owners, lock %s"
            % (
                arch,
                target["provider_count"],
                target["rpm_owner_count"],
                target["sysroot_lock_sha256"],
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except python_runtime_providers.RuntimeProviderPolicyError as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
