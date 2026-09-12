#!/usr/bin/env python3
"""Prepare raw Python components for trusted main CI through Docker/Bake."""

import argparse
import json
from pathlib import Path
import runpy
import subprocess
import sys

from crossforge_internal import ci_python, component_ci
from crossforge_internal.identity import IdentityError, parse_json, require


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    ensure = commands.add_parser("ensure", allow_abbrev=False)
    ensure.add_argument("--row", required=True)
    ensure.add_argument("--parts", required=True, help="sorted JSON list of raw parts, including build Python")
    ensure.add_argument("--output", type=Path, required=True)
    ensure.add_argument("--builder", required=True)
    ensure.add_argument("--oras", type=Path, required=True)
    ensure.add_argument("--cosign", type=Path, required=True)
    ensure.add_argument("--docker-config", type=Path)
    execution = commands.add_parser("execution", allow_abbrev=False)
    execution.add_argument("--profile", required=True)
    execution.add_argument("--selection", required=True)
    execution.add_argument("--parts", required=True)
    check = commands.add_parser("check", allow_abbrev=False)
    check.add_argument("results")
    args = parser.parse_args(argv)
    try:
        if args.command in ("execution", "check"):
            stages = runpy.run_path(str(ROOT / "scripts/ci-build.py"))["STAGES"]
            if args.command == "check":
                return 0 if ci_python.check_results(parse_json(args.results), stages) else 1
            component_ci.checked_source(ROOT, "main")
            for key, value in ci_python.execution(args.selection, args.profile, stages, args.parts).items():
                print(key + "=" + value)
            return 0
        require(args.command == "ensure", "a Python CI command is required")
        result = ci_python.ensure(ROOT, args.row, parse_json(args.parts), args.output, args.builder,
                                  args.oras, args.cosign, args.docker_config)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (IdentityError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
