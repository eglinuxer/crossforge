"""Acquire raw Python parts in dependency order and publish only new artifacts."""

import copy
import json
import os
from pathlib import Path

from . import ci_execution, component_build, component_ci, component_handoff, component_inputs, component_resolution
from . import python_components, python_handoff, registry_transfer
from .identity import IdentityError, content_sha256, exact_fields, load_json, parse_json, require


def parts(value):
    require(type(value) is list and value and all(type(name) is str for name in value) and
            value == sorted(set(value)) and not set(value) - {name for name, _, _ in python_handoff.PARTS},
            "Python producer requires sorted unique raw component parts")
    require("build" in value, "cross Python parts require the build Python dependency")
    return value


def requirements(value, selection):
    """Validate the raw graph plan independently of matrix job outcomes."""
    rows = {stage[len("python-"):] for stage in ci_execution.GROUPS["python"]}
    required = {
        stage[len("python-"):] for stage in selection["targets"] if stage in ci_execution.GROUPS["python"]}
    allowed = rows if "sdk" in selection["targets"] else required
    # SDK groups can resolve to selected row roots. The captured Bake graph,
    # rather than the presence of an SDK job, determines its exact row subset.
    require(type(value) is dict and required <= set(value) <= allowed,
            "Python component rows differ from selected consumers")
    for row, selected in value.items():
        require(row in rows, "unknown Python component row")
        parts(selected)
    return value


def execution(selection_text, profile, stages, requirements_text):
    result = ci_execution.prepare(selection_text, profile, stages)
    selected = requirements(parse_json(requirements_text), parse_json(result["selection"]))
    result["python-parts"] = json.dumps(selected, sort_keys=True, separators=(",", ":"))
    result["python-components"] = "true" if selected else "false"
    matrix = [{"row": row, "parts": selected[row]} for row in sorted(selected)]
    result["python-components-matrix"] = json.dumps({"include": matrix or [{"row": "cp39", "parts": ["build"]}]},
                                                   separators=(",", ":"))
    return result


def check_results(results, stages):
    try:
        exact_fields(results, ["plan", "python-components"] + list(ci_execution.GROUPS), "main incremental jobs")
        require(results["plan"].get("result") == "success", "main component planning failed")
        output = results["plan"].get("outputs", {})
        expected = execution(output["selection"], "full", stages, output["python-parts"])
        require(output == expected, "main job outputs differ from selected work")
        require(results["python-components"].get("result") == (
            "success" if expected["python-components"] == "true" else "skipped"),
            "required Python producers did not succeed or unselected producers executed")
        ordinary = copy.deepcopy(results)
        del ordinary["python-components"]
        for key in ("python-parts", "python-components", "python-components-matrix"):
            del ordinary["plan"]["outputs"][key]
        return ci_execution.check_results(ordinary, stages)
    except (IdentityError, AttributeError, KeyError, TypeError, ValueError):
        return False


def ensure(source, row, requested, directory, builder, oras, cosign, docker_config=None):
    """Produce and resolve raw parts; this function never asserts qualification."""
    selected = python_handoff.specs(source, row)
    parts(requested)
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "Python producer output directory must be new")
    producer = component_ci.checked_source(source, "main")
    graph = component_ci.source_graph(source, [selected[name]["target"] for name in requested],
                                      directory / "source", builder, docker_config)
    execution = component_build.execution_identity(builder, docker_config)
    policy = registry_transfer.validate_tool(load_json(Path(source) / ".github/locked-tools/oras.json"))
    config = Path(docker_config or os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"
    subjects, resolutions, fresh = {}, {}, {}
    report = directory / "report"
    arches = [arch for arch in python_components.ARCHES if any(selected[name]["arch"] == arch for name in requested)]
    for arch in arches:
        output = directory / (arch + "-toolchain")
        try:
            result = component_resolution.toolchain(source, graph, arch, "toolchain-install", execution,
                cosign, output, builder, oras, docker_config)
        finally:
            if output.exists():
                component_resolution.preserve_evidence(output, report / (arch + "-toolchain"))
        require(result["status"] == "verified-build-component", "Python producer requires a prepared toolchain: " + arch)
        subjects[arch + "-toolchain"] = result["subject"]
        resolutions[arch + "-toolchain"] = result
    for name, arch, kind in python_handoff.PARTS:
        if name not in requested:
            continue
        dependencies = {} if arch == "build" else {
            "build-python": subjects["build"], "toolchain-install": subjects[arch + "-toolchain"]}
        output = directory / (name + "-resolution")
        try:
            result = component_resolution.python(source, graph, row, arch, kind, execution, dependencies,
                cosign, output, builder, oras, docker_config)
        finally:
            if output.exists():
                component_resolution.preserve_evidence(output, report / name)
        resolutions[name] = result
        if result["status"] == "verified-build-component":
            subjects[name] = result["subject"]
            continue
        require(result["status"] == "build-required", "unsupported Python component resolution")
        output = directory / name
        built = python_components.produce(source, graph, row, arch, kind, execution, producer,
                                          dependencies, output, builder, docker_config)
        receipt = load_json(output / "receipt.json")
        require(component_inputs.identity(receipt["contract"]["inputs"]) == result["inputs_sha256"],
                "Python inputs changed after the missing component was planned")
        published = registry_transfer.publish(output / "oci", built["artifact"]["root_digest"],
            component_handoff.REPOSITORY, oras, policy, config)
        fresh[name] = {"reference": published["reference"], "receipt_sha256": built["receipt_sha256"], "receipt": receipt}
        subjects[name] = {"receipt": str(output / "receipt.json"), "receipt_sha256": built["receipt_sha256"],
                          "layout": str(output / "oci")}
    require(component_build.execution_identity(builder, docker_config) == execution, "Python producer environment changed")
    current = component_ci.checked_source(source, "main")
    require(all(current[key] == producer[key] for key in ("source_commit", "source_dirty", "invocation")),
            "Python producer source or invocation changed")
    result = {"kind": "crossforge-ci-python-production", "schema_version": 1, "row": row,
              "requested": requested, "produced": bool(fresh), "new_parts": sorted(fresh), "subjects": subjects,
              "resolutions": resolutions, "qualification": "not asserted; row gates remain required"}
    if fresh:
        handoff = python_handoff.document(source, row, producer, execution, fresh)
        path = directory / "handoff.json"
        component_build.write_json(path, handoff)
        result.update(handoff=str(path), handoff_sha256=content_sha256(handoff), producer_invocation=producer["invocation"])
    component_build.write_json(report / "result.json", result)
    return result
