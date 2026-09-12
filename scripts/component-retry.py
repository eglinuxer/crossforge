#!/usr/bin/env python3
"""Validate original producer outputs before raw-component signing or storage."""

import argparse
from pathlib import Path
import subprocess
import sys

from crossforge_internal import component_ci, component_retry
from crossforge_internal.identity import parse_json

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    upstream = commands.add_parser("upstream", allow_abbrev=False)
    upstream.add_argument("--stage", choices=("sign", "store"), required=True)
    upstream.add_argument("--needs-json", required=True)
    outputs = commands.add_parser("catalog-outputs", allow_abbrev=False)
    outputs.add_argument("--directory", type=Path, required=True)
    outputs.add_argument("--producer-invocation", required=True)
    verify = commands.add_parser("verify-catalog", allow_abbrev=False)
    verify.add_argument("--directory", type=Path, required=True)
    verify.add_argument("--needs-json", required=True)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error("a component retry command is required")
    try:
        current = component_ci.checked_source(ROOT, "main")
        if args.command == "upstream":
            component_retry.upstream(parse_json(args.needs_json), args.stage, current)
        elif args.command == "verify-catalog":
            component_retry.verify_catalog(args.directory, parse_json(args.needs_json), current)
        else:
            values = component_retry.catalog_metadata(args.directory, current, args.producer_invocation)
            values["signer-invocation"] = current["invocation"]
            for key, value in sorted(values.items()):
                print(key + "=" + value)
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print("component retry failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
