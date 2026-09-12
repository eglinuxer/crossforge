#!/usr/bin/env python3
"""Prepare and recheck the authenticated raw-component SDK publication graph."""

import argparse
import os
from pathlib import Path
import subprocess
import sys

from crossforge_internal import candidate_components, candidate_qualification
from crossforge_internal.identity import content_sha256, require

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=("prepare", "check", "build"))
    parser.add_argument("--source-binding", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--builder", required=True)
    parser.add_argument("--docker-config", type=Path)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--oras", type=Path)
    parser.add_argument("--cosign", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--reference")
    parser.add_argument("--sbom-generator")
    args = parser.parse_args(argv)
    try:
        build_options = (args.output_root, args.reference, args.sbom_generator)
        require(all(value is not None for value in build_options) if args.command == "build" else
                all(value is None for value in build_options), "candidate build options differ from command")
        if args.command == "prepare":
            require(all(value is not None for value in (args.data, args.oras, args.cosign)) and args.sha256 is None,
                    "candidate preparation requires transport/verifier/data and no prior digest")
            ready = candidate_components.prepare(ROOT, args.source_binding, args.directory, args.data,
                args.builder, args.oras, args.cosign, args.docker_config)
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write("binding_sha256=" + content_sha256(ready) + "\n")
        else:
            require(args.sha256 is not None and all(value is None for value in (args.data, args.oras, args.cosign)),
                    "candidate recheck requires the original binding digest")
            if args.command == "build":
                candidate_qualification.execute(ROOT, args.source_binding, args.directory, args.sha256, args.builder,
                    args.output_root, args.reference, args.sbom_generator, args.docker_config)
            else:
                candidate_components.check(ROOT, args.source_binding, args.directory, args.sha256, args.builder, args.docker_config)
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print("candidate component binding failed: " + str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            return error.returncode if error.returncode > 0 else 128 - error.returncode
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
