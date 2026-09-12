#!/usr/bin/env python3
"""Resolve or freshly qualify a complete Python row for trusted main CI."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from crossforge_internal import ci_python_rows
from crossforge_internal.identity import IdentityError


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--row", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--builder", required=True)
    parser.add_argument("--oras", type=Path, required=True)
    parser.add_argument("--cosign", type=Path, required=True)
    parser.add_argument("--docker-config", type=Path)
    args = parser.parse_args(argv)
    try:
        result = ci_python_rows.ensure(ROOT, args.row, args.output, args.builder, args.oras, args.cosign, args.docker_config)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (IdentityError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
