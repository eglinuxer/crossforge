#!/usr/bin/env python3
"""Run the trusted-main x86_64 registry handoff pilot through existing gates."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from crossforge_internal import bake_materials, component_artifacts, component_build, component_handoff
from crossforge_internal import component_inputs, component_qualification, component_resolution
from crossforge_internal import qualification_execution, registry_transfer
from crossforge_internal.identity import IdentityError, content_sha256, load_json, require


ROOT = Path(__file__).resolve().parents[1]


def github_producer(environment, commit, dirty):
    require(environment.get("GITHUB_REPOSITORY") == "eglinuxer/crossforge" and
            environment.get("GITHUB_SERVER_URL") == "https://github.com" and
            environment.get("GITHUB_REF") == "refs/heads/main" and
            environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "component pilot requires trusted main dispatch")
    require(environment.get("GITHUB_SHA") == commit and not dirty, "component pilot requires the exact clean source")
    value = {"kind": "github-actions", "source_commit": commit, "source_dirty": False,
             "invocation": "https://github.com/eglinuxer/crossforge/actions/runs/%s/attempts/%s" %
                (environment.get("GITHUB_RUN_ID", ""), environment.get("GITHUB_RUN_ATTEMPT", "")),
             "started_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
    return component_artifacts.validate_producer(value)


def checked_source():
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT)).decode().strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=str(ROOT)))
    producer = github_producer(os.environ, commit, dirty)
    for script in ("render-release-components.py", "render-vcpkg-integration.py", "render-bake.py"):
        subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--check"], cwd=str(ROOT), stdout=sys.stderr, check=True)
    return producer


def source_graph(targets, directory, builder, docker_config):
    directory.mkdir(parents=True, exist_ok=False)
    cache = directory / "cache.json"
    environment = dict(os.environ)
    if docker_config:
        environment["DOCKER_CONFIG"] = str(docker_config)
    subprocess.run([sys.executable, "scripts/ci-build.py", "cache", "--output", str(cache)] + targets,
                   cwd=str(ROOT), env=environment, stdout=sys.stderr, check=True)
    command = component_build.docker_command(docker_config) + ["buildx", "bake", "--builder", builder,
        "-f", "docker-bake.hcl", "-f", "docker-bake.override.json", "-f", str(cache), "--print"]
    graph = json.loads(subprocess.check_output(command + targets, cwd=str(ROOT)))
    component_build.write_json(directory / "graph.json", graph)
    return graph


def producer_run(directory, builder, oras, docker_config=None):
    producer = checked_source()
    require(not directory.exists(), "component pilot output directory must be new")
    graph = source_graph([component_build.toolchain_spec("x86_64", role)["target"] for role in component_handoff.ROLES],
                         directory / "source", builder, docker_config)
    execution = component_build.execution_identity(builder, docker_config)
    policy = registry_transfer.validate_tool(load_json(ROOT / ".github/locked-tools/oras.json"))
    config = Path(docker_config or Path.home() / ".docker") / "config.json"
    components = {}
    for role in component_handoff.ROLES:
        output = directory / role
        built = component_build.produce_toolchain(ROOT, graph, "x86_64", role, execution, producer, output, builder, docker_config)
        published = registry_transfer.publish(output / "oci", built["artifact"]["root_digest"],
            component_handoff.REPOSITORY, oras, policy, config)
        components[role] = {"reference": published["reference"], "receipt_sha256": built["receipt_sha256"],
                            "receipt": load_json(output / "receipt.json")}
    handoff = component_handoff.document(producer, execution, components)
    path = directory / "handoff.json"
    component_build.write_json(path, handoff)
    return {"handoff": str(path), "handoff_sha256": content_sha256(handoff)}


def consumer_run(handoff_path, trusted_sha256, directory, builder, oras, docker_config=None):
    producer = checked_source()
    require(not directory.exists(), "component pilot output directory must be new")
    handoff = component_handoff.verify(load_json(handoff_path), trusted_sha256, producer["source_commit"], producer["invocation"])
    execution = component_build.execution_identity(builder, docker_config)
    require(execution == handoff["build_execution"], "consumer component build policy differs")
    roots = ["toolchain-x86_64-dev", "gcc-testsuite-x86_64-smoke"]
    graph = source_graph(roots + [component_build.toolchain_spec("x86_64", role)["target"] for role in component_handoff.ROLES],
                         directory / "source", builder, docker_config)
    policy = registry_transfer.validate_tool(load_json(ROOT / ".github/locked-tools/oras.json"))
    config = Path(docker_config or Path.home() / ".docker") / "config.json"
    subjects, references = {}, {}
    report = directory / "report"
    report.mkdir()
    for role, component in handoff["components"].items():
        expected = component_build.toolchain_inputs(ROOT, graph, "x86_64", role, execution)
        component_inputs.require_match(component["receipt"]["contract"]["inputs"], expected)
        output = directory / (role + "-download")
        registry_transfer.fetch(component["reference"], output, oras, policy, config)
        frontend = expected["parameters"]["recipes"][component_build.toolchain_spec("x86_64", role)["target"]]["frontend"]
        references[role] = component_build.verify_local(component["receipt"], component["receipt_sha256"], expected,
            role, output, frontend, builder, docker_config, directory)
        receipt_path = report / (role + "-receipt.json")
        component_build.write_json(receipt_path, component["receipt"])
        subjects[role] = {"receipt": str(receipt_path), "receipt_sha256": component["receipt_sha256"], "layout": str(output)}
    result = consume_subjects(graph, execution, producer, subjects, references, directory, builder, docker_config)
    result["handoff_sha256"] = trusted_sha256
    component_build.write_json(report / "result.json", result)
    return result


def catalog_consumer_run(directory, builder, oras, cosign, docker_config=None, catalog_reference=None):
    producer = checked_source()
    require(not directory.exists(), "component pilot output directory must be new")
    roots = ["toolchain-x86_64-dev", "gcc-testsuite-x86_64-smoke"]
    graph = source_graph(roots + [component_build.toolchain_spec("x86_64", role)["target"] for role in component_handoff.ROLES],
                         directory / "source", builder, docker_config)
    execution = component_build.execution_identity(builder, docker_config)
    subjects, references, resolutions = {}, {}, {}
    report = directory / "report"
    report.mkdir()
    for role in component_handoff.ROLES:
        output = directory / (role + "-resolution")
        resolution = component_resolution.toolchain(ROOT, graph, "x86_64", role, execution, cosign,
            output, builder, oras, docker_config, catalog_reference)
        resolutions[role] = resolution
        evidence = report / role
        evidence.mkdir()
        for name in ("inputs.json", "resolution.json", "receipt.json"):
            if (output / name).exists():
                shutil.copyfile(str(output / name), str(evidence / name))
        if (output / "catalog").exists():
            shutil.copytree(str(output / "catalog"), str(evidence / "catalog"))
        component_build.write_json(report / (role + "-resolution.json"), resolution)
        require(resolution["status"] == "verified-build-component",
                "component producer required for %s: %s; run the build pilot to populate this input" % (
                    role, resolution["reason"]))
        subjects[role] = resolution["subject"]
        references[role] = resolution["context"]
    result = consume_subjects(graph, execution, producer, subjects, references, directory, builder, docker_config)
    result["component_resolutions"] = resolutions
    component_build.write_json(report / "result.json", result)
    return result


def consume_subjects(graph, execution, producer, subjects, references, directory, builder, docker_config):
    """Both trust paths use the same fresh gates and keep subject provenance."""
    report = directory / "report"
    qualifications = {}
    qualification_environment = qualification_execution.execution_identity(builder, docker_config)
    for profile in ("toolchain", "gcc-smoke"):
        selected = {role: subjects[role] for role in component_qualification.spec("x86_64", profile)["contexts"].values()}
        output = directory / ("qualification-" + profile)
        result = component_qualification.produce(ROOT, graph, "x86_64", profile, qualification_environment,
            producer, selected, output, builder, docker_config)
        # Revalidate the sealed report artifact, keeping the original producer.
        verified = component_qualification.verify_local(load_json(result["receipt"]), result["receipt_sha256"],
            load_json(output / "inputs.json"), ROOT, output / "oci", builder, docker_config, directory)
        qualifications[profile] = verified
        shutil.copytree(str(output / "payload/component"), str(report / profile))
        shutil.copyfile(str(output / "receipt.json"), str(report / (profile + "-receipt.json")))
    python = "cpython-cross-cp39-x86_64"
    python_graph = source_graph([python], directory / "python-source", builder, docker_config)
    python_graph["target"][python]["contexts"]["crossforge_toolchain"] = references["toolchain-install"]
    toolchain = load_json(subjects["toolchain-install"]["receipt"])
    binding = {"crossforge_toolchain": {"component": "toolchain/x86_64-install",
        "inputs_sha256": component_inputs.identity(toolchain["contract"]["inputs"]),
        "artifact_digest": toolchain["artifact"]["platform_digest"]}}
    inputs = bake_materials.capture(ROOT, python_graph, python, "pilot/cp39-x86_64", "python-row",
        ["x86_64-unknown-linux-gnu"], execution, artifacts=binding)
    require(not any("build-gcc.sh" in instruction for recipe in inputs["parameters"]["recipes"].values()
                    for stage in recipe["stages"].values() for instruction in stage), "Python pilot still compiles GCC source")
    component_build.write_json(report / "python-materials.json", inputs)
    for definition in python_graph["target"].values():
        for field in ("cache-to", "tags", "attest"):
            definition.pop(field, None)
        definition["output"] = [{"type": "cacheonly"}]
    path = directory / "python.bake.json"
    component_build.write_json(path, python_graph)
    with (report / "python-build.log").open("w", encoding="utf-8") as log:
        subprocess.run(component_build.docker_command(docker_config) + ["buildx", "bake", "--builder", builder,
            "-f", str(path), python, "--progress=plain"], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, check=True)
    return {"qualifications": qualifications,
              "python_target": python, "python_inputs_sha256": component_inputs.identity(inputs),
              "scope": "x86_64 toolchain/runtime and GCC smoke; cp39 x86_64 cross build only, not a complete qualified Python row"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=("produce", "consume", "consume-catalog"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--builder", required=True)
    parser.add_argument("--oras", type=Path, required=True)
    parser.add_argument("--docker-config", type=Path)
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--handoff-sha256")
    parser.add_argument("--cosign", type=Path)
    parser.add_argument("--catalog-reference", help="optional original digest-only catalog; otherwise discover current inputs")
    args = parser.parse_args(argv)
    try:
        require(args.command == "consume-catalog" or (args.cosign is None and args.catalog_reference is None),
                "catalog inputs are only valid for consume-catalog")
        if args.command == "produce":
            require(args.handoff is None and args.handoff_sha256 is None, "producer cannot accept a prior handoff")
            result = producer_run(args.output.resolve(), args.builder, args.oras, args.docker_config)
        elif args.command == "consume":
            require(args.handoff is not None and args.handoff_sha256 is not None, "consumer requires an upstream job handoff and digest")
            result = consumer_run(args.handoff, args.handoff_sha256, args.output.resolve(), args.builder, args.oras, args.docker_config)
        else:
            require(args.handoff is None and args.handoff_sha256 is None and args.cosign is not None,
                    "catalog consumer requires the pinned verifier and cannot accept a same-run handoff")
            result = catalog_consumer_run(args.output.resolve(), args.builder, args.oras, args.cosign,
                args.docker_config, args.catalog_reference)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (IdentityError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
