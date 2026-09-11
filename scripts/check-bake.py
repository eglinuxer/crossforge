#!/usr/bin/env python3
"""Check the complete resolved Bake graph without executing build dependencies."""

import argparse
from pathlib import Path

from crossforge_internal import bake_check
from crossforge_internal.identity import IdentityError


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--builder")
    parser.add_argument("targets", nargs="+")
    args = parser.parse_args()
    try:
        return bake_check.check(Path.cwd(), args.targets, args.builder)
    except IdentityError as error:
        parser.exit(1, "Bake static check rejected: %s\n" % error)


if __name__ == "__main__":
    raise SystemExit(main())
