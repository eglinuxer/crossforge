#!/usr/bin/env python3
"""Build and verify internal local OCI components through Docker/Bake."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from crossforge_internal import component_build, component_qualification, qualification_execution
from crossforge_internal.identity import IdentityError, load_json, require
from crossforge_internal.oci_layout import inspect


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    check = commands.add_parser("inspect-layout", allow_abbrev=False)
    check.add_argument("--layout", type=Path, required=True)
    check.add_argument("--digest", required=True)
    for command in ("execution-identity", "qualification-execution-identity"):
        environment = commands.add_parser(command, allow_abbrev=False)
        environment.add_argument("--builder", required=True)
        environment.add_argument("--docker-config", type=Path)
    for command in ("qualification-inputs", "produce-qualification", "verify-qualification"):
        qualification = commands.add_parser(command, allow_abbrev=False)
        qualification.add_argument("--source", type=Path, required=True)
        qualification.add_argument("--graph", type=Path, required=True, help="checked canonical Bake graph including subject producers")
        qualification.add_argument("--arch", choices=("x86_64", "aarch64"), required=True)
        qualification.add_argument("--profile", choices=("toolchain", "gcc-smoke", "gcc-full"), required=True)
        qualification.add_argument("--execution", type=Path, required=True)
        qualification.add_argument("--subjects", type=Path, required=True, help="role to receipt, independently trusted receipt_sha256 and local layout")
        qualification.add_argument("--builder", required=True)
        qualification.add_argument("--docker-config", type=Path)
        if command == "produce-qualification":
            qualification.add_argument("--producer", type=Path, required=True)
            qualification.add_argument("--output", type=Path, required=True)
        else:
            qualification.add_argument("--temporary-parent", type=Path)
        if command == "verify-qualification":
            qualification.add_argument("--receipt", type=Path, required=True)
            qualification.add_argument("--receipt-sha256", required=True)
            qualification.add_argument("--layout", type=Path, required=True)
    for command in ("plan-toolchain", "produce-toolchain", "toolchain-inputs"):
        plan = commands.add_parser(command, allow_abbrev=False)
        plan.add_argument("--source", type=Path, required=True)
        plan.add_argument("--graph", type=Path, required=True, help="checked docker buildx bake --print JSON")
        plan.add_argument("--arch", choices=("x86_64", "aarch64"), required=True)
        plan.add_argument("--role", choices=("toolchain-install", "gcc-test-context"), required=True)
        plan.add_argument("--execution", type=Path, required=True)
        if command != "toolchain-inputs":
            plan.add_argument("--producer", type=Path, required=True)
            plan.add_argument("--output", type=Path, required=True, help="new local output directory")
        if command == "produce-toolchain":
            plan.add_argument("--builder", required=True)
            plan.add_argument("--docker-config", type=Path)
    verify = commands.add_parser("verify-local", allow_abbrev=False)
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--receipt-sha256", required=True, help="independently trusted canonical receipt SHA256")
    verify.add_argument("--expected-inputs", type=Path, required=True, help="independently captured current inputs")
    verify.add_argument("--role", choices=("toolchain-install", "gcc-test-context", "python-row", "qualification"), required=True)
    verify.add_argument("--layout", type=Path, required=True)
    verify.add_argument("--frontend", required=True)
    verify.add_argument("--builder", required=True)
    verify.add_argument("--docker-config", type=Path)
    verify.add_argument("--temporary-parent", type=Path)
    verify.add_argument("--consumer-target")
    verify.add_argument("--context-name")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect-layout":
            value = inspect(args.layout, args.digest)
        elif args.command == "execution-identity":
            value = component_build.execution_identity(args.builder, args.docker_config)
        elif args.command == "qualification-execution-identity":
            value = qualification_execution.execution_identity(args.builder, args.docker_config)
        elif args.command in ("qualification-inputs", "produce-qualification", "verify-qualification"):
            graph, execution, subjects = load_json(args.graph), load_json(args.execution), load_json(args.subjects)
            require(qualification_execution.execution_identity(args.builder, args.docker_config) == execution,
                    "qualification execution environment differs")
            if args.command == "produce-qualification":
                value = component_qualification.produce(args.source, graph, args.arch, args.profile, execution,
                    load_json(args.producer), subjects, args.output, args.builder, args.docker_config)
            else:
                settings = component_qualification.spec(args.arch, args.profile)
                resolved, bindings = component_qualification.bind_subjects(args.source, graph, settings, execution,
                    subjects, args.builder, args.docker_config, args.temporary_parent)
                value = component_qualification.qualification_inputs(args.source, resolved, settings, execution, bindings)
                if args.command == "verify-qualification":
                    value = component_qualification.verify_local(load_json(args.receipt), args.receipt_sha256,
                        value, args.source, args.layout, args.builder, args.docker_config, args.temporary_parent)
        elif args.command in ("plan-toolchain", "produce-toolchain", "toolchain-inputs"):
            values = [args.source, load_json(args.graph), args.arch, args.role, load_json(args.execution)]
            if args.command == "toolchain-inputs":
                value = component_build.toolchain_inputs(*values)
            else:
                values.extend([load_json(args.producer), args.output])
                if args.command == "plan-toolchain":
                    value = component_build.plan_toolchain(*values)
                else:
                    value = component_build.produce_toolchain(*values, builder=args.builder, docker_config=args.docker_config)
        elif args.command == "verify-local":
            require(bool(args.consumer_target) == bool(args.context_name),
                    "consumer target and context name must be specified together")
            if args.consumer_target:
                require(all(re.fullmatch(r"[a-zA-Z0-9_-]+", name) for name in (args.consumer_target, args.context_name)),
                        "consumer target or context name is invalid")
            reference = component_build.verify_local(load_json(args.receipt), args.receipt_sha256,
                load_json(args.expected_inputs), args.role, args.layout, args.frontend,
                args.builder, args.docker_config, args.temporary_parent)
            value = {"reference": reference}
            if args.consumer_target:
                value = {"target": {args.consumer_target: {"contexts": {args.context_name: reference}}}}
        else:
            parser.error("a command is required")
        print(json.dumps(value, sort_keys=True, indent=2))
        return 0
    except (IdentityError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
