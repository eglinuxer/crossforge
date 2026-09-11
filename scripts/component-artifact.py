#!/usr/bin/env python3
"""Build and verify internal local OCI components through Docker/Bake."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from crossforge_internal import component_artifacts, component_build, component_qualification, qualification_execution, python_components
from crossforge_internal import python_qualification, python_sdk
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
    for command in ("python-inputs", "produce-python", "bind-python-row"):
        python = commands.add_parser(command, allow_abbrev=False)
        python.add_argument("--source", type=Path, required=True)
        python.add_argument("--graph", type=Path, required=True)
        python.add_argument("--row", required=True)
        python.add_argument("--execution", type=Path, required=True)
        python.add_argument("--subjects", type=Path, required=True)
        python.add_argument("--builder", required=True)
        python.add_argument("--docker-config", type=Path)
        python.add_argument("--temporary-parent", type=Path)
        if command != "bind-python-row":
            python.add_argument("--arch", choices=("build", "x86_64", "aarch64"), required=True)
            python.add_argument("--kind", choices=("install", "test-context"), required=True)
        if command == "produce-python":
            python.add_argument("--producer", type=Path, required=True)
            python.add_argument("--output", type=Path, required=True)
    for command in ("python-qualification-inputs", "produce-python-qualification", "verify-python-qualification"):
        row = commands.add_parser(command, allow_abbrev=False)
        row.add_argument("--source", type=Path, required=True)
        row.add_argument("--graph", type=Path, required=True)
        row.add_argument("--row", required=True)
        row.add_argument("--execution", type=Path, required=True)
        row.add_argument("--subjects", type=Path, required=True)
        row.add_argument("--builder", required=True)
        row.add_argument("--docker-config", type=Path)
        if command == "produce-python-qualification":
            row.add_argument("--producer", type=Path, required=True)
            row.add_argument("--output", type=Path, required=True)
        else:
            row.add_argument("--temporary-parent", type=Path)
        if command == "verify-python-qualification":
            row.add_argument("--receipt", type=Path, required=True)
            row.add_argument("--receipt-sha256", required=True)
            row.add_argument("--layout", type=Path, required=True)
    for command in ("bind-python-sdk", "execute-python-sdk"):
        sdk = commands.add_parser(command, allow_abbrev=False)
        sdk.add_argument("--source", type=Path, required=True)
        sdk.add_argument("--graph", type=Path, required=True)
        sdk.add_argument("--root", choices=sorted(python_sdk.ROOTS), required=True)
        sdk.add_argument("--execution", type=Path, required=True)
        sdk.add_argument("--components", type=Path, required=True)
        sdk.add_argument("--builder", required=True)
        sdk.add_argument("--docker-config", type=Path)
        if command == "execute-python-sdk":
            sdk.add_argument("--output", type=Path, required=True)
        else:
            sdk.add_argument("--temporary-parent", type=Path)
    for command in ("acquire-python-sdk", "execute-python-sdk-catalog"):
        sdk = commands.add_parser(command, allow_abbrev=False)
        sdk.add_argument("--source", type=Path, required=True)
        sdk.add_argument("--graph", type=Path, required=True)
        sdk.add_argument("--root", choices=sorted(python_sdk.ROOTS), required=True)
        sdk.add_argument("--execution", type=Path, required=True)
        sdk.add_argument("--builder", required=True)
        sdk.add_argument("--docker-config", type=Path)
        sdk.add_argument("--oras", type=Path, required=True)
        sdk.add_argument("--cosign", type=Path, required=True)
        sdk.add_argument("--component-directory", type=Path, required=True, help="new OCI data directory outside uploaded diagnostics")
        sdk.add_argument("--output", type=Path, required=True, help="new diagnostics directory")
    verify = commands.add_parser("verify-local", allow_abbrev=False)
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--receipt-sha256", required=True, help="independently trusted canonical receipt SHA256")
    verify.add_argument("--expected-inputs", type=Path, required=True, help="independently captured current inputs")
    verify.add_argument("--role", choices=sorted(component_artifacts.ROLES), required=True)
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
        elif args.command in ("python-inputs", "produce-python", "bind-python-row"):
            graph, execution, subjects = load_json(args.graph), load_json(args.execution), load_json(args.subjects)
            require(component_build.execution_identity(args.builder, args.docker_config) == execution,
                    "Python component execution environment differs")
            if args.command == "produce-python":
                value = python_components.produce(args.source, graph, args.row, args.arch, args.kind, execution,
                    load_json(args.producer), subjects, args.output, args.builder, args.docker_config)
            elif args.command == "bind-python-row":
                resolved, bindings = python_components.bind_row(args.source, graph, args.row, execution, subjects,
                    args.builder, args.docker_config, args.temporary_parent)
                value = {"graph": resolved, "bindings": bindings, "qualification": "not executed by binding"}
            else:
                settings = python_components.spec(args.source, args.row, args.arch, args.kind)
                resolved, bindings = python_components.bind_build(args.source, graph, settings, execution, subjects,
                    args.builder, args.docker_config, args.temporary_parent)
                value = python_components.inputs(args.source, resolved, settings, execution, bindings)
        elif args.command in ("python-qualification-inputs", "produce-python-qualification", "verify-python-qualification"):
            graph, execution, subjects = load_json(args.graph), load_json(args.execution), load_json(args.subjects)
            require(qualification_execution.execution_identity(args.builder, args.docker_config) == execution,
                    "Python qualification execution environment differs")
            if args.command == "produce-python-qualification":
                value = python_qualification.produce(args.source, graph, args.row, execution,
                    load_json(args.producer), subjects, args.output, args.builder, args.docker_config)
            else:
                settings = python_qualification.spec(args.source, args.row)
                resolved, bindings = python_components.bind_row(args.source, graph, args.row, execution["build"], subjects,
                    args.builder, args.docker_config, args.temporary_parent)
                value = python_qualification.inputs(args.source, resolved, settings, execution, bindings)
                if args.command == "verify-python-qualification":
                    value = python_qualification.verify_local(load_json(args.receipt), args.receipt_sha256,
                        value, args.source, args.layout, args.builder, args.docker_config, args.temporary_parent)
        elif args.command in ("bind-python-sdk", "execute-python-sdk"):
            graph, execution, components = load_json(args.graph), load_json(args.execution), load_json(args.components)
            require(qualification_execution.execution_identity(args.builder, args.docker_config) == execution,
                    "SDK execution environment differs")
            if args.command == "execute-python-sdk":
                value = python_sdk.execute(args.source, graph, args.root, execution, components,
                    args.output, args.builder, args.docker_config)
            else:
                resolved, bindings, reused = python_sdk.bind(args.source, graph, args.root, execution,
                    components, args.builder, args.docker_config, args.temporary_parent)
                expected = python_sdk.inputs(args.source, resolved, args.root, execution, bindings)
                value = {"graph": resolved, "bindings": bindings, "inputs": expected, "reused_rows": reused,
                         "integration": "not executed by binding"}
        elif args.command in ("acquire-python-sdk", "execute-python-sdk-catalog"):
            from crossforge_internal import python_sdk_catalog
            operation = python_sdk_catalog.acquire if args.command == "acquire-python-sdk" else python_sdk_catalog.execute
            value = operation(args.source, load_json(args.graph), args.root, load_json(args.execution),
                args.component_directory, args.output, args.builder, args.oras, args.cosign, args.docker_config)
            if args.command == "acquire-python-sdk" and value["status"] != "ready":
                print(json.dumps(value, sort_keys=True, indent=2))
                return 1
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
    except (IdentityError, RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
