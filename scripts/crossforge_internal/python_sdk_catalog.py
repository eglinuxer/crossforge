"""Acquire a complete SDK's components from signed catalogs before integration."""

from pathlib import Path

from . import component_build, component_resolution, python_components, python_handoff
from . import python_row_resolution, python_sdk, qualification_execution
from .identity import exact_fields, require


def directories(data, evidence):
    data, evidence = Path(data).absolute(), Path(evidence).absolute()
    require(not data.exists() and not data.is_symlink() and not evidence.exists() and not evidence.is_symlink(),
            "SDK catalog output directories must be new")
    data, evidence = data.resolve(), evidence.resolve()
    require(data != evidence and data not in evidence.parents and evidence not in data.parents,
            "SDK OCI data must be outside uploaded evidence")
    return data, evidence


def acquire(source, graph, root, execution, directory, evidence, builder, oras, cosign, docker_config=None):
    """Resolve shared toolchains once, then all raw parts and qualified rows.

    A missing component is an explicit producer requirement. This read-only
    consumer neither publishes nor falls back to compiler or qualification RUNs.
    The integration executor must reverify the returned local component set.
    """
    directory, evidence = directories(directory, evidence)
    rows = python_sdk.validate_graph(source, graph, root)
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "SDK catalog acquisition execution environment differs")
    expected_toolchains = {(arch, "toolchain-install") for arch in python_components.ARCHES}
    require(set(component_resolution.toolchain_edges(graph)) == expected_toolchains,
            "SDK catalog graph must require exactly both toolchain installations")
    parts = {name: (arch, kind) for name, arch, kind in python_handoff.PARTS}
    raw_parts = sorted(parts)
    require(component_resolution.python_requirements(source, graph) == {row: raw_parts for row in rows},
            "SDK catalog graph must require every raw part of exactly six rows")
    resolved, toolchains = component_resolution.bind_toolchains(source, graph, execution["build"], cosign,
        directory / "toolchains", evidence / "toolchains", builder, oras, docker_config)
    _, python = component_resolution.bind_python(source, graph, resolved, toolchains["components"], execution["build"],
        cosign, directory / "python", evidence / "python", builder, oras, docker_config)
    exact_fields(toolchains["components"], [arch + "-toolchain-install" for arch in python_components.ARCHES],
                 "SDK resolved toolchains")
    exact_fields(python["components"], [row + "-" + part for row in rows for part in raw_parts], "SDK resolved Python parts")
    components = {"toolchains": {}, "rows": {}}
    required_builds = sorted(set(toolchains["required_producers"] + python["required_producers"]))
    required_rows, resolutions = [], {}
    for arch in python_components.ARCHES:
        result = toolchains["components"][arch + "-toolchain-install"]
        if result["status"] == "verified-build-component":
            components["toolchains"][arch] = result["subject"]
        else:
            require(result["status"] == "build-required", "unsupported SDK toolchain resolution")
            require(component_build.toolchain_spec(arch, "toolchain-install")["target"] in required_builds,
                    "missing SDK toolchain is not planned for production")
    for row in rows:
        subjects = {arch + "-toolchain": components["toolchains"][arch]
                    for arch in python_components.ARCHES if arch in components["toolchains"]}
        for part in raw_parts:
            result = python["components"][row + "-" + part]
            if result["status"] == "verified-build-component":
                subjects[part] = result["subject"]
            else:
                require(result["status"] in ("build-required", "dependency-build-required"),
                        "unsupported SDK Python part resolution")
                require(python_components.spec(source, row, *parts[part])["target"] in required_builds,
                        "missing SDK Python part is not planned for production")
        if len(subjects) != 7:
            resolutions[row] = {"status": "dependency-build-required", "reason": "one or more of seven raw subjects is missing"}
            required_rows.append(row)
            continue
        output = directory / "rows" / row
        try:
            result = python_row_resolution.resolve(source, graph, row, execution, subjects, cosign,
                output, builder, oras, docker_config)
        finally:
            if output.exists():
                component_resolution.preserve_evidence(output, evidence / "rows" / row)
        resolutions[row] = result
        if result["status"] == "qualification-required":
            required_rows.append(row)
            continue
        require(result["status"] == "verified-qualified-row", "unsupported SDK row qualification resolution")
        components["rows"][row] = {"subjects": subjects, "qualification": result["subject"]}
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "SDK catalog acquisition execution environment changed")
    ready = not required_builds and not required_rows
    if ready:
        exact_fields(components["toolchains"], python_components.ARCHES, "SDK ready toolchains")
        exact_fields(components["rows"], rows, "SDK ready qualified rows")
        component_build.write_json(evidence / "components.json", components)
    result = {"schema_version": 1, "kind": "crossforge-sdk-catalog-acquisition", "root": root,
        "status": "ready" if ready else "components-required", "raw_toolchains": toolchains, "raw_python": python,
        "rows": resolutions, "required_builds": required_builds, "required_rows": sorted(required_rows),
        "components": components if ready else None, "integration": "not executed by acquisition"}
    component_build.write_json(evidence / "result.json", result)
    return result


def execute(source, graph, root, execution, directory, output, builder, oras, cosign, docker_config=None):
    """Acquire authenticated inputs and freshly execute the existing SDK gates."""
    directory, output = directories(directory, output)
    acquired = acquire(source, graph, root, execution, directory, output / "acquisition", builder, oras, cosign, docker_config)
    require(acquired["status"] == "ready", "SDK components are missing; inspect acquisition/result.json for required producers")
    integrated = python_sdk.execute(source, graph, root, execution, acquired["components"],
        output / "integration", builder, docker_config)
    result = {"schema_version": 1, "kind": "crossforge-sdk-catalog-integration", "root": root,
              "acquisition": acquired, "integration": integrated}
    component_build.write_json(output / "result.json", result)
    return result
