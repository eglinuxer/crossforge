#!/usr/bin/env python3
"""Create same-run catalogs or authenticate and select cross-run references."""

import argparse
import json
from pathlib import Path
import runpy
import subprocess
import sys

from crossforge_internal import component_catalog, component_handoff
from crossforge_internal.identity import IdentityError, canonical_bytes, load_json, require


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    create = commands.add_parser("from-handoff", allow_abbrev=False)
    create.add_argument("--handoff", type=Path, required=True)
    create.add_argument("--handoff-sha256", required=True)
    create.add_argument("--output", type=Path, required=True)
    for name in ("verify", "select"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--catalog", type=Path, required=True)
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--cosign", type=Path, required=True)
        command.add_argument("--temporary-parent", type=Path)
        if name == "select":
            command.add_argument("--expected-inputs", type=Path, required=True)
            command.add_argument("--role", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "from-handoff":
            pilot = runpy.run_path(str(ROOT / "scripts/component-pilot.py"))
            current = pilot["checked_source"]()
            value = component_handoff.verify(load_json(args.handoff), args.handoff_sha256,
                current["source_commit"], current["invocation"])
            catalog = component_catalog.document(value["producer"], list(value["components"].values()))
            require(not args.output.exists() and not args.output.is_symlink(), "catalog output must be new")
            with args.output.open("xb") as stream:
                stream.write(canonical_bytes(catalog) + b"\n")
            result = {"catalog": str(args.output), "status": "unsigned"}
        elif args.command == "verify":
            _, result = component_catalog.verify(ROOT, args.catalog, args.bundle, args.cosign, args.temporary_parent)
        elif args.command == "select":
            result = component_catalog.select(ROOT, args.catalog, args.bundle, args.cosign,
                load_json(args.expected_inputs), args.role, args.temporary_parent)
        else:
            parser.error("a command is required")
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (IdentityError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
