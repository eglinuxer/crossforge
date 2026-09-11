"""Centralize missing raw toolchain producers before selected downstream jobs."""

import os
from pathlib import Path

from . import catalog_registry, ci_execution, component_build, component_catalog, component_ci
from . import component_handoff, component_inputs, component_resolution, registry_transfer
from .identity import IdentityError, content_sha256, exact_fields, load_json, parse_json, require


ARCHITECTURES = ("x86_64", "aarch64")


def roles(value):
    require(type(value) is list and value and all(type(role) is str for role in value) and
            value == sorted(set(value)) and not set(value) - set(component_handoff.ROLES),
            "producer requires sorted unique raw toolchain roles")
    return value


def plan(source, selection_text, profile, stages, directory, builder, docker_config=None):
    output = ci_execution.prepare(selection_text, profile, stages)
    selection = parse_json(output["selection"])
    targets = sorted({target for values in selection["targets"].values() for target in values})
    graph = component_ci.source_graph(source, targets, directory, builder, docker_config) if targets else {"target": {}}
    edges = component_resolution.toolchain_edges(graph)
    return {"selection": selection, "python_parts": component_resolution.python_requirements(source, graph),
            "roles": {arch: sorted(role for found, role in edges if found == arch)
                                             for arch in ARCHITECTURES}}


def ensure(source, arch, requested, directory, builder, oras, cosign, docker_config=None):
    """Authenticate availability, build only misses, and hand off new receipts.

    An existing catalog preserves its original producer. Availability does not
    assert that OCI bytes were checked here; downstream consumers still fetch
    and verify the complete artifact before binding it to their Bake graph.
    """
    require(arch in ARCHITECTURES, "unsupported producer architecture")
    roles(requested)
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "producer output directory must be new")
    producer = component_ci.checked_source(source, "main")
    graph = component_ci.source_graph(source, [component_build.toolchain_spec(arch, role)["target"] for role in requested],
                                      directory / "source", builder, docker_config)
    execution = component_build.execution_identity(builder, docker_config)
    policy = registry_transfer.validate_tool(load_json(Path(source) / ".github/locked-tools/oras.json"))
    config = Path(docker_config or os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"
    fresh, available = {}, {}
    report = directory / "report"
    for role in requested:
        expected = component_build.toolchain_inputs(source, graph, arch, role, execution)
        component_build.write_json(report / role / "inputs.json", expected)
        selected = catalog_registry.lookup(source, expected, role, cosign, report / role / "catalog",
            component_catalog.REPOSITORY, oras, policy, config)
        require(selected["status"] in ("missing", "authenticated-reference"), "unsupported catalog availability result")
        component_build.write_json(report / role / "lookup.json", selected)
        if selected["status"] == "authenticated-reference":
            component_inputs.require_match(selected["entry"]["receipt"]["contract"]["inputs"], expected)
            available[role] = selected
        else:
            output = directory / role
            built = component_build.produce_toolchain(source, graph, arch, role, execution, producer,
                                                      output, builder, docker_config)
            receipt = load_json(output / "receipt.json")
            component_inputs.require_match(receipt["contract"]["inputs"], expected)
            published = registry_transfer.publish(output / "oci", built["artifact"]["root_digest"],
                component_handoff.REPOSITORY, oras, policy, config)
            fresh[role] = {"reference": published["reference"], "receipt_sha256": built["receipt_sha256"], "receipt": receipt}
        component_inputs.require_match(expected, component_build.toolchain_inputs(source, graph, arch, role, execution))
    require(component_build.execution_identity(builder, docker_config) == execution, "producer build environment changed")
    current = component_ci.checked_source(source, "main")
    require(all(current[key] == producer[key] for key in ("source_commit", "source_dirty", "invocation")),
            "producer source or invocation changed")
    result = {"schema_version": 1, "kind": "crossforge-ci-toolchain-production", "architecture": arch,
              "requested": requested, "available": available, "produced": bool(fresh), "new_roles": sorted(fresh),
              "qualification": "not asserted; downstream jobs retain their selected gates"}
    if fresh:
        handoff = component_handoff.document(producer, execution, fresh, architecture=arch)
        path = directory / "handoff.json"
        component_build.write_json(path, handoff)
        result.update(handoff=str(path), handoff_sha256=content_sha256(handoff), producer_invocation=producer["invocation"])
    component_build.write_json(report / "result.json", result)
    return result


def check_production(results):
    try:
        exact_fields(results, ("ensure", "sign", "store"), "component production jobs")
        require(results["ensure"].get("result") == "success", "component availability job did not succeed")
        produced = results["ensure"].get("outputs", {}).get("produced")
        require(produced in ("true", "false"), "missing component production outcome")
        for name in ("sign", "store"):
            require(results[name].get("result") == ("success" if produced == "true" else "skipped"),
                    "fresh component catalog did not finish or an unselected job executed")
        return True
    except (IdentityError, AttributeError, KeyError, TypeError):
        return False


def check_ready(results, stages):
    try:
        exact_fields(results, ("plan",) + ARCHITECTURES, "main component preparation jobs")
        require(results["plan"].get("result") == "success", "component requirements planning failed")
        output = results["plan"].get("outputs", {})
        exact_fields(output, ("selection", "x86_64-roles", "aarch64-roles", "python-parts"), "main component plan outputs")
        selection = parse_json(output["selection"])
        ci_execution.output_values(selection, stages)
        from . import ci_python
        ci_python.requirements(parse_json(output["python-parts"]), selection)
        for arch in ARCHITECTURES:
            selected = parse_json(output[arch + "-roles"])
            if selected != []:
                roles(selected)
            require(results[arch].get("result") == ("success" if selected else "skipped"),
                    "required component producer did not succeed or unselected producer executed")
        return True
    except (IdentityError, AttributeError, KeyError, TypeError, ValueError):
        return False
