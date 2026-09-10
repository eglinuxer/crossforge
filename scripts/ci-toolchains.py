#!/usr/bin/env python3
"""Plan and centralize missing raw toolchain producers for trusted main CI."""

import argparse
import json
from pathlib import Path
import runpy
import subprocess
import sys

from crossforge_internal import ci_toolchains, component_ci
from crossforge_internal.identity import IdentityError, parse_json, require


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    plan = commands.add_parser("plan", allow_abbrev=False)
    plan.add_argument("--profile", required=True)
    plan.add_argument("--selection", default="")
    ensure = commands.add_parser("ensure", allow_abbrev=False)
    ensure.add_argument("--architecture", required=True, choices=ci_toolchains.ARCHITECTURES)
    ensure.add_argument("--roles", required=True)
    ensure.add_argument("--oras", type=Path, required=True)
    ensure.add_argument("--cosign", type=Path, required=True)
    for command in (plan, ensure):
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--builder", required=True)
        command.add_argument("--docker-config", type=Path)
    for name in ("check-production", "check-ready"):
        check = commands.add_parser(name, allow_abbrev=False)
        check.add_argument("results")
    args = parser.parse_args(argv)
    try:
        if args.command == "check-production":
            return 0 if ci_toolchains.check_production(parse_json(args.results)) else 1
        if args.command == "check-ready":
            stages = runpy.run_path(str(ROOT / "scripts/ci-build.py"))["STAGES"]
            return 0 if ci_toolchains.check_ready(parse_json(args.results), stages) else 1
        if args.command == "plan":
            component_ci.checked_source(ROOT, "main")
            stages = runpy.run_path(str(ROOT / "scripts/ci-build.py"))["STAGES"]
            result = ci_toolchains.plan(ROOT, args.selection, args.profile, stages, args.output,
                                       args.builder, args.docker_config)
            print("selection=" + json.dumps(result["selection"], sort_keys=True, separators=(",", ":")))
            for arch in ci_toolchains.ARCHITECTURES:
                print(arch + "-roles=" + json.dumps(result["roles"][arch], separators=(",", ":")))
            return 0
        require(args.command == "ensure", "a toolchain CI command is required")
        result = ci_toolchains.ensure(ROOT, args.architecture, parse_json(args.roles), args.output,
                                      args.builder, args.oras, args.cosign, args.docker_config)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (IdentityError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
