#!/usr/bin/env python3
"""Create same-run catalogs or authenticate and select cross-run references."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from crossforge_internal import catalog_registry, component_catalog, component_ci, component_handoff, python_handoff
from crossforge_internal import python_row_handoff
from crossforge_internal.identity import IdentityError, canonical_bytes, load_json, require


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    create = commands.add_parser("from-handoff", allow_abbrev=False)
    create.add_argument("--handoff", type=Path, required=True)
    create.add_argument("--handoff-sha256", required=True)
    create.add_argument("--producer-invocation", help="original successful producer invocation from the upstream job output")
    create.add_argument("--output", type=Path, required=True)
    modes = create.add_mutually_exclusive_group()
    modes.add_argument("--main-ci", action="store_true", help="sign only new raw toolchains from the exact main CI run")
    modes.add_argument("--python-ci", action="store_true", help="sign only new raw Python artifacts from the exact main CI run")
    modes.add_argument("--python-row-ci", action="store_true", help="sign only freshly qualified Python rows from the exact main CI run")
    for name in ("verify", "select"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--catalog", type=Path, required=True)
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--cosign", type=Path, required=True)
        command.add_argument("--temporary-parent", type=Path)
        if name == "select":
            command.add_argument("--expected-inputs", type=Path, required=True)
            command.add_argument("--role", required=True)
    for name in ("publish", "lookup"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--cosign", type=Path, required=True)
        command.add_argument("--oras", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True, help="new local packing or download directory")
        command.add_argument("--repository", default=component_catalog.REPOSITORY)
        command.add_argument("--registry-config", type=Path)
        command.add_argument("--loopback-http", action="store_true", help="local isolated registry experiments only")
        if name == "publish":
            command.add_argument("--catalog", type=Path, required=True)
            command.add_argument("--bundle", type=Path, required=True)
        else:
            command.add_argument("--expected-inputs", type=Path, required=True)
            command.add_argument("--role", required=True)
            command.add_argument("--catalog-reference", help="exact previously verified catalog digest for recovery; do not consult mutable index")
    args = parser.parse_args(argv)
    try:
        if args.command == "from-handoff":
            main_ci = args.main_ci or args.python_ci or args.python_row_ci
            current = component_ci.checked_source(ROOT, "main" if main_ci else "pilot")
            invocation = current["invocation"]
            if args.producer_invocation is not None:
                require(main_ci, "legacy pilot handoff requires the current attempt")
                from crossforge_internal.component_retry import prior_invocation
                invocation = prior_invocation(args.producer_invocation, current["invocation"])
            if args.python_row_ci:
                value = python_row_handoff.verify(ROOT, load_json(args.handoff), args.handoff_sha256,
                    current["source_commit"], invocation)
                signing = {"workflow": component_catalog.PYTHON_ROW_WORKFLOW, "event": os.environ["GITHUB_EVENT_NAME"]}
            elif args.python_ci:
                value = python_handoff.verify(ROOT, load_json(args.handoff), args.handoff_sha256,
                    current["source_commit"], invocation)
                signing = {"workflow": component_catalog.PYTHON_WORKFLOW, "event": os.environ["GITHUB_EVENT_NAME"]}
            else:
                value = component_handoff.verify(load_json(args.handoff), args.handoff_sha256,
                    current["source_commit"], invocation)
                require(value["schema_version"] == (2 if args.main_ci else 1), "handoff schema differs from signing entry point")
                signing = {"workflow": component_catalog.MAIN_WORKFLOW, "event": os.environ["GITHUB_EVENT_NAME"]} if args.main_ci else None
            catalog = component_catalog.document(value["producer"], list(value["components"].values()), signing)
            require(not args.output.exists() and not args.output.is_symlink(), "catalog output must be new")
            with args.output.open("xb") as stream:
                stream.write(canonical_bytes(catalog) + b"\n")
            result = {"catalog": str(args.output), "status": "unsigned"}
        elif args.command == "verify":
            _, result = component_catalog.verify(ROOT, args.catalog, args.bundle, args.cosign, args.temporary_parent)
        elif args.command == "select":
            result = component_catalog.select(ROOT, args.catalog, args.bundle, args.cosign,
                load_json(args.expected_inputs), args.role, args.temporary_parent)
        elif args.command == "publish":
            result = catalog_registry.publish(ROOT, args.catalog, args.bundle, args.cosign, args.output,
                args.repository, args.oras, load_json(ROOT / ".github/locked-tools/oras.json"),
                args.registry_config, args.loopback_http)
        elif args.command == "lookup":
            result = catalog_registry.lookup(ROOT, load_json(args.expected_inputs), args.role, args.cosign, args.output,
                args.repository, args.oras, load_json(ROOT / ".github/locked-tools/oras.json"),
                args.registry_config, args.loopback_http, args.catalog_reference)
        else:
            parser.error("a command is required")
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (IdentityError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
