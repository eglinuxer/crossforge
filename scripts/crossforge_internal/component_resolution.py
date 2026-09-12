"""Resolve build components through authenticated catalogs and verified OCI bytes.

An absent input index requests a producer. All other failures remain errors;
neither a catalog signature nor an acquisition result asserts qualification.
"""

import copy
import os
from pathlib import Path
import re
import shutil

from . import catalog_registry, component_build, component_catalog, component_inputs
from . import registry_transfer
from .identity import load_json, require


def toolchain(source, graph, arch, role, execution, cosign, directory, builder, oras,
              docker_config=None, catalog_reference=None):
    spec = component_build.toolchain_spec(arch, role)
    return _build_component(source, lambda: component_build.toolchain_inputs(source, graph, arch, role, execution),
        spec["target"], role, execution, cosign, directory, builder, oras, docker_config, catalog_reference)


def python(source, graph, row, arch, kind, execution, subjects, cosign, directory, builder, oras,
           docker_config=None, catalog_reference=None):
    """Resolve a raw Python artifact only after verifying its own dependencies."""
    from . import python_components
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "component resolution directory must be new")
    settings = python_components.spec(source, row, arch, kind)
    require(component_build.execution_identity(builder, docker_config) == execution,
            "Python component resolution build environment differs")
    directory.parent.mkdir(parents=True, exist_ok=True)
    resolved, bindings = python_components.bind_build(source, graph, settings, execution, subjects,
        builder, docker_config, directory.parent)
    return _build_component(source, lambda: python_components.inputs(source, resolved, settings, execution, bindings),
        settings["target"], settings["role"], execution, cosign, directory, builder, oras, docker_config, catalog_reference)


def _build_component(source, capture, target, role, execution, cosign, directory, builder, oras,
                     docker_config, catalog_reference):
    """Shared catalog/transport boundary; each domain supplies current inputs."""
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "component resolution directory must be new")
    require(component_build.execution_identity(builder, docker_config) == execution,
            "component resolution build environment differs")
    expected = capture()
    component_build.write_json(directory / "inputs.json", expected)
    policy = registry_transfer.validate_tool(load_json(Path(source) / ".github/locked-tools/oras.json"))
    registry_config = Path(docker_config or os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"
    selected = catalog_registry.lookup(source, expected, role, cosign, directory / "catalog",
        component_catalog.REPOSITORY, oras, policy, registry_config, catalog_reference=catalog_reference)
    result = {"schema_version": 1, "kind": "crossforge-component-resolution",
        "component": expected["component"], "role": role, "inputs_sha256": component_inputs.identity(expected)}
    if selected["status"] == "missing":
        require(catalog_reference is None, "fixed recovery catalog cannot be replaced by a producer")
        result.update(status="build-required", reason=selected["reason"], input_tag=selected["input_tag"])
    else:
        require(selected["status"] == "authenticated-reference", "unsupported catalog selection result")
        entry = selected["entry"]
        receipt = entry["receipt"]
        layout = directory / "oci"
        registry_transfer.fetch(entry["reference"], layout, oras, policy, registry_config)
        frontend = expected["parameters"]["recipes"][target]["frontend"]
        context = component_build.verify_local(receipt, entry["receipt_sha256"], expected, role,
            layout, frontend, builder, docker_config, directory)
        receipt_path = directory / "receipt.json"
        component_build.write_json(receipt_path, receipt)
        result.update(status="verified-build-component", reason="authenticated-catalog-and-matching-artifact",
            context=context, subject={"receipt": str(receipt_path), "receipt_sha256": entry["receipt_sha256"],
                "layout": str(layout)}, catalog=selected["catalog"], authentication=selected["authentication"],
            producer=receipt["contract"]["producer"], reference=entry["reference"])
    require(component_build.execution_identity(builder, docker_config) == execution,
            "component resolution build environment changed")
    component_inputs.require_match(expected, capture())
    component_build.write_json(directory / "resolution.json", result)
    return result


def preserve_evidence(directory, destination):
    """Keep small original trust records, never the downloaded OCI layout."""
    directory, destination = Path(directory), Path(destination)
    require(not destination.exists(), "resolution evidence directory must be new")
    destination.mkdir(parents=True)
    for name in ("inputs.json", "resolution.json", "receipt.json"):
        if (directory / name).exists():
            shutil.copyfile(str(directory / name), str(destination / name))
    if (directory / "catalog").exists():
        shutil.copytree(str(directory / "catalog"), str(destination / "catalog"))


def toolchain_edges(graph):
    """Find canonical installation/test-context boundaries in a resolved graph."""
    require(type(graph) is dict and type(graph.get("target")) is dict, "resolved Bake graph is required")
    producers = {"target:" + component_build.toolchain_spec(arch, role)["target"]: (arch, role)
        for arch in ("x86_64", "aarch64") for role in ("toolchain-install", "gcc-test-context")}
    edges = {}
    for target, definition in graph["target"].items():
        require(type(definition) is dict and type(definition.get("contexts", {})) is dict, "invalid Bake target contexts")
        for name, reference in definition.get("contexts", {}).items():
            require(type(reference) is str, "Bake context reference must be a string")
            if reference in producers:
                arch, role = producers[reference]
                spec = component_build.toolchain_spec(arch, role)
                require(graph["target"].get(spec["target"], {}).get("target") == spec["target"],
                        "toolchain producer stage differs from canonical boundary")
                edges.setdefault((arch, role), []).append((target, name))
    return edges


def bind_toolchains(source, graph, execution, cosign, directory, evidence, builder, oras, docker_config=None,
                    recovery=None):
    """Resolve each needed artifact once; retain explicit producers on a miss.

    This is a build graph binding, not a qualification record. Cache-only CI
    still executes all its selected roots and preserves their existing gates.
    """
    directory, evidence = Path(directory).absolute(), Path(evidence).absolute()
    require(not directory.exists() and not evidence.exists(), "component binding outputs must be new")
    require(directory != evidence and directory not in evidence.parents and evidence not in directory.parents,
            "component OCI data must be outside uploaded evidence")
    edges = toolchain_edges(graph)
    if recovery is not None:
        from . import component_recovery
        require(set(recovery) == {arch + "-" + role for arch, role in edges}, "toolchain recovery set differs")
        for pin in recovery.values():
            component_recovery.validate_pin(pin)
    resolved, resolutions, required = copy.deepcopy(graph), {}, []
    for (arch, role), consumers in sorted(edges.items()):
        name = arch + "-" + role
        output = directory / name
        try:
            options = {} if recovery is None else {"catalog_reference": recovery[name]["catalog_reference"]}
            result = toolchain(source, graph, arch, role, execution, cosign, output, builder, oras, docker_config, **options)
            if recovery is not None:
                component_recovery.verify_selection(recovery[name], result)
        finally:
            if output.exists():
                preserve_evidence(output, evidence / name)
        resolutions[name] = result
        if result["status"] == "build-required":
            required.append(component_build.toolchain_spec(arch, role)["target"])
            continue
        require(result["status"] == "verified-build-component", "unsupported toolchain resolution result")
        for target, context in consumers:
            resolved["target"][target]["contexts"][context] = result["context"]
    result = {"schema_version": 1, "kind": "crossforge-ci-component-binding",
        "components": resolutions, "required_producers": sorted(required),
        "qualification": "not asserted; selected CI roots retain their existing gates"}
    component_build.write_json(evidence / "binding.json", result)
    return resolved, result


def python_edges(source, graph):
    """Find explicit raw Python handoff boundaries without broadening row scope."""
    from . import python_components
    require(type(graph) is dict and type(graph.get("target")) is dict, "resolved Bake graph is required")
    result = {}
    for target, definition in graph["target"].items():
        require(type(definition) is dict and type(definition.get("contexts", {})) is dict, "invalid Bake target contexts")
        for context, reference in definition.get("contexts", {}).items():
            require(type(reference) is str, "Bake context reference must be a string")
            build = re.fullmatch(r"target:cpython-build-(cp[0-9]+)-export", reference)
            cross = re.fullmatch(r"target:cpython-cross-(cp[0-9]+)-(x86_64|aarch64)-export", reference)
            audit = re.fullmatch(r"target:cpython-(cp[0-9]+)-(x86_64|aarch64)-test-context-export", reference)
            if build:
                row, arch, kind = build.group(1), "build", "install"
            elif cross or audit:
                row, arch = (cross or audit).groups()
                kind = "install" if cross else "test-context"
            else:
                require(not (reference.startswith("target:cpython-") and reference.endswith("-export")),
                        "unsupported Python export boundary: " + reference)
                continue
            settings = python_components.spec(source, row, arch, kind)
            producer = graph["target"].get(settings["target"], {})
            require(producer.get("target") == settings["stage"] and producer.get("dockerfile") == "docker/python.Dockerfile",
                    "Python producer stage differs from canonical boundary")
            name = "build" if arch == "build" else arch + "-" + kind
            result.setdefault((row, name), []).append((target, context))
    return result


def python_requirements(source, graph):
    result = {}
    for row, name in python_edges(source, graph):
        result.setdefault(row, {"build"}).add(name)
    return {row: sorted(names) for row, names in sorted(result.items())}


def bind_python(source, graph, resolved, toolchains, execution, cosign, directory, evidence,
                builder, oras, docker_config=None, recovery=None):
    """Bind raw row parts against the original graph and verified toolchains.

    The original graph remains authoritative for producer input capture. The
    separately resolved graph already carries the checked toolchain contexts.
    Neither a miss nor a dependency miss is accepted as an artifact subject.
    """
    from . import python_components, python_handoff
    directory, evidence = Path(directory).absolute(), Path(evidence).absolute()
    require(not directory.exists() and not evidence.exists(), "Python binding outputs must be new")
    require(directory != evidence and directory not in evidence.parents and evidence not in directory.parents,
            "Python OCI data must be outside uploaded evidence")
    resolved = copy.deepcopy(resolved)
    edges, requirements = python_edges(source, graph), python_requirements(source, graph)
    if recovery is not None:
        from . import component_recovery
        require(set(recovery) == {row + "-" + name for row, names in requirements.items() for name in names},
                "Python recovery set differs")
        for pin in recovery.values():
            component_recovery.validate_pin(pin)
    results, required = {}, []
    for row, names in requirements.items():
        subjects = {}
        for name, arch, kind in python_handoff.PARTS:
            if name not in names:
                continue
            settings = python_components.spec(source, row, arch, kind)
            label = row + "-" + name
            toolchain = toolchains.get(arch + "-toolchain-install", {}) if arch != "build" else None
            if arch != "build" and ("build" not in subjects or toolchain.get("status") != "verified-build-component"):
                require(recovery is None, "Python recovery dependency cannot be replaced by a producer")
                required.append(settings["target"])
                results[label] = {"status": "dependency-build-required", "component": settings["component"],
                                  "reason": "build Python or target toolchain requires production"}
                continue
            dependencies = {} if arch == "build" else {
                "build-python": subjects["build"], "toolchain-install": toolchain["subject"]}
            output = directory / label
            try:
                options = {} if recovery is None else {"catalog_reference": recovery[label]["catalog_reference"]}
                result = python(source, graph, row, arch, kind, execution, dependencies, cosign,
                                output, builder, oras, docker_config, **options)
                if recovery is not None:
                    component_recovery.verify_selection(recovery[label], result)
            finally:
                if output.exists():
                    preserve_evidence(output, evidence / label)
            results[label] = result
            if result["status"] == "build-required":
                required.append(settings["target"])
                continue
            require(result["status"] == "verified-build-component", "unsupported Python resolution result")
            subjects[name] = result["subject"]
            for target, context in edges.get((row, name), []):
                require(resolved.get("target", {}).get(target, {}).get("contexts", {}).get(context) ==
                        graph["target"][target]["contexts"][context], "Python consumer boundary changed before binding")
                resolved["target"][target]["contexts"][context] = result["context"]
    result = {"schema_version": 1, "kind": "crossforge-ci-python-binding", "components": results,
              "required_producers": sorted(required), "qualification": "not asserted; selected row and SDK gates remain required"}
    component_build.write_json(evidence / "binding.json", result)
    return resolved, result
