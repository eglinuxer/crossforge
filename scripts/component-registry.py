#!/usr/bin/env python3
"""Transfer verified internal component artifacts without rebuilding them."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tarfile

from crossforge_internal import component_artifacts, component_build, component_inputs, registry_transfer
from crossforge_internal.identity import IdentityError, content_sha256, load_json, require


POLICY = Path(__file__).resolve().parents[1] / ".github/locked-tools/oras.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    install = commands.add_parser("install-tool", allow_abbrev=False)
    install.add_argument("--output", type=Path, required=True)
    install.add_argument("--archive", type=Path, help="optional predownloaded pinned archive")
    for name in ("publish", "fetch"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--receipt", type=Path, required=True)
        command.add_argument("--receipt-sha256", required=True, help="independently trusted canonical receipt SHA256")
        command.add_argument("--expected-inputs", type=Path, required=True, help="independently captured current inputs")
        command.add_argument("--role", choices=sorted(component_artifacts.ROLES), required=True)
        command.add_argument("--frontend", required=True)
        command.add_argument("--builder", required=True)
        command.add_argument("--docker-config", type=Path)
        command.add_argument("--registry-config", type=Path, help="ORAS authentication file; never put tokens on command line")
        command.add_argument("--oras", type=Path, required=True)
        command.add_argument("--loopback-http", action="store_true", help="local loopback registry tests only")
        command.add_argument("--temporary-parent", type=Path)
        if name == "publish":
            command.add_argument("--layout", type=Path, required=True)
            command.add_argument("--repository", required=True)
        else:
            command.add_argument("--reference", required=True, help="independently trusted registry/repository@sha256:digest")
            command.add_argument("--output", type=Path, required=True, help="new local OCI directory")
    args = parser.parse_args(argv)
    try:
        policy = registry_transfer.validate_tool(load_json(POLICY))
        if args.command == "install-tool":
            result = {"binary": str(registry_transfer.install_tool(policy, args.output, args.archive)),
                      "policy": policy}
        elif args.command in ("publish", "fetch"):
            receipt = component_artifacts.validate_receipt(load_json(args.receipt))
            require(content_sha256(receipt) == args.receipt_sha256, "receipt differs from trusted reference")
            expected = load_json(args.expected_inputs)
            component_inputs.require_match(receipt["contract"]["inputs"], expected)
            require(receipt["contract"]["role"] == args.role, "component artifact role differs")
            if args.command == "fetch":
                _, digest = registry_transfer.reference(args.reference, args.loopback_http)
                require(digest == receipt["artifact"]["root_digest"], "registry artifact differs from trusted receipt")
                registry_transfer.fetch(args.reference, args.output, args.oras, policy,
                    args.registry_config, args.loopback_http)
                layout = args.output
            else:
                layout = args.layout
            local = component_build.verify_local(receipt, args.receipt_sha256, expected, args.role,
                layout, args.frontend, args.builder, args.docker_config, args.temporary_parent)
            if args.command == "publish":
                result = registry_transfer.publish(layout, receipt["artifact"]["root_digest"],
                    args.repository, args.oras, policy, args.registry_config, args.loopback_http)
            else:
                result = {"reference": args.reference, "layout": str(Path(layout).resolve())}
            result.update({"local_reference": local, "receipt_sha256": args.receipt_sha256,
                           "qualification": "not asserted by transport; use verify-qualification for report acceptance"})
        else:
            parser.error("a command is required")
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (IdentityError, OSError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
